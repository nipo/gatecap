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
