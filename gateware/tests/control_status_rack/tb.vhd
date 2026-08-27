library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.apb.all;
use work.panel_pkg.all;

-- A generated rack of two control/status panels, driven over its plain APB
-- completer. "panel" runs on a clock of its own, so every read and write goes
-- through the crossings the generator instantiated; "mini" runs on the rack's
-- clock with two of the four kinds left out, so the same scenario also covers
-- the collapsed wiring and the null boundary arrays.
--
-- The map is the one the descriptor advertises: the ROM at 0 in a 4 KB
-- segment, then the two 1 KB panel segments.
entity tb is
end entity;

architecture sim of tb is

  constant apb_cfg_c : config_t := panel_rack_apb_config;

  constant counter_width_c : natural := 4;

  -- panel: 2 tick-out words, 1 tick-in word, 2 statuses, 3 controls.
  constant PANEL_C : natural := 16#1000#;
  constant P_TICK_OUT0_C : natural := PANEL_C + 16#000#;
  constant P_TICK_OUT1_C : natural := PANEL_C + 16#004#;
  constant P_STICKY_CLEAR_C : natural := PANEL_C + 16#008#;
  constant P_COUNTER_CLEAR_C : natural := PANEL_C + 16#00c#;
  constant P_STATUS_C : natural := PANEL_C + 16#200#;
  constant P_FINGERPRINT_C : natural := PANEL_C + 16#204#;
  constant P_STICKY_C : natural := PANEL_C + 16#208#;
  constant P_STATE_C : natural := PANEL_C + 16#20c#;
  constant P_DONE_C : natural := PANEL_C + 16#210#;
  constant P_COUNTER_C : natural := PANEL_C + 16#214#;
  constant P_LED_C : natural := PANEL_C + 16#300#;
  constant P_DAC_LEVEL_C : natural := PANEL_C + 16#304#;
  constant P_MODE_C : natural := PANEL_C + 16#308#;

  -- mini: no tick-out word and no status, so its regions start earlier.
  constant MINI_C : natural := 16#1400#;
  constant M_STICKY_CLEAR_C : natural := MINI_C + 16#000#;
  constant M_COUNTER_CLEAR_C : natural := MINI_C + 16#004#;
  constant M_FINGERPRINT_C : natural := MINI_C + 16#204#;
  constant M_STICKY_C : natural := MINI_C + 16#208#;
  constant M_COUNTER_C : natural := MINI_C + 16#20c#;
  constant M_GATE_C : natural := MINI_C + 16#300#;

  signal clock_s : std_ulogic := '0';
  signal panel_clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;

  signal led_s : std_ulogic;
  signal dac_level_s : unsigned(11 downto 0);
  signal mode_s : unsigned(1 downto 0);
  signal state_s : unsigned(3 downto 0) := (others => '0');
  signal done_in_s : std_ulogic := '0';
  signal start_s, stop_s, soft_reset_s : std_ulogic;
  signal overflow_s, underflow_s : std_ulogic := '0';
  signal gate_s : std_ulogic;
  signal pulse_s : std_ulogic := '0';

  -- Tick-out observation, in the panel domain: cycles each tick was asserted,
  -- and cycles the two ticks of one word were asserted together.
  signal start_n_s, stop_n_s, both_n_s, soft_reset_n_s : natural := 0;

  function a(offset : natural) return unsigned is
  begin
    return to_unsigned(offset, apb_cfg_c.address_width);
  end function;

