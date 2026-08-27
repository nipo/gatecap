library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_clocking, nsl_data, gatecap;
use nsl_amba.apb.all;
use nsl_data.text.all;
use gatecap.bus_explorer.all;

-- One bus explorer assembled the way the rack assembles it: the shell on the
-- host clock, the core on the target clock, and the command and response
-- streams crossed in between. With async_c false the two clocks are one and
-- both crossings are replaced by plain wiring, which is what an instrument
-- without a declared clock elaborates to.
--
-- Behind the core sits a four-register target, one address that always
-- answers pslverr, and two switches the stimulus owns: poison_s makes one of
-- the four registers answer pslverr as well, dead_s makes the target withhold
-- pready for good, which is how a timeout is provoked.
entity explorer_bench is
  generic (
    async_c : boolean;
    address_width_c : natural;
    data_width_c : natural
    );
  port (
    done_o : out boolean
    );
end entity;

architecture sim of explorer_bench is

  constant apb_cfg_c : config_t := config(address_width => 12,
                                          data_bus_width => 32,
                                          err => true);
  constant target_cfg_c : config_t := target_apb_config(address_width_c,
                                                        data_width_c);
  constant target_bits_c : natural :=
    8 * 2**target_cfg_c.data_bus_width_l2;

  constant slot_count_c : natural := 4;
  constant timeout_c : positive := 64;
  constant fingerprint_c : unsigned(31 downto 0) := x"5eed1e55";

  -- Register offsets of this instrument's layout.
  constant ADDR_COMMAND_C : natural := 16#000#;
  constant ADDR_ADDRESS_C : natural := 16#100#;
  constant ADDR_WDATA_C : natural := 16#104#;
  constant ADDR_WMASK_C : natural := 16#108#;
  constant ADDR_SLOT_ENABLE_C : natural := 16#10c#;
  constant ADDR_SCAN_CTRL_C : natural := 16#110#;
  constant ADDR_STATUS_C : natural := 16#200#;
  constant ADDR_FINGERPRINT_C : natural := 16#204#;
  constant ADDR_RDATA_C : natural := 16#208#;
  constant ADDR_SCAN_VALID_C : natural := 16#20c#;
  constant ADDR_SCAN_ERROR_C : natural := 16#210#;
  constant ADDR_SCAN_RESULT_C : natural := 16#214#;
  constant ADDR_SLOT_ADDR_C : natural := 16#300#;

  -- Target map. The four registers are the stub's own; the fifth address
  -- always errors, and so does anything else.
  constant TGT_A_C : natural := 16#10#;
  constant TGT_B_C : natural := 16#14#;
  constant TGT_C_C : natural := 16#18#;
  constant TGT_D_C : natural := 16#1c#;
  constant TGT_ERR_C : natural := 16#40#;

  signal clock_s : std_ulogic := '0';
  signal target_clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;

  -- Shell side of the inner contract.
  signal sh_command_s : std_ulogic_vector(
    command_width(address_width_c, data_width_c)-1 downto 0);
  signal sh_command_valid_s, sh_command_ready_s : std_ulogic;
  signal sh_response_s : std_ulogic_vector(
    response_width(data_width_c)-1 downto 0);
  signal sh_response_valid_s, sh_response_ready_s : std_ulogic;

  -- Core side of the inner contract.
  signal co_command_s : std_ulogic_vector(
    command_width(address_width_c, data_width_c)-1 downto 0);
  signal co_command_valid_s, co_command_ready_s : std_ulogic;
  signal co_response_s : std_ulogic_vector(
    response_width(data_width_c)-1 downto 0);
  signal co_response_valid_s, co_response_ready_s : std_ulogic;

  -- Target bus and its stub.
  signal target_m : master_t;
  signal target_s : slave_t;

  subtype target_data_t is unsigned(data_width_c-1 downto 0);
  type target_data_vector is array (natural range <>) of target_data_t;
  signal target_reg_s : target_data_vector(0 to 3);

  signal dead_s : std_ulogic := '0';
  signal poison_s : std_ulogic := '0';

  function a(offset : natural) return unsigned is
  begin
    return to_unsigned(offset, apb_cfg_c.address_width);
  end function;

  -- What the instrument gives back for a value carried over a data bus of
  -- data_width_c bits: truncated on the way out, zero-extended on the way
  -- back in.
  function td(value : unsigned) return unsigned is
  begin
    return resize(resize(value, data_width_c), 32);
  end function;

  -- Likewise for an address of address_width_c bits.
  function ta(value : unsigned) return unsigned is
  begin
    return resize(resize(value, address_width_c), 32);
  end function;

