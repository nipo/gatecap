Generating and building
=======================

Running the generator
---------------------

The generator is part of the host plugin (:doc:`../host/install`) and needs
no board, no simulator and no resource path — it reads a file and writes
files::

   $ acrobe gatecap generate description.yaml -o generated/
   description.yaml: rack link_pkg.link_capture, 1 instrument(s) over axi4_stream
   wrote generated/link_capture_la.vhd
   wrote generated/link_pkg.pkg.vhd
   wrote generated/link_capture_backplane.vhd
   wrote generated/link_capture.vhd
   wrote generated/link_pkg.gbs.yaml

The output directory is created if needed, and holds, in analysis order:

``<entity>_<instance>.vhd``
   One file per instrument that generates an entity of its own — here the
   logic analyzer ``la``: its domains, capture cores, buffers, controls,
   triggers and crossings, behind a single APB port. An instrument taken from
   a library emits nothing.

``<package>.pkg.vhd``
   The package: the word width, the descriptor ROM's segment, one descriptor
   envelope function per instrument, the composed descriptor, the default APB
   configuration, and the component declaration of everything above.

``<entity>_backplane.vhd``
   The APB backplane: descriptor ROM, the router, and the instruments.

``<entity>.vhd``
   The rack: the transport adapter, and the backplane behind it. This is what
   you instantiate.

``<package>.gbs.yaml``
   A gbs partition manifest listing those files and everything the generated
   code depends on.

Emission is deterministic: the same description gives byte-for-byte the same
files, in stable order and with no timestamp. So the output is a build product
like any other — commit it next to the description if you want the diff, or
regenerate it as a build step; nothing downstream can tell.

What the generator refuses
--------------------------

Everything decidable from the description is checked before any VHDL is
written, and reported with the path of the offending node — and, when the
YAML parser knew it, the line::

   $ acrobe gatecap generate description.yaml -o generated/
   Error: instruments.la.domains.d.signals.a, line 10: !bus takes a mapping of keys

   $ acrobe gatecap generate description.yaml -o generated/
   Error: instruments.la.domains.sample.trigger: a capturing domain needs a trigger: mark signals with trigger, or subscribe with trigger: {from: <domain>}

   $ acrobe gatecap generate description.yaml -o generated/
   Error: communication: unknown key 'max_rate' (known: clock, mode)

The last of those is the mode deciding what its own section may hold: keys are
checked against the chosen mode's set, so ``max_rate`` is required under
``mode: spi`` and refused under every other.

Geometry that only a stream configuration settles — how wide a packed sample
ends up, how many bits a trigger vector really has, how many address bits the
map needs — cannot be decided from the description. The generated code carries
those checks as assertions instead, failing both simulation and synthesis when
the configuration you hand it does not fit.

Adding it to a gbs build
------------------------

The emitted manifest is a gbs *partition*: it lists the generated sources and
the partitions they depend on — ``gatecap.*`` for the blocks and the transport
adapter, ``nsl_*`` for everything portable underneath. The point of building
against that manifest rather than around it is that the dependency list is the
generator's business, and it moves when the description does: add an instrument
or change the transport, and the set of partitions changes with no edit of
yours.

Here is a sample project tree where main GBS project file references
generated library.

.. code-block:: none

   project/
     description.yaml               # the rack
     main.vhd                       # the design instantiating it
     phy/, constraint/              # the rest of the design
     project.gbs.yaml               # the gbs project
     generated/
       repository.gbs.yaml          # a repository holding one library
       custom_lib/
         library.gbs.yaml           # that library, and its partitions
         rack_partition/              # the generator's output directory

The two manifests are a handful of lines each and are written once. The
repository names the library and where it lives:

.. code-block:: yaml

   # generated/repository.gbs.yaml
   name: generated
   description: Rack generated library
   libraries:
     - path: custom_lib

and the library names the partitions in it, by path and without the
suffix — here ``rack_partition/rack_partition``, the
``rack_partition.gbs.yaml`` the generator wrote into
``rack_partition/``:

.. code-block:: yaml

   # generated/custom_lib/library.gbs.yaml
   name: custom_lib
   partitions:
     - rack_partition/rack_partition

The generator writes into that directory and nothing else has to be touched::

   $ acrobe gatecap generate description.yaml -o generated/custom_lib/rack_partition/

The project then loads the repository and depends on the partition, exactly as
it depends on an NSL one:

.. code-block:: yaml

   # project.gbs.yaml
   root:
     name: top
     deps:
       - custom_lib.rack_partition
       - nsl_hwdep.clock
       - nsl_usb.func
       - nsl_bnoc.axi_adapter
       # ...the design's own dependencies
     sources:
       - file_type: vhdl
         files:
           - main.vhd
           # ...and the design's own sources

   repositories:
     - path: generated/repository.gbs.yaml
       loader: yaml

``custom_lib.rack_partition`` is a library-qualified partition key like any other,
so gbs reads the generated manifest and pulls in the ``gatecap`` and ``nsl_*``
partitions *it* declares. The project's own ``deps`` list only what ``main.vhd``
itself uses. ``custom_lib`` is also the VHDL library the package lands in, which
is how the design names the rack:

.. code-block:: vhdl

   library custom_lib;
   ...
   capture: custom_lib.rack_partition.demo_rack

Any library name will do, as long as the three places agree: the two manifests
and the ``library`` clause. See :doc:`instantiation` for the rest of that
instantiation.
