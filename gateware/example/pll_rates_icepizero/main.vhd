library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_clocking;
use nsl_clocking.pll.all;
library gatecap_generated;

-- PLL-under-test bench on the iCEPi-Zero, reached over the board's FT231X
-- UART carrying HDLC frames. An NSL pll_multi instance runs from the 50 MHz
-- board oscillator; the rack's clock measurer watches its three outputs
-- against that same oscillator.
--
-- out1 and out2 are exact by construction; out0 asks for 512 x 44.1 kHz,
-- which no integer divisor reaches exactly, with 500 ppm of slack: the rate
-- the host reads back is the solver's approximation.
--
-- The LED shows PLL lock.
entity main is
  port (
    global_clk_i: in std_ulogic;

    uart_tx_o: out std_ulogic;
    uart_rx_i: in std_ulogic;

    led_o : out std_ulogic
  );
end main;

architecture arch of main is

  constant clock_hz_c : natural := 50_000_000;

  constant cfg_c : pll_config_t := pll_config(
    input_hz => clock_hz_c,
    o0 => pll_output(22_579_200, tolerance_ppm => 500),
    o1 => pll_output(100_000_000),
    o2 => pll_output(50_000_000));

  signal reset_count_s : unsigned(7 downto 0) := (others => '0');
  signal reset_n_s : std_ulogic;
  signal pll_clock_s : std_ulogic_vector(0 to cfg_c.output_count-1);
  signal locked_s : std_ulogic;

begin

  startup: process(global_clk_i)
  begin
    if rising_edge(global_clk_i) then
      if reset_count_s /= x"ff" then
        reset_count_s <= reset_count_s + 1;
      end if;
    end if;
  end process;

  reset_n_s <= '1' when reset_count_s = x"ff" else '0';

  pll: nsl_clocking.pll.pll_multi
    generic map(
      config_c => cfg_c
      )
    port map(
      clock_i => global_clk_i,
      clock_o => pll_clock_s,
      reset_n_i => reset_n_s,
      locked_o => locked_s
      );

  led_o <= locked_s;

  rack: gatecap_generated.pll_rates_ipz.pll_rates_core
    generic map(
      clock_frequency_c => clock_hz_c,
      baud_rate_c => 1_000_000,
      burst_length_l2_c => 8
      )
    port map(
      clock_i => global_clk_i,
      reset_n_i => reset_n_s,

      uart_rx_i => uart_rx_i,
      uart_tx_o => uart_tx_o,

      rates_ref_i => global_clk_i,
      rates_out0_i => pll_clock_s(0),
      rates_out1_i => pll_clock_s(1),
      rates_out2_i => pll_clock_s(2)
      );

end architecture;
