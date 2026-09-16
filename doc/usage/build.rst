Generating and building
=======================

The description is a source
---------------------------

A description is not a file you turn into VHDL once and keep: it is a source
of your build, like any ``.vhd`` beside it. Under gbs there is no generation
step to run and nothing generated to commit — you declare the description, and
the rack's units exist.

Three lines in the project manifest do it:

.. code-block:: yaml

   # project.gbs.yaml
   repositories:
     - path: description.yaml
       loader: gatecap-description

   root:
     name: top
     deps:
       - gatecap_generated.link_pkg
       - nsl_hwdep.clock
       # ...the design's own dependencies
     sources:
       - file_type: vhdl
         files:
           - main.vhd
           # ...and the design's own sources

The ``repositories`` entry says the description stands for a library's worth
of units; the ``deps`` entry names the one partition in it. That partition is
always ``gatecap_generated.<package>``, where ``<package>`` is the first
half of the description's ``name`` (:doc:`description`) — here
``name: link_pkg.link_capture``, so ``gatecap_generated.link_pkg``.

What the project does *not* list is what the rack itself needs. The generated
code depends on ``gatecap.*`` for the blocks and the transport adapter and on
``nsl_*`` for everything portable underneath, and that list moves when the
description does: add an instrument or change the transport, and the set of
partitions changes with no edit of yours. The description's partition declares
them, so ordinary dependency resolution pulls them in. Your ``deps`` list only
what ``main.vhd`` itself uses.

The loader needs the host plugin installed (:doc:`../host/install`) — the
generator is part of it. A description that does not hold up is reported as a
project error, with the offending node and its line, before anything is built.

Naming the rack
---------------

``gatecap_generated`` is the VHDL library the units land in, so the design
names the rack through it:

.. code-block:: vhdl

   library gatecap_generated;
   use gatecap_generated.link_pkg.all;

   ...

   capture: gatecap_generated.link_pkg.link_capture

The ``use`` clause brings in the component declaration and the package's
constants and functions; see :doc:`instantiation` for the rest of that
instantiation.

What the description becomes
----------------------------

One description is one library, holding, in analysis order:

``<entity>_<instance>.vhd``
   One file per instrument that generates an entity of its own — for a logic
   analyzer ``la``: its domains, capture cores, buffers, controls, triggers
   and crossings, behind a single APB port. An instrument taken from a library
   emits nothing.

``<package>.pkg.vhd``
   The package: the word width, the descriptor ROM's segment, one descriptor
   envelope function per instrument, the composed descriptor, the default APB
   configuration, and the component declaration of everything above.

``<entity>_backplane.vhd``
   The APB backplane: descriptor ROM, the router, and the instruments.

``<entity>.vhd``
   The rack: the transport adapter, and the backplane behind it. This is what
   you instantiate.

They are written under the build directory and are build products like any
other. Emission is deterministic: the same description gives byte-for-byte the
same files, in stable order and with no timestamp.

One caveat: a rack is regenerated when its description is newer than the
emitted VHDL. Changing the generator itself — upgrading the host plugin, or
editing a plugin of your own — does not trigger one, so clean the build
directory after such a change.

Packaging the rack as a Vivado IP
---------------------------------

A rack can be packaged as a block-design IP, and the topcell that packaging
needs is generated too. A Vivado IP's boundary is flat pins carrying
``X_INTERFACE_INFO`` attributes, where a rack's carries NSL records; the
topcell is what stands between them, with one packer per record and the rack
behind it.

It is a partition of its own, and naming it is how a project asks for it::

   # project.gbs.yaml
   root_library_name: gatecap_generated

   root:
     name: <rack entity>_vivado_ip
     deps:
       - gatecap_generated.<rack entity>_vivado_ip

   output:
     - name: my_ip
       topcell: <rack entity>_vivado_ip
       target:
         part: xczu9eg-ffvb1156-2-e
       backend_config:
         gbs.builtin.vivado-ip:
           vendor: example.com
           library: ip
           name: my_ip
           version: "1.0"
           taxonomy: /UserIP
       outputs:
         - type: vivado-ip-zip
           path: my_ip.zip

The wrapper partition is named after the topcell it holds rather than after
the package, so the ``deps`` entry and the ``topcell`` are the same word: a
description called ``name: zynqmp.ila`` has its rack in
``gatecap_generated.zynqmp`` and its wrapper in
``gatecap_generated.ila_vivado_ip``.

The description is not touched: it says nothing about Xilinx, and a project
that never packages an IP never mentions the partition and never pays for it.
The topcell is a unit of the generated library, so that library is the root's
and the root declares no source of its own. What the IP *is* called stays in
the output group, beside the part — both are the build's business, not the
rack's.

What ends up on the boundary:

* the ``axi4_stream`` transport's two port pairs become an AXI4-Stream slave
  (``s_axis``) and master (``m_axis``), and the ``apb`` transport's completer
  an APB slave (``s_apb``), both packed by ``nsl_amba.packer``;
* a bus explorer's target bus becomes an APB master of its own, named after
  the instance;
* a probed ``!axi4-stream`` becomes an AXI4-Stream *monitor* interface named
  after the probe, packed by ``nsl_amba.packer``'s monitor packer. The core
  drives nothing on a bus it only watches, so every pin of it is an input,
  ``TREADY`` included;