begin

  sync_clock: if not async_c generate
    -- Same clock on both sides, driven by one process: no skew, not even a
    -- delta cycle.
    process
    begin
      while not done_s loop
        clock_s <= '0';
        target_clock_s <= '0';
        wait for 5 ns;
        clock_s <= '1';
        target_clock_s <= '1';
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
        target_clock_s <= '0';
        wait for 3500 ps;
        target_clock_s <= '1';
        wait for 3500 ps;
      end loop;
      wait;
    end process;
  end generate;

  shell: gatecap.bus_explorer.bus_explorer_shell
    generic map(
      apb_config_c => apb_cfg_c,
      target_address_width_c => address_width_c,
      target_data_width_c => data_width_c,
      slot_count_c => slot_count_c,
      timeout_c => timeout_c,
      fingerprint_c => fingerprint_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,
      command_o => sh_command_s,
      command_valid_o => sh_command_valid_s,
      command_ready_i => sh_command_ready_s,
      response_i => sh_response_s,
      response_valid_i => sh_response_valid_s,
      response_ready_o => sh_response_ready_s
      );

  core: gatecap.bus_explorer.bus_explorer_core
    generic map(
      target_address_width_c => address_width_c,
      target_data_width_c => data_width_c
      )
    port map(
      clock_i => target_clock_s,
      reset_n_i => reset_n_s,
      command_i => co_command_s,
      command_valid_i => co_command_valid_s,
      command_ready_o => co_command_ready_s,
      response_o => co_response_s,
      response_valid_o => co_response_valid_s,
      response_ready_i => co_response_ready_s,
      apb_o => target_m,
      apb_i => target_s
      );

  -- The assembler's job: the inner contract, crossed.
  crossings: if async_c generate
    command_x: nsl_clocking.interdomain.interdomain_fifo_slice
      generic map(data_width_c => command_width(address_width_c, data_width_c))
      port map(reset_n_i => reset_n_s,
               clock_i(0) => clock_s,
               clock_i(1) => target_clock_s,
               in_data_i => sh_command_s,
               in_valid_i => sh_command_valid_s,
               in_ready_o => sh_command_ready_s,
               out_data_o => co_command_s,
               out_valid_o => co_command_valid_s,
               out_ready_i => co_command_ready_s);

    response_x: nsl_clocking.interdomain.interdomain_fifo_slice
      generic map(data_width_c => response_width(data_width_c))
      port map(reset_n_i => reset_n_s,
               clock_i(0) => target_clock_s,
               clock_i(1) => clock_s,
               in_data_i => co_response_s,
               in_valid_i => co_response_valid_s,
               in_ready_o => co_response_ready_s,
               out_data_o => sh_response_s,
               out_valid_o => sh_response_valid_s,
               out_ready_i => sh_response_ready_s);
  end generate;

  wires: if not async_c generate
    co_command_s <= sh_command_s;
    co_command_valid_s <= sh_command_valid_s;
    sh_command_ready_s <= co_command_ready_s;
    sh_response_s <= co_response_s;
    sh_response_valid_s <= co_response_valid_s;
    co_response_ready_s <= sh_response_ready_s;
  end generate;

  -- Target stub: combinational decode, registered storage.
  target_decode: process(target_m, target_reg_s, dead_s, poison_s)
    variable addr_v : unsigned(address_width_c-1 downto 0);
    variable error_v : boolean;
    variable value_v : target_data_t;
  begin
    target_s <= response_idle(target_cfg_c);

    if is_selected(target_cfg_c, target_m) then
      addr_v := address(target_cfg_c, target_m);
      error_v := true;
      value_v := (others => '0');

      for i in 0 to 3 loop
        if addr_v = to_unsigned(TGT_A_C + 4*i, address_width_c) then
          error_v := false;
          value_v := target_reg_s(i);
        end if;
      end loop;

      -- Register 2 turns hostile on demand, which is how a scan slot loses
      -- its value source without losing the value it already has.
      if poison_s = '1'
        and addr_v = to_unsigned(TGT_C_C, address_width_c) then
        error_v := true;
      end if;

      if is_write(target_cfg_c, target_m) then
        target_s <= write_response(target_cfg_c,
                                   error => error_v,
                                   ready => dead_s = '0');
      else
        target_s <= read_response(target_cfg_c,
                                  value => resize(value_v, target_bits_c),
                                  error => error_v,
                                  ready => dead_s = '0');
      end if;
    end if;
  end process;

  target_store: process(target_clock_s, reset_n_s)
    variable addr_v : unsigned(address_width_c-1 downto 0);
  begin
    if rising_edge(target_clock_s) then
      if dead_s = '0'
        and is_access(target_cfg_c, target_m)
        and is_write(target_cfg_c, target_m) then
        addr_v := address(target_cfg_c, target_m);
        for i in 0 to 3 loop
          if addr_v = to_unsigned(TGT_A_C + 4*i, address_width_c) then
            target_reg_s(i) <= resize(value(target_cfg_c, target_m),
                                      data_width_c);
          end if;
        end loop;
      end if;
    end if;

    if reset_n_s = '0' then
      target_reg_s <= (others => (others => '0'));
    end if;
  end process;

  stim: process
    variable v : unsigned(31 downto 0);
    variable e : boolean;
    variable seen : boolean;
    variable code : unsigned(1 downto 0);

    procedure settle(constant cycles : natural := 24) is
    begin
      for i in 1 to cycles loop
        wait until falling_edge(clock_s);
      end loop;
    end procedure;

    procedure reg_write(constant offset : natural;
                        constant value : unsigned(31 downto 0)) is
      variable err : boolean;
    begin
      apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(offset), value,
                err => err);
      assert not err
        report "register write at " & integer'image(offset) & " errored"
        severity failure;
    end procedure;

    procedure reg_write(constant offset : natural;
                        constant value : natural) is
    begin
      reg_write(offset, to_unsigned(value, 32));
    end procedure;

    procedure check(constant offset : natural;
                    constant expected : unsigned(31 downto 0);
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

    -- Poll the status register the way the host does, and hand back the
    -- error code of the completed command.
    procedure wait_done(variable code : out unsigned(1 downto 0)) is
      variable value : unsigned(31 downto 0);
      variable err : boolean;
    begin
      for i in 1 to 400 loop
        apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_STATUS_C),
                 value, err);
        assert not err report "STATUS read errored" severity failure;
        if value(STATUS_DONE_C) = '1' and value(STATUS_BUSY_C) = '0' then
          code := value(STATUS_ERROR_LSB_C+1 downto STATUS_ERROR_LSB_C);
          return;
        end if;
      end loop;
      assert false report "command never completed" severity failure;
      code := (others => '0');
    end procedure;

    procedure fire(constant op : unsigned(1 downto 0);
                   variable code : out unsigned(1 downto 0)) is
    begin
      reg_write(ADDR_COMMAND_C, resize(op, 32));
      wait_done(code);
    end procedure;

    procedure target_write(constant target_addr : natural;
                           constant value : unsigned(31 downto 0);
                           variable code : out unsigned(1 downto 0)) is
    begin
      reg_write(ADDR_ADDRESS_C, target_addr);
      reg_write(ADDR_WDATA_C, value);
      fire(OP_WRITE_C, code);
    end procedure;

    procedure target_read(constant target_addr : natural;
                          variable code : out unsigned(1 downto 0)) is
    begin
      reg_write(ADDR_ADDRESS_C, target_addr);
      fire(OP_READ_C, code);
    end procedure;

  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 47 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    settle;

    check(ADDR_FINGERPRINT_C, fingerprint_c, "FINGERPRINT");
    check(ADDR_STATUS_C, 0, "STATUS at reset");
    check(ADDR_COMMAND_C, 0, "COMMAND reads as zero");

    -- Config registers hold what they are given, truncated to the target's
    -- dimensions.
    reg_write(ADDR_ADDRESS_C, unsigned'(x"00000abc"));
    check(ADDR_ADDRESS_C, ta(x"00000abc"), "ADDRESS readback");
    reg_write(ADDR_WDATA_C, unsigned'(x"12345678"));
    check(ADDR_WDATA_C, td(x"12345678"), "WDATA readback");
    reg_write(ADDR_WMASK_C, unsigned'(x"00ff00ff"));
    check(ADDR_WMASK_C, td(x"00ff00ff"), "WMASK readback");
    reg_write(ADDR_SCAN_CTRL_C, 0);
    check(ADDR_SCAN_CTRL_C, 0, "SCAN_CTRL readback");

    -- Manual write then manual read, round trip through the target.
    target_write(TGT_A_C, x"12345678", code);
    assert code = ERROR_OK_C report "write to A errored" severity failure;
    target_read(TGT_A_C, code);
    assert code = ERROR_OK_C report "read of A errored" severity failure;
    check(ADDR_RDATA_C, td(x"12345678"), "RDATA of A");

    target_write(TGT_B_C, x"a5a5a5a5", code);
    assert code = ERROR_OK_C report "write to B errored" severity failure;
    target_read(TGT_B_C, code);
    assert code = ERROR_OK_C report "read of B errored" severity failure;
    check(ADDR_RDATA_C, td(x"a5a5a5a5"), "RDATA of B");

    -- Masked write: a read-modify-write on the target, leaving the bits
    -- outside the mask as they were and reporting the value it read.
    reg_write(ADDR_ADDRESS_C, TGT_A_C);
    reg_write(ADDR_WDATA_C, unsigned'(x"aabbccdd"));
    reg_write(ADDR_WMASK_C, unsigned'(x"00ff00ff"));
    fire(OP_MASKED_WRITE_C, code);
    assert code = ERROR_OK_C report "masked write errored" severity failure;
    check(ADDR_RDATA_C, td(x"12345678"), "RDATA of the masked write's read");
    target_read(TGT_A_C, code);
    assert code = ERROR_OK_C report "read after masked write errored"
      severity failure;
    check(ADDR_RDATA_C, td(x"12bb56dd"), "A after the masked write");

    -- An erroring target address, read and written.
    target_read(TGT_ERR_C, code);
    assert code = ERROR_SLVERR_C
      report "read of the erroring address did not report slverr"
      severity failure;
    target_write(TGT_ERR_C, x"deadbeef", code);
    assert code = ERROR_SLVERR_C
      report "write to the erroring address did not report slverr"
      severity failure;

    -- A reserved operation code touches nothing and says so.
    reg_write(ADDR_ADDRESS_C, TGT_A_C);
    fire("11", code);
    assert code = ERROR_COMMAND_C
      report "reserved operation code did not report a command error"
      severity failure;
    target_read(TGT_A_C, code);
    assert code = ERROR_OK_C report "read after a reserved code errored"
      severity failure;
    check(ADDR_RDATA_C, td(x"12bb56dd"), "A after a reserved code");

    -- A target that never answers: the access times out, and busy is
    -- visible while it is outstanding.
    dead_s <= '1';
    settle(2);
    reg_write(ADDR_ADDRESS_C, TGT_A_C);
    reg_write(ADDR_COMMAND_C, resize(OP_READ_C, 32));
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_STATUS_C), v, e);
    assert not e report "STATUS read errored" severity failure;
    assert v(STATUS_BUSY_C) = '1' and v(STATUS_DONE_C) = '0'
      report "engine is not busy while an access is outstanding"
      severity failure;
    wait_done(code);
    assert code = ERROR_TIMEOUT_C
      report "dead target did not report a timeout" severity failure;

    -- Still dead, and the abandoned access still outstanding: the next one
    -- waits for it, gives up in turn, and reports the same. It reads A too,
    -- so every answer left in flight carries A's value and none of them can
    -- pass for the answer to what comes next.
    target_read(TGT_A_C, code);
    assert code = ERROR_TIMEOUT_C
      report "second access to a dead target did not report a timeout"
      severity failure;

    -- Revived under a command already waiting on the abandoned one: the
    -- answer to the read of A comes back while the engine is draining, and
    -- is dropped there. What the command reports must be B's value, not that
    -- stale answer.
    reg_write(ADDR_ADDRESS_C, TGT_B_C);
    reg_write(ADDR_COMMAND_C, resize(OP_READ_C, 32));
    settle(10);
    dead_s <= '0';
    wait_done(code);
    assert code = ERROR_OK_C
      report "read across the target reviving errored" severity failure;
    check(ADDR_RDATA_C, td(x"a5a5a5a5"), "RDATA after a discarded answer");

    -- And once quiet again, with nothing left outstanding.
    settle;
    target_read(TGT_B_C, code);
    assert code = ERROR_OK_C
      report "read after the target revived errored" severity failure;
    check(ADDR_RDATA_C, td(x"a5a5a5a5"), "RDATA once the target is back");

    settle;
    target_read(TGT_A_C, code);
    assert code = ERROR_OK_C report "read of A after the timeouts errored"
      severity failure;
    check(ADDR_RDATA_C, td(x"12bb56dd"), "A after the timeouts");

    -- Scanner. Slots 0 to 3 point at A, B, the erroring address and C.
    target_write(TGT_C_C, x"0f0f0f0f", code);
    assert code = ERROR_OK_C report "write to C errored" severity failure;
    target_write(TGT_D_C, x"77777777", code);
    assert code = ERROR_OK_C report "write to D errored" severity failure;

    reg_write(ADDR_SLOT_ADDR_C + 0, TGT_A_C);
    reg_write(ADDR_SLOT_ADDR_C + 4, TGT_B_C);
    reg_write(ADDR_SLOT_ADDR_C + 8, TGT_ERR_C);
    reg_write(ADDR_SLOT_ADDR_C + 12, TGT_C_C);
    check(ADDR_SLOT_ADDR_C + 4, ta(to_unsigned(TGT_B_C, 32)),
          "slot 1 address readback");

    -- Only the enabled slots are swept.
    reg_write(ADDR_SLOT_ENABLE_C, 2#0111#);
    reg_write(ADDR_SCAN_CTRL_C, 1);
    settle(400);

    -- The scanner owns the engine nearly all the time it is enabled, so a
    -- handful of status polls must catch it at work.
    seen := false;
    for i in 1 to 50 loop
      apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_STATUS_C), v, e);
      assert not e report "STATUS read errored" severity failure;
      if v(STATUS_SCAN_ACTIVE_C) = '1' then
        seen := true;
      end if;
    end loop;
    assert seen report "scan-active never showed while scanning"
      severity failure;

    check(ADDR_SCAN_VALID_C, 2#0011#, "SCAN_VALID of the enabled slots");
    check(ADDR_SCAN_ERROR_C, 2#0100#, "SCAN_ERROR of the erroring slot");
    check(ADDR_SCAN_RESULT_C + 0, td(x"12bb56dd"), "scan result 0");
    check(ADDR_SCAN_RESULT_C + 4, td(x"a5a5a5a5"), "scan result 1");
    check(ADDR_SCAN_RESULT_C + 12, 0, "scan result of a disabled slot");

    -- A manual command preempts the scanner, and the scanner picks the new
    -- value up.
    target_write(TGT_A_C, x"01020304", code);
    assert code = ERROR_OK_C
      report "manual write during a scan errored" severity failure;
    settle(400);
    check(ADDR_SCAN_RESULT_C + 0, td(x"01020304"),
          "scan result 0 after a manual write");

    -- Enabling a slot brings it into the sweep.
    reg_write(ADDR_SLOT_ENABLE_C, 2#1111#);
    settle(400);
    check(ADDR_SCAN_VALID_C, 2#1011#, "SCAN_VALID with slot 3 enabled");
    check(ADDR_SCAN_RESULT_C + 12, td(x"0f0f0f0f"), "scan result 3");

    -- An erroring slot raises its error bit and keeps its last good value.
    poison_s <= '1';
    settle(400);
    check(ADDR_SCAN_ERROR_C, 2#1100#, "SCAN_ERROR with slot 3 poisoned");
    check(ADDR_SCAN_VALID_C, 2#1011#, "SCAN_VALID with slot 3 poisoned");
    check(ADDR_SCAN_RESULT_C + 12, td(x"0f0f0f0f"),
          "scan result 3 retained through an error");

    poison_s <= '0';
    settle(400);
    check(ADDR_SCAN_ERROR_C, 2#0100#, "SCAN_ERROR once slot 3 recovers");

    -- Disabling a slot drops what was said about it.
    reg_write(ADDR_SLOT_ENABLE_C, 2#0001#);
    settle(400);
    check(ADDR_SCAN_VALID_C, 2#0001#, "SCAN_VALID after disabling slots");
    check(ADDR_SCAN_ERROR_C, 0, "SCAN_ERROR after disabling slots");

    reg_write(ADDR_SCAN_CTRL_C, 0);
    settle;
    check(ADDR_STATUS_C, 2#00010#, "STATUS once the scanner is stopped");

    done_o <= true;
    done_s <= true;
    wait;
  end process;

end architecture;

library ieee;
use ieee.std_logic_1164.all;

entity tb is
end entity;

architecture sim of tb is

  signal async_done_s, sync_done_s, narrow_done_s : boolean := false;

begin

  -- The instrument with its own target clock: both crossings in place.
  async_explorer: entity work.explorer_bench
    generic map(async_c => true,
                address_width_c => 12,
                data_width_c => 32)
    port map(done_o => async_done_s);

  -- The same instrument without a declared clock: shell and core share the
  -- host clock and the crossings are plain wires.
  sync_explorer: entity work.explorer_bench
    generic map(async_c => false,
                address_width_c => 12,
                data_width_c => 32)
    port map(done_o => sync_done_s);

  -- A narrow target: 16-bit data on an 8-bit address, so every value the
  -- host stages is truncated and everything it reads back zero-extended.
  narrow_explorer: entity work.explorer_bench
    generic map(async_c => true,
                address_width_c => 8,
                data_width_c => 16)
    port map(done_o => narrow_done_s);

  report_result: process
  begin
    wait until async_done_s and sync_done_s and narrow_done_s;
    report "bus_explorer testbench PASSED" severity note;
    wait;
  end process;

end architecture;
