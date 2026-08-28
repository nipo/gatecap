library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.axi4_stream.all;
library gatecap_generated;
use gatecap_generated.enum_pkg.all;

-- Like the socket harness, but the 8 counter bits are advertised as one bus
-- carrying an enum: values 0..2 come from a well-known base (demo.phase), 3..5
-- are the description's own additions, and 6..255 are left undefined (the host
-- renders them as hex). This exercises every enum path end to end -- splice,
-- extend and undefined -- through a real descriptor. Driven by the acrobe host.
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
      bind_port_c => 4247
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      tx_i => tx_rsp_s,
      tx_o => tx_rdy_s,
      rx_o => rx_cmd_s,
      rx_i => rx_rdy_s
      );

  dut: enum_capture
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
      la_control_count_i => signals_s
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
