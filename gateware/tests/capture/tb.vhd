library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library gatecap;

entity tb is
end entity;

architecture sim of tb is

  constant signal_count_c : natural := 8;
  constant len_width_c : natural := 8;
  constant depth_l2_c : natural := 6;
  constant window_count_c : natural := 4;

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal signals_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');
  signal arm_s : std_ulogic := '0';
  signal abort_s : std_ulogic := '0';
  signal trig_value_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');
  signal trig_mask_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');
  -- The trigger block now lives outside the core; model its value/mask
  -- compare here and feed the core a single trigger line.
  signal trigger_s : std_ulogic;
  signal capture_len_s : unsigned(len_width_c-1 downto 0) := (others => '0');
  signal pre_trigger_len_s : unsigned(len_width_c-1 downto 0) := (others => '0');
  signal window_count_s : unsigned(len_width_c-1 downto 0) := to_unsigned(1, len_width_c);

  signal state_s : std_ulogic_vector(1 downto 0);
  signal triggered_s : std_ulogic;
  signal head_s : unsigned(depth_l2_c-1 downto 0);
  signal head_we_s : std_ulogic;
  signal write_en_s : std_ulogic;
  signal write_addr_s : unsigned(depth_l2_c-1 downto 0);
  signal write_data_s : std_ulogic_vector(signal_count_c-1 downto 0);

  constant STATE_IDLE_C : std_ulogic_vector(1 downto 0) := "00";
  constant STATE_ARMED_C : std_ulogic_vector(1 downto 0) := "01";
  constant STATE_CAPTURING_C : std_ulogic_vector(1 downto 0) := "10";

  -- Local model of the dumb trace buffer.
  type mem_t is array (0 to 2**depth_l2_c-1) of std_ulogic_vector(signal_count_c-1 downto 0);
  signal mem_s : mem_t;

  -- Per-window heads latched from the core (windows complete in order),
  -- with a running completed-window count reset on arm.
  type head_array_t is array (0 to window_count_c-1) of unsigned(depth_l2_c-1 downto 0);
  signal heads_s : head_array_t;
  signal ndone_s : natural := 0;

