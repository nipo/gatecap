library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_math, nsl_synthesis, gatecap;
use nsl_amba.apb.all;
use nsl_math.int_ext.all;
use gatecap.control_status.all;

-- Host-domain half of the control/status instrument: the APB register file.
-- It holds the control words and the counter bases, turns register writes
-- into the mask-plus-strobe pairs the core consumes, and reads back the
-- levels the core exposes. It contains no clock crossing: every port facing
-- the core is crossed by the assembler (see the control_status package for
-- the contract and the register map).
--
-- The host clock may stop between accesses; nothing here needs it to run.
entity control_status_shell is
  generic (
    apb_config_c : config_t;
    -- Width of each control and status register, 1 to 32 bits.
    control_width_c : integer_vector := no_panel_signals_c;
    status_width_c : integer_vector := no_panel_signals_c;
    -- Ticks packed in each tick word, 1 to 32. All ticks of a word strobe
    -- together, which is the simultaneity guarantee.
    tick_out_count_c : integer_vector := no_panel_signals_c;
    tick_in_count_c : integer_vector := no_panel_signals_c;
    -- Shared by every tick-input counter. Counters wrap at this width.
    tick_counter_width_c : natural range 1 to 32 := 32;
    -- Descriptor fingerprint, exposed read-only so the host can poll for the
    -- gateware instance changing under it.
    fingerprint_c : unsigned(31 downto 0) := (others => '0')
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    apb_i : in master_t;
    apb_o : out slave_t;

    -- To user logic, through one whole-word interdomain_reg each.
    control_o : out panel_word_vector(0 to control_width_c'length-1);

    -- From the core's sampled status levels.
    status_i : in panel_word_vector(0 to status_width_c'length-1);

    -- To the core: the written tick mask, and the write event.
    tick_out_mask_o : out panel_word_vector(0 to tick_out_count_c'length-1);
    tick_out_strobe_o : out std_ulogic;

    -- To the core: the sticky bits to clear, and the clear event.
    sticky_clear_o : out panel_word_vector(0 to tick_in_count_c'length-1);
    sticky_clear_strobe_o : out std_ulogic;
    -- From the core.
    sticky_i : in panel_word_vector(0 to tick_in_count_c'length-1);

    -- From the core, one free-running counter per tick input, meaningful on
    -- bits tick_counter_width_c-1 downto 0.
    counter_i : in panel_word_vector(0 to panel_bit_total(tick_in_count_c)-1)
    );
end entity;

architecture rtl of control_status_shell is

  constant reg_count_l2_c : natural := 8;
  constant region_words_c : natural := 16#100# / 4;

  constant control_count_c : natural := control_width_c'length;
  constant status_count_c : natural := status_width_c'length;
  constant tick_out_words_c : natural := tick_out_count_c'length;
  constant tick_in_words_c : natural := tick_in_count_c'length;
  constant counter_count_c : natural := panel_bit_total(tick_in_count_c);

  -- Word indices (byte offset / 4)
  constant REG_TICK_OUT_C : natural := 16#000# / 4;
  constant REG_STICKY_CLEAR_C : natural := REG_TICK_OUT_C + tick_out_words_c;
  constant REG_COUNTER_CLEAR_C : natural := REG_STICKY_CLEAR_C + tick_in_words_c;
  constant REG_STATUS_C : natural := 16#200# / 4;
  constant REG_FINGERPRINT_C : natural := REG_STATUS_C + 1;
  constant REG_STICKY_C : natural := REG_STATUS_C + 2;
  constant REG_STATUS_IN_C : natural := REG_STICKY_C + tick_in_words_c;
  constant REG_COUNTER_C : natural := REG_STATUS_IN_C + status_count_c;
  constant REG_CONTROL_C : natural := 16#300# / 4;

  constant word_bits_c : natural := 8 * 2**apb_config_c.data_bus_width_l2;

  constant action_fits_c : boolean :=
    REG_COUNTER_CLEAR_C + tick_in_words_c <= REG_TICK_OUT_C + region_words_c;
  constant status_fits_c : boolean :=
    REG_COUNTER_C + counter_count_c <= REG_STATUS_C + region_words_c;
  constant control_fits_c : boolean := control_count_c <= region_words_c;
  constant widths_legal_c : boolean :=
    panel_widths_legal(control_width_c)
    and panel_widths_legal(status_width_c)
    and panel_widths_legal(tick_out_count_c)
    and panel_widths_legal(tick_in_count_c);

  signal reg_no_s : integer range 0 to 2**reg_count_l2_c-1;
  signal w_value_s : unsigned(word_bits_c-1 downto 0);
  signal w_strobe_s : std_ulogic;
  signal r_value_s : unsigned(word_bits_c-1 downto 0);

  signal control_s : panel_word_vector(0 to control_count_c-1);
  signal tick_out_mask_s : panel_word_vector(0 to tick_out_words_c-1);
  signal tick_out_strobe_s : std_ulogic;
  signal sticky_clear_s : panel_word_vector(0 to tick_in_words_c-1);
  signal sticky_clear_strobe_s : std_ulogic;

  subtype counter_t is unsigned(tick_counter_width_c-1 downto 0);
  type counter_vector is array (natural range <>) of counter_t;
  signal counter_base_s : counter_vector(0 to counter_count_c-1);

begin

  assert widths_legal_c
    report "control/status boundary widths must all be in 1 to 32"
    severity failure;
  assert action_fits_c
    report "control/status action region overflows: too many tick words"
    severity failure;
  assert status_fits_c
    report "control/status status region overflows: too many sticky, status "
         & "or counter words"
    severity failure;
  assert control_fits_c
    report "control/status array region overflows: too many control words"
    severity failure;
  assert word_bits_c = 32
    report "control/status registers are 32-bit words" severity failure;

  -- The plain asserts above cover simulation; synth_assert also fails
  -- elaboration under synthesis (some vendors ignore plain asserts), so a
  -- misconfigured panel cannot silently produce a broken bitstream.
  widths_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "control/status boundary widths must all be in 1 to 32",
      condition_c => widths_legal_c
      )
    port map(
      unused_i => '0'
      );

  fit_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "control/status panel overflows its register regions",
      condition_c => action_fits_c and status_fits_c and control_fits_c
      )
    port map(
      unused_i => '0'
      );

  width_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "control/status registers are 32-bit words",
      condition_c => word_bits_c = 32
      )
    port map(
      unused_i => '0'
      );

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

  -- One write touches one word of one mask; the other words of that mask are
  -- cleared in the same cycle, so the single strobe crossing to the core can
  -- serve every word of the kind without re-firing a stale one.
  write_regs: process(clock_i, reset_n_i)
  begin
    if rising_edge(clock_i) then
      tick_out_strobe_s <= '0';
      sticky_clear_strobe_s <= '0';

      if w_strobe_s = '1' then
        for g in 0 to tick_out_words_c-1 loop
          if reg_no_s = REG_TICK_OUT_C + g then
            tick_out_mask_s <= (others => (others => '0'));
            tick_out_mask_s(g) <= panel_masked(w_value_s,
                                               panel_width(tick_out_count_c, g));
            tick_out_strobe_s <= '1';
          end if;
        end loop;

        for g in 0 to tick_in_words_c-1 loop
          if reg_no_s = REG_STICKY_CLEAR_C + g then
            sticky_clear_s <= (others => (others => '0'));
            sticky_clear_s(g) <= panel_masked(w_value_s,
                                              panel_width(tick_in_count_c, g));
            sticky_clear_strobe_s <= '1';
          end if;

          -- Rebasing a counter is entirely host-side: the base takes the
          -- currently visible crossed value, so events still in flight are
          -- counted after the clear rather than lost.
          if reg_no_s = REG_COUNTER_CLEAR_C + g then
            for b in 0 to panel_width(tick_in_count_c, g)-1 loop
              if w_value_s(b) = '1' then
                counter_base_s(panel_bit_base(tick_in_count_c, g) + b)
                  <= unsigned(counter_i(panel_bit_base(tick_in_count_c, g) + b)
                              (tick_counter_width_c-1 downto 0));
              end if;
            end loop;
          end if;
        end loop;

        for k in 0 to control_count_c-1 loop
          if reg_no_s = REG_CONTROL_C + k then
            control_s(k) <= panel_masked(w_value_s,
                                         panel_width(control_width_c, k));
          end if;
        end loop;
      end if;
    end if;

    if reset_n_i = '0' then
      tick_out_strobe_s <= '0';
      sticky_clear_strobe_s <= '0';
      tick_out_mask_s <= (others => (others => '0'));
      sticky_clear_s <= (others => (others => '0'));
      control_s <= (others => (others => '0'));
      counter_base_s <= (others => (others => '0'));
    end if;
  end process;

  read_mux: process(reg_no_s, control_s, status_i, sticky_i, counter_i,
                    counter_base_s)
    variable value : unsigned(31 downto 0);
  begin
    value := (others => '0');

    if reg_no_s = REG_FINGERPRINT_C then
      value := fingerprint_c;
    end if;

    for g in 0 to tick_in_words_c-1 loop
      if reg_no_s = REG_STICKY_C + g then
        value := unsigned(sticky_i(g));
      end if;
    end loop;

    for k in 0 to status_count_c-1 loop
      if reg_no_s = REG_STATUS_IN_C + k then
        value := unsigned(status_i(k));
      end if;
    end loop;

    for i in 0 to counter_count_c-1 loop
      if reg_no_s = REG_COUNTER_C + i then
        value := resize(unsigned(counter_i(i)(tick_counter_width_c-1 downto 0))
                        - counter_base_s(i), 32);
      end if;
    end loop;

    for k in 0 to control_count_c-1 loop
      if reg_no_s = REG_CONTROL_C + k then
        value := unsigned(control_s(k));
      end if;
    end loop;

    r_value_s <= resize(value, word_bits_c);
  end process;

  control_o <= control_s;
  tick_out_mask_o <= tick_out_mask_s;
  tick_out_strobe_o <= tick_out_strobe_s;
  sticky_clear_o <= sticky_clear_s;
  sticky_clear_strobe_o <= sticky_clear_strobe_s;

end architecture;
