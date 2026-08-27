library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_clocking;
use nsl_amba.apb.all;

-- Edge/transition trigger block. Like trigger_control, but it matches the
-- current ("new") and previous-cycle ("old") signal values with independent
-- value/mask pairs, so one term can mix levels and edges: e.g. A=1 (match A's
-- new value, don't care its old) together with B falling (B old=1, new=0).
-- A level is old-mask 0; a rising edge is old=0/new=1; a falling edge is
-- old=1/new=0. Two independent masks are required -- a single mask could not
-- express "match the new value but not care how we got there".
--
-- The inputs are registered twice (raw_reg = 1 cycle, delayed = 2 cycles) so
-- the compare is flop-to-flop, and the match is registered. The strobe trails
-- the new-value cycle by trigger_control_edge_latency_c; the capture core
-- back-dates by that, landing the trigger sample on the new-value cycle -- the
-- same position a value trigger would give.
--
-- Config lives in the 0x100 group, per the gatecap map convention (this block
-- has no action or status group).
--   0x100 NEW_VALUE (W/R)  required current value, under NEW_MASK
--   0x104 NEW_MASK  (W/R)  1 = the bit's new value participates
--   0x108 OLD_VALUE (W/R)  required previous value, under OLD_MASK
--   0x10c OLD_MASK  (W/R)  1 = the bit's old (previous-cycle) value participates
entity trigger_control_edge is
  generic (
    apb_config_c : config_t;
    signal_count_c : natural;
    -- When true the match runs on capture_clock_i (asynchronous to the APB
    -- clock_i) with new/old value/mask crossed in; when false (default) the
    -- whole block is on clock_i, byte-for-byte unchanged.
    async_c : boolean := false
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    apb_i : in master_t;
    apb_o : out slave_t;

    -- Capture-domain clock/reset for the match. Tie to clock_i/reset_n_i for a
    -- single-clock block; drive from the capture clock when async_c.
    capture_clock_i : in std_ulogic := '0';
    capture_reset_n_i : in std_ulogic := '1';

    -- Signals watched by the trigger (capture domain), independent of probes.
    signals_i : in std_ulogic_vector(signal_count_c-1 downto 0);

    -- Trigger armed only while high (from the capture core's ready).
    enable_i : in std_ulogic := '1';

    -- Trigger event to the capture core: a one-cycle tick on first edge.
    trigger_o : out std_ulogic
    );
end entity;

architecture rtl of trigger_control_edge is

  constant reg_count_l2_c : natural := 8;   -- config group at 0x100

  constant REG_NEW_VALUE_C : natural := 16#100# / 4;
  constant REG_NEW_MASK_C : natural := 16#104# / 4;
  constant REG_OLD_VALUE_C : natural := 16#108# / 4;
  constant REG_OLD_MASK_C : natural := 16#10c# / 4;

  constant word_bits_c : natural := 8 * 2**apb_config_c.data_bus_width_l2;

  signal reg_no_s : integer range 0 to 2**reg_count_l2_c-1;
  signal w_value_s : unsigned(word_bits_c-1 downto 0);
  signal w_strobe_s : std_ulogic;
  signal r_value_s : unsigned(word_bits_c-1 downto 0);

  signal new_value_s, new_mask_s, old_value_s, old_mask_s
    : std_ulogic_vector(signal_count_c-1 downto 0);
  -- Effective config in the match's clock domain (CDC'd when async).
  signal nv_eff_s, nm_eff_s, ov_eff_s, om_eff_s
    : std_ulogic_vector(signal_count_c-1 downto 0);
  -- raw_reg: inputs delayed one cycle (the "new" value); delayed: two cycles
  -- (the "old" value).
  signal raw_reg_s, delayed_s : std_ulogic_vector(signal_count_c-1 downto 0);
  -- enable pipelined to the two edge cycles (en1 aligns with raw_reg/new,
  -- en2 with delayed/old), so a tick fires only for a transition that occurred
  -- entirely while enabled -- not one straddling the enable boundary.
  signal en1_s, en2_s : std_ulogic;
  -- Latched once a tick has fired; cleared when enable drops (re-arm).
  signal fired_s : std_ulogic;

begin

  regmap: nsl_amba.apb.apb_regmap
    generic map(
      config_c => apb_config_c,
      reg_count_l2_c => reg_count_l2_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,

      apb_i => apb_i,
      apb_o => apb_o,

      reg_no_o => reg_no_s,
      w_value_o => w_value_s,
      w_strobe_o => w_strobe_s,
      r_value_i => r_value_s,
      r_strobe_o => open
      );

  write_regs: process(clock_i, reset_n_i)
  begin
    if rising_edge(clock_i) then
      if w_strobe_s = '1' then
        case reg_no_s is
          when REG_NEW_VALUE_C =>
            new_value_s <= std_ulogic_vector(w_value_s(signal_count_c-1 downto 0));
          when REG_NEW_MASK_C =>
            new_mask_s <= std_ulogic_vector(w_value_s(signal_count_c-1 downto 0));
          when REG_OLD_VALUE_C =>
            old_value_s <= std_ulogic_vector(w_value_s(signal_count_c-1 downto 0));
          when REG_OLD_MASK_C =>
            old_mask_s <= std_ulogic_vector(w_value_s(signal_count_c-1 downto 0));
          when others =>
            null;
        end case;
      end if;
    end if;

    if reset_n_i = '0' then
      new_value_s <= (others => '0');
      new_mask_s <= (others => '0');
      old_value_s <= (others => '0');
      old_mask_s <= (others => '0');
    end if;
  end process;

  read_mux: process(reg_no_s, new_value_s, new_mask_s, old_value_s, old_mask_s)
  begin
    case reg_no_s is
      when REG_NEW_VALUE_C =>
        r_value_s <= resize(unsigned(new_value_s), word_bits_c);
      when REG_NEW_MASK_C =>
        r_value_s <= resize(unsigned(new_mask_s), word_bits_c);
      when REG_OLD_VALUE_C =>
        r_value_s <= resize(unsigned(old_value_s), word_bits_c);
      when REG_OLD_MASK_C =>
        r_value_s <= resize(unsigned(old_mask_s), word_bits_c);
      when others =>
        r_value_s <= (others => '0');
    end case;
  end process;

  -- Effective config in the match domain: single-clock passes straight
  -- through; async crosses new/old value/mask from the APB domain (set-and-hold
  -- config). The match always clocks on the capture_clock_i port (wired to
  -- clock_i for a single-clock block) -- never a clock routed through a signal.
  sync_domain: if not async_c generate
    nv_eff_s <= new_value_s;
    nm_eff_s <= new_mask_s;
    ov_eff_s <= old_value_s;
    om_eff_s <= old_mask_s;
  end generate;
  async_domain: if async_c generate
    cdc_nv: nsl_clocking.interdomain.interdomain_static_reg
      generic map(data_width_c => signal_count_c)
      port map(input_clock_i => clock_i, data_i => new_value_s, data_o => nv_eff_s);
    cdc_nm: nsl_clocking.interdomain.interdomain_static_reg
      generic map(data_width_c => signal_count_c)
      port map(input_clock_i => clock_i, data_i => new_mask_s, data_o => nm_eff_s);
    cdc_ov: nsl_clocking.interdomain.interdomain_static_reg
      generic map(data_width_c => signal_count_c)
      port map(input_clock_i => clock_i, data_i => old_value_s, data_o => ov_eff_s);
    cdc_om: nsl_clocking.interdomain.interdomain_static_reg
      generic map(data_width_c => signal_count_c)
      port map(input_clock_i => clock_i, data_i => old_mask_s, data_o => om_eff_s);
  end generate;

  -- Two input register stages then a registered match (raw_reg is the new
  -- value, delayed the old, each under its own mask). Gated by enable and
  -- emitted as a one-cycle tick: a strobe on the first qualifying edge while
  -- enabled (latency 2), held off by `fired` until enable drops and re-arms.
  match: process(capture_clock_i, capture_reset_n_i)
    variable matched : boolean;
  begin
    if rising_edge(capture_clock_i) then
      raw_reg_s <= to_x01(signals_i);
      delayed_s <= raw_reg_s;
      en1_s <= enable_i;
      en2_s <= en1_s;
      matched := (raw_reg_s and nm_eff_s) = (nv_eff_s and nm_eff_s)
        and (delayed_s and om_eff_s) = (ov_eff_s and om_eff_s);
      trigger_o <= '0';
      if enable_i = '0' then
        fired_s <= '0';
      elsif matched and en1_s = '1' and en2_s = '1' and fired_s = '0' then
        trigger_o <= '1';
        fired_s <= '1';
      end if;
    end if;
    if capture_reset_n_i = '0' then
      raw_reg_s <= (others => '0');
      delayed_s <= (others => '0');
      en1_s <= '0';
      en2_s <= '0';
      trigger_o <= '0';
      fired_s <= '0';
    end if;
  end process;

end architecture;
