``JTAG``
========

The host reaches the rack through the FPGA's own test-access port, over a user
data register — no pins, no transport of your own. It adds ``chip_tck_i``,
``chip_tms_i``, ``chip_tdi_i`` and ``chip_tdo_o``, and the
``burst_length_l2_c`` generic.

Description
-----------

.. code-block:: yaml

   communication:
     mode: jtag
     clock: la.sample

Instantiation
-------------

The three inputs are tied off in the entity, so leave them unbound on
vendors whose TAP primitive reaches the boundary pins by itself
(Xilinx); wire them where your flow expects the pins routed
explicitly.

The rack brings its own transport: nothing to instantiate around
it. The host walks the chain to the user data register the transport
claims (:ref:`JTAG paths <host-transport-jtag>`); the command protocol
above the link is unchanged.

.. code-block:: vhdl

   ila: jtag_capture
     generic map(
       burst_length_l2_c => 6
       )
     port map(
       reset_n_i => reset_n_s,

       chip_tck_i => chip_tck_i,
       chip_tms_i => chip_tms_i,
       chip_tdi_i => chip_tdi_i,
       chip_tdo_o => chip_tdo_o,

       la_sample_clock_i => sample_clock_s,
       la_sample_reset_n_i => sample_reset_n_s,
       la_sample_state_i => state_s,
       la_sample_data_i => data_s
       );
