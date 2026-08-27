library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.axi4_stream.all;
use work.mixed_pkg.all;

-- UDP harness for a generated rack holding two instruments of different
-- types: a logic analyzer over a free-running bench on the transport's own
-- clock, and a control/status panel on a clock unrelated to it.
--
-- The panel's wires are looped back here, so a host can observe what it drove:
-- every control feeds the status of the same width (with the same enumeration
-- where it has one), and every tick output feeds the tick input of the
-- matching name. A tick output is one panel-clock cycle wide, so each strobe
-- is exactly one event on its counter.
--
-- The analyzer's stimulus is the same shape as the other socket benches: a
-- free-running counter and a state bus asserting DONE for one cycle every 64.
-- Driven externally by the acrobe host; runs until killed.
entity tb is
end entity;

architecture sim of tb is

  constant stream_cfg_c : config_t := config(1, last => true);

  constant event_period_c : natural := 64;
  constant period_bits_c : natural := 6;

  signal host_clock_s : std_ulogic := '0';
  signal host_reset_n_s : std_ulogic := '0';
  signal panel_clock_s : std_ulogic := '0';
  signal panel_reset_n_s : std_ulogic := '0';

  signal rx_cmd_s : master_t;
  signal rx_rdy_s : slave_t;
  signal tx_rsp_s : master_t;
  signal tx_rdy_s : slave_t;

  signal count_s : unsigned(7 downto 0) := (others => '0');
  signal state_s : std_ulogic_vector(1 downto 0) := "01";

  signal led_s : std_ulogic;
  signal level_s : unsigned(11 downto 0);
  signal mode_s : unsigned(1 downto 0);
  signal start_s, stop_s, soft_reset_s : std_ulogic;

begin

  net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
    generic map(
      config_c => stream_cfg_c,
      bind_port_c => 4251
      )
    port map(
      clock_i => host_clock_s,
      reset_n_i => host_reset_n_s,
      tx_i => tx_rsp_s,
      tx_o => tx_rdy_s,
      rx_o => rx_cmd_s,
      rx_i => rx_rdy_s
      );

  dut: mixed_rack
    generic map(
      stream_config_c => stream_cfg_c,
      burst_length_l2_c => 8
      )
    port map(
      reset_n_i => host_reset_n_s,
      rx_i => rx_cmd_s,
      rx_o => rx_rdy_s,
      tx_o => tx_rsp_s,
      tx_i => tx_rdy_s,

      la_main_clock_i => host_clock_s,
      la_main_reset_n_i => host_reset_n_s,
      la_main_state_i => state_s,
      la_main_count_i => std_ulogic_vector(count_s),

      panel_clk_i => panel_clock_s,
      panel_reset_n_i => panel_reset_n_s,
      panel_led_o => led_s,
      panel_level_o => level_s,
      panel_mode_o => mode_s,
      -- Loopback: what the host wrote comes back as the panel's statuses.
      panel_led_echo_i => led_s,
      panel_level_echo_i => level_s,
      panel_mode_echo_i => mode_s,
      panel_start_o => start_s,
      panel_stop_o => stop_s,
      panel_soft_reset_o => soft_reset_s,
      -- Loopback: each strobed tick output is one event on its counter.
      panel_started_i => start_s,
      panel_stopped_i => stop_s,
      panel_was_reset_i => soft_reset_s
      );

  host_clock_s <= not host_clock_s after 5 ns;      -- 100 MHz
  panel_clock_s <= not panel_clock_s after 3500 ps; -- ~143 MHz, unrelated

  reset: process
  begin
    host_reset_n_s <= '0';
    panel_reset_n_s <= '0';
    wait for 42 ns;
    host_reset_n_s <= '1';
    panel_reset_n_s <= '1';
    wait;
  end process;

  -- Analyzer stimulus: a free-running counter, and the state asserted for the
  -- single cycle where the counter reaches a multiple of the event period.
  bench: process(host_clock_s)
  begin
    if rising_edge(host_clock_s) then
      count_s <= count_s + 1;
      if count_s(period_bits_c-1 downto 0) = event_period_c - 1 then
        state_s <= "11";                -- DONE
      else
        state_s <= "01";                -- BUSY
      end if;
    end if;
  end process;

end architecture;
