Clocking
========

.. _rack-host-clock:

One clock
---------

The simplest core runs on one clock: the host link, the trigger, the capture
datapath and the trace memory. Name the clock the transport rides as the
capture domain's own, and there is nothing else to think about:

.. code-block:: yaml

   communication:
     mode: axi4_stream
     clock: la.sample          # the rack rides the sample domain's clock

   instruments:
     la: !logic-analyzer
       domains:
         sample:
           clock: clock
           frequency: 100_000_000
           signals:
             ...

The rack then has no ``clock_i`` of its own — only ``reset_n_i`` — and
``la_sample_clock_i`` carries everything.

``frequency`` is advertised, not used by the logic. With it, the waveform gets
a real time axis, durations are shown in seconds, and an RLE capture can be
capped in seconds rather than in cycles. Without it, the host falls back to
counting samples.

A separate capture clock
------------------------

The clock that carries the host link is often not the clock you want to
sample on — a USB transport runs at 60 MHz, a network stack at 125 MHz, while
the logic under observation runs at whatever your design needed. Sampling
such logic on the transport clock would mean resynchronising the signals
first, which is exactly the deformation you are trying to observe.

Give the domain a clock the rack does not ride. Either leave
``communication.clock`` out, so the rack takes a dedicated
``clock_i``/``reset_n_i`` pair:

.. code-block:: yaml

   communication:
     mode: axi4_stream         # no clock: the rack has its own

   instruments:
     la: !logic-analyzer
       domains:
         dut:
           clock: clock
           frequency: 25_000_000
           signals:
             ...

or point it at another domain, which is what a multi-domain core does.

.. code-block:: vhdl

   capture: work.probe_pkg.probe_capture
     generic map(
       stream_config_c => cfg_c,
       burst_length_l2_c => 8
       )
     port map(
       clock_i => clock_usb_s,              -- host link domain
       reset_n_i => reset_n_s,
       la_dut_clock_i => clock_dut_s,       -- sampling domain
       la_dut_reset_n_i => dut_reset_n_s,
       ...
       );

The capture core, the trigger comparison and the trace-memory write side then
run on the domain's clock; the bridge to the host, the registers and the
self-description stay on the host clock. Everything crossing between the two —
configuration, arm, status, buffer read-back — crosses through the
clock-domain crossings the generator emits, so nothing is required of you
beyond connecting the two clocks. A domain whose clock *is* the host's gets no
crossings at all: they collapse to plain wires.

Probed signals must be signals of the capture domain: connect them as they
are, without a synchroniser in front, and let the sample be taken where the
logic lives. The trigger is evaluated in the same domain, and the delay
between the match and the trigger point is accounted for, so the marker still
lands on the matching cycle.

Watching several domains
------------------------

One capture domain samples one clock, but one core may hold several — one
buffer per domain, each sized and clocked on its own, all behind one link and
one address map. List them under ``domains``, and have one subscribe to
another's trigger when the two are worth cutting on the same event; see
:doc:`../index`.

Domains in the same core are otherwise independent: each is armed and read
back on its own, and only a subscribed trigger correlates their traces in
time.
