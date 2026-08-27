Connecting from client
======================

Nothing on the host side is generated, and nothing is configured
twice: the rack describes itself, and the driver builds the tree,
instantiates all the UI elements from that description. What you
supply is the *resource path* — the link, and where on it the rack
answers::

   acrobe gatecap -r udp/127.0.0.1:4242/gatecap info

The path is the only thing that changes between links, and
:doc:`../host/transports` gives it for each of them: a UDP or TCP socket, a
JTAG chain, an SWD debug port, a serial line with its HDLC framing, a SPI chip
select. Install the plugin first (:doc:`../host/install`).

The command surface
-------------------

The plugin adds one command group, ``acrobe gatecap``. Every command but
``generate`` takes the target as ``-r``/``--root``:

``info``
   Everything the rack said about itself: the instruments, the blocks under
   them, the probes, the trigger's flavour and limits, the buffer geometry, the
   clock rates. This is the reference the other commands' arguments come from.

``capture TARGET``
   Set a trigger, arm, wait, read the trace back and write it out as VCD or
   CSV. ``TARGET`` is a capture control, or a logic analyzer to arm its whole
   correlated group. Options and the trigger-term syntax:
   :doc:`../host/cli`.

``rates [INSTRUMENT]``
   One read of every rate a clock measurer publishes, as CSV. Nothing to arm —
   the measurement is free-running.

``bus read``, ``bus write``, ``bus field``, ``bus dump``
   The verbs of a bus explorer: one access, a masked write, a field write by
   name, or a swept range as CSV.

``bus map add``, ``bus map list``, ``bus map remove``
   Bind an SVD document to the map identifier a rack announces, so register and
   field names show up in the output. These need no target and take no ``-r``.

``gui``
   The window: the same drivers, with a trigger editor, the instrument panels
   and an embedded waveform viewer. Takes ``-r`` to autoconnect, or starts
   disconnected. See :doc:`../host/gui`.

``generate DESCRIPTION -o DIR``
   The generator (:doc:`build`). The one command that talks to no board.

:doc:`../host/cli` documents each of them in full, and
``acrobe info enumerate -r <path>`` prints the raw node tree when a path is in
doubt.

What the host sees
------------------

The driver enumerates one node per instrument, with its blocks under it. This
is the two-domain analyzer of :doc:`description`::

   $ acrobe gatecap -r udp/127.0.0.1:4248/gatecap info
   la:
     correlated capture group of 2 control(s)
     member control.control: 21 probes, sample clock 100 MHz, trigger integration latency 0 cycle(s)
     member phy.control: 9 probes, sample clock 125 MHz, trigger integration latency 3 cycle(s)
   control.control:
     probes (21): command.data[0], command.data[1], ..., state[0], state[1], count[0], ...
     trigger: value-mask match, up to 511 samples, up to 1 window(s), pre-trigger capable
     sample clock: 100 MHz
     sink control.buffer: 32-bit samples, depth 256 samples
   control.trigger:
     signals (4): command.valid, command.last, state[0], state[1]
     value-mask match
   phy.control:
     probes (9): word[0], word[1], ..., mark
     trigger: value-mask match, up to 511 samples, up to 1 window(s), pre-trigger capable
     sample clock: 125 MHz
     sink phy.buffer: 32-bit samples, depth 256 samples

Read that from the top:

* The logic analyzer ``la`` itself, naming the domains it correlates and each
  member's trigger integration latency — the crossing depth that member
  back-dates by. It owns no registers; it orchestrates the group.
* One capture control per domain, ``<domain>.control``, each with its own
  buffer and its own rate. A block's full address is
  ``<instance>.<domain>.<block>`` — ``la.control.control`` — and the bare name
  works wherever it is unambiguous.
* One trigger block, on the hosting domain only. The subscribing domain has
  no ``phy.trigger``: both captures are cut by ``control.trigger``, which is
  where you set the condition.

