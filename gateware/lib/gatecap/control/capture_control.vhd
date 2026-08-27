library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.apb.all;

-- Capture control block: an APB register file that turns COMMAND writes
-- into arm/abort pulses, holds the capture-length and window-count config,
-- reflects the capture core's state into STATUS, and stores the per-window
-- head addresses the core emits as each window completes. The sample
-- pointers live in the capture core; the control block only latches the
-- finished heads. The trigger is a separate block (see trigger_control),
-- so this block holds no trigger config.
--
-- Registers follow the gatecap map convention: one 0x100-stride group per
-- role -- action (commands), config, status -- so a host burst-reads a whole
-- group in a single transaction:
--   0x000    COMMAND     (W)  1 = ARM, 2 = ABORT              -- action
--   0x100    CAPTURE_LEN     (W)                              -- config
--   0x104    PRE_TRIGGER_LEN (W)
--   0x108    WINDOW_COUNT(W)  windows to capture (1..window_count_c)
--   0x200    STATUS      (R)  [1:0] state, [2] triggered,     -- status
--                             [31:16] windows completed
--   0x204    FINGERPRINT (R)  per-instance descriptor UID (see fingerprint_c)
--   0x300+4k HEAD[k]     (R)  window k start address, valid once completed
entity capture_control is
  generic (
    apb_config_c : config_t;
    capture_len_width_c : natural;
    depth_l2_c : natural;
    window_count_c : natural := 1;
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
    capture_len_o : out unsigned(capture_len_width_c-1 downto 0);
    pre_trigger_len_o : out unsigned(capture_len_width_c-1 downto 0);
    window_count_o : out unsigned(capture_len_width_c-1 downto 0);

    -- Trigger enable (to the trigger block): the core's ready, surfaced at the
    -- control boundary so a shared trigger can AND the ready of every capture
    -- it feeds.
    enable_o : out std_ulogic;

    -- From capture core
    state_i : in std_ulogic_vector(1 downto 0);
    triggered_i : in std_ulogic;
    ready_i : in std_ulogic;
    head_i : in unsigned(depth_l2_c-1 downto 0);
    head_we_i : in std_ulogic
    );
end entity;

architecture rtl of capture_control is

  constant reg_count_l2_c : natural := 8;

  -- Word indices (byte offset / 4)
  constant REG_COMMAND_C : natural := 16#000# / 4;         -- action
  constant REG_CAPTURE_LEN_C : natural := 16#100# / 4;     -- config
  constant REG_PRE_TRIGGER_LEN_C : natural := 16#104# / 4;
  constant REG_WINDOW_COUNT_C : natural := 16#108# / 4;
  constant REG_STATUS_C : natural := 16#200# / 4;          -- status
  constant REG_FINGERPRINT_C : natural := 16#204# / 4;
  constant REG_HEAD_BASE_C : natural := 16#300# / 4;       -- array

  constant CMD_ARM_C : natural := 1;
  constant CMD_ABORT_C : natural := 2;

  constant word_bits_c : natural := 8 * 2**apb_config_c.data_bus_width_l2;

  signal reg_no_s : integer range 0 to 2**reg_count_l2_c-1;
  signal w_value_s : unsigned(word_bits_c-1 downto 0);
  signal w_strobe_s : std_ulogic;
  signal r_value_s : unsigned(word_bits_c-1 downto 0);

  signal arm_s, abort_s : std_ulogic;
  signal capture_len_s : unsigned(capture_len_width_c-1 downto 0);
  signal pre_trigger_len_s : unsigned(capture_len_width_c-1 downto 0);
  signal window_count_s : unsigned(capture_len_width_c-1 downto 0);

  type head_array_t is array (0 to window_count_c-1) of unsigned(depth_l2_c-1 downto 0);
  signal heads_s : head_array_t;
  signal head_wr_s : natural range 0 to window_count_c;

begin

  assert REG_HEAD_BASE_C + window_count_c <= 2**reg_count_l2_c
    report "too many windows for the control register space" severity failure;

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
          when REG_WINDOW_COUNT_C =>
            window_count_s <= w_value_s(capture_len_width_c-1 downto 0);
          when REG_CAPTURE_LEN_C =>
            capture_len_s <= w_value_s(capture_len_width_c-1 downto 0);
          when REG_PRE_TRIGGER_LEN_C =>
            pre_trigger_len_s <= w_value_s(capture_len_width_c-1 downto 0);
          when others =>
            null;
        end case;
      end if;
    end if;

    if reset_n_i = '0' then
      arm_s <= '0';
      abort_s <= '0';
      capture_len_s <= (others => '0');
      pre_trigger_len_s <= (others => '0');
      window_count_s <= to_unsigned(1, capture_len_width_c);
    end if;
  end process;

  -- Latch each completed window's head; arm restarts the sequence, so the
  -- write index doubles as the completed-window count.
  head_store: process(clock_i, reset_n_i)
  begin
    if rising_edge(clock_i) then
      if arm_s = '1' then
        head_wr_s <= 0;
      elsif head_we_i = '1' and head_wr_s < window_count_c then
        heads_s(head_wr_s) <= head_i;
        head_wr_s <= head_wr_s + 1;
      end if;
    end if;
    if reset_n_i = '0' then
      head_wr_s <= 0;
    end if;
  end process;

  read_mux: process(reg_no_s, state_i, triggered_i,
                    capture_len_s, pre_trigger_len_s,
                    window_count_s, heads_s, head_wr_s)
  begin
    if reg_no_s >= REG_HEAD_BASE_C and reg_no_s < REG_HEAD_BASE_C + window_count_c then
      r_value_s <= resize(heads_s(reg_no_s - REG_HEAD_BASE_C), word_bits_c);
    else
      case reg_no_s is
        when REG_STATUS_C =>
          r_value_s <= resize(unsigned(triggered_i & state_i), word_bits_c);
          r_value_s(word_bits_c-1 downto 16) <=
            to_unsigned(head_wr_s, word_bits_c-16);
        when REG_WINDOW_COUNT_C =>
          r_value_s <= resize(window_count_s, word_bits_c);
        when REG_FINGERPRINT_C =>
          r_value_s <= resize(fingerprint_c, word_bits_c);
        when REG_CAPTURE_LEN_C =>
          r_value_s <= resize(capture_len_s, word_bits_c);
        when REG_PRE_TRIGGER_LEN_C =>
          r_value_s <= resize(pre_trigger_len_s, word_bits_c);
        when others =>
          r_value_s <= (others => '0');
      end case;
    end if;
  end process;

  arm_o <= arm_s;
  abort_o <= abort_s;
  capture_len_o <= capture_len_s;
  pre_trigger_len_o <= pre_trigger_len_s;
  window_count_o <= window_count_s;
  enable_o <= ready_i;

end architecture;
