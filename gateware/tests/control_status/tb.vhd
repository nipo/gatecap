library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_clocking, nsl_math, nsl_data, gatecap;
use nsl_amba.apb.all;
use nsl_data.text.all;
use nsl_math.int_ext.all;
use gatecap.control_status.all;

-- One control/status panel assembled the way the rack assembles it: the
-- shell on the host clock, the core on the instrument clock, and the
-- crossings of the inner contract in between. With async_c false the two
-- clocks are one and every crossing is replaced by plain wiring, which is
-- what a panel without a declared clock elaborates to.
entity panel_bench is
  generic (
    async_c : boolean
    );
  port (
    done_o : out boolean
    );
end entity;

architecture sim of panel_bench is

  constant apb_cfg_c : config_t := config(address_width => 12,
                                          data_bus_width => 32,
                                          err => true);

  constant control_width_c : integer_vector := (1, 12, 32);
  constant status_width_c : integer_vector := (1, 4);
  constant tick_out_count_c : integer_vector := (2, 1);
  constant tick_in_count_c : integer_vector := (2, 3);
  constant counter_width_c : natural := 4;
  constant fingerprint_c : unsigned(31 downto 0) := x"deadbeef";

  constant control_count_c : natural := control_width_c'length;
  constant status_count_c : natural := status_width_c'length;
  constant tick_out_words_c : natural := tick_out_count_c'length;
  constant tick_in_words_c : natural := tick_in_count_c'length;
  constant counter_count_c : natural := panel_bit_total(tick_in_count_c);

  -- Register addresses of this panel's layout.
  constant ADDR_TICK_OUT0_C : natural := 16#000#;
  constant ADDR_TICK_OUT1_C : natural := 16#004#;
  constant ADDR_STICKY_CLEAR0_C : natural := 16#008#;
  constant ADDR_STICKY_CLEAR1_C : natural := 16#00c#;
  constant ADDR_COUNTER_CLEAR0_C : natural := 16#010#;
  constant ADDR_COUNTER_CLEAR1_C : natural := 16#014#;
  constant ADDR_STATUS_C : natural := 16#200#;
  constant ADDR_FINGERPRINT_C : natural := 16#204#;
  constant ADDR_STICKY0_C : natural := 16#208#;
  constant ADDR_STICKY1_C : natural := 16#20c#;
  constant ADDR_STATUS_IN0_C : natural := 16#210#;
  constant ADDR_STATUS_IN1_C : natural := 16#214#;
  constant ADDR_COUNTER_C : natural := 16#218#;
  constant ADDR_CONTROL_C : natural := 16#300#;

  signal clock_s : std_ulogic := '0';
  signal core_clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;

  -- Shell side of the inner contract.
  signal sh_control_s : panel_word_vector(0 to control_count_c-1);
  signal sh_status_s : panel_word_vector(0 to status_count_c-1);
  signal sh_tick_out_mask_s : panel_word_vector(0 to tick_out_words_c-1);
  signal sh_tick_out_strobe_s : std_ulogic;
  signal sh_sticky_clear_s : panel_word_vector(0 to tick_in_words_c-1);
  signal sh_sticky_clear_strobe_s : std_ulogic;
  signal sh_sticky_s : panel_word_vector(0 to tick_in_words_c-1);
  signal sh_counter_s : panel_word_vector(0 to counter_count_c-1);

  -- Core side of the inner contract.
  signal co_control_s : panel_word_vector(0 to control_count_c-1);
  signal co_status_in_s : panel_word_vector(0 to status_count_c-1)
    := (others => (others => '0'));
  signal co_status_out_s : panel_word_vector(0 to status_count_c-1);
  signal co_tick_out_mask_s : panel_word_vector(0 to tick_out_words_c-1);
  signal co_tick_out_strobe_s : std_ulogic;
  signal co_tick_out_s : panel_word_vector(0 to tick_out_words_c-1);
  signal co_tick_in_s : panel_word_vector(0 to tick_in_words_c-1);
  signal co_sticky_clear_s : panel_word_vector(0 to tick_in_words_c-1);
  signal co_sticky_clear_strobe_s : std_ulogic;
  signal co_sticky_s : panel_word_vector(0 to tick_in_words_c-1);
  signal co_counter_s : panel_word_vector(0 to counter_count_c-1);

  subtype counter_t is unsigned(counter_width_c-1 downto 0);
  type counter_vector is array (natural range <>) of counter_t;
  signal crossed_counter_s : counter_vector(0 to counter_count_c-1);

  -- Tick inputs driven by the stimulus; bit 0 of word 0 additionally takes
  -- an event exactly on the clear strobe cycle when armed, which is the only
  -- way to observe the set-wins-over-clear rule.
  signal tick_drv_s : panel_word_vector(0 to tick_in_words_c-1)
    := (others => (others => '0'));
  signal concurrent_arm_s : std_ulogic := '0';

  -- Tick-out observation, in the core domain: cycles a tick was asserted.
  signal to0_b0_s, to0_b1_s, to0_both_s, to1_b0_s : natural := 0;

  -- Torn-value observation on a crossed control word.
  signal control1_a_s, control1_b_s : panel_word_t := (others => '0');

  function a(offset : natural) return unsigned is
  begin
    return to_unsigned(offset, apb_cfg_c.address_width);
  end function;

