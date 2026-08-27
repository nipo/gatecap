library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, gatecap;
use nsl_amba.apb.all;
use nsl_data.text.all;

-- One clock-rate measurement instrument, driven over APB the way a rack
-- drives it.
--
-- The host clock is unrelated to the reference, so every rate the register
-- file returns has crossed the block's clock boundary. The observed clocks
-- are spread over more than a decade, and one of them has a rate that is not
-- a round number, so a rate paired with the wrong clock or truncated could
-- not read as correct.
entity tb is
end entity;

architecture sim of tb is

  constant apb_cfg_c : config_t := config(address_width => 12,
                                          data_bus_width => 32,
                                          err => true);

  constant reference_hz_c : natural := 100_000_000;
  -- 2**-14 reference seconds, ~61 us: a one-second window is not something a
  -- simulation reaches. Rates come out to the nearest 16384 Hz.
  constant update_hz_l2_c : natural := 14;
  constant quantum_c : natural := 2**update_hz_l2_c;
  -- What 200 MHz, the highest rate this bench expects, needs.
  constant rate_width_c : natural := 28;
  constant measured_count_c : natural := 3;
  constant fingerprint_c : unsigned(31 downto 0) := x"5ea17e57";

  constant ADDR_STATUS_C : natural := 16#200#;
  constant ADDR_FINGERPRINT_C : natural := 16#204#;
  constant ADDR_RATE_C : natural := 16#300#;

  -- Observed clock periods, and the rates they are.
  constant fast_period_c : time := 6 ns;
  constant slow_period_c : time := 125 ns;
  constant odd_period_c : time := 13 ns;
  constant fast_hz_c : natural := 166_666_666;
  constant slow_hz_c : natural := 8_000_000;
  constant odd_hz_c : natural := 76_923_076;

  signal clock_s : std_ulogic := '0';
  signal reference_clock_s : std_ulogic := '0';
  signal measured_clock_s : std_ulogic_vector(measured_count_c-1 downto 0)
    := (others => '0');
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;

  function a(offset : natural) return unsigned is
  begin
    return to_unsigned(offset, apb_cfg_c.address_width);
  end function;

begin

  -- Host clock, unrelated to the reference: the rate crossing is on every
  -- read.
  host_clock: process
  begin
    while not done_s loop
      clock_s <= '0';
      wait for 6 ns;
      clock_s <= '1';
      wait for 6 ns;
    end loop;
    wait;
  end process;

  reference_clock: process
  begin
    while not done_s loop
      reference_clock_s <= '0';
      wait for 5 ns;
      reference_clock_s <= '1';
      wait for 5 ns;
    end loop;
    wait;
  end process;

  fast_clock: process
  begin
    while not done_s loop
      measured_clock_s(0) <= '0';
      wait for fast_period_c / 2;
      measured_clock_s(0) <= '1';
      wait for fast_period_c / 2;
    end loop;
    wait;
  end process;

  slow_clock: process
  begin
    while not done_s loop
      measured_clock_s(1) <= '0';
      wait for slow_period_c / 2;
      measured_clock_s(1) <= '1';
      wait for slow_period_c / 2;
    end loop;
    wait;
  end process;

  odd_clock: process
  begin
    while not done_s loop
      measured_clock_s(2) <= '0';
      wait for odd_period_c / 2;
      measured_clock_s(2) <= '1';
      wait for odd_period_c / 2;
    end loop;
    wait;
  end process;

  dut: gatecap.clock_measurer.clock_rate_block
    generic map(
      apb_config_c => apb_cfg_c,
      size_l2_c => gatecap.clock_measurer.clock_measurer_size_l2(
        apb_cfg_c.data_bus_width_l2),
      measured_count_c => measured_count_c,
      reference_hz_c => reference_hz_c,
      rate_width_c => rate_width_c,
      update_hz_l2_c => update_hz_l2_c,
      fingerprint_c => fingerprint_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,
      reference_clock_i => reference_clock_s,
      measured_clock_i => measured_clock_s
      );

  stim: process
    variable value : unsigned(31 downto 0);
    variable e : boolean;
    variable fast_hz, slow_hz, odd_hz : integer;

    procedure read(constant offset : natural;
                   constant what : string;
                   variable value : out unsigned(31 downto 0)) is
      variable err : boolean;
    begin
      apb_read(apb_cfg_c, clock_s, apb_s, apb_m, a(offset), value, err);
      assert not err report what & " read errored" severity failure;
    end procedure;

    procedure check(constant offset : natural;
                    constant expected : unsigned;
                    constant what : string) is
      variable got : unsigned(31 downto 0);
    begin
      read(offset, what, got);
      assert got = expected
        report what & " is " & to_string(std_ulogic_vector(got))
             & ", expected " & to_string(std_ulogic_vector(expected))
        severity failure;
    end procedure;

    -- What one measurement may be off by: three quanta covers the integer
    -- count and the one edge a resynchronised counter may move a window
    -- boundary by, and 200 ppm covers the window being a whole number of
    -- reference cycles rather than exactly 2**-update_hz_l2_c seconds.
    procedure check_rate(constant index : natural;
                         constant expected : natural;
                         constant what : string;
                         variable measured : out integer) is
      variable got : unsigned(31 downto 0);
      variable hz, tolerance : integer;
    begin
      read(ADDR_RATE_C + 4 * index, what, got);
      hz := to_integer(got);
      tolerance := 3 * quantum_c + expected / 5000;
      assert abs (hz - expected) <= tolerance
        report what & " is " & integer'image(hz) & " Hz, expected "
             & integer'image(expected) & " Hz +/- "
             & integer'image(tolerance)
        severity failure;
      assert hz mod quantum_c = 0
        report what & " is " & integer'image(hz)
             & " Hz, not a multiple of the " & integer'image(quantum_c)
             & " Hz quantum the block advertises"
        severity failure;
      measured := hz;
    end procedure;
  begin
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 47 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';

    -- The status group answers before any measurement has completed: the
    -- fingerprint is what a host polls this instrument for.
    for i in 1 to 8 loop
      wait until falling_edge(clock_s);
    end loop;
    check(ADDR_FINGERPRINT_C, fingerprint_c, "FINGERPRINT");
    check(ADDR_STATUS_C, to_unsigned(0, 32), "STATUS");

    -- Three measurement windows: the first publishes, the rest confirm the
    -- measurement is free-running rather than a one-shot.
    wait for 200 us;

    check_rate(0, fast_hz_c, "the fast clock's rate", fast_hz);
    check_rate(1, slow_hz_c, "the slow clock's rate", slow_hz);
    check_rate(2, odd_hz_c, "the odd clock's rate", odd_hz);
    assert fast_hz > odd_hz and odd_hz > slow_hz
      report "the rates are not ordered as the clocks are: a rate landed in "
           & "the wrong register"
      severity failure;

    -- A rate slot past the observed clocks belongs to no measurer.
    check(ADDR_RATE_C + 4 * measured_count_c, to_unsigned(0, 32),
          "the rate register past the last observed clock");

    -- One more window, and the rates still stand: the beat between an
    -- observed clock and the window is worth one edge either way.
    wait for 100 us;
    check_rate(0, fast_hz_c, "the fast clock's rate, one window later",
               fast_hz);
    check_rate(1, slow_hz_c, "the slow clock's rate, one window later",
               slow_hz);
    check_rate(2, odd_hz_c, "the odd clock's rate, one window later",
               odd_hz);

    report "clock_measurer testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
