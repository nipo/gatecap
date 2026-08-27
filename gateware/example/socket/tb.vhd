library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.axi4_stream.all;
use work.socket_pkg.all;

-- Simulation harness: a UDP socket gateway in front of a generated capture
-- rack. Each UDP packet is one command frame; each response frame is sent back
-- as one packet. The sampled signals are a free-running counter, so a
-- captured trace reads back as consecutive values. Driven externally by the
-- acrobe host; runs until killed.
entity tb is
end entity;

architecture sim of tb is

  constant stream_cfg_c : config_t := config(1, last => true);
  constant signal_count_c : natural := 8;

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';

  signal rx_cmd_s : master_t;
  signal rx_rdy_s : slave_t;
  signal tx_rsp_s : master_t;
  signal tx_rdy_s : slave_t;

  signal signals_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');

begin

  net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
    generic map(
      config_c => stream_cfg_c,
      bind_port_c => 4242
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      tx_i => tx_rsp_s,
      tx_o => tx_rdy_s,
      rx_o => rx_cmd_s,
      rx_i => rx_rdy_s
      );

  dut: socket_capture
    generic map(
      stream_config_c => stream_cfg_c,
      burst_length_l2_c => 8
      )
    port map(
      reset_n_i => reset_n_s,
      rx_i => rx_cmd_s,
      rx_o => rx_rdy_s,
      tx_o => tx_rsp_s,
      tx_i => tx_rdy_s,
      la_control_clock_i => clock_s,
      la_control_reset_n_i => reset_n_s,
      la_control_count0_i => signals_s(0),
      la_control_count1_i => signals_s(1),
      la_control_count2_i => signals_s(2),
      la_control_count3_i => signals_s(3),
      la_control_count4_i => signals_s(4),
      la_control_count5_i => signals_s(5),
      la_control_count6_i => signals_s(6),
      la_control_count7_i => signals_s(7)
      );

  clock_s <= not clock_s after 5 ns;

  reset: process
  begin
    reset_n_s <= '0';
    wait for 42 ns;
    reset_n_s <= '1';
    wait;
  end process;

  sig_gen: process(clock_s)
  begin
    if rising_edge(clock_s) then
      signals_s <= std_ulogic_vector(unsigned(signals_s) + 1);
    end if;
  end process;

end architecture;
