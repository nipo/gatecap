Serial port with HDLC
=====================

Two wires: ``uart_rx_i`` and ``uart_tx_o``, 8n1, carrying HDLC-delimited
frames. XON/XOFF handles flow control in both directions, so no modem line is
needed.

Description
-----------

.. code-block:: yaml

   communication:
     mode: serial_hdlc
     clock: la.sample

Instantiation
-------------

It adds ``baud_rate_c`` — the wire rate, with no default, since it is what the
two ends must agree on — and ``burst_length_l2_c``. The adapter divides the
host clock down to the baud rate, so it must know the clock's rate: give the
domain whose clock the rack rides a ``frequency`` and it is taken from there;
otherwise the rack adds a ``clock_frequency_c`` generic for you to state it in
Hz. Host side: :ref:`serial paths <host-transport-serial>`.

.. code-block:: vhdl

   ila: serial_capture
     generic map(
       baud_rate_c => 1000000,
       burst_length_l2_c => 6
       )
     port map(
       reset_n_i => reset_n_s,

       uart_rx_i => rx_s,
       uart_tx_o => tx_s,

       la_sample_clock_i => clock_s,
       la_sample_reset_n_i => reset_n_s,
       la_sample_state_i => state_s,
       la_sample_data_i => data_s
       );

The bit period must be at least four host-clock cycles; a baud rate too fast
for the clock fails elaboration, in simulation and in synthesis alike. On the
line, one HDLC frame is one command or one reply, checked by its frame check
sequence, with XON/XOFF carried in band — which is what the host's HDLC pipe
speaks.
