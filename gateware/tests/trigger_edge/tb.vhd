library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, gatecap;
use nsl_amba.apb.all;

entity tb is
end entity;

architecture sim of tb is

  constant signal_count_c : natural := 8;
  constant apb_cfg_c : config_t := config(address_width => 12,
                                          data_bus_width => 32,
                                          err => true);

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;

  signal sig_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');
  signal en_s : std_ulogic := '1';
  signal trigger_o_s : std_ulogic;

begin

  dut: gatecap.control.trigger_control_edge
    generic map(
      apb_config_c => apb_cfg_c,
      signal_count_c => signal_count_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,
      capture_clock_i => clock_s,
      capture_reset_n_i => reset_n_s,
      signals_i => sig_s,
      enable_i => en_s,
      trigger_o => trigger_o_s
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

  stim: process
    variable e : boolean;

    procedure cfg(reg : natural; val : unsigned(31 downto 0)) is
    begin
      apb_write(apb_cfg_c, clock_s, apb_s, apb_m, to_unsigned(reg, 12), val,
                err => e);
    end procedure;

    -- Program a term: new value/mask and old value/mask.
    procedure term(nv, nm, ov, om : natural) is
    begin
      cfg(16#100#, to_unsigned(nv, 32));
      cfg(16#104#, to_unsigned(nm, 32));
      cfg(16#108#, to_unsigned(ov, 32));
      cfg(16#10c#, to_unsigned(om, 32));
    end procedure;

    -- Re-arm: drop enable, raise it, and let the enable pipeline (en1/en2)
    -- refill, so the next event is evaluated fully-enabled with `fired` clear.
    procedure rearm is
    begin
      en_s <= '0';
      wait until falling_edge(clock_s);
      en_s <= '1';
      wait until falling_edge(clock_s);
      wait until falling_edge(clock_s);
    end procedure;

    -- Set the watched value and settle for `cycles` falling edges.
    procedure drive(val : natural; cycles : natural) is
    begin
      sig_s <= std_ulogic_vector(to_unsigned(val, signal_count_c));
      for i in 1 to cycles loop wait until falling_edge(clock_s); end loop;
    end procedure;

    -- Drive `val` and count trigger ticks over `cycles`.
    procedure count(val, cycles : natural; got : out natural) is
      variable n : natural := 0;
    begin
      sig_s <= std_ulogic_vector(to_unsigned(val, signal_count_c));
      for i in 1 to cycles loop
        wait until falling_edge(clock_s);
        if trigger_o_s = '1' then n := n + 1; end if;
      end loop;
      got := n;
    end procedure;

    variable n : natural;
  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);

    -- The output is a one-cycle tick, one-shot per arm: each qualifying event
    -- fires exactly one tick, and a fresh event needs a re-arm (see rearm).

    -- Test 1: value match A(bit0)=1, don't-care old. One tick, then held.
    term(16#01#, 16#01#, 16#00#, 16#00#);
    drive(16#00#, 3);
    rearm;
    count(16#01#, 6, n);
    assert n = 1 report "T1: value match expected 1 tick, got "
      & integer'image(n) severity failure;
    count(16#01#, 4, n);   -- persists high, no re-fire without a re-arm
    assert n = 0 report "T1: value match re-fired without a re-arm" severity failure;

    -- Test 2: rising edge on bit0 (old=0, new=1). One tick.
    term(16#01#, 16#01#, 16#00#, 16#01#);
    drive(16#00#, 3);
    rearm;
    count(16#01#, 6, n);
    assert n = 1 report "T2: rising expected 1 tick, got "
      & integer'image(n) severity failure;
    count(16#01#, 4, n);   -- steady high is not an edge
    assert n = 0 report "T2: rising re-fired on steady high" severity failure;

    -- Test 3: falling edge on bit1 (old=1, new=0).
    term(16#00#, 16#02#, 16#02#, 16#02#);
    drive(16#02#, 3);
    rearm;
    count(16#00#, 6, n);
    assert n = 1 report "T3: falling expected 1 tick, got "
      & integer'image(n) severity failure;

    -- Test 4: mixed -- A(bit0)=1 by value AND B(bit1) falling.
    term(16#01#, 16#03#, 16#02#, 16#02#);
    drive(16#03#, 3);
    rearm;
    count(16#03#, 3, n);   -- A=1, B=1: B new must be 0 -> no fire
    assert n = 0 report "T4: fired with B high" severity failure;
    count(16#01#, 6, n);   -- B falls to 0, A stays 1: one tick
    assert n = 1 report "T4: mixed expected 1 tick, got "
      & integer'image(n) severity failure;

    report "trigger_control_edge testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
