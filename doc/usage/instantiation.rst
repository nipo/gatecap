Instantiating the rack
======================

The component
-------------

The rack is the entity the generator wrote out from description file
(:doc:`description`), and its component declaration is in the
generated package, so a selected name — or a ``use`` clause on that
package — is all the declaration needed.

For instance the following declaration:

.. code-block:: yaml

   name: rack_partition.demo_rack
   
   communication:
     mode: axi4_stream
   
   instruments:
     panel: !control-status
       clock: clock
       tick-counter-width: 8
   
       control:
         seven_seg: 8
   
       status:
         s2: 1
   
     clocks: !clock-measurer
       reference: ext
       frequency: 50_000_000
       clocks: [pll]
       max_rate: 150_000_000


can be instantiated as:

.. code-block:: vhdl

   library custom_lib;

   ...

   capture: custom_lib.rack_partition.demo_rack
     generic map(
       stream_config_c => nsl_bnoc.axi_adapter.axi4_stream_framed_config_c,
       burst_length_l2_c => 6
       )
     port map(
       clock_i => clock_i,
       reset_n_i => reset_n_i,

       rx_i => gatecap_cmd_s.m,
       rx_o => gatecap_cmd_s.s,
       tx_o => gatecap_rsp_s.m,
       tx_i => gatecap_rsp_s.s,

       panel_clock_i => ext_clock_i,
       panel_reset_n_i => ext_reset_n_i,
       panel_seven_seg_o => seven_seg_s,
       panel_s2_i => switch_i(2),

       clocks_ext_i => ext_clock_i,
       clocks_pll_i => pll_clk_i
       );

That one component is a complete core: the communication adapter, the
self-description ROM, the APB router and every instrument behind
it. The backplane and the instrument entities are instantiated by the
rack; you never name them.  They are in the package all the same,
which is what lets a design put its own front end in front of the
backplane if it has reason to.

Almost everything is settled by the description: the probe widths and
names, the buffer depths, the storage modes, the trigger's
capabilities, the sampling clock rates, the address map. What is left
on the boundary is the link, the clocks and the signals.

Ports
-----

``reset_n_i``
   The reset of the host side. There is a ``clock_i`` beside it unless
   ``communication.clock`` named an exported clock, in which case the rack
   rides that instrument port instead and has no clock of its own
   (:ref:`rack-host-clock`).

The link's own ports
   One set per communication mode: the four stream ports above,
   ``apb_i``/``apb_o``, ``swd_i``/``swd_o``, ``spi_i``/``spi_o``, the four TAP
   pins, or the two UART wires.

``<instance>_…``
   Everything an instrument brings, prefixed with its instance name: the
   clocks and resets of its domains, the signals it watches or drives, the bus
   it masters. :doc:`../instrument/index` lists them per instrument type; the
   description is what names them.

Generics
--------

Generics depend on :doc:`communication method
<../communication/index>` or :doc:`instruments <../instrument/index>`
selected.

Clocks and resets
-----------------

Each capture domain takes its own clock and reset, and its probes must
be signals of that domain — connect them raw, with no synchroniser in
front, as in :doc:`../instrument/logic_analyzer/clocking`. The host-side clock is
either one of those instrument ports, when ``communication.clock``
named an exported clock, or the rack's own ``clock_i``. In the example
above the rack has its own ``clock_i``, and the panel and the measurer
run on another clock — three domains, one entity, crossings generated
between them.
