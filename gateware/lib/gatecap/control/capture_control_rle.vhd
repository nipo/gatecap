library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.apb.all;

-- Control block for the run-length-encoding capture core. A distinct type
-- (own UUID and host driver) because its readback protocol differs: there
-- is no window count, no head array and no random-access window; the whole
-- captured region is read from 0 and decoded. The arm/abort front end
-- mirrors the raw control block (deliberately duplicated -- the shared
-- surface is small and VHDL sharing is cumbersome). The trigger is a
-- separate block (see trigger_control), so this block holds no trigger
-- config.
--
-- Registers follow the gatecap map convention: a 0x100-stride group per role
-- (action / config / status), so a host burst-reads the live status group --
-- STATUS, FINGERPRINT, CYCLES, END_PTR -- in one transaction.
--   0x000 COMMAND     (W)  1 = ARM, 2 = ABORT                     -- action
--   0x100 PRE_LINES   (W)  pre-region ring size in lines (0 = post only) -- config
--   0x104 MAX_CYCLES  (W)  post-trigger real-cycle cap (0 = to buffer full)
--   0x200 STATUS      (R)  [1:0] state, [2] triggered             -- status
--   0x204 FINGERPRINT (R)  per-instance descriptor UID (see fingerprint_c)
--   0x208 CYCLES      (R)  post-trigger real cycles so far (live progress)
--   0x20c END_PTR     (R)  post-region end line (buffer fill), valid once idle
--   0x210 PRE_HEAD    (R)  pre-region ring oldest line
--   0x214 PRE_N       (R)  pre-region valid line count
entity capture_control_rle is
  generic (
    apb_config_c : config_t;
    depth_l2_c : natural;
    -- Descriptor fingerprint, exposed read-only so the host can poll for the
    -- gateware instance changing under it.
    fingerprint_c : unsigned(31 downto 0) := (others => '0')
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    apb_i : in master_t;
    apb_o : out slave_t;

    -- To capture core
    arm_o : out std_ulogic;
    abort_o : out std_ulogic;
    pre_lines_o : out unsigned(depth_l2_c downto 0);
    max_cycles_o : out unsigned(31 downto 0);

    -- From capture core
    -- Trigger enable (to the trigger block): the core's ready, surfaced at the
    -- control boundary so a shared trigger can AND the ready of every capture.
    enable_o : out std_ulogic;

    state_i : in std_ulogic_vector(1 downto 0);
    triggered_i : in std_ulogic;
    ready_i : in std_ulogic;
    end_ptr_i : in unsigned(depth_l2_c downto 0);
    pre_head_i : in unsigned(depth_l2_c-1 downto 0);
    pre_n_i : in unsigned(depth_l2_c downto 0)
    );
end entity;

architecture rtl of capture_control_rle is

  constant reg_count_l2_c : natural := 8;

  constant REG_COMMAND_C : natural := 16#000# / 4;       -- action
  constant REG_PRE_LINES_C : natural := 16#100# / 4;     -- config
  constant REG_MAX_CYCLES_C : natural := 16#104# / 4;
  constant REG_STATUS_C : natural := 16#200# / 4;        -- status
  constant REG_FINGERPRINT_C : natural := 16#204# / 4;
  constant REG_CYCLES_C : natural := 16#208# / 4;
  constant REG_END_PTR_C : natural := 16#20c# / 4;
  constant REG_PRE_HEAD_C : natural := 16#210# / 4;
  constant REG_PRE_N_C : natural := 16#214# / 4;

  constant CMD_ARM_C : natural := 1;
  constant CMD_ABORT_C : natural := 2;
  constant STATE_CAPTURING_C : std_ulogic_vector(1 downto 0) := "10";

  constant word_bits_c : natural := 8 * 2**apb_config_c.data_bus_width_l2;

  signal reg_no_s : integer range 0 to 2**reg_count_l2_c-1;
  signal w_value_s : unsigned(word_bits_c-1 downto 0);
  signal w_strobe_s : std_ulogic;
  signal r_value_s : unsigned(word_bits_c-1 downto 0);

  signal arm_s, abort_s : std_ulogic;
  signal pre_lines_s : unsigned(depth_l2_c downto 0);
  signal max_cycles_s : unsigned(31 downto 0);
  signal cycles_s : unsigned(31 downto 0);  -- post-trigger cycles, for readback

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
      arm_s <= '0';
      abort_s <= '0';
      if w_strobe_s = '1' then
        case reg_no_s is
          when REG_COMMAND_C =>
            if w_value_s = CMD_ARM_C then
              arm_s <= '1';
            elsif w_value_s = CMD_ABORT_C then
              abort_s <= '1';
            end if;
          when REG_PRE_LINES_C =>
            pre_lines_s <= w_value_s(depth_l2_c downto 0);
          when REG_MAX_CYCLES_C =>
            max_cycles_s <= w_value_s(31 downto 0);
          when others =>
            null;
        end case;
      end if;
    end if;

    if reset_n_i = '0' then
      arm_s <= '0';
      abort_s <= '0';
      pre_lines_s <= (others => '0');
      max_cycles_s <= (others => '0');
    end if;
  end process;

  -- Count real cycles while the core is capturing (post-trigger), reset on
  -- arm, for the CYCLES readback -- live capture progress toward the cap.
  cycle_count: process(clock_i, reset_n_i)
  begin
    if rising_edge(clock_i) then
      if arm_s = '1' then
        cycles_s <= (others => '0');
      elsif state_i = STATE_CAPTURING_C then
        cycles_s <= cycles_s + 1;
      end if;
    end if;
    if reset_n_i = '0' then
      cycles_s <= (others => '0');
    end if;
  end process;

  read_mux: process(reg_no_s, state_i, triggered_i, pre_lines_s, max_cycles_s,
                    cycles_s, end_ptr_i, pre_head_i, pre_n_i)
  begin
    case reg_no_s is
      when REG_STATUS_C =>
        r_value_s <= resize(unsigned(triggered_i & state_i), word_bits_c);
      when REG_PRE_LINES_C =>
        r_value_s <= resize(pre_lines_s, word_bits_c);
      when REG_FINGERPRINT_C =>
        r_value_s <= resize(fingerprint_c, word_bits_c);
      when REG_MAX_CYCLES_C =>
        r_value_s <= resize(max_cycles_s, word_bits_c);
      when REG_CYCLES_C =>
        r_value_s <= resize(cycles_s, word_bits_c);
      when REG_END_PTR_C =>
        r_value_s <= resize(end_ptr_i, word_bits_c);
      when REG_PRE_HEAD_C =>
        r_value_s <= resize(pre_head_i, word_bits_c);
      when REG_PRE_N_C =>
        r_value_s <= resize(pre_n_i, word_bits_c);
      when others =>
        r_value_s <= (others => '0');
    end case;
  end process;

  arm_o <= arm_s;
  abort_o <= abort_s;
  pre_lines_o <= pre_lines_s;
  max_cycles_o <= max_cycles_s;
  enable_o <= ready_i;

end architecture;
