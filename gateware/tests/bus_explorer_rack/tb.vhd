library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, gatecap;
use nsl_amba.apb.all;
use nsl_data.text.all;
use gatecap.bus_explorer.all;
use work.explorer_pkg.all;

-- A generated rack of one bus explorer, driven over its plain APB completer.
-- The explorer masters a 10-bit-address, 16-bit-data target bus on a clock of
-- its own, so every command and every answer goes through the crossings the
-- generator instantiated.
--
-- Behind the target port sits a four-register stub, plus one address that
-- always answers pslverr. The map is the one the descriptor advertises: the
-- ROM at 0 in a 4 KB segment, then the explorer's 1 KB segment.
entity tb is
end entity;

architecture sim of tb is

  constant apb_cfg_c : config_t := explorer_rack_apb_config;

  constant address_width_c : natural := 10;
  constant data_width_c : natural := 16;
  constant target_cfg_c : config_t := target_apb_config(address_width_c,
                                                        data_width_c);
  constant target_bits_c : natural := 8 * 2**target_cfg_c.data_bus_width_l2;

  -- The explorer's segment, and the register map inside it.
  constant GT0_C : natural := 16#1000#;
  constant COMMAND_C : natural := GT0_C + 16#000#;
  constant ADDRESS_C : natural := GT0_C + 16#100#;
  constant WDATA_C : natural := GT0_C + 16#104#;
  constant WMASK_C : natural := GT0_C + 16#108#;
  constant SLOT_ENABLE_C : natural := GT0_C + 16#10c#;
  constant SCAN_CTRL_C : natural := GT0_C + 16#110#;
  constant STATUS_C : natural := GT0_C + 16#200#;
  constant FINGERPRINT_C : natural := GT0_C + 16#204#;
  constant RDATA_C : natural := GT0_C + 16#208#;
  constant SCAN_VALID_C : natural := GT0_C + 16#20c#;
  constant SCAN_ERROR_C : natural := GT0_C + 16#210#;
  constant SCAN_RESULT_C : natural := GT0_C + 16#214#;
  constant SLOT_ADDR_C : natural := GT0_C + 16#300#;

  -- Target map. The four registers are the stub's own; the fifth address
  -- always errors, and so does anything else.
  constant TGT_A_C : natural := 16#10#;
  constant TGT_B_C : natural := 16#14#;
  constant TGT_C_C : natural := 16#18#;
  constant TGT_D_C : natural := 16#1c#;
  constant TGT_ERR_C : natural := 16#40#;

  signal clock_s : std_ulogic := '0';
  signal drp_clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;

  signal target_m : master_t;
  signal target_s : slave_t;

  subtype target_data_t is unsigned(data_width_c-1 downto 0);
  type target_data_vector is array (natural range <>) of target_data_t;
  signal target_reg_s : target_data_vector(0 to 3);

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