begin

  dut: panel_rack
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,

      panel_clk_i => panel_clock_s,
      panel_reset_n_i => reset_n_s,
      panel_led_o => led_s,
      panel_dac_level_o => dac_level_s,
      panel_mode_o => mode_s,
      panel_state_i => state_s,
      panel_done_i => done_in_s,
      panel_start_o => start_s,
      panel_stop_o => stop_s,
      panel_soft_reset_o => soft_reset_s,
      panel_overflow_i => overflow_s,
      panel_underflow_i => underflow_s,

      mini_gate_o => gate_s,
      mini_pulse_i => pulse_s
      );

  host_clock: process
  begin
    while not done_s loop
      clock_s <= '0';
      wait for 5 ns;
      clock_s <= '1';
      wait for 5 ns;
    end loop;
    wait;
  end process;

  instrument_clock: process
  begin
    while not done_s loop
      panel_clock_s <= '0';
      wait for 3500 ps;
      panel_clock_s <= '1';
      wait for 3500 ps;
    end loop;
    wait;
  end process;

  tick_out_monitor: process(panel_clock_s)
    variable n0, n1, both, sr : natural := 0;
  begin
    if rising_edge(panel_clock_s) then
      if start_s = '1' then
        n0 := n0 + 1;
      end if;
      if stop_s = '1' then
        n1 := n1 + 1;
      end if;
      if start_s = '1' and stop_s = '1' then
        both := both + 1;
      end if;
      if soft_reset_s = '1' then
        sr := sr + 1;
      end if;

      start_n_s <= n0;
      stop_n_s <= n1;
      both_n_s <= both;
      soft_reset_n_s <= sr;
    end if;
  end process;

  stim: process
    variable v : unsigned(31 downto 0);
    variable fp : unsigned(31 downto 0);
    variable e : boolean;
    variable n0, n1, both, sr : natural;

    procedure settle is
    begin
      for i in 1 to 24 loop
        wait until falling_edge(clock_s);
      end loop;
    end procedure;

    -- One-cycle pulses on a tick input of the panel, in its own domain.
    procedure emit(signal tick : out std_ulogic; constant n : natural) is
    begin
      for i in 1 to n loop
        wait until rising_edge(panel_clock_s);
        tick <= '1';
        wait until rising_edge(panel_clock_s);
        tick <= '0';
      end loop;
    end procedure;

    procedure check(constant offset : natural;
                    constant expected : unsigned;
                    constant what : string) is
      variable value : unsigned(31 downto 0);
      variable err : boolean;
    begin
      apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(offset), value, err);
      assert not err report what & " read errored" severity failure;
      assert value = expected
        report what & " is " & integer'image(to_integer(value))
             & ", expected " & integer'image(to_integer(expected))
        severity failure;
    end procedure;

    procedure check(constant offset : natural;
                    constant expected : natural;
                    constant what : string) is
    begin
      check(offset, to_unsigned(expected, 32), what);
    end procedure;

    procedure poke(constant offset : natural;
                   constant value : unsigned(31 downto 0);
                   constant what : string) is
      variable err : boolean;
    begin
      apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(offset), value, err => err);
      assert not err report what & " write errored" severity failure;
    end procedure;
  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 47 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    settle;

    -- Descriptor: first byte is the root array header (0x83: type,
    -- next-offset, instruments map).
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(0), v, e);
    assert not e report "descriptor read errored" severity failure;
    assert v(7 downto 0) = x"83" report "descriptor header wrong"
      severity failure;

    -- Instance fingerprint: non-trivial, stable, and the same in both panels
    -- -- it is the whole rack's descriptor that is keyed.
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(P_FINGERPRINT_C), v, e);
    assert not e report "fingerprint read errored" severity failure;
    assert v /= 0 and v /= x"ffffffff" report "fingerprint looks trivial"
      severity failure;
    fp := v;
    check(P_FINGERPRINT_C, fp, "fingerprint on a second read");
    check(M_FINGERPRINT_C, fp, "fingerprint of the second panel");
    check(P_STATUS_C, 0, "STATUS");

    -- Controls: read back from the shell, and reaching the user ports at
    -- their declared width, high bits dropped.
    poke(P_LED_C, x"ffffffff", "led");
    poke(P_DAC_LEVEL_C, x"00000abc", "dac_level");
    poke(P_MODE_C, x"00000002", "mode");
    check(P_LED_C, 1, "led readback");
    check(P_DAC_LEVEL_C, 16#abc#, "dac_level readback");
    check(P_MODE_C, 2, "mode readback");
    settle;
    assert led_s = '1' report "led did not reach its port" severity failure;
    assert dac_level_s = x"abc" report "dac_level did not reach its port"
      severity failure;
    assert mode_s = "10" report "mode did not reach its port" severity failure;

    -- Statuses: input levels sampled and masked to their declared width.
    state_s <= x"5";
    done_in_s <= '1';
    settle;
    check(P_STATE_C, 5, "state");
    check(P_DONE_C, 1, "done");
    state_s <= x"2";
    settle;
    check(P_STATE_C, 2, "state after change");

    -- Tick out: one write, one panel cycle, every masked tick in that same
    -- cycle.
    n0 := start_n_s;
    n1 := stop_n_s;
    both := both_n_s;
    poke(P_TICK_OUT0_C, x"00000003", "tick-out word 0");
    settle;
    assert start_n_s = n0 + 1 and stop_n_s = n1 + 1
      report "masked ticks did not pulse exactly one cycle each"
      severity failure;
    assert both_n_s = both + 1
      report "ticks of one word did not pulse in the same cycle"
      severity failure;

    n0 := start_n_s;
    n1 := stop_n_s;
    sr := soft_reset_n_s;
    poke(P_TICK_OUT1_C, x"ffffffff", "tick-out word 1");
    settle;
    assert soft_reset_n_s = sr + 1 report "soft_reset did not pulse"
      severity failure;
    assert start_n_s = n0 and stop_n_s = n1
      report "another tick-out word fired" severity failure;

    -- Action registers are write-only.
    check(P_TICK_OUT0_C, 0, "tick-out register readback");
    check(P_STICKY_CLEAR_C, 0, "sticky-clear register readback");

    -- Tick in: a sticky bit and a counter per input, both maintained in the
    -- panel's own domain.
    emit(overflow_s, 3);
    emit(underflow_s, 1);
    settle;
    check(P_STICKY_C, 3, "sticky word");
    check(P_COUNTER_C, 3, "overflow counter");
    check(P_COUNTER_C + 4, 1, "underflow counter");

    -- Clearing is per bit, and clearing a counter rebases that one alone.
    poke(P_STICKY_CLEAR_C, x"00000001", "sticky clear");
    settle;
    check(P_STICKY_C, 2, "sticky word after clearing bit 0");
    poke(P_COUNTER_CLEAR_C, x"00000001", "counter clear");
    settle;
    check(P_COUNTER_C, 0, "overflow counter after rebase");
    check(P_COUNTER_C + 4, 1, "underflow counter after another rebase");

    -- Counters wrap at their declared width.
    emit(overflow_s, 20);
    settle;
    check(P_COUNTER_C, 20 mod 2**counter_width_c,
          "overflow counter after wrapping");

    -- The panel with no clock of its own: same behaviour over plain wires,
    -- and its counter takes the default 32-bit width.
    poke(M_GATE_C, x"00000001", "gate");
    check(M_GATE_C, 1, "gate readback");
    settle;
    assert gate_s = '1' report "gate did not reach its port" severity failure;

    for i in 1 to 5 loop
      wait until rising_edge(clock_s);
      pulse_s <= '1';
      wait until rising_edge(clock_s);
      pulse_s <= '0';
    end loop;
    settle;
    check(M_STICKY_C, 1, "mini sticky word");
    check(M_COUNTER_C, 5, "mini counter");
    poke(M_STICKY_CLEAR_C, x"00000001", "mini sticky clear");
    poke(M_COUNTER_CLEAR_C, x"00000001", "mini counter clear");
    settle;
    check(M_STICKY_C, 0, "mini sticky word after clear");
    check(M_COUNTER_C, 0, "mini counter after rebase");

    report "control_status_rack testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
