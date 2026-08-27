Declaring signals
=================

A capture domain has two vectors: the probes it records and the signals its
trigger compares. Each comes with a *grouping spec* — a string naming its
bits — which is compiled into the bitstream and read back by the host. You
write the signals in the description, one entry per probe, and the generator
renders both the packing and the spec from the same statement; what this
chapter describes is the grammar you see in the result, and what the naming
choices mean.

Bit order
---------

The spec is a comma-separated list, and its order is the vector's bit order:
the first name is bit 0 — which is description order:

.. code-block:: yaml

   signals:
     sck: {}
     cs_n: {}
     mosi: {}
     miso: {}

gives the spec ``sck,cs_n,mosi,miso`` and four one-bit ports, ``sck`` on the
lowest probe bit.

Scopes and groups
-----------------

A dotted name nests: ``command.valid`` shows up as ``valid`` inside a
``command`` scope in the waveform viewer. A brace group factors a common
prefix out, and groups nest freely::

   command.{valid,ready,last}

expands to ``command.valid``, ``command.ready``, ``command.last``.

Buses
-----

A range in brackets declares a bus, and the host regroups it into one
multi-bit signal — displayed as a value, and triggerable as a value::

   bus[7:0]     -- descending: bus[7] is the first bit named, so the lowest
                -- probe bit
   bus[0:7]     -- ascending: bus[0] is the lowest probe bit

A ``!bus`` signal in the description is named ascending, ``name[0:width-1]``,
so element 0 is the lowest probe bit and a vector connects as it is. That is
almost always what you want: naming a byte ``data[7:0]`` instead would put
``data[7]`` on the bit carrying element 0, and every captured byte would come
back bit-reversed.

Putting it together, this is a complete spec for an SPI bus and the two
framed streams around it::

   sck,cs_n,mosi,miso,
   command.{valid,ready,last,data[0:7]},
   response.{valid,ready,last,data[0:7]}

which comes out of four bare scalars and two ``!bnoc-framed`` signals — the
bus types name their own fields, and the brace scope is the signal's name.

Fields may also carry symbolic values; see :doc:`enums`.

Packing helpers
---------------

The library functions the generated code calls are usable directly, for a
probe vector you assemble yourself — in a design that predates a description,
or one feeding signals into a rack port of its own.

``gatecap.bnoc_packer`` covers the NSL ``bnoc`` buses:
``framed_pack`` / ``framed_names`` / ``framed_length`` over the elements
``dvlr`` (data, valid, last, ready), and ``pipe_pack`` / ``pipe_names`` /
``pipe_length`` over ``dvr``. ``gatecap.axi4_stream_packer`` does the same
for AXI4-Stream with ``axis_pack`` / ``axis_names`` / ``axis_length`` over
``idskouvlr``. Each takes the element string, so you can drop what you do not
care about — dropping ``ready`` from a stream saves a probe bit. In a
description the same letters are the ``trace`` and ``trigger`` selections of
the bus type.

To capture an entire AXI4-Stream bus without assembling anything by hand,
give the signal an ``!axi4-stream`` type in the description; see
:doc:`index`.

Disjoint capture and trigger sets
---------------------------------

The trigger has its own vector, its own width and its own names. It does not
have to be the probe vector, and there are good reasons for it not to be:

* The trigger is capped at 32 signals, while the probe set is not. On a wide
  capture you must select what to trigger on.
* Comparing fewer signals costs less logic and helps timing.
* Some signals are worth triggering on but not worth storing (a state-machine
  encoding, an error flag from elsewhere in the design), and some are worth
  storing but useless as a condition.

Mark each signal for the vectors it belongs to. A signal is traced unless you
say ``trace: false``, and joins the trigger when you say so — for a bus type,
by naming the elements the trigger watches:

.. code-block:: yaml

   signals:
     sck: {}
     cs_n: {trigger: true}
     mosi: {}
     miso: {}
     command: !bnoc-framed
       trigger: vrd            # valid, ready, data -- not the frame boundary
     response: !bnoc-framed {}
     state: !bus
       width: 3
       trace: false            # worth triggering on, not worth storing
       trigger: true

That traces 26 bits and compares 14 of them, each vector named from the same
entries. The host builds its trigger editor from the trigger spec and its
waveform from the probe spec, so the two sets appear where they belong and
nowhere else.
