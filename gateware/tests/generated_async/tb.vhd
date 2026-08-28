library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;
use nsl_amba.apb.all;

library gatecap_generated;
use gatecap_generated.async_pkg.all;

-- Asynchronous capture clock: the APB/host side runs on clock_s, the capture
-- core, trigger match and buffer write side on cap_clock_s (a different rate,
-- so the two are asynchronous). Config and arm cross in, status and head cross
-- back out, through the crossings the rack emits for a domain whose clock is
-- not the one the transport rides. A match-all capture must still read back the
-- consecutive capture-domain counter.
entity tb is
end entity;

architecture sim of tb is

  constant signal_count_c : natural := 8;
  constant apb_cfg_c : config_t := async_capture_apb_config;
  constant capture_len_c : natural := 8;

  -- Region bases, as the descriptor advertises them.
  constant ADDR_COMMAND_C : natural := 16#4000#;      -- action group
  constant ADDR_CAPTURE_LEN_C : natural := 16#4100#;  -- config group
  constant ADDR_STATUS_C : natural := 16#4200#;       -- status group
  constant ADDR_BUFFER_C : natural := 16#5000#;
  constant ADDR_TRIG_VALUE_C : natural := 16#6100#;   -- trigger config group
  constant ADDR_TRIG_MASK_C : natural := 16#6104#;

  signal clock_s : std_ulogic := '0';         -- host / APB clock (10 ns)
  signal reset_n_s : std_ulogic := '0';
  signal cap_clock_s : std_ulogic := '0';     -- capture clock (7 ns, async)
  signal cap_reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;
  signal signals_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');

  function a(offset : natural) return unsigned is
  begin
    return to_unsigned(offset, apb_cfg_c.address_width);
  end function;

begin

  dut: async_capture
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,
      la_sample_clock_i => cap_clock_s,
      la_sample_reset_n_i => cap_reset_n_s,
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

  cap_clock_gen: process
  begin
    while not done_s loop
      cap_clock_s <= '0';
      wait for 3500 ps;
      cap_clock_s <= '1';
      wait for 3500 ps;
    end loop;
    wait;
  end process;

  -- Free-running signal source in the capture domain: consecutive samples.
  sig_gen: process(cap_clock_s)
  begin
    if rising_edge(cap_clock_s) then
      signals_s <= std_ulogic_vector(unsigned(signals_s) + 1);
    end if;
  end process;

  stim: process
    variable v : unsigned(31 downto 0);
    variable e : boolean;
    variable base : unsigned(signal_count_c-1 downto 0);
    variable tries : natural;
  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    cap_reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    cap_reset_n_s <= '1';
    wait until falling_edge(clock_s);

    -- Match-all trigger, capture_len samples.
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_TRIG_VALUE_C),
              unsigned'(x"00000000"), err => e);
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_TRIG_MASK_C),
              unsigned'(x"00000000"), err => e);
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_CAPTURE_LEN_C),
              to_unsigned(capture_len_c, 32), err => e);

    -- Arm and poll STATUS until the capture completes (back to idle,
    -- triggered set). Idle+triggered and windows-done cross the crossings
    -- independently; wait for both to settle (a real host polls far slower
    -- than the crossing latency).
    apb_write(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_COMMAND_C),
              unsigned'(x"00000001"), err => e);
    tries := 0;
    loop
      apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_STATUS_C), v, e);
      exit when v(2 downto 0) = "100" and v(31 downto 16) = to_unsigned(1, 16);
      tries := tries + 1;
      assert tries < 1000 report "capture did not complete" severity failure;
    end loop;

    -- Read the trace back: consecutive samples.
    apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_BUFFER_C), v, e);
    assert not e report "buffer read errored" severity failure;
    base := v(signal_count_c-1 downto 0);
    for k in 1 to capture_len_c-1 loop
      apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(ADDR_BUFFER_C + k*4), v, e);
      assert v(signal_count_c-1 downto 0) = base + k
        report "async trace not consecutive at " & integer'image(k) severity failure;
    end loop;

    report "generated async capture testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
