library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_synthesis, gatecap;
use nsl_amba.apb.all;
use gatecap.bus_explorer.all;

-- Host-domain half of the bus-explorer instrument: the APB register file,
-- the command engine, the scanner and the timeout. It contains no clock
-- crossing; the command and response streams facing the core are crossed by
-- the assembler (see the bus_explorer package for the contract and the
-- register map).
--
-- The engine runs one target access at a time, whether it serves a manual
-- command or the scanner. An access lives through three phases, each with
-- the full timeout budget:
--
--   ST_DRAIN   an earlier access was abandoned on a timeout and its answer
--              has not come back yet; it is waited for and dropped, so a
--              single tag bit stays enough to tell answers apart
--   ST_ISSUE   the command is offered to the crossing
--   ST_WAIT    the answer is waited for
--
-- On a timeout in ST_WAIT the access is abandoned: the tag flips, so the
-- answer that eventually arrives matches nothing, and the access is
-- remembered as outstanding. It is dropped either right there in ST_IDLE --
-- responses are always accepted -- or in the ST_DRAIN of the next access.
-- Only one abandoned access can ever be outstanding, which is what a
-- one-bit tag can disambiguate.
--
-- A masked write is a read then a write with no return to ST_IDLE in
-- between, and the scanner only ever starts from ST_IDLE: the pair is
-- indivisible with respect to the scanner, and a manual command preempts the
-- scanner between target transactions rather than inside one.
entity bus_explorer_shell is
  generic (
    apb_config_c : config_t;
    -- Target bus dimensions. Write data and masks truncate to the data
    -- width, read data and scan results zero-extend from it.
    target_address_width_c : natural range 1 to 32 := 32;
    target_data_width_c : natural range 1 to 32 := 32;
    -- Scan slots, hence one enable bit, one valid bit, one error bit and one
    -- result word each.
    slot_count_c : natural range 1 to 32 := 8;
    -- Host cycles an access may spend in any of its phases before the engine
    -- abandons it and reports ERROR_TIMEOUT_C.
    timeout_c : positive := 65536;
    -- Descriptor fingerprint, exposed read-only so the host can poll for the
    -- gateware instance changing under it.
    fingerprint_c : unsigned(31 downto 0) := (others => '0')
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    apb_i : in master_t;
    apb_o : out slave_t;

    -- To the core, through an interdomain_fifo_slice when the target has its
    -- own clock.
    command_o : out std_ulogic_vector(
      command_width(target_address_width_c, target_data_width_c)-1 downto 0);
    command_valid_o : out std_ulogic;
    command_ready_i : in std_ulogic;

    -- From the core, likewise.
    response_i : in std_ulogic_vector(
      response_width(target_data_width_c)-1 downto 0);
    response_valid_i : in std_ulogic;
    response_ready_o : out std_ulogic
    );
end entity;

