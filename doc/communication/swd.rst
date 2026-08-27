``SWD``
=======

Two wires, and a stock SWD debug probe on the other end. The adapter
carries a whole debug port — a serial-wire DP, one Mem-AP behind it
and a bridge down to the rack — so there is nothing to instantiate
around it. It adds ``swd_i`` and ``swd_o``, the pin records of
``nsl_coresight.swd``, and no generic.

Description
-----------

.. code-block:: yaml

   communication:
     mode: swd
     clock: la.sample

The Mem-AP addresses a flat 32-bit space, and the bridge below it requires the
same address width on both of its sides, so both sit at 32 bits. The rack's own
map is narrower and applies from the router inward, where the top address bits
simply go undecoded: the descriptor sits at address zero of the AP's memory
space, and the host reads on from there.

The access port answers with an identification register of ``0x04ed0001``, a
reserved JEP106 code, which is how the host tells a capture rack from a CPU.
Nothing above the wire is gatecap-specific: a debugger walks the DP and the AP
as it would on any other target. Host side, including the board demo in
``gateware/example/swd_transport``: :ref:`SWD paths <host-transport-swd>`.

Instantiation
-------------

The rack needs nothing around it but its two pins and a clock. Wire them to the
pads through ``nsl_coresight.swd.swd_slave_driver``, or to a debug-port block
already in your design:

.. code-block:: vhdl

   dut: swd_capture
     port map(
       clock_i => clock_s,
       reset_n_i => reset_n_s,
       swd_i => swd_s.i,
       swd_o => swd_s.o,
       la_sample_clock_i => sample_clock_s,
       la_sample_reset_n_i => sample_reset_n_s,
       la_sample_state_i => state_s,
       la_sample_data_i => data_s
       );

Connection
----------

The host reaches it through any SWD probe, naming the access port the rack
presents — over a simulated probe, that is:

.. code-block:: console

   $ acrobe gatecap -r "udp/127.0.0.1:4249/nsl_swd(fin=100M)/dp(ap_probe=0)" info

The rack's map starts at address zero of the Mem-AP's 32-bit space; the
descriptor is the first thing there, so nothing has to tell the host where to
look.
