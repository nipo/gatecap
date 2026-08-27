library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_simulation;
use nsl_amba.axi4_stream.all;
use work.clkrate_pkg.all;

-- UDP harness for an instrument-only rack: a UDP socket gateway in front of a
-- generated rack whose only instrument is a clock measurer. Driven externally
-- by the acrobe host; runs until killed.
--
-- Five clocks come out of one simulation driver. The host clock is unrelated
-- to the reference, so the instrument's rate crossing is exercised on every
-- read, and the three observed clocks are deliberately spread over more than a
-- decade -- 166 MHz, 8 MHz, and a 13 ns clock whose rate is not a round
-- number -- so a host that paired a rate with the wrong clock, or that lost
-- the low bits of one, could not read as correct. All three stay under the
-- 200 MHz the description declares as the highest rate expected.
--
-- The measurement window is 2**-14 reference seconds (see the description):
-- about 6100 reference cycles, so rates are valid a fraction of a millisecond
-- of simulated time after reset.
entity tb is
end entity;

architecture sim of tb is

  constant stream_cfg_c : config_t := config(1, last => true);

  signal clock_s : std_ulogic;
  signal reset_n_s : std_ulogic;

  -- Reference clock: 10 ns, the 100 MHz the description states as nominal.
  signal ref_clock_s : std_ulogic;
  -- Observed clocks: 6 ns (~166.7 MHz), 125 ns (8 MHz), 13 ns (~76.9 MHz).
  signal fast_clock_s : std_ulogic;
  signal slow_clock_s : std_ulogic;
  signal odd_clock_s : std_ulogic;

  signal rx_cmd_s : master_t;
  signal rx_rdy_s : slave_t;
  signal tx_rsp_s : master_t;
  signal tx_rdy_s : slave_t;

begin

  simdrv: nsl_simulation.driver.simulation_driver
    generic map(
      clock_count => 5,
      reset_count => 1,
      done_count => 1
      )
    port map(
      clock_period(0) => 12 ns,       -- host / APB clock
      clock_period(1) => 10 ns,       -- reference
      clock_period(2) => 6 ns,        -- fast
      clock_period(3) => 125 ns,      -- slow
      clock_period(4) => 13 ns,       -- odd
      reset_duration => (others => 44 ns),
      clock_o(0) => clock_s,
      clock_o(1) => ref_clock_s,
      clock_o(2) => fast_clock_s,
      clock_o(3) => slow_clock_s,
      clock_o(4) => odd_clock_s,
      reset_n_o(0) => reset_n_s,
      done_i => "0"
      );

  net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
    generic map(
      config_c => stream_cfg_c,
      bind_port_c => 4252
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      tx_i => tx_rsp_s,
      tx_o => tx_rdy_s,
      rx_o => rx_cmd_s,
      rx_i => rx_rdy_s
      );

  dut: clkrate_core
    generic map(
      stream_config_c => stream_cfg_c,
      burst_length_l2_c => 8
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      rx_i => rx_cmd_s,
      rx_o => rx_rdy_s,
      tx_o => tx_rsp_s,
      tx_i => tx_rdy_s,
      rates_ref_i => ref_clock_s,
      rates_fast_i => fast_clock_s,
      rates_slow_i => slow_clock_s,
      rates_odd_i => odd_clock_s
      );

end architecture;