architecture rtl of bus_explorer_shell is

  constant reg_count_l2_c : natural := BUS_EXPLORER_REG_COUNT_L2_C;
  constant region_words_c : natural := 16#100# / 4;

  -- Word indices (byte offset / 4)
  constant REG_COMMAND_C : natural := 16#000# / 4;
  constant REG_ADDRESS_C : natural := 16#100# / 4;
  constant REG_WDATA_C : natural := REG_ADDRESS_C + 1;
  constant REG_WMASK_C : natural := REG_ADDRESS_C + 2;
  constant REG_SLOT_ENABLE_C : natural := REG_ADDRESS_C + 3;
  constant REG_SCAN_CTRL_C : natural := REG_ADDRESS_C + 4;
  constant REG_STATUS_C : natural := 16#200# / 4;
  constant REG_FINGERPRINT_C : natural := REG_STATUS_C + 1;
  constant REG_RDATA_C : natural := REG_STATUS_C + 2;
  constant REG_SCAN_VALID_C : natural := REG_STATUS_C + 3;
  constant REG_SCAN_ERROR_C : natural := REG_STATUS_C + 4;
  constant REG_SCAN_RESULT_C : natural := REG_STATUS_C + 5;
  constant REG_SLOT_ADDR_C : natural := 16#300# / 4;

  constant word_bits_c : natural := 8 * 2**apb_config_c.data_bus_width_l2;

  constant widths_legal_c : boolean :=
    target_address_width_c >= 1 and target_address_width_c <= 32
    and target_data_width_c >= 1 and target_data_width_c <= 32
    and slot_count_c >= 1 and slot_count_c <= 32;
  constant status_fits_c : boolean :=
    REG_SCAN_RESULT_C + slot_count_c <= REG_STATUS_C + region_words_c;
  constant arrays_fits_c : boolean := slot_count_c <= region_words_c;

  subtype target_addr_t is unsigned(target_address_width_c-1 downto 0);
  type target_addr_vector is array (natural range <>) of target_addr_t;
  subtype target_data_t is unsigned(target_data_width_c-1 downto 0);
  type target_data_vector is array (natural range <>) of target_data_t;
  subtype slot_mask_t is std_ulogic_vector(slot_count_c-1 downto 0);

  type state_t is (
    ST_IDLE,
    ST_DRAIN,
    ST_ISSUE,
    ST_WAIT
    );

  signal reg_no_s : integer range 0 to 2**reg_count_l2_c-1;
  signal w_value_s : unsigned(word_bits_c-1 downto 0);
  signal w_strobe_s : std_ulogic;
  signal r_value_s : unsigned(word_bits_c-1 downto 0);

  -- Config region.
  signal address_s : target_addr_t;
  signal wdata_s : target_data_t;
  signal wmask_s : target_data_t;
  signal slot_enable_s : slot_mask_t;
  signal scan_enable_s : std_ulogic;
  signal slot_addr_s : target_addr_vector(0 to slot_count_c-1);

  -- Status region.
  signal busy_s, done_s : std_ulogic;
  signal error_s : unsigned(1 downto 0);
  signal rdata_s : target_data_t;
  signal scan_valid_s, scan_error_s : slot_mask_t;
  signal scan_result_s : target_data_vector(0 to slot_count_c-1);

  -- Engine.
  signal state_s : state_t;
  signal timer_s : natural range 0 to timeout_c;
  signal tag_s : std_ulogic;
  signal orphan_s : std_ulogic;
  signal pending_s : std_ulogic;
  signal pending_op_s : unsigned(1 downto 0);
  signal rmw_s : boolean;
  signal scan_op_s : boolean;
  signal write_issue_s : std_ulogic;
  signal address_issue_s : target_addr_t;
  signal wdata_issue_s : target_data_t;
  signal command_valid_s : std_ulogic;
  signal slot_ptr_s : natural range 0 to slot_count_c-1;
  signal scan_slot_s : natural range 0 to slot_count_c-1;
  -- The scan configuration moved under the read in flight, whose answer
  -- therefore describes nothing the host still asked for.
  signal scan_stale_s : std_ulogic;

