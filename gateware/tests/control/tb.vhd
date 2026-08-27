library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, gatecap;
use nsl_amba.apb.all;

entity tb is
end entity;

architecture sim of tb is

  constant signal_count_c : natural := 8;
  constant len_width_c : natural := 16;
  constant apb_cfg_c : config_t := config(address_width => 12,
                                          data_bus_width => 32,
                                          err => true);

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;

  signal arm_o_s, abort_o_s : std_ulogic;
  signal capture_len_o_s : unsigned(len_width_c-1 downto 0);
  signal state_i_s : std_ulogic_vector(1 downto 0) := "00";
  signal triggered_i_s : std_ulogic := '0';

  signal arm_count_s : natural := 0;
  signal abort_count_s : natural := 0;

  -- Trigger block, on its own point-to-point APB link.
  signal apb_m2 : master_t;
  signal apb_s2 : slave_t;
  signal sig_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');
  -- Start disabled: value/mask reset to 0 is match-all, which would tick and
  -- latch `fired` at startup. The sub-test enables explicitly to arm.
  signal trig_en_s : std_ulogic := '0';
  signal trigger_o_s : std_ulogic;

begin

  dut: gatecap.control.capture_control
    generic map(
      apb_config_c => apb_cfg_c,
      capture_len_width_c => len_width_c,
      depth_l2_c => 6
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,
      arm_o => arm_o_s,
      abort_o => abort_o_s,
      capture_len_o => capture_len_o_s,
      pre_trigger_len_o => open,
      window_count_o => open,
      enable_o => open,
      state_i => state_i_s,
      triggered_i => triggered_i_s,
      ready_i => '0',
      head_i => to_unsigned(0, 6),
      head_we_i => '0'
      );

  trig: gatecap.control.trigger_control
    generic map(
      apb_config_c => apb_cfg_c,
      signal_count_c => signal_count_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m2,
      apb_o => apb_s2,
      capture_clock_i => clock_s,
      capture_reset_n_i => reset_n_s,
      signals_i => sig_s,
      enable_i => trig_en_s,
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

  monitor: process(clock_s)
    variable ac, bc : natural := 0;
  begin
    if rising_edge(clock_s) then
      if arm_o_s = '1' then
        ac := ac + 1;
      end if;
      if abort_o_s = '1' then
        bc := bc + 1;
      end if;
      arm_count_s <= ac;
      abort_count_s <= bc;
    end if;
  end process;

  stim: process
    variable v : unsigned(31 downto 0);
    variable e : boolean;
    variable c : natural;
    variable n : natural;
  begin
    apb_m <= transfer_idle(apb_cfg_c);
    apb_m2 <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);

    -- COMMAND = ARM -> one arm pulse
    c := arm_count_s;
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m,
              to_unsigned(16#000#, 12), unsigned'(x"00000001"), err => e);
    assert not e report "COMMAND write errored" severity failure;
    for i in 0 to 2 loop wait until falling_edge(clock_s); end loop;
    assert arm_count_s = c + 1 report "ARM did not pulse once" severity failure;

    -- COMMAND = ABORT -> one abort pulse
    c := abort_count_s;
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m,
              to_unsigned(16#000#, 12), unsigned'(x"00000002"), err => e);
    for i in 0 to 2 loop wait until falling_edge(clock_s); end loop;
    assert abort_count_s = c + 1 report "ABORT did not pulse once" severity failure;

    -- CAPTURE_LEN register (config group).
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m,
              to_unsigned(16#100#, 12), unsigned'(x"00000040"), err => e);
    wait until falling_edge(clock_s);
    assert capture_len_o_s = to_unsigned(16#40#, len_width_c)
      report "CAPTURE_LEN wrong" severity failure;

    -- STATUS reflects state/triggered: state=CAPTURING (2), triggered=1 -> 0b110.
    state_i_s <= "10";
    triggered_i_s <= '1';
    wait until falling_edge(clock_s);
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, to_unsigned(16#200#, 12), v, e);
    assert not e report "STATUS read errored" severity failure;
    assert v = to_unsigned(6, 32) report "STATUS wrong" severity failure;

    -- Trigger block: VALUE=0xa0, MASK=0xf0 -> match when the high nibble is
    -- 0xa. The output is a one-cycle tick on the first match while enabled
    -- (trig_en_s), held off until a re-arm (enable dropped then raised).
    apb_write(apb_cfg_c, clock_s, apb_s2, apb_m2,
              to_unsigned(16#100#, 12), unsigned'(x"000000a0"), err => e);
    apb_write(apb_cfg_c, clock_s, apb_s2, apb_m2,
              to_unsigned(16#104#, 12), unsigned'(x"000000f0"), err => e);
    apb_read(apb_cfg_c, clock_s, apb_s2, apb_m2, to_unsigned(16#100#, 12), v, e);
    assert v = to_unsigned(16#a0#, 32) report "VALUE readback wrong" severity failure;
    -- Matching value, then enable: exactly one tick over a short window.
    sig_s <= x"a5";
    trig_en_s <= '1';
    n := 0;
    for i in 1 to 5 loop
      wait until falling_edge(clock_s);
      if trigger_o_s = '1' then n := n + 1; end if;
    end loop;
    assert n = 1 report "trigger did not tick exactly once on match, got "
      & integer'image(n) severity failure;
    -- Mismatch after a re-arm: no tick.
    sig_s <= x"b5";
    trig_en_s <= '0';
    wait until falling_edge(clock_s);
    trig_en_s <= '1';
    n := 0;
    for i in 1 to 5 loop
      wait until falling_edge(clock_s);
      if trigger_o_s = '1' then n := n + 1; end if;
    end loop;
    assert n = 0 report "trigger fired on mismatch" severity failure;

    report "capture_control testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