begin

  sync_clock: if not async_c generate
    -- Same clock on both sides, driven by one process: no skew, not even a
    -- delta cycle.
    process
    begin
      while not done_s loop
        clock_s <= '0';
        core_clock_s <= '0';
        wait for 5 ns;
        clock_s <= '1';
        core_clock_s <= '1';
        wait for 5 ns;
      end loop;
      wait;
    end process;
  end generate;

  async_clock: if async_c generate
    process
    begin
      while not done_s loop
        clock_s <= '0';
        wait for 5 ns;
        clock_s <= '1';
        wait for 5 ns;
      end loop;
      wait;
    end process;

    process
    begin
      while not done_s loop
        core_clock_s <= '0';
        wait for 3500 ps;
        core_clock_s <= '1';
        wait for 3500 ps;
      end loop;
      wait;
    end process;
  end generate;

  shell: gatecap.control_status.control_status_shell
    generic map(
      apb_config_c => apb_cfg_c,
      control_width_c => control_width_c,
      status_width_c => status_width_c,
      tick_out_count_c => tick_out_count_c,
      tick_in_count_c => tick_in_count_c,
      tick_counter_width_c => counter_width_c,
      fingerprint_c => fingerprint_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,
      control_o => sh_control_s,
      status_i => sh_status_s,
      tick_out_mask_o => sh_tick_out_mask_s,
      tick_out_strobe_o => sh_tick_out_strobe_s,
      sticky_clear_o => sh_sticky_clear_s,
      sticky_clear_strobe_o => sh_sticky_clear_strobe_s,
      sticky_i => sh_sticky_s,
      counter_i => sh_counter_s
      );

  core: gatecap.control_status.control_status_core
    generic map(
      status_width_c => status_width_c,
      tick_out_count_c => tick_out_count_c,
      tick_in_count_c => tick_in_count_c,
      tick_counter_width_c => counter_width_c
      )
    port map(
      clock_i => core_clock_s,
      reset_n_i => reset_n_s,
      status_i => co_status_in_s,
      status_o => co_status_out_s,
      tick_out_mask_i => co_tick_out_mask_s,
      tick_out_strobe_i => co_tick_out_strobe_s,
      tick_out_o => co_tick_out_s,
      tick_in_i => co_tick_in_s,
      sticky_clear_i => co_sticky_clear_s,
      sticky_clear_strobe_i => co_sticky_clear_strobe_s,
      sticky_o => co_sticky_s,
      counter_o => co_counter_s
      );

  -- The assembler's job: the inner contract, crossed.
  crossings: if async_c generate
    control_x: for k in 0 to control_count_c-1 generate
      inst: nsl_clocking.interdomain.interdomain_reg
        generic map(data_width_c => 32)
        port map(clock_i => core_clock_s,
                 data_i => sh_control_s(k),
                 data_o => co_control_s(k));
    end generate;

    status_x: for k in 0 to status_count_c-1 generate
      inst: nsl_clocking.interdomain.interdomain_reg
        generic map(data_width_c => 32)
        port map(clock_i => clock_s,
                 data_i => co_status_out_s(k),
                 data_o => sh_status_s(k));
    end generate;

    tick_out_mask_x: for g in 0 to tick_out_words_c-1 generate
      inst: nsl_clocking.interdomain.interdomain_static_reg
        generic map(data_width_c => 32)
        port map(input_clock_i => clock_s,
                 data_i => sh_tick_out_mask_s(g),
                 data_o => co_tick_out_mask_s(g));
    end generate;

    tick_out_strobe_x: nsl_clocking.interdomain.interdomain_tick
      port map(input_clock_i => clock_s,
               output_clock_i => core_clock_s,
               input_reset_n_i => reset_n_s,
               tick_i => sh_tick_out_strobe_s,
               tick_o => co_tick_out_strobe_s);

    sticky_clear_x: for g in 0 to tick_in_words_c-1 generate
      mask: nsl_clocking.interdomain.interdomain_static_reg
        generic map(data_width_c => 32)
        port map(input_clock_i => clock_s,
                 data_i => sh_sticky_clear_s(g),
                 data_o => co_sticky_clear_s(g));

      sticky: nsl_clocking.interdomain.interdomain_reg
        generic map(data_width_c => 32)
        port map(clock_i => clock_s,
                 data_i => co_sticky_s(g),
                 data_o => sh_sticky_s(g));
    end generate;

    sticky_clear_strobe_x: nsl_clocking.interdomain.interdomain_tick
      port map(input_clock_i => clock_s,
               output_clock_i => core_clock_s,
               input_reset_n_i => reset_n_s,
               tick_i => sh_sticky_clear_strobe_s,
               tick_o => co_sticky_clear_strobe_s);

    -- Only the meaningful bits of a counter may cross: they wrap, and a wrap
    -- is a single gray step at that width only.
    counter_x: for i in 0 to counter_count_c-1 generate
      inst: nsl_clocking.interdomain.interdomain_counter
        generic map(data_width_c => counter_width_c)
        port map(clock_in_i => core_clock_s,
                 clock_out_i => clock_s,
                 data_i => unsigned(co_counter_s(i)(counter_width_c-1 downto 0)),
                 data_o => crossed_counter_s(i));

      sh_counter_s(i) <= std_ulogic_vector(resize(crossed_counter_s(i), 32));
    end generate;
  end generate;

  wires: if not async_c generate
    co_control_s <= sh_control_s;
    sh_status_s <= co_status_out_s;
    co_tick_out_mask_s <= sh_tick_out_mask_s;
    co_tick_out_strobe_s <= sh_tick_out_strobe_s;
    co_sticky_clear_s <= sh_sticky_clear_s;
    co_sticky_clear_strobe_s <= sh_sticky_clear_strobe_s;
    sh_sticky_s <= co_sticky_s;
    sh_counter_s <= co_counter_s;
  end generate;

  co_tick_in_s(0)(0) <= tick_drv_s(0)(0)
                        or (co_sticky_clear_strobe_s and concurrent_arm_s);
  co_tick_in_s(0)(1) <= tick_drv_s(0)(1);
  co_tick_in_s(0)(31 downto 2) <= (others => '0');
  co_tick_in_s(1) <= tick_drv_s(1);

  tick_out_monitor: process(core_clock_s)
    variable b0, b1, both, w1b0 : natural := 0;
  begin
    if rising_edge(core_clock_s) then
      assert co_tick_out_s(0)(31 downto 2) = (co_tick_out_s(0)(31 downto 2)'range => '0')
        report "tick_out word 0 drives bits beyond its declared count"
        severity failure;
      assert co_tick_out_s(1)(31 downto 1) = (co_tick_out_s(1)(31 downto 1)'range => '0')
        report "tick_out word 1 drives bits beyond its declared count"
        severity failure;

      if co_tick_out_s(0)(0) = '1' then
        b0 := b0 + 1;
      end if;
      if co_tick_out_s(0)(1) = '1' then
        b1 := b1 + 1;
      end if;
      if co_tick_out_s(0)(0) = '1' and co_tick_out_s(0)(1) = '1' then
        both := both + 1;
      end if;
      if co_tick_out_s(1)(0) = '1' then
        w1b0 := w1b0 + 1;
      end if;

      to0_b0_s <= b0;
      to0_b1_s <= b1;
      to0_both_s <= both;
      to1_b0_s <= w1b0;
    end if;
  end process;

  -- A crossed control word is only ever seen as its old or its new value.
  control_monitor: process(core_clock_s)
  begin
    if rising_edge(core_clock_s) then
      if reset_n_s = '1' then
        assert co_control_s(1) = control1_a_s or co_control_s(1) = control1_b_s
          report "control word tore while crossing" severity failure;
      end if;
    end if;
  end process;

  stim: process
    variable v : unsigned(31 downto 0);
    variable e : boolean;
    variable c0, c1, cb, d0 : natural;

    procedure settle is
    begin
      for i in 1 to 24 loop
        wait until falling_edge(clock_s);
      end loop;
    end procedure;

    procedure emit(constant g, b, n : natural) is
    begin
      for i in 1 to n loop
        wait until rising_edge(core_clock_s);
        tick_drv_s(g)(b) <= '1';
        wait until rising_edge(core_clock_s);
        tick_drv_s(g)(b) <= '0';
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
        report what & " is " & to_string(std_ulogic_vector(value))
             & ", expected " & to_string(std_ulogic_vector(expected))
        severity failure;
    end procedure;

    procedure check(constant offset : natural;
                    constant expected : natural;
                    constant what : string) is
    begin
      check(offset, to_unsigned(expected, 32), what);
    end procedure;
  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 47 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    settle;

    check(ADDR_FINGERPRINT_C, fingerprint_c, "FINGERPRINT");
    check(ADDR_STATUS_C, 0, "STATUS");

    -- Control: readback from the shell's own storage, value reaching the
    -- core, and high bits beyond the declared width dropped.
    control1_a_s <= (others => '0');
    control1_b_s <= x"00000abc";
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_CONTROL_C + 4),
              unsigned'(x"00000abc"), err => e);
    assert not e report "control write errored" severity failure;
    check(ADDR_CONTROL_C + 4, 16#abc#, "control 1 readback");
    settle;
    control1_a_s <= x"00000abc";
    assert co_control_s(1) = x"00000abc"
      report "control 1 did not reach the core" severity failure;

    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_CONTROL_C),
              unsigned'(x"ffffffff"), err => e);
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_CONTROL_C + 8),
              unsigned'(x"12345678"), err => e);
    check(ADDR_CONTROL_C, 1, "control 0 readback");
    check(ADDR_CONTROL_C + 8, unsigned'(x"12345678"), "control 2 readback");
    settle;
    assert co_control_s(0) = x"00000001"
      report "control 0 not masked to its declared width" severity failure;
    assert co_control_s(2) = x"12345678"
      report "control 2 did not reach the core" severity failure;

    -- Status: input levels sampled and masked to their declared width.
    co_status_in_s(0) <= x"ffffffff";
    co_status_in_s(1) <= x"0000005a";
    settle;
    check(ADDR_STATUS_IN0_C, 1, "status 0");
    check(ADDR_STATUS_IN1_C, 16#a#, "status 1");

    co_status_in_s(1) <= x"00000003";
    settle;
    check(ADDR_STATUS_IN1_C, 3, "status 1 after change");

    -- Tick out: one write, one cycle, every masked bit in the same cycle.
    c0 := to0_b0_s;
    c1 := to0_b1_s;
    cb := to0_both_s;
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_TICK_OUT0_C),
              unsigned'(x"00000003"), err => e);
    assert not e report "tick-out write errored" severity failure;
    settle;
    assert to0_b0_s = c0 + 1 and to0_b1_s = c1 + 1
      report "masked ticks did not pulse exactly one cycle each"
      severity failure;
    assert to0_both_s = cb + 1
      report "ticks of one word did not pulse in the same cycle"
      severity failure;

    c0 := to0_b0_s;
    c1 := to0_b1_s;
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_TICK_OUT0_C),
              unsigned'(x"00000001"), err => e);
    settle;
    assert to0_b0_s = c0 + 1 and to0_b1_s = c1
      report "an unmasked tick pulsed" severity failure;

    -- Another word: bits beyond its declared count stay low, and the word
    -- written before does not fire again.
    c0 := to0_b0_s;
    d0 := to1_b0_s;
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_TICK_OUT1_C),
              unsigned'(x"ffffffff"), err => e);
    settle;
    assert to1_b0_s = d0 + 1 report "tick-out word 1 did not pulse"
      severity failure;
    assert to0_b0_s = c0 report "tick-out word 0 fired on a word 1 write"
      severity failure;

    -- Back-to-back writes, paced by the transport.
    c1 := to0_b1_s;
    for i in 1 to 4 loop
      apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_TICK_OUT0_C),
                unsigned'(x"00000002"), err => e);
      for j in 1 to 3 loop
        wait until falling_edge(clock_s);
      end loop;
    end loop;
    settle;
    assert to0_b1_s = c1 + 4
      report "back-to-back tick-out writes did not all pulse" severity failure;

    -- Action registers are write-only.
    check(ADDR_TICK_OUT0_C, 0, "tick-out register readback");
    check(ADDR_STICKY_CLEAR0_C, 0, "sticky-clear register readback");
    check(ADDR_COUNTER_CLEAR0_C, 0, "counter-clear register readback");

    -- Sticky: set on event, W1C clears only the bits written.
    emit(1, 1, 1);
    settle;
    check(ADDR_STICKY1_C, 2, "sticky word 1 after event");
    emit(1, 0, 1);
    settle;
    check(ADDR_STICKY1_C, 3, "sticky word 1 after second event");

    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_STICKY_CLEAR1_C),
              unsigned'(x"00000001"), err => e);
    settle;
    check(ADDR_STICKY1_C, 2, "sticky word 1 after clearing bit 0");

    -- An event landing on the clear cycle wins over the clear.
    emit(0, 1, 1);
    settle;
    check(ADDR_STICKY0_C, 2, "sticky word 0 after event");
    concurrent_arm_s <= '1';
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_STICKY_CLEAR0_C),
              unsigned'(x"00000003"), err => e);
    settle;
    concurrent_arm_s <= '0';
    settle;
    check(ADDR_STICKY0_C, 1, "sticky word 0 after a clear an event raced");

    -- Counters: one per tick input, counting every event.
    check(ADDR_COUNTER_C + 4 * 2, 1, "counter 2");
    check(ADDR_COUNTER_C + 4 * 3, 1, "counter 3");
    emit(1, 2, 5);
    settle;
    check(ADDR_COUNTER_C + 4 * 4, 5, "counter 4");

    -- Rebasing takes the visible value, so events still crossing when the
    -- clear is written are counted, not lost.
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_COUNTER_CLEAR1_C),
              unsigned'(x"00000004"), err => e);
    emit(1, 2, 3);
    settle;
    check(ADDR_COUNTER_C + 4 * 4, 3, "counter 4 after rebase");
    check(ADDR_COUNTER_C + 4 * 3, 1, "counter 3 after another counter rebase");

    -- Counters wrap at their declared width.
    emit(1, 2, 20);
    settle;
    check(ADDR_COUNTER_C + 4 * 4, (3 + 20) mod 2**counter_width_c,
          "counter 4 after wrapping");

    done_o <= true;
    done_s <= true;
    wait;
  end process;

