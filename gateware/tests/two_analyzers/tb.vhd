library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.apb.all;
use work.twola_pkg.all;

-- Two logic analyzers behind one backplane: each lays out its own map and
-- declares its own footprint, and the rack allocates them a segment apiece
-- above the descriptor ROM. What is checked here is that the whole thing
-- elaborates -- the two footprints, the two routers, the two envelopes -- and
-- that the descriptor reads back from the address the host looks at first.
-- The elaboration transcript carries the allocation the backplane reports.
entity tb is
end entity;

architecture sim of tb is
  constant apb_cfg_c : config_t := twola_core_apb_config;
  signal clock_s : std_ulogic := '0';
  signal done_s : boolean := false;
  signal reset_n_s : std_ulogic := '0';
  signal apb_m : master_t;
  signal apb_s : slave_t;
  signal state_s : std_ulogic_vector(3 downto 0) := (others => '0');
  signal word_s : std_ulogic_vector(7 downto 0) := (others => '0');
begin
  dut: twola_core
    port map(
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,
      front_control_clock_i => clock_s,
      front_control_reset_n_i => reset_n_s,
      front_control_state_i => state_s,
      back_control_clock_i => clock_s,
      back_control_reset_n_i => reset_n_s,
      back_control_word_i => word_s,
      back_aux_clock_i => clock_s,
      back_aux_reset_n_i => reset_n_s,
      back_aux_flag_i => '0'
      );

  clock_gen: process
  begin
    while not done_s loop
      clock_s <= '0';
      wait for 5 ns;
      clock_s <= '1';
      wait for 5 ns;
    end loop;
    wait;
  end process;

  check: process
    variable v : unsigned(31 downto 0);
    variable e : boolean;
  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m,
             to_unsigned(0, apb_cfg_c.address_width), v, e);
    assert not e report "descriptor read errored" severity failure;
    assert v(7 downto 0) = x"83"
      report "descriptor header wrong" severity failure;
    report "two-analyzer rack testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;
end architecture;
