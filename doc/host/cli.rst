Command line
============

The plugin adds a ``gatecap`` command group to acrobe. Every command takes
the target as ``-r``/``--root``::

   acrobe gatecap -r <resource-path> <command> [...]

Looking at a target
-------------------

::

   $ acrobe gatecap -r udp/127.0.0.1:4242/gatecap info
   spi.control:
     probes (26): sck, cs_n, mosi, miso, command.valid, command.ready, ...
     trigger: value-mask match, up to 8191 samples, up to 4 window(s), pre-trigger capable
     sample clock: 100 MHz
     sink spi.buffer: 32-bit samples, depth 4096 samples
   spi.trigger:
     signals (26): sck, cs_n, mosi, miso, command.valid, ...
     value-mask match

``info`` is the reference for everything else: it names the blocks (the
``capture`` command takes a control block's name), lists the signals you can
trigger on, and states the limits the host will hold you to.

An instrument that is not a logic analyzer prints its own inventory the same
way — a control/status panel lists what it holds, kind by kind::

   panel:
     control/status panel
     control led: 1 bit(s)
     control dac_level: 12 bit(s)
     control mode: 2 bit(s) <0=idle, 1=run, 2=test>
     status busy: 1 bit(s)
     tick out word 0: start, stop
     tick in word 0: overflow, underflow
     counters: 2, 8 bit(s), wrapping

There is no command-line way to drive a panel: its widgets are the point, so
it is the GUI (:ref:`its panel <gui-panel-pane>`) or a Python script.

A clock measurer states what it watches and how precisely::

   rates:
     reference ref: 100 MHz nominal
     measured clocks (3): fast, slow, odd
     refreshed 16384 time(s) per second, to 16.384 kHz

A bus explorer states its target's dimensions and what it knows of its
register map::

   dut:
     bus explorer: 12 address bit(s), 32 data bit(s), 8 scan slot(s)
     map gatecap-demo-device: 7 register(s) from /home/you/maps/demo_device.svd

With no map registered under that identifier the line says so instead, and
every address is read and shown as raw hexadecimal.

Blocks live under the instrument that holds them — here a logic analyzer
``la`` with one capture domain ``spi``, which prints nothing of its own
because the control and trigger lines already say it all. A block is named
after the domain it belongs to, its full address is
``<instrument>.<block>``, and the bare name works wherever one node answers to
it, so ``capture spi.control`` and ``capture la.spi.control`` name the same
target above.

Capturing
---------

::

   acrobe gatecap -r <path> capture <control> [--trigger TERM]... [options]

The command sets the trigger, arms the capture, waits, then reads the trace
back. While waiting it shows live progress; ``Ctrl-C`` stops the wait, aborts
the capture and still dumps what the buffer holds.

Options for a normal capture:

``--count N``
   Number of samples. Required.

``--pretrigger N``
   How many of those samples come from before the trigger. The trigger sample
   is index 0 and pre-trigger samples are negative.

Options for a run-length-encoded capture:

``--pre-lines N``
   Size of the pre-trigger ring, in buffer lines.

``--max-time S``
   Post-trigger duration cap, in seconds. 0 captures until the buffer fills.

Options for a correlated capture group — a logic analyzer holding several
capture domains, named as the target (see :doc:`../usage/client`):

``--span D``, ``--pre D``
   The capture window, and how much of it precedes the trigger, as durations
   — ``2us``, ``1.5ms``, ``800ns``, or plain seconds. The group's members
   sample at different rates, so each converts the window with its own
   capture clock; what they derived is printed before the capture runs.
   ``--pre-lines`` still applies, to the group's run-length-encoded members.

Options for either:

``--timeout S``
   Give up waiting for the trigger after S seconds (0, the default, waits
   indefinitely). On timeout the capture is aborted and the pre-trigger
   content is written out, with a warning.

``--output FILE``, ``--format {csv,vcd}``
   Where and how to write the trace. Without ``--output`` it goes to standard
   output; without ``--format`` it follows the output extension, defaulting
   to CSV.

The option sets are not interchangeable: pointing ``--count`` at an RLE
control (or ``--pre-lines`` at a normal one, or ``--count`` at a group, whose
window is a duration) is refused rather than quietly ignored.

Trigger terms
-------------

``--trigger`` is repeatable; each term constrains one signal or one bus, and
unmentioned signals stay don't-care. With no term at all, the capture
triggers on the very first sample.

.. code-block:: console

   # a scalar low, a bus equal to a value
   --trigger cs_n=0 --trigger command.data=0x66

   # a bus with a mask: match the top nibble only
   --trigger command.data=0x60/0xf0

   # an enumerated field, by label
   --trigger state=RUN

   # the whole trigger vector at once, value/mask
   --trigger 0xa0/0xf0

On a core built with the edge trigger, scalars additionally take
``rising`` and ``falling`` (``r``/``f``, ``up``/``down``), and ``-`` for
don't-care::

   --trigger cs_n=falling --trigger command.valid=1

Buses keep their level meaning there: they constrain the current sample.

Examples
--------

Post-trigger capture of an SPI transaction, as a waveform::

   acrobe gatecap -r udp/127.0.0.1:4242/gatecap capture control \
       --trigger cs_n=0 --count 4096 --pretrigger 64 --output trace.vcd

The same, to CSV on standard output, giving up after 5 s::

   acrobe gatecap -r udp/127.0.0.1:4242/gatecap capture control \
       --trigger cs_n=0 --count 1024 --timeout 5 --format csv

A run-length-encoded capture of a slow bus, one second of activity with a
little history before it::

   acrobe gatecap -r udp/127.0.0.1:4244/gatecap capture control \
       --trigger cs_n=falling --pre-lines 64 --max-time 1.0 --output slow.vcd

A correlated capture of every domain of a logic analyzer, armed as one group
over a window given in real time::

   acrobe gatecap -r udp/127.0.0.1:4248/gatecap capture la \
       --trigger state=DONE --span 2us --pre 400ns --output group.vcd

Reading clock rates
-------------------

::

   acrobe gatecap -r <path> rates [INSTRUMENT] [--output FILE]

One read of every rate a clock measurer (:ref:`clock-measurer-instrument`)
publishes, as CSV::

   $ acrobe gatecap -r udp/127.0.0.1:4252/gatecap rates
   clock,rate_hz
   fast,166658048
   slow,7995392
   odd,76922880

The measurement is free-running, so there is nothing to arm and the command is
a single read; run it again for a fresh value, or watch the GUI's pane
(:ref:`gui-rates-pane`) for a rolling history. ``INSTRUMENT`` names the
measurer, and is only needed when a rack holds several. ``--output`` writes a
file instead of standard output.

Exploring a target bus
----------------------

::

   acrobe gatecap -r <path> bus {read,write,field,dump} [...]

The verbs of a bus explorer (:ref:`bus-explorer-instrument`). Each takes
``-i``/``--instrument`` to name the explorer when a rack holds more than one,
and ``--map FILE`` to decode with an SVD document of your choosing instead of
the one the self-description's map identifier resolves to. Addresses and values
are hexadecimal with ``0x``, binary with ``0b``, decimal otherwise, and ``_``
separators are allowed.

``bus read ADDRESS``
   One read, printed with the register name and the field breakdown a map
   gives it::

      $ acrobe gatecap -r udp/127.0.0.1:4253/gatecap bus read 0x4
      0x4: 0x21  DEMO.CTRL
        [0:0] ENABLE = 0x1
        [2:1] MODE = 0x0 (IDLE)
        [11:4] GAIN = 0x2

``bus write ADDRESS VALUE [--mask MASK]``
   One write. With ``--mask``, only those bits are written and the rest are
   left as the target holds them — the instrument does the read-modify-write
   itself, on the target bus, so nothing of the target's own gets clobbered by
   a host round trip in between.

``bus field REGISTER FIELD VALUE``
   The same masked write, addressed by name: the map turns the register and
   field into an address and a mask. ``VALUE`` may be an enumerated label::

      acrobe gatecap -r udp/127.0.0.1:4253/gatecap bus field DEMO.CTRL MODE RUN

``bus dump START COUNT [--step N] [--output FILE]``
   Read a run of addresses and write ``address,register,value,error`` CSV.
   ``--step`` defaults to the width of the target data bus in bytes. An address
   the target refuses is reported in its row rather than stopping the sweep,
   since sweeping an unknown map is expected to hit holes.

A read or a write the target refuses (``slverr``) or never answers
(``timeout``) fails the command with that reason.

Registering a register map
~~~~~~~~~~~~~~~~~~~~~~~~~~

The gateware carries only an *identifier* for its target's register map. Bind
it to an SVD file once, and every session on any target announcing that
identifier decodes with it::

   $ acrobe gatecap bus map add gatecap-demo-device demo_device.svd
   gatecap-demo-device: 7 register(s) in 1 peripheral(s) from /home/you/demo_device.svd

   $ acrobe gatecap bus map list
   gatecap-demo-device	/home/you/demo_device.svd

   $ acrobe gatecap bus map remove gatecap-demo-device

These need no target and take no ``-r``: the bindings are yours, kept in the
user configuration directory (``GATECAP_CONFIG_DIR`` overrides where that is).
The file is parsed when it is registered, so a document this decoder cannot
read is refused there and then rather than at the next session.

Output formats
--------------

**VCD** carries the scope structure implied by the names, buses as buses,
enumerated fields as labels, and a marker on the trigger. Feed it to a
waveform viewer, or to a protocol decoder such as ``sigrok-cli``.

**CSV** is one row per sample, one column per scalar or bus, with the first
column counting samples relative to the trigger (negative before it). For an
RLE capture it is one row per *run* instead, stamped with the cycle it starts
at — so a capture that idles for a million cycles stays a few lines long.

Launching the GUI
-----------------

::

   acrobe gatecap gui                                    # start disconnected
   acrobe gatecap -r udp/127.0.0.1:4242/gatecap gui       # autoconnect

On a machine with no display, ``serve`` hosts the same interface over HTTP
for a browser instead of opening a window::

   acrobe gatecap serve                                   # http://127.0.0.1:8000/
   acrobe gatecap -r udp/127.0.0.1:4242/gatecap serve --bind 0.0.0.0:8000

By default it listens on loopback only: the interface is unauthenticated and
drives hardware, so reach it from another machine through an SSH tunnel
(``ssh -L 8000:127.0.0.1:8000 lab-host``) and point your browser at
``http://127.0.0.1:8000/``. ``--bind`` a public address only on a network you
trust with the target.

See :doc:`gui`.
