library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.apb.all;
use work.gen_pkg.all;

-- A generated single-domain core over APB, every capture-domain clock being the
-- host clock: descriptor header, fingerprint stability, idle/armed/triggered
-- status, the completed-window count and the read-back trace. The
-- generated_async bench runs the same scenario with the capture domain on a
-- clock of its own, so what differs between the two is the crossings.
entity tb is
end entity;

architecture sim of tb is

  constant signal_count_c : natural := 8;
  -- The map the rack allocated, as the generated package publishes it: the
  -- descriptor ROM pinned at 0 in a 4 KB segment, then the analyzer's own
  -- 16 KB segment holding its three 4 KB regions.
  constant apb_cfg_c : config_t := gen_capture_apb_config;
  constant capture_len_c : natural := 8;

  -- Region bases, as the descriptor advertises them.
  constant ADDR_DESC_C : natural := 16#0000#;
  constant ADDR_COMMAND_C : natural := 16#4000#;      -- action group
  constant ADDR_CAPTURE_LEN_C : natural := 16#4100#;  -- config group
  constant ADDR_STATUS_C : natural := 16#4200#;       -- status group
  constant ADDR_FINGERPRINT_C : natural := 16#4204#;
  constant ADDR_BUFFER_C : natural := 16#5000#;
  constant ADDR_TRIG_VALUE_C : natural := 16#6100#;   -- trigger config group
  constant ADDR_TRIG_MASK_C : natural := 16#6104#;

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;
  signal signals_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');

  function a(offset : natural) return unsigned is
  begin
    return to_unsigned(offset, apb_cfg_c.address_width);
  end function;

begin

  dut: gen_capture
    port map(
      apb_i => apb_m,
      apb_o => apb_s,
      reset_n_i => reset_n_s,
      la_sample_clock_i => clock_s,
      la_sample_reset_n_i => reset_n_s,
      la_sample_count_i => signals_s
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

  -- Free-running signal source: captured samples are consecutive.
  sig_gen: process(clock_s)
  begin
    if rising_edge(clock_s) then
      signals_s <= std_ulogic_vector(unsigned(signals_s) + 1);
    end if;
  end process;

  stim: process
    variable v : unsigned(31 downto 0);
    variable fp : unsigned(31 downto 0);
    variable e : boolean;
    variable base : unsigned(signal_count_c-1 downto 0);
  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);

    -- Descriptor: first byte is the top-level array header (0x83: type,
    -- next-offset, siblings-map).
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_DESC_C), v, e);
    assert not e report "descriptor read errored" severity failure;
    assert v(7 downto 0) = x"83" report "descriptor header wrong" severity failure;

    -- Instance fingerprint: non-trivial and stable across reads.
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_FINGERPRINT_C), v, e);
    assert not e report "fingerprint read errored" severity failure;
    assert v /= 0 and v /= x"ffffffff"
      report "fingerprint looks trivial" severity failure;
    fp := v;
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_FINGERPRINT_C), v, e);
    assert v = fp report "fingerprint not stable" severity failure;

    -- Configure: match-all trigger, capture_len samples.
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_TRIG_VALUE_C),
              unsigned'(x"00000000"), err => e);
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_TRIG_MASK_C),
              unsigned'(x"00000000"), err => e);
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_CAPTURE_LEN_C),
              to_unsigned(capture_len_c, 32), err => e);

    -- Idle before arming.
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_STATUS_C), v, e);
    assert v = to_unsigned(0, 32) report "not idle before arm" severity failure;

    -- Arm and let the capture run.
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_COMMAND_C),
              unsigned'(x"00000001"), err => e);
    for i in 0 to 29 loop
      wait until falling_edge(clock_s);
    end loop;

    -- Back to idle, triggered sticky set: STATUS[2:0] = 0b100, and one
    -- window completed in STATUS[31:16].
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_STATUS_C), v, e);
    assert v(2 downto 0) = "100" report "not idle+triggered after capture" severity failure;
    assert v(31 downto 16) = to_unsigned(1, 16) report "wrong windows-done" severity failure;

    -- Read the trace back: consecutive samples, zero-padded above the width.
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_BUFFER_C), v, e);
    assert not e report "buffer read errored" severity failure;
    assert v(31 downto signal_count_c) = 0 report "sample padding not zero" severity failure;
    base := v(signal_count_c-1 downto 0);
    for k in 1 to capture_len_c-1 loop
      apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_BUFFER_C + k*4), v, e);
      assert v(31 downto signal_count_c) = 0
        report "sample padding not zero" severity failure;
      assert v(signal_count_c-1 downto 0) = base + k
        report "trace not consecutive at " & integer'image(k) severity failure;
    end loop;

    report "generated capture core testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