begin

  assert widths_legal_c
    report "bus explorer target widths and slot count must be in 1 to 32"
    severity failure;
  assert status_fits_c
    report "bus explorer status region overflows: too many scan slots"
    severity failure;
  assert arrays_fits_c
    report "bus explorer array region overflows: too many scan slots"
    severity failure;
  assert word_bits_c = 32
    report "bus explorer registers are 32-bit words" severity failure;

  -- The plain asserts above cover simulation; synth_assert also fails
  -- elaboration under synthesis (some vendors ignore plain asserts), so a
  -- misconfigured instrument cannot silently produce a broken bitstream.
  widths_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "bus explorer target widths and slot count must be in 1 to 32",
      condition_c => widths_legal_c
      )
    port map(
      unused_i => '0'
      );

  fit_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "bus explorer scan slots overflow the register regions",
      condition_c => status_fits_c and arrays_fits_c
      )
    port map(
      unused_i => '0'
      );

  width_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => "bus explorer registers are 32-bit words",
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

  -- Engine, scanner and register writes share one process: a register write
  -- and the engine touch the same state (the scan flags, the busy and done
  -- bits), and the write is applied last, so the host's view of a register
  -- it just wrote is never overwritten by the same cycle's engine step.
  main: process(clock_i, reset_n_i)
    -- Whether an abandoned access is still outstanding, as of this cycle:
    -- ST_IDLE may have just drained one.
    variable orphan_v : std_ulogic;
    variable rsp_error_v, rsp_tag_v : std_ulogic;
    variable rsp_data_v : target_data_t;
    variable next_slot_v, idx_v : natural;
    variable found_v : boolean;

    -- Complete the access in flight. A scan read only marks its slot; the
    -- busy, done and error bits describe manual commands alone.
    procedure finish(constant code : unsigned(1 downto 0)) is
    begin
      state_s <= ST_IDLE;
      if scan_op_s then
        if code /= ERROR_OK_C and scan_stale_s = '0' then
          scan_error_s(scan_slot_s) <= '1';
        end if;
      else
        busy_s <= '0';
        done_s <= '1';
        error_s <= code;
      end if;
    end procedure;

    -- Start the access whose parameters have just been latched.
    procedure start_access is
    begin
      timer_s <= timeout_c;
      if orphan_v = '1' then
        state_s <= ST_DRAIN;
      else
        state_s <= ST_ISSUE;
        command_valid_s <= '1';
      end if;
    end procedure;
  begin
    if rising_edge(clock_i) then
      orphan_v := orphan_s;
      rsp_error_v := response_error(response_i);
      rsp_tag_v := response_tag(response_i);
      rsp_data_v := response_rdata(response_i, target_data_width_c);

      if timer_s /= 0 then
        timer_s <= timer_s - 1;
      end if;

      case state_s is
        when ST_IDLE =>
          -- Responses are accepted in every state, so the answer to an
          -- abandoned access is dropped as soon as it turns up.
          if response_valid_i = '1' then
            orphan_v := '0';
          end if;

          if pending_s = '1' then
            pending_s <= '0';
            scan_op_s <= false;
            address_issue_s <= address_s;
            wdata_issue_s <= wdata_s;
            if pending_op_s = OP_READ_C then
              write_issue_s <= '0';
              rmw_s <= false;
              start_access;
            elsif pending_op_s = OP_WRITE_C then
              write_issue_s <= '1';
              rmw_s <= false;
              start_access;
            elsif pending_op_s = OP_MASKED_WRITE_C then
              -- Read phase of the read-modify-write.
              write_issue_s <= '0';
              rmw_s <= true;
              start_access;
            else
              busy_s <= '0';
              done_s <= '1';
              error_s <= ERROR_COMMAND_C;
            end if;

          elsif scan_enable_s = '1'
            and slot_enable_s /= slot_mask_t'(others => '0') then
            -- Round-robin: the first enabled slot after the last one served.
            found_v := false;
            next_slot_v := 0;
            for i in 1 to slot_count_c loop
              idx_v := slot_ptr_s + i;
              if idx_v >= slot_count_c then
                idx_v := idx_v - slot_count_c;
              end if;
              if not found_v and slot_enable_s(idx_v) = '1' then
                next_slot_v := idx_v;
                found_v := true;
              end if;
            end loop;

            slot_ptr_s <= next_slot_v;
            scan_slot_s <= next_slot_v;
            scan_stale_s <= '0';
            scan_op_s <= true;
            rmw_s <= false;
            write_issue_s <= '0';
            address_issue_s <= slot_addr_s(next_slot_v);
            start_access;
          end if;

        when ST_DRAIN =>
          if response_valid_i = '1' then
            orphan_v := '0';
            command_valid_s <= '1';
            state_s <= ST_ISSUE;
            timer_s <= timeout_c;
          elsif timer_s = 0 then
            -- The abandoned access is still outstanding; this one never
            -- reached the crossing, so nothing more is owed.
            finish(ERROR_TIMEOUT_C);
          end if;

        when ST_ISSUE =>
          if command_ready_i = '1' then
            command_valid_s <= '0';
            state_s <= ST_WAIT;
            timer_s <= timeout_c;
          elsif timer_s = 0 then
            command_valid_s <= '0';
            finish(ERROR_TIMEOUT_C);
          end if;

        when ST_WAIT =>
          if response_valid_i = '1' and rsp_tag_v = tag_s then
            if scan_op_s then
              if scan_stale_s = '0' then
                if rsp_error_v = '1' then
                  -- The slot keeps its last good value.
                  scan_error_s(scan_slot_s) <= '1';
                else
                  scan_result_s(scan_slot_s) <= rsp_data_v;
                  scan_valid_s(scan_slot_s) <= '1';
                  scan_error_s(scan_slot_s) <= '0';
                end if;
              end if;
              state_s <= ST_IDLE;
            elsif rmw_s then
              -- The value read is what the host asked to modify; it is also
              -- worth reporting, so it lands in RDATA either way.
              rdata_s <= rsp_data_v;
              if rsp_error_v = '1' then
                finish(ERROR_SLVERR_C);
              else
                rmw_s <= false;
                write_issue_s <= '1';
                wdata_issue_s <= (rsp_data_v and not wmask_s)
                                 or (wdata_s and wmask_s);
                command_valid_s <= '1';
                state_s <= ST_ISSUE;
                timer_s <= timeout_c;
              end if;
            else
              if write_issue_s = '0' then
                rdata_s <= rsp_data_v;
              end if;
              if rsp_error_v = '1' then
                finish(ERROR_SLVERR_C);
              else
                finish(ERROR_OK_C);
              end if;
            end if;
          elsif timer_s = 0 then
            -- Abandoned: the answer, whenever it comes, will carry the tag
            -- the next access no longer uses.
            orphan_v := '1';
            tag_s <= not tag_s;
            finish(ERROR_TIMEOUT_C);
          end if;
      end case;

      orphan_s <= orphan_v;

      if w_strobe_s = '1' then
        if reg_no_s = REG_COMMAND_C then
          -- Taken as busy at once: a host that fires and polls must never
          -- see the previous command's completion as this one's.
          pending_s <= '1';
          pending_op_s <= w_value_s(1 downto 0);
          busy_s <= '1';
          done_s <= '0';
          error_s <= ERROR_OK_C;
        end if;

        if reg_no_s = REG_ADDRESS_C then
          address_s <= resize(w_value_s, target_address_width_c);
        end if;

        if reg_no_s = REG_WDATA_C then
          wdata_s <= resize(w_value_s, target_data_width_c);
        end if;

        if reg_no_s = REG_WMASK_C then
          wmask_s <= resize(w_value_s, target_data_width_c);
        end if;

        if reg_no_s = REG_SLOT_ENABLE_C then
          slot_enable_s <= std_ulogic_vector(
            resize(w_value_s, slot_count_c));
          for i in 0 to slot_count_c-1 loop
            if w_value_s(i) = '0' then
              scan_valid_s(i) <= '0';
              scan_error_s(i) <= '0';
            end if;
          end loop;
          scan_stale_s <= '1';
        end if;

        if reg_no_s = REG_SCAN_CTRL_C then
          scan_enable_s <= w_value_s(0);
        end if;

        for i in 0 to slot_count_c-1 loop
          if reg_no_s = REG_SLOT_ADDR_C + i then
            slot_addr_s(i) <= resize(w_value_s, target_address_width_c);
            -- The stored value describes the old address, not this one.
            scan_valid_s(i) <= '0';
            scan_error_s(i) <= '0';
            scan_stale_s <= '1';
          end if;
        end loop;
      end if;
    end if;

    if reset_n_i = '0' then
      state_s <= ST_IDLE;
      timer_s <= 0;
      tag_s <= '0';
      orphan_s <= '0';
      pending_s <= '0';
      pending_op_s <= OP_READ_C;
      rmw_s <= false;
      scan_op_s <= false;
      write_issue_s <= '0';
      address_issue_s <= (others => '0');
      wdata_issue_s <= (others => '0');
      command_valid_s <= '0';
      slot_ptr_s <= 0;
      scan_slot_s <= 0;
      scan_stale_s <= '0';

      address_s <= (others => '0');
      wdata_s <= (others => '0');
      wmask_s <= (others => '0');
      slot_enable_s <= (others => '0');
      scan_enable_s <= '0';
      slot_addr_s <= (others => (others => '0'));

      busy_s <= '0';
      done_s <= '0';
      error_s <= ERROR_OK_C;
      rdata_s <= (others => '0');
      scan_valid_s <= (others => '0');
      scan_error_s <= (others => '0');
      scan_result_s <= (others => (others => '0'));
    end if;
  end process;

  read_mux: process(reg_no_s, address_s, wdata_s, wmask_s, slot_enable_s,
                    scan_enable_s, slot_addr_s, busy_s, done_s, error_s,
                    rdata_s, scan_valid_s, scan_error_s, scan_result_s,
                    scan_op_s, state_s)
    variable value : unsigned(31 downto 0);
  begin
    value := (others => '0');

    if reg_no_s = REG_ADDRESS_C then
      value := resize(address_s, 32);
    end if;
    if reg_no_s = REG_WDATA_C then
      value := resize(wdata_s, 32);
    end if;
    if reg_no_s = REG_WMASK_C then
      value := resize(wmask_s, 32);
    end if;
    if reg_no_s = REG_SLOT_ENABLE_C then
      value := resize(unsigned(slot_enable_s), 32);
    end if;
    if reg_no_s = REG_SCAN_CTRL_C then
      value := (0 => scan_enable_s, others => '0');
    end if;

    if reg_no_s = REG_STATUS_C then
      value(STATUS_BUSY_C) := busy_s;
      value(STATUS_DONE_C) := done_s;
      value(STATUS_ERROR_LSB_C+1 downto STATUS_ERROR_LSB_C) := error_s;
      if scan_op_s and state_s /= ST_IDLE then
        value(STATUS_SCAN_ACTIVE_C) := '1';
      end if;
    end if;
    if reg_no_s = REG_FINGERPRINT_C then
      value := fingerprint_c;
    end if;
    if reg_no_s = REG_RDATA_C then
      value := resize(rdata_s, 32);
    end if;
    if reg_no_s = REG_SCAN_VALID_C then
      value := resize(unsigned(scan_valid_s), 32);
    end if;
    if reg_no_s = REG_SCAN_ERROR_C then
      value := resize(unsigned(scan_error_s), 32);
    end if;

    for i in 0 to slot_count_c-1 loop
      if reg_no_s = REG_SCAN_RESULT_C + i then
        value := resize(scan_result_s(i), 32);
      end if;
      if reg_no_s = REG_SLOT_ADDR_C + i then
        value := resize(slot_addr_s(i), 32);
      end if;
    end loop;

    r_value_s <= resize(value, word_bits_c);
  end process;

  command_o <= command_pack(target_address_width_c, target_data_width_c,
                            write => write_issue_s,
                            tag => tag_s,
                            address => address_issue_s,
                            wdata => wdata_issue_s);
  command_valid_o <= command_valid_s;
  -- An answer is always taken off the crossing: the ones no longer awaited
  -- must not sit there blocking the next access.
  response_ready_o <= '1';

end architecture;