* every bus pin carries a width written as a number. The packager works its
  IP-XACT expressions out of the port clause itself and evaluates no VHDL, so
  a width it cannot read as a literal is a width it refuses; the rack's
  completer is therefore packaged at the 24 address bits and 32 data bits it
  provisions, and what the map really elaborated to is *checked* against the
  boundary rather than sizing it. A rack that outgrows them fails elaboration
  with the pin and the two widths named, in simulation and in synthesis
  alike;
* the host clock and reset become ``aclk`` and ``aresetn``. When
  ``communication.clock`` names a domain's clock, the rack and that domain are
  one domain, so its reset and the rack's host reset are one pin;
* every other domain keeps a clock and reset pair of its own, named after its
  rack ports. Each clock publishes the rate the description states, the buses
  it clocks and the reset that goes with it, so a block design wires them
  without being told;
* every probed signal crosses as a plain pin, under the very name the rack
  gives it;
* the ``jtag`` transport's TAP pins are left off altogether. Xilinx wires the
  TAP internally, so the adapter's primitive reaches the chip's own and a pin
  for each of them would be a pin nothing may drive; the rack takes them open,
  which is what their default assignments are for.

``burst_length_l2_c`` becomes an IP parameter — the rack has no default for
it, and a packaged IP needs one — and the transport's stream geometry is fixed
by the adapter's own contract rather than exposed.

A probed bus's geometry is neither: the rack takes it as a ``config_t``
generic with no default, deliberately, and a block design has nowhere to write
a record. The IP states it in scalars instead, eight per probe —
``<probe>_data_bytes``, ``<probe>_id_width``, ``<probe>_dest_width``,
``<probe>_user_width``, ``<probe>_has_keep``, ``<probe>_has_strobe``,
``<probe>_has_ready`` and ``<probe>_has_last`` — which the IP's configuration
panel carries. The pins are sized from them and the ``config_t`` behind them
is rebuilt from them, so the boundary and the capture cannot disagree.
``has_ready`` and ``has_last`` default true where the library's own factory
leaves them off: an observed stream almost always has both, and a selection
over them would silently lose bits.

Every pin of a probed stream exists whatever those parameters say. A field
turned off contributes a null range rather than an absent pin, and an optional
pin carries a default assignment, so a block design leaves open what its bus
does not have and the IP has one boundary rather than one per geometry.

A port the generator has no way to flatten is refused by name, with the type
and the types it does know::

   port 'la_link_command_i' of type nsl_bnoc.framed.framed_bus_t has no
   Vivado binding (bound: nsl_amba.axi4_stream.bus_t, ...)

That is the state of things today: every transport of loose logic wires
(``jtag``, ``serial_hdlc``), the two bus transports (``axi4_stream``,
``apb``), the bus explorer's target bus, every probed vector and a probed
AXI4-Stream are packaged; a probed bnoc bus and the ``spi``, ``swd`` and
``usb`` pins are not, for want of a packer to flatten them with.

Outside gbs, ``acrobe gatecap generate --vivado-ip`` writes the topcell beside
the rack and lists it in the emitted partition manifest.

What the generator refuses
--------------------------

Everything decidable from the description is checked before any VHDL is
written, and reported with the path of the offending node — and, when the
YAML parser knew it, the line::

   description.yaml: instruments.la.domains.d.signals.a, line 10: !bus takes a mapping of keys

   description.yaml: instruments.la.domains.sample.trigger: a capturing domain needs a trigger: mark signals with trigger, or subscribe with trigger: {from: <domain>}

   description.yaml: communication: unknown key 'max_rate' (known: clock, mode)

The last of those is the mode deciding what its own section may hold: keys are
checked against the chosen mode's set, so ``max_rate`` is required under
``mode: spi`` and refused under every other.

Geometry that only a stream configuration settles — how wide a packed sample
ends up, how many bits a trigger vector really has, how many address bits the
map needs — cannot be decided from the description. The generated code carries
those checks as assertions instead, failing both simulation and synthesis when
the configuration you hand it does not fit.

Generating without gbs
----------------------

If your build is not gbs-driven, run the generator by hand. It needs no board,
no simulator and no resource path — it reads a file and writes files::

   $ acrobe gatecap generate description.yaml -o generated/
   description.yaml: rack link_pkg.link_capture, 1 instrument(s) over axi4_stream
   wrote generated/link_capture_la.vhd
   wrote generated/link_pkg.pkg.vhd
   wrote generated/link_capture_backplane.vhd
   wrote generated/link_capture.vhd
   wrote generated/link_pkg.gbs.yaml

The output directory is created if needed and holds the four units above, in
analysis order, plus one extra file: ``<package>.gbs.yaml``, a gbs partition
manifest listing those sources and everything the generated code depends on.
Analyse the units into a library of your choosing, and adjust the ``library``
clause of your design to match.

That manifest also lets a gbs project build against a *committed* output
directory rather than the loader: name it as a partition of a library of your
own, through the ordinary ``yaml`` repository loader, and depend on that key.
You then own the regeneration — run the command again whenever the description
changes — which is the whole of what the ``gatecap-description`` loader does
for you.
