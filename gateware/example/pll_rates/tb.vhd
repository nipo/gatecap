library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_clocking, nsl_simulation;
use nsl_amba.axi4_stream.all;
use nsl_clocking.pll.all;
library gatecap_generated;
use gatecap_generated.pll_rates_pkg.all;

-- UDP harness for the PLL-under-test bench: an NSL pll_multi instance
-- realized by its simulation backend, every output watched by the clock
-- measurer of a generated rack. Driven externally by the acrobe host; runs
-- until killed.
--
-- The PLL runs from the same 50 MHz clock the measurer takes as reference,
-- so a measured rate is the realized ratio. Three outputs are exact by
-- construction; out0 asks for 512 x 44.1 kHz, which no divisor reaches
-- exactly, with 500 ppm of slack and fractional division allowed: the rate
-- the host reads is the solver's approximation, not the request.
--
-- The host clock is unrelated to both.
entity tb is
end entity;

architecture sim of tb is

  constant stream_cfg_c : config_t := config(1, last => true);

  constant cfg_c : pll_config_t := pll_config(
    input_hz => 50_000_000,
    o0 => pll_output(22_579_200,
                     tolerance_ppm => 500,
                     allow_fractional => true),
    o1 => pll_output(150_000_000),
    o2 => pll_output(100_000_000),
    o3 => pll_output(75_000_000));

  signal clock_s : std_ulogic;
  signal reset_n_s : std_ulogic;

  signal ref_clock_s : std_ulogic;
  signal pll_clock_s : std_ulogic_vector(0 to cfg_c.output_count-1);
  signal locked_s : std_ulogic;

  signal rx_cmd_s : master_t;
  signal rx_rdy_s : slave_t;
  signal tx_rsp_s : master_t;
  signal tx_rdy_s : slave_t;

begin

  simdrv: nsl_simulation.driver.simulation_driver
    generic map(
      clock_count => 2,
      reset_count => 1,
      done_count => 1
      )
    port map(
      clock_period(0) => 12 ns,       -- host clock
      clock_period(1) => 20 ns,       -- reference and PLL input
      reset_duration => (others => 44 ns),
      clock_o(0) => clock_s,
      clock_o(1) => ref_clock_s,
      reset_n_o(0) => reset_n_s,
      done_i => "0"
      );

  pll: nsl_clocking.pll.pll_multi
    generic map(
      config_c => cfg_c
      )
    port map(
      clock_i => ref_clock_s,
      clock_o => pll_clock_s,
      reset_n_i => reset_n_s,
      locked_o => locked_s
      );

  net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
    generic map(
      config_c => stream_cfg_c,
      bind_port_c => 4255
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      tx_i => tx_rsp_s,
      tx_o => tx_rdy_s,
      rx_o => rx_cmd_s,
      rx_i => rx_rdy_s
      );

  dut: pll_rates_core
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
      rates_out0_i => pll_clock_s(0),
      rates_out1_i => pll_clock_s(1),
      rates_out2_i => pll_clock_s(2),
      rates_out3_i => pll_clock_s(3)
      );

end architecture;