end architecture;

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_math, gatecap;
use nsl_amba.apb.all;
use nsl_math.int_ext.all;
use gatecap.control_status.all;

-- A panel missing whole signal kinds: the generator emits these as soon as a
-- description leaves a kind out. Null arrays must elaborate and the register
-- file must still answer.
entity panel_degenerate is
  generic (
    control_width_c : integer_vector := no_panel_signals_c;
    status_width_c : integer_vector := no_panel_signals_c;
    tick_out_count_c : integer_vector := no_panel_signals_c;
    tick_in_count_c : integer_vector := no_panel_signals_c
    );
  port (
    done_o : out boolean
    );
end entity;

architecture sim of panel_degenerate is

  constant apb_cfg_c : config_t := config(address_width => 12,
                                          data_bus_width => 32,
                                          err => true);
  constant fingerprint_c : unsigned(31 downto 0) := x"01234567";
  constant counter_count_c : natural := panel_bit_total(tick_in_count_c);

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;

  signal control_s : panel_word_vector(0 to control_width_c'length-1);
  signal status_in_s : panel_word_vector(0 to status_width_c'length-1)
    := (others => (others => '0'));
  signal status_out_s : panel_word_vector(0 to status_width_c'length-1);
  signal tick_out_mask_s : panel_word_vector(0 to tick_out_count_c'length-1);
  signal tick_out_strobe_s : std_ulogic;
  signal tick_out_s : panel_word_vector(0 to tick_out_count_c'length-1);
  signal tick_in_s : panel_word_vector(0 to tick_in_count_c'length-1)
    := (others => (others => '0'));
  signal sticky_clear_s : panel_word_vector(0 to tick_in_count_c'length-1);
  signal sticky_clear_strobe_s : std_ulogic;
  signal sticky_s : panel_word_vector(0 to tick_in_count_c'length-1);
  signal counter_s : panel_word_vector(0 to counter_count_c-1);

begin

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

  shell: gatecap.control_status.control_status_shell
    generic map(
      apb_config_c => apb_cfg_c,
      control_width_c => control_width_c,
      status_width_c => status_width_c,
      tick_out_count_c => tick_out_count_c,
      tick_in_count_c => tick_in_count_c,
      tick_counter_width_c => 8,
      fingerprint_c => fingerprint_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,
      control_o => control_s,
      status_i => status_out_s,
      tick_out_mask_o => tick_out_mask_s,
      tick_out_strobe_o => tick_out_strobe_s,
      sticky_clear_o => sticky_clear_s,
      sticky_clear_strobe_o => sticky_clear_strobe_s,
      sticky_i => sticky_s,
      counter_i => counter_s
      );

  core: gatecap.control_status.control_status_core
    generic map(
      status_width_c => status_width_c,
      tick_out_count_c => tick_out_count_c,
      tick_in_count_c => tick_in_count_c,
      tick_counter_width_c => 8
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      status_i => status_in_s,
      status_o => status_out_s,
      tick_out_mask_i => tick_out_mask_s,
      tick_out_strobe_i => tick_out_strobe_s,
      tick_out_o => tick_out_s,
      tick_in_i => tick_in_s,
      sticky_clear_i => sticky_clear_s,
      sticky_clear_strobe_i => sticky_clear_strobe_s,
      sticky_o => sticky_s,
      counter_o => counter_s
      );

  stim: process
    variable v : unsigned(31 downto 0);
    variable e : boolean;
  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 47 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    for i in 1 to 8 loop
      wait until falling_edge(clock_s);
    end loop;

    apb_read(apb_cfg_c, clock_s, apb_s, apb_m,
             to_unsigned(16#204#, apb_cfg_c.address_width), v, e);
    assert not e report "FINGERPRINT read errored" severity failure;
    assert v = fingerprint_c report "FINGERPRINT wrong" severity failure;

    apb_read(apb_cfg_c, clock_s, apb_s, apb_m,
             to_unsigned(16#200#, apb_cfg_c.address_width), v, e);
    assert not e report "STATUS read errored" severity failure;
    assert v = 0 report "STATUS wrong" severity failure;

    done_o <= true;
    done_s <= true;
    wait;
  end process;

end architecture;

library ieee;
use ieee.std_logic_1164.all;

library nsl_math;
use nsl_math.int_ext.all;

entity tb is
end entity;

architecture sim of tb is

  signal async_done_s, sync_done_s : boolean := false;
  signal empty_done_s, tick_only_done_s, control_only_done_s : boolean := false;

begin

  -- The panel with its own clock: every crossing of the inner contract in
  -- place.
  async_panel: entity work.panel_bench
    generic map(async_c => true)
    port map(done_o => async_done_s);

  -- The same panel without a declared clock: shell and core share the host
  -- clock and the crossings are plain wires.
  sync_panel: entity work.panel_bench
    generic map(async_c => false)
    port map(done_o => sync_done_s);

  empty_panel: entity work.panel_degenerate
    port map(done_o => empty_done_s);

  tick_only_panel: entity work.panel_degenerate
    generic map(tick_out_count_c => (0 => 3),
                tick_in_count_c => (0 => 2))
    port map(done_o => tick_only_done_s);

  control_only_panel: entity work.panel_degenerate
    generic map(control_width_c => (0 => 8))
    port map(done_o => control_only_done_s);

  report_result: process
  begin
    wait until async_done_s and sync_done_s and empty_done_s
      and tick_only_done_s and control_only_done_s;
    report "control_status testbench PASSED" severity note;
    wait;
  end process;

end architecture;
