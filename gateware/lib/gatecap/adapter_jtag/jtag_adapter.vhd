library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_bnoc, nsl_jtag, gatecap;

-- JTAG front end of a rack: the host reaches it through the FPGA's own TAP.
--
-- nsl_jtag.continuous_transport carries framed bytes both ways over a single
-- Shift-DR run of a user data register, with credit-based flow control. A
-- frame is exactly one command or one response, so the transport hands the
-- stream adapter whole frames through the byte-wide AXI4-Stream the bridge
-- speaks. The transport crosses TCK to clock_i in its own FIFOs, so the
-- adapter is clocked like any other.
--
-- The TAP pins are ports because some vendors expect the boundary pins routed
-- to the primitive explicitly; the ones that wire the TAP internally leave
-- them unbound.
entity jtag_adapter is
  generic (
    apb_config_c : nsl_amba.apb.config_t;
    burst_length_l2_c : natural;
    descriptor_base_c : natural := 0
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    chip_tck_i : in std_ulogic := '0';
    chip_tms_i : in std_ulogic := '0';
    chip_tdi_i : in std_ulogic := '0';
    chip_tdo_o : out std_ulogic;

    apb_o : out nsl_amba.apb.master_t;
    apb_i : in nsl_amba.apb.slave_t
    );
end entity;

architecture rtl of jtag_adapter is

  signal rx_framed_s : nsl_bnoc.framed.framed_bus_t;
  signal tx_framed_s : nsl_bnoc.framed.framed_bus_t;
  signal rx_s : nsl_amba.axi4_stream.bus_t;
  signal tx_s : nsl_amba.axi4_stream.bus_t;

begin

  -- Test-access port to framed bytes. Its TAP-reset output stays open: the
  -- rack is reset with the rest of the design, not by a debugger touching the
  -- chain.
  tap: nsl_jtag.continuous_transport.jtag_continuous_transport_tap
    port map(
      chip_tck_i => chip_tck_i,
      chip_tms_i => chip_tms_i,
      chip_tdi_i => chip_tdi_i,
      chip_tdo_o => chip_tdo_o,
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      reset_n_o => open,
      rx_o => rx_framed_s.req,
      rx_i => rx_framed_s.ack,
      tx_i => tx_framed_s.req,
      tx_o => tx_framed_s.ack
      );

  rx_adapter: nsl_bnoc.axi_adapter.framed_to_axi4_stream
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      framed_i => rx_framed_s.req,
      framed_o => rx_framed_s.ack,
      axi_o => rx_s.m,
      axi_i => rx_s.s
      );

  tx_adapter: nsl_bnoc.axi_adapter.axi4_stream_to_framed
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      axi_i => tx_s.m,
      axi_o => tx_s.s,
      framed_o => tx_framed_s.req,
      framed_i => tx_framed_s.ack
      );

  stream: gatecap.adapter_stream.stream_adapter
    generic map(
      apb_config_c => apb_config_c,
      stream_config_c => nsl_bnoc.axi_adapter.axi4_stream_framed_config_c,
      burst_length_l2_c => burst_length_l2_c,
      descriptor_base_c => descriptor_base_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      rx_i => rx_s.m,
      rx_o => rx_s.s,
      tx_o => tx_s.m,
      tx_i => tx_s.s,
      apb_o => apb_o,
      apb_i => apb_i
      );

end architecture;