begin

  dut: explorer_rack
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,

      gt0_drpclk_i => drp_clock_s,
      gt0_reset_n_i => reset_n_s,
      gt0_target_o => target_m,
      gt0_target_i => target_s
      );

  host_clock: process
  begin
    while not done_s loop
      clock_s <= '0';
      wait for 5 ns;
      clock_s <= '1';
      wait for 5 ns;
    end loop;
    wait;
  end process;

  target_clock: process
  begin
    while not done_s loop
      drp_clock_s <= '0';
      wait for 3500 ps;
      drp_clock_s <= '1';
      wait for 3500 ps;
    end loop;
    wait;
  end process;

  -- Target stub: combinational decode, registered storage.
  target_decode: process(target_m, target_reg_s)
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

      if is_write(target_cfg_c, target_m) then
        target_s <= write_response(target_cfg_c, error => error_v);
      else
        target_s <= read_response(target_cfg_c,
                                  value => resize(value_v, target_bits_c),
                                  error => error_v);
      end if;
    end if;
  end process;

  target_store: process(drp_clock_s, reset_n_s)
    variable addr_v : unsigned(address_width_c-1 downto 0);
  begin
    if rising_edge(drp_clock_s) then
      if is_access(target_cfg_c, target_m)
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

    -- Poll the status register the way the host does, and hand back the error
    -- code of the completed command.
    procedure wait_done(variable code : out unsigned(1 downto 0)) is
      variable value : unsigned(31 downto 0);
      variable err : boolean;
    begin
      for i in 1 to 400 loop
        apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(STATUS_C), value, err);
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
      reg_write(COMMAND_C, resize(op, 32));
      wait_done(code);
    end procedure;

    procedure target_write(constant target_addr : natural;
                           constant value : unsigned(31 downto 0);
                           variable code : out unsigned(1 downto 0)) is
    begin
      reg_write(ADDRESS_C, target_addr);
      reg_write(WDATA_C, value);
      fire(OP_WRITE_C, code);
    end procedure;

    procedure target_read(constant target_addr : natural;
                          variable code : out unsigned(1 downto 0)) is
    begin
      reg_write(ADDRESS_C, target_addr);
      fire(OP_READ_C, code);
    end procedure;

  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 47 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    settle;

    -- Descriptor: first byte is the root array header (0x83: type,
    -- next-offset, instruments map).
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(0), v, e);
    assert not e report "descriptor read errored" severity failure;
    assert v(7 downto 0) = x"83" report "descriptor header wrong"
      severity failure;

    -- Instance fingerprint: the whole rack's descriptor, keyed.
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(FINGERPRINT_C), v, e);
    assert not e report "fingerprint read errored" severity failure;
    assert v /= 0 and v /= x"ffffffff" report "fingerprint looks trivial"
      severity failure;
    check(STATUS_C, 0, "STATUS at reset");

    -- Manual write then manual read, round trip through the target and its
    -- clock.
    target_write(TGT_A_C, x"12345678", code);
    assert code = ERROR_OK_C report "write to A errored" severity failure;
    target_read(TGT_A_C, code);
    assert code = ERROR_OK_C report "read of A errored" severity failure;
    check(RDATA_C, td(x"12345678"), "RDATA of A");

    target_write(TGT_B_C, x"a5a5a5a5", code);
    assert code = ERROR_OK_C report "write to B errored" severity failure;
    target_read(TGT_B_C, code);
    assert code = ERROR_OK_C report "read of B errored" severity failure;
    check(RDATA_C, td(x"a5a5a5a5"), "RDATA of B");

    -- Masked write: a read-modify-write on the target, leaving the bits
    -- outside the mask as they were.
    reg_write(ADDRESS_C, TGT_A_C);
    reg_write(WDATA_C, unsigned'(x"aabbccdd"));
    reg_write(WMASK_C, unsigned'(x"00ff00ff"));
    fire(OP_MASKED_WRITE_C, code);
    assert code = ERROR_OK_C report "masked write errored" severity failure;
    check(RDATA_C, td(x"12345678"), "RDATA of the masked write's read");
    target_read(TGT_A_C, code);
    assert code = ERROR_OK_C report "read after the masked write errored"
      severity failure;
    check(RDATA_C, td(x"12bb56dd"), "A after the masked write");

    -- An address the target refuses, reported as a slave error and nothing
    -- worse: the transport is still there to say so.
    target_read(TGT_ERR_C, code);
    assert code = ERROR_SLVERR_C
      report "read of the erroring address did not report slverr"
      severity failure;

    -- Scanner: three slots of the four, swept while the host does nothing.
    target_write(TGT_C_C, x"0f0f0f0f", code);
    assert code = ERROR_OK_C report "write to C errored" severity failure;
    target_write(TGT_D_C, x"77777777", code);
    assert code = ERROR_OK_C report "write to D errored" severity failure;

    reg_write(SLOT_ADDR_C + 0, TGT_A_C);
    reg_write(SLOT_ADDR_C + 4, TGT_B_C);
    reg_write(SLOT_ADDR_C + 8, TGT_C_C);
    reg_write(SLOT_ADDR_C + 12, TGT_ERR_C);
    reg_write(SLOT_ENABLE_C, 2#0111#);
    reg_write(SCAN_CTRL_C, 1);
    settle(400);

    check(SCAN_VALID_C, 2#0111#, "SCAN_VALID of the enabled slots");
    check(SCAN_ERROR_C, 0, "SCAN_ERROR of the enabled slots");
    check(SCAN_RESULT_C + 0, td(x"12bb56dd"), "scan result 0");
    check(SCAN_RESULT_C + 4, td(x"a5a5a5a5"), "scan result 1");
    check(SCAN_RESULT_C + 8, td(x"0f0f0f0f"), "scan result 2");
    check(SCAN_RESULT_C + 12, 0, "scan result of a disabled slot");

    -- A manual command preempts the scanner, and the scanner picks the new
    -- value up.
    target_write(TGT_A_C, x"01020304", code);
    assert code = ERROR_OK_C
      report "manual write during a scan errored" severity failure;
    settle(400);
    check(SCAN_RESULT_C + 0, td(x"01020304"),
          "scan result 0 after a manual write");

    -- The erroring slot brought into the sweep raises its error bit alone.
    reg_write(SLOT_ENABLE_C, 2#1111#);
    settle(400);
    check(SCAN_ERROR_C, 2#1000#, "SCAN_ERROR with the erroring slot enabled");
    check(SCAN_VALID_C, 2#0111#, "SCAN_VALID with the erroring slot enabled");

    reg_write(SCAN_CTRL_C, 0);
    settle;
    check(STATUS_C, 2#00010#, "STATUS once the scanner is stopped");

    report "bus_explorer_rack testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
