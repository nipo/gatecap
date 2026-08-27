library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_bnoc, nsl_amba, nsl_data, nsl_line_coding, nsl_math, nsl_simulation,
  nsl_uart, gatecap;
use nsl_bnoc.testing.all;
use nsl_simulation.logging.all;
use nsl_data.bytestream.all;
use gatecap.testing.all;

-- serial_hdlc_adapter against the conformance sequence.
--
-- The simulation master is the adapter's own stack mirrored: an HDLC framer
-- and an XON/XOFF-guarded UART turn a command frame into line bits, and the
-- same blocks in the other direction recover the response frame. The two ends
-- run on unrelated clocks, as two ends of a serial line do, so the bit periods
-- only nominally agree.
entity tb is
end entity;

architecture sim of tb is

  constant dut_clock_hz_c : natural := 100_000_000;
  constant dut_clock_period_c : time := 10 ns;
  -- The master's clock is not the adapter's: same nominal rate, a percent off.
  constant ate_clock_period_c : time := 10130 ps;
  constant baud_rate_c : natural := 6_250_000;
  constant divisor_c : unsigned :=
    nsl_math.arith.to_unsigned_auto(dut_clock_hz_c / baud_rate_c - 1);

  signal ate_clock_s, ate_reset_n_s : std_ulogic;
  signal dut_clock_s, dut_reset_n_s : std_ulogic;
  signal done_s : std_ulogic_vector(0 to 0);

  -- The serial line, master to adapter and back.
  signal to_dut_s, from_dut_s : std_ulogic;

  signal cmd_framed_s, rsp_framed_s : nsl_bnoc.framed.framed_bus_t;
  signal cmd_hdlc_s, rsp_hdlc_s : nsl_bnoc.pipe.pipe_bus_t;
  signal ate_tx_s, ate_rx_s : nsl_bnoc.pipe.pipe_bus_t;
  signal peer_ready_s, rx_ready_s : std_ulogic;

  signal apb_s : nsl_amba.apb.bus_t;

begin

  dut: gatecap.adapter_serial_hdlc.serial_hdlc_adapter
    generic map(
      apb_config_c => adapter_apb_config_c,
      clock_frequency_c => dut_clock_hz_c,
      baud_rate_c => baud_rate_c,
      burst_length_l2_c => adapter_burst_length_l2_c,
      descriptor_base_c => adapter_descriptor_base_c
      )
    port map(
      clock_i => dut_clock_s,
      reset_n_i => dut_reset_n_s,
      uart_rx_i => to_dut_s,
      uart_tx_o => from_dut_s,
      apb_o => apb_s.m,
      apb_i => apb_s.s
      );

  completer: nsl_amba.ram.apb_ram
    generic map(
      config_c => adapter_apb_config_c,
      byte_size_l2_c => adapter_completer_size_l2_c
      )
    port map(
      clock_i => dut_clock_s,
      reset_n_i => dut_reset_n_s,
      apb_i => apb_s.m,
      apb_o => apb_s.s
      );

  framer: nsl_line_coding.hdlc.hdlc_framed_framer
    port map(
      clock_i => ate_clock_s,
      reset_n_i => ate_reset_n_s,
      framed_i => cmd_framed_s.req,
      framed_o => cmd_framed_s.ack,
      hdlc_o => cmd_hdlc_s.req,
      hdlc_i => cmd_hdlc_s.ack
      );

  flow_tx: nsl_uart.flow_control.xonxoff_tx
    port map(
      clock_i => ate_clock_s,
      reset_n_i => ate_reset_n_s,
      can_transmit_i => peer_ready_s,
      can_receive_i => rx_ready_s,
      tx_i => cmd_hdlc_s.req,
      tx_o => cmd_hdlc_s.ack,
      serdes_o => ate_tx_s.req,
      serdes_i => ate_tx_s.ack
      );

  uart: nsl_uart.transactor.uart8
    port map(
      clock_i => ate_clock_s,
      reset_n_i => ate_reset_n_s,
      divisor_i => divisor_c,
      rx_i => from_dut_s,
      tx_o => to_dut_s,
      rx_data_o => ate_rx_s.req,
      rx_data_i => ate_rx_s.ack,
      tx_data_i => ate_tx_s.req,
      tx_data_o => ate_tx_s.ack
      );

  flow_rx: nsl_uart.flow_control.xonxoff_rx
    port map(
      clock_i => ate_clock_s,
      reset_n_i => ate_reset_n_s,
      peer_ready_o => peer_ready_s,
      rx_ready_o => rx_ready_s,
      serdes_i => ate_rx_s.req,
      serdes_o => ate_rx_s.ack,
      rx_o => rsp_hdlc_s.req,
      rx_i => rsp_hdlc_s.ack
      );

  unframer: nsl_line_coding.hdlc.hdlc_framed_unframer
    port map(
      clock_i => ate_clock_s,
      reset_n_i => ate_reset_n_s,
      hdlc_i => rsp_hdlc_s.req,
      hdlc_o => rsp_hdlc_s.ack,
      framed_o => rsp_framed_s.req,
      framed_i => rsp_framed_s.ack
      );

  stim: process
  begin
    done_s(0) <= '0';
    cmd_framed_s.req <= nsl_bnoc.framed.framed_req_idle_c;
    rsp_framed_s.ack <= nsl_bnoc.framed.framed_ack_idle_c;
    wait for 1 us;

    for step in 0 to conformance_step_count_c - 1 loop
      framed_put(cmd_framed_s.req, cmd_framed_s.ack, ate_clock_s,
                 conformance_command(step));
      framed_check("serial_hdlc", rsp_framed_s.req, rsp_framed_s.ack,
                   ate_clock_s, conformance_response(step), LOG_LEVEL_FATAL);
    end loop;

    report "adapter_serial_hdlc testbench PASSED" severity note;
    done_s(0) <= '1';
    wait;
  end process;

  driver: nsl_simulation.driver.simulation_driver
    generic map(
      clock_count => 2,
      reset_count => 2,
      done_count => done_s'length
      )
    port map(
      clock_period(0) => ate_clock_period_c,
      clock_period(1) => dut_clock_period_c,
      reset_duration(0) => 42 ns,
      reset_duration(1) => 42 ns,
      reset_n_o(0) => ate_reset_n_s,
      reset_n_o(1) => dut_reset_n_s,
      clock_o(0) => ate_clock_s,
      clock_o(1) => dut_clock_s,
      done_i => done_s
      );

end architecture;