An analyzer with a single domain is a group of one: it enumerates as a node all
the same, and it is still the instrument the GUI shows a panel for, but ``info``
prints nothing of its own — the domain's control and trigger lines already say
everything there is to say. An instrument that is not a logic analyzer prints
its own inventory the same way, kind by kind.

Capturing one domain
--------------------

A domain is captured by the name ``info`` gave it::

   acrobe gatecap -r udp/127.0.0.1:4248/gatecap capture control.control \
       --trigger state=DONE --count 32 --pretrigger 8 --output control.vcd

The enum labels, the bus grouping and the stream field names that command
accepts all came out of the description you wrote, through the packers, into
the bitstream.

Capturing a correlated group
----------------------------

A shared trigger has to be armed as a group, and the analyzer is the handle
for that: name *it* as the capture target and the whole group is configured,
armed and read back in one go::

   acrobe gatecap -r udp/127.0.0.1:4248/gatecap capture la \
       --trigger state=DONE --span 2us --pre 400ns --output group.vcd

A group's window is given in real time, not in samples. The members run at
different rates, so the same sample count would be a different stretch of
the capture on each of them — 200 samples are 2 µs at 100 MHz and 1.6 µs at
125 MHz, and the two traces would stop at different instants for no reason
you asked for. ``--span`` and ``--pre`` take a duration (``2us``, ``1.5ms``,
``800ns``, or plain seconds); each member converts it with the capture clock
its descriptor reports, and what it derived is printed before the capture
runs::

   control.control: 200 samples (2.0 µs), 40 pre-trigger (400 ns), 1 window(s)
   phy.control: 250 samples (2.0 µs), 50 pre-trigger (400 ns), 1 window(s)

A member whose buffer cannot hold the whole window captures what it can, and
the difference is reported on a ``note:`` line rather than passing
unmentioned. A run-length-encoded member takes the post-trigger part of the
span as its time cap and keeps its pre-trigger ring in buffer lines
(``--pre-lines``): how much time a ring covers is what the captured data
says, so that one is not a duration. ``--count``/``--pretrigger`` are
refused on a group.

The trigger condition is programmed once, on the hosting domain's trigger;
every member control then arms itself, and the core's ready gating
guarantees nothing fires until the last one is ready. The result is one VCD
on an absolute timebase: each member's samples advance at its own clock
rate, all members share the trigger instant, and each sits under its own
scope (``capture.control.control``, ``capture.phy.control``). A group has
one timebase per member, so formats that cannot express that (``csv``) are
refused.

The GUI offers the same group in the analyzer's panel: the window in
microseconds, arm, abort, and the composed waveform in one surface. The
capture domains get no panel of their own — they are driven as part of the
group (:ref:`the panel <gui-analyzer-panel>`).

From Python
-----------

The same drivers are usable directly when neither front-end fits — a
regression that arms a capture, drives a stimulus over a second transport, and
asserts on the result. A group is one call:

.. code-block:: python

   from acrobe_plugin.gatecap.session import Session

   session = Session("udp/127.0.0.1:4248/gatecap")
   await session.open()

   group = session.block_by_name("la")
   trigger = group.trigger_node_get()
   value, mask = trigger.ui_adaptor("console").parse_terms(["state=DONE"])

   result = await group.capture(value, mask, seconds=2e-6, pre_seconds=400e-9)
   vcd = group.compose(result).to_vcd()

``configure_and_arm`` / ``read_trace`` are also available separately;
``configure_and_arm`` returns the plan it resolved (``plan.lines()`` is the
text above), and ``overrides={"phy.control": {"count": 64}}`` gives one
member parameters of its own — which is also the only way to capture a
member whose descriptor carries no capture clock, since nothing can convert
a duration for it.

Panels, measurers and bus explorers are driven the same way, by the names
their description gave the wires; :doc:`../host/index` has an example of each.
