library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.axi4_stream.all;
library gatecap_generated;
use gatecap_generated.link_pkg.all;

-- UDP harness for a generated two-domain core whose single trigger correlates
-- both captures. The control domain runs at 100 MHz and carries a probed
-- AXI4-Stream, a state bus and a counter; the phy domain runs at 125 MHz and
-- carries its own counter plus a mark bit driven straight from the control
-- domain's trigger condition, so a host reading both buffers can line the two
-- windows up in absolute time.
--
-- The trigger condition (state = DONE) lasts one control cycle and recurs
-- every 64: a host that arms only one of the two capture controls sees the
-- condition go by repeatedly without firing, since the shared trigger stays
-- disabled until every subscriber is ready. Driven externally by the acrobe
-- host; runs until killed.
entity tb is
end entity;

architecture sim of tb is

  constant stream_cfg_c : config_t := config(1, last => true);
  constant command_cfg_c : config_t := config(1, last => true, ready => true);

  -- Control cycles between two trigger conditions. Both capture windows are
  -- shorter than this, so a window holds exactly one marked sample.
  constant event_period_c : natural := 64;
  constant period_bits_c : natural := 6;

  signal control_clock_s : std_ulogic := '0';
  signal control_reset_n_s : std_ulogic := '0';
  signal phy_clock_s : std_ulogic := '0';
  signal phy_reset_n_s : std_ulogic := '0';

  signal rx_cmd_s : master_t;
  signal rx_rdy_s : slave_t;
  signal tx_rsp_s : master_t;
  signal tx_rdy_s : slave_t;

  signal count_s : unsigned(7 downto 0) := (others => '0');
  signal state_s : std_ulogic_vector(1 downto 0) := "01";
  signal mark_s : std_ulogic := '0';
  signal command_s : bus_t;

  signal word_s : unsigned(7 downto 0) := (others => '0');

begin

  net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
    generic map(
      config_c => stream_cfg_c,
      bind_port_c => 4248
      )
    port map(
      clock_i => control_clock_s,
      reset_n_i => control_reset_n_s,
      tx_i => tx_rsp_s,
      tx_o => tx_rdy_s,
      rx_o => rx_cmd_s,
      rx_i => rx_rdy_s
      );

  dut: link_capture
    generic map(
      stream_config_c => stream_cfg_c,
      burst_length_l2_c => 8,
      la_control_command_config_c => command_cfg_c
      )
    port map(
      reset_n_i => control_reset_n_s,
      rx_i => rx_cmd_s,
      rx_o => rx_rdy_s,
      tx_o => tx_rsp_s,
      tx_i => tx_rdy_s,
      la_control_clock_i => control_clock_s,
      la_control_reset_n_i => control_reset_n_s,
      la_control_command_i => command_s,
      la_control_state_i => state_s,
      la_control_count_i => std_ulogic_vector(count_s),
      la_phy_clock_i => phy_clock_s,
      la_phy_reset_n_i => phy_reset_n_s,
      la_phy_word_i => std_ulogic_vector(word_s),
      la_phy_mark_i => mark_s
      );

  control_clock_s <= not control_clock_s after 5 ns;   -- 100 MHz
  phy_clock_s <= not phy_clock_s after 4 ns;           -- 125 MHz

  reset: process
  begin
    control_reset_n_s <= '0';
    phy_reset_n_s <= '0';
    wait for 42 ns;
    control_reset_n_s <= '1';
    phy_reset_n_s <= '1';
    wait;
  end process;

  -- Control-domain stimulus: a free-running counter, and the state/mark pair
  -- asserted for the single cycle where the counter reaches a multiple of the
  -- event period. Both are registered, so the mark the phy domain probes is
  -- glitch-free and coincident with the sample the trigger matches.
  control_gen: process(control_clock_s)
  begin
    if rising_edge(control_clock_s) then
      count_s <= count_s + 1;
      if count_s(period_bits_c-1 downto 0) = event_period_c - 1 then
        state_s <= "11";                -- DONE
        mark_s <= '1';
      else
        state_s <= "01";                -- BUSY
        mark_s <= '0';
      end if;
    end if;
  end process;

  -- Probed AXI4-Stream: the counter as payload, one transfer every other
  -- cycle, last on every sixteenth value, always accepted.
  command_s.m <= transfer(command_cfg_c,
                          value => count_s,
                          valid => count_s(0) = '1',
                          last => count_s(3 downto 0) = "1111");
  command_s.s <= accept(command_cfg_c, true);

  -- Phy-domain stimulus: its own free-running counter on the faster clock.
  phy_gen: process(phy_clock_s)
  begin
    if rising_edge(phy_clock_s) then
      word_s <= word_s + 1;
    end if;
  end process;

end architecture;