begin

  dut: gatecap.capture.capture_core
    generic map(
      signal_count_c => signal_count_c,
      capture_len_width_c => len_width_c,
      depth_l2_c => depth_l2_c,
      window_count_c => window_count_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      signals_i => signals_s,
      arm_i => arm_s,
      abort_i => abort_s,
      trigger_i => trigger_s,
      capture_len_i => capture_len_s,
      pre_trigger_len_i => pre_trigger_len_s,
      window_count_i => window_count_s,
      state_o => state_s,
      triggered_o => triggered_s,
      ready_o => open,
      head_o => head_s,
      head_we_o => head_we_s,
      write_en_o => write_en_s,
      write_addr_o => write_addr_s,
      write_data_o => write_data_s
      );

  trigger_s <= '1' when (signals_s and trig_mask_s) = (trig_value_s and trig_mask_s)
               else '0';

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

  -- Free-running signal source: captured samples are consecutive.
  sig_gen: process(clock_s)
  begin
    if rising_edge(clock_s) then
      signals_s <= std_ulogic_vector(unsigned(signals_s) + 1);
    end if;
  end process;

  -- The dumb buffer honours whatever address/data the core drives.
  mem_writer: process(clock_s)
  begin
    if rising_edge(clock_s) then
      if write_en_s = '1' then
        mem_s(to_integer(write_addr_s)) <= write_data_s;
      end if;
    end if;
  end process;

  head_latch: process(clock_s)
    variable cnt : natural := 0;
  begin
    if rising_edge(clock_s) then
      if arm_s = '1' then
        cnt := 0;
      elsif head_we_s = '1' then
        heads_s(cnt) <= head_s;
        cnt := cnt + 1;
      end if;
      ndone_s <= cnt;
    end if;
  end process;

  stim: process
    -- Read a window of `count` samples from head, wrapping within its slot
    -- (a count-sized region), check they are consecutive, and return the
    -- sample at the trigger index.
    procedure check_window(head : natural;
                           count : natural;
                           trig_index : natural;
                           trig_sample : out unsigned(signal_count_c-1 downto 0)) is
      variable slot_base : natural := (head / count) * count;
      variable idx : natural;
      variable prev : unsigned(signal_count_c-1 downto 0);
      variable cur : unsigned(signal_count_c-1 downto 0);
    begin
      for k in 0 to count-1 loop
        idx := slot_base + ((head - slot_base + k) mod count);
        cur := unsigned(mem_s(idx));
        if k > 0 then
          assert cur = prev + 1
            report "window not consecutive at index " & integer'image(k)
            severity failure;
        end if;
        if k = trig_index then
          trig_sample := cur;
        end if;
        prev := cur;
      end loop;
    end procedure;

    variable base : unsigned(signal_count_c-1 downto 0);
    variable tsample : unsigned(signal_count_c-1 downto 0);
  begin
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);

    -- Test 1: match-all trigger, post-trigger only, 4 samples, one window.
    trig_mask_s <= (others => '0');
    trig_value_s <= (others => '0');
    capture_len_s <= to_unsigned(4, len_width_c);
    pre_trigger_len_s <= to_unsigned(0, len_width_c);
    window_count_s <= to_unsigned(1, len_width_c);
    wait until falling_edge(clock_s);
    arm_s <= '1';
    wait until falling_edge(clock_s);
    arm_s <= '0';
    for i in 0 to 15 loop
      wait until falling_edge(clock_s);
    end loop;
    assert state_s = STATE_IDLE_C report "T1: not IDLE after capture" severity failure;
    assert triggered_s = '1' report "T1: triggered not set" severity failure;
    assert ndone_s = 1 report "T1: wrong window count" severity failure;
    check_window(to_integer(heads_s(0)), 4, 0, tsample);

    -- Test 2: pre-trigger. capture_len 8, pre_trigger_len 3, trigger on a
    -- value reached well after the pre-fill wraps in the slot.
    base := unsigned(signals_s);
    trig_mask_s <= (others => '1');
    trig_value_s <= std_ulogic_vector(base + 30);
    capture_len_s <= to_unsigned(8, len_width_c);
    pre_trigger_len_s <= to_unsigned(3, len_width_c);
    window_count_s <= to_unsigned(1, len_width_c);
    wait until falling_edge(clock_s);
    arm_s <= '1';
    wait until falling_edge(clock_s);
    arm_s <= '0';
    for i in 0 to 50 loop
      wait until falling_edge(clock_s);
    end loop;
    assert state_s = STATE_IDLE_C report "T2: not IDLE after capture" severity failure;
    assert triggered_s = '1' report "T2: triggered not set" severity failure;
    check_window(to_integer(heads_s(0)), 8, 3, tsample);
    assert tsample = base + 30
      report "T2: trigger sample not at pre_trigger_len index" severity failure;

    -- Test 3: abort while ARMED. Trigger on a value the counter has just
    -- passed, so it will not recur before we abort.
    trig_mask_s <= (others => '1');
    trig_value_s <= std_ulogic_vector(unsigned(signals_s) - 5);
    capture_len_s <= to_unsigned(8, len_width_c);
    pre_trigger_len_s <= to_unsigned(0, len_width_c);
    window_count_s <= to_unsigned(1, len_width_c);
    wait until falling_edge(clock_s);
    arm_s <= '1';
    wait until falling_edge(clock_s);
    arm_s <= '0';
    wait until falling_edge(clock_s);
    assert state_s = STATE_ARMED_C report "T3: not ARMED" severity failure;
    abort_s <= '1';
    wait until falling_edge(clock_s);
    abort_s <= '0';
    wait until falling_edge(clock_s);
    assert state_s = STATE_IDLE_C report "T3: not IDLE after abort" severity failure;
    assert triggered_s = '0' report "T3: triggered set after abort" severity failure;

    -- Test 4: abort while CAPTURING (long capture cut short).
    trig_mask_s <= (others => '0');
    trig_value_s <= (others => '0');
    capture_len_s <= to_unsigned(60, len_width_c);
    pre_trigger_len_s <= to_unsigned(0, len_width_c);
    window_count_s <= to_unsigned(1, len_width_c);
    wait until falling_edge(clock_s);
    arm_s <= '1';
    wait until falling_edge(clock_s);
    arm_s <= '0';
    for i in 0 to 4 loop
      wait until falling_edge(clock_s);
    end loop;
    assert state_s = STATE_CAPTURING_C report "T4: not CAPTURING" severity failure;
    abort_s <= '1';
    wait until falling_edge(clock_s);
    abort_s <= '0';
    wait until falling_edge(clock_s);
    assert state_s = STATE_IDLE_C report "T4: not IDLE after abort" severity failure;
    assert triggered_s = '1' report "T4: triggered not set" severity failure;

    -- Test 5: multi-window, immediate re-trigger. 3 windows of 4 samples,
    -- back-to-back at slots 0, 4, 8; each window's samples consecutive.
    trig_mask_s <= (others => '0');
    trig_value_s <= (others => '0');
    capture_len_s <= to_unsigned(4, len_width_c);
    pre_trigger_len_s <= to_unsigned(0, len_width_c);
    window_count_s <= to_unsigned(3, len_width_c);
    wait until falling_edge(clock_s);
    arm_s <= '1';
    wait until falling_edge(clock_s);
    arm_s <= '0';
    for i in 0 to 30 loop
      wait until falling_edge(clock_s);
    end loop;
    assert state_s = STATE_IDLE_C report "T5: not IDLE after captures" severity failure;
    assert ndone_s = 3 report "T5: wrong window count" severity failure;
    for w in 0 to 2 loop
      assert heads_s(w) = to_unsigned(w*4, depth_l2_c)
        report "T5: wrong head for window " & integer'image(w) severity failure;
      check_window(to_integer(heads_s(w)), 4, 0, tsample);
    end loop;

    -- Test 6: multi-window with pre-trigger. 2 windows of 8 samples,
    -- pre_trigger 2, immediate value. Slots 0 and 8, trigger at index 2.
    trig_mask_s <= (others => '0');
    trig_value_s <= (others => '0');
    capture_len_s <= to_unsigned(8, len_width_c);
    pre_trigger_len_s <= to_unsigned(2, len_width_c);
    window_count_s <= to_unsigned(2, len_width_c);
    wait until falling_edge(clock_s);
    arm_s <= '1';
    wait until falling_edge(clock_s);
    arm_s <= '0';
    for i in 0 to 40 loop
      wait until falling_edge(clock_s);
    end loop;
    assert state_s = STATE_IDLE_C report "T6: not IDLE after captures" severity failure;
    assert ndone_s = 2 report "T6: wrong window count" severity failure;
    for w in 0 to 1 loop
      check_window(to_integer(heads_s(w)), 8, 2, tsample);
    end loop;

    report "capture_core testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
