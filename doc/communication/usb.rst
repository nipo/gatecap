``USB``
=======

The rack is a USB Full Speed device of its own: three IOs to a USB
connector, a 60 MHz clock, and no probe, no bridge between it and the
host.  The whole stack — the transceiver included — is fabric, so
nothing but a 1.5 kΩ resistor on the board has to be brought for it.

The device enumerates as vendor ``1500``, product ``deca``, with one
vendor-defined interface, and its serial number is the rack's own
fingerprint — so the host finds the rack, and knows *which* rack,
without being told anything.

Description
-----------

.. code-block:: yaml

   communication:
     mode: usb
     clock: panel.clk        # a 60 MHz domain

Wiring
------

Add the usual USB receptacle part — the 5 V/GND pins, and series
resistors of about 22 Ω in D+ and D- if the board is not otherwise
impedance-matched.  Also provide a 1.5 kΩ resistor between D+ and a
third IO. Nothing else: no transceiver chip, no crystal of its own, no
bridge.

Instantiation
-------------

It adds ``usb_o`` and ``usb_i``, the line records of ``nsl_usb.io``, an
``online_o`` status wire, and the ``burst_length_l2_c`` generic. The mode has
no keys of its own: the only rate involved is the host clock's, and the
transceiver recovers bits at 60 MHz or at 48 MHz and at no other rate. Give
the domain whose clock the rack rides a ``frequency`` of one of the two and it
is taken from there — anything else is refused when the rack is generated::

   Error: communication.clock: the usb transport runs on the phy's reference clock (48 MHz, 60 MHz), and the clock it rides is stated at 100 MHz

State no frequency and 60 MHz is assumed, which is what the rack must then be
clocked at.

``nsl_usb.io.io_fs_driver`` turns the rack's two line records into those three
pads:

.. code-block:: vhdl

   io_driver: nsl_usb.io.io_fs_driver
     port map(
       bus_o => usb_i_s,
       bus_i => usb_o_s,
       bus_io.dp => usb_dp_io,
       bus_io.dm => usb_dn_io,
       dp_pullup_control_io => usb_dp_pull_io
       );

   dut: usb_capture
     generic map(
       burst_length_l2_c => 8
       )
     port map(
       clock_i => clock_60m_s,
       reset_n_i => pll_locked_s,
       usb_o => usb_o_s,
       usb_i => usb_i_s,
       online_o => online_s,
       la_sample_clock_i => sample_clock_s,
       la_sample_reset_n_i => sample_reset_n_s,
       la_sample_state_i => state_s,
       la_sample_data_i => data_s
       );

``online_o`` goes high once the host has configured the device. It is status
only — a LED, or nothing — and the rack does not depend on it.

Plugging the board in is the whole connection procedure: the device
enumerates, the host recognises it by vendor and product and names it by the
rack's fingerprint (:ref:`USB paths <host-transport-usb>`). A USB bus reset
restarts the link and the command bridge, and leaves the instruments alone —
re-enumerating does not throw away a capture in progress.

``gateware/example/usb_transport`` is a board demo of exactly this: a PLL from
the 50 MHz oscillator, the three IOs, and a control/status panel behind the
rack.
