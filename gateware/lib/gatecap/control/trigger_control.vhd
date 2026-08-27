library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_clocking;
use nsl_amba.apb.all;

-- Trigger block: a standalone APB slave that compares its own signal
-- vector against a value/mask and drives a single trigger line to a
-- capture core. Splitting the trigger out of the capture control decouples
-- trigger complexity from data-encoding complexity and, since it has its
-- own input vector, lets a capture trigger on signals it does not store
-- (or on an external trigger wired into signals_i).
--
-- The match is registered: the trigger fires the cycle after the condition
-- holds, keeping the compare off the capture core's timing path. That is a
-- one-cycle latency; the raw capture core back-dates the trigger sample by
-- the block's declared latency (gatecap.control.trigger_control_latency_c),
-- so the trigger still points at the exact cycle the condition held with no
-- data-path delay. A future trigger type with a deeper pipeline just raises
-- its latency constant.
--
-- The output is a one-cycle tick (not a level), gated by enable_i: a strobe
-- fires only on the first match while enabled, then holds off until the
-- consumer re-arms by dropping enable. enable_i is driven by the capture
-- core's "ready" (armed with pre-trigger filled), so a trigger cannot fire
-- before nominal pre-trigger context exists, and the tick suits an
-- interdomain_tick fan-out to several capture domains.
--
-- Config lives in the 0x100 group, per the gatecap map convention (this block
-- has no action or status group).
--   0x100 VALUE (W/R)  compared under MASK
--   0x104 MASK  (W/R)  1 = bit participates, 0 = don't care
entity trigger_control is
  generic (
    apb_config_c : config_t;
    signal_count_c : natural;
    -- When true the match runs on capture_clock_i (asynchronous to the APB
    -- clock_i) with value/mask crossed in; when false (default) the whole block
    -- is on clock_i, byte-for-byte unchanged.
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

    -- Trigger event to the capture core: a one-cycle tick on first match.
    trigger_o : out std_ulogic
    );
end entity;

architecture rtl of trigger_control is

  constant reg_count_l2_c : natural := 8;   -- config group at 0x100

  constant REG_VALUE_C : natural := 16#100# / 4;
  constant REG_MASK_C : natural := 16#104# / 4;

  constant word_bits_c : natural := 8 * 2**apb_config_c.data_bus_width_l2;

  signal reg_no_s : integer range 0 to 2**reg_count_l2_c-1;
  signal w_value_s : unsigned(word_bits_c-1 downto 0);
  signal w_strobe_s : std_ulogic;
  signal r_value_s : unsigned(word_bits_c-1 downto 0);

  signal value_s, mask_s : std_ulogic_vector(signal_count_c-1 downto 0);
  -- Effective value/mask in the match's clock domain (CDC'd when async).
  signal value_eff_s, mask_eff_s : std_ulogic_vector(signal_count_c-1 downto 0);
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
          when REG_VALUE_C =>
            value_s <= std_ulogic_vector(w_value_s(signal_count_c-1 downto 0));
          when REG_MASK_C =>
            mask_s <= std_ulogic_vector(w_value_s(signal_count_c-1 downto 0));
          when others =>
            null;
        end case;
      end if;
    end if;

    if reset_n_i = '0' then
      value_s <= (others => '0');
      mask_s <= (others => '0');
    end if;
  end process;

  read_mux: process(reg_no_s, value_s, mask_s)
  begin
    case reg_no_s is
      when REG_VALUE_C =>
        r_value_s <= resize(unsigned(value_s), word_bits_c);
      when REG_MASK_C =>
        r_value_s <= resize(unsigned(mask_s), word_bits_c);
      when others =>
        r_value_s <= (others => '0');
    end case;
  end process;

  -- Effective config in the match domain. Single-clock passes straight
  -- through; async crosses value/mask from the APB domain (set-and-hold
  -- config, so a static_reg suffices). The match always clocks on the
  -- capture_clock_i port (wired to clock_i for a single-clock block) -- never
  -- a clock routed through a signal, which would delta-skew the match.
  sync_domain: if not async_c generate
    value_eff_s <= value_s;
    mask_eff_s <= mask_s;
  end generate;
  async_domain: if async_c generate
    cdc_value: nsl_clocking.interdomain.interdomain_static_reg
      generic map(data_width_c => signal_count_c)
      port map(input_clock_i => clock_i, data_i => value_s, data_o => value_eff_s);
    cdc_mask: nsl_clocking.interdomain.interdomain_static_reg
      generic map(data_width_c => signal_count_c)
      port map(input_clock_i => clock_i, data_i => mask_s, data_o => mask_eff_s);
  end generate;

  -- Probes may be opendrain/tristated; normalise to X01 before the compare.
  -- Registered compare gated by enable, emitted as a one-cycle tick: a strobe
  -- on the first match while enabled (latency 1), then held off by `fired`
  -- until enable drops and re-arms it.
  match: process(capture_clock_i, capture_reset_n_i)
    variable matched : boolean;
  begin
    if rising_edge(capture_clock_i) then
      matched := (to_x01(signals_i) and mask_eff_s) = (value_eff_s and mask_eff_s);
      trigger_o <= '0';
      if enable_i = '0' then
        fired_s <= '0';
      elsif matched and fired_s = '0' then
        trigger_o <= '1';
        fired_s <= '1';
      end if;
    end if;
    if capture_reset_n_i = '0' then
      trigger_o <= '0';
      fired_s <= '0';
    end if;
  end process;

end architecture;
