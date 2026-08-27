``AXI4-Stream``
===============

A frame interface — ``rx_i``/``rx_o``/``tx_o``/``tx_i`` — driven by a
transport you wire yourself, a USB endpoint, a network stack, the UDP
gateway of a simulation. This is the mode to pick when the design
already has a link to the host, or when the link is one gatecap has no
adapter for.

Description
-----------

.. code-block:: yaml

   communication:
     mode: axi4_stream
     clock: la.sample      # the transport rides the sample domain's clock

NSL BNOC retrofit
-----------------

Many NSL transports speak the ``nsl_bnoc`` framed protocol rather than
AXI4-Stream; ``nsl_bnoc.axi_adapter`` converts in both directions:

.. code-block:: vhdl

   rsp_adapter: nsl_bnoc.axi_adapter.framed_to_axi4_stream
     port map(
       clock_i => clock_s,
       reset_n_i => reset_n_s,
       framed_i => gatecap_framed_s.cmd.req,
       framed_o => gatecap_framed_s.cmd.ack,
       axi_o => cap_cmd_s.m,
       axi_i => cap_cmd_s.s
       );

   cmd_adapter: nsl_bnoc.axi_adapter.axi4_stream_to_framed
     port map(
       clock_i => clock_s,
       reset_n_i => reset_n_s,
       axi_i => cap_rsp_s.m,
       axi_o => cap_rsp_s.s,
       framed_o => gatecap_framed_s.rsp.req,
       framed_i => gatecap_framed_s.rsp.ack
       );

The host side of the link is chosen at connection time, by resource path, and
needs no matching configuration in the gateware.
