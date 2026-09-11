library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_clocking, nsl_spi;
use nsl_clocking.pll.all;
library gatecap_generated;

-- PLL-under-test bench on the iCEstick, reached over the FT2232H port-B SPI
-- like the icestick demo. An NSL pll_multi instance runs from the 12 MHz
-- board oscillator; the rack runs on that same oscillator and its clock
-- measurer watches the PLL output against it.
--
-- The request, 20 MHz with 2% slack, is deliberately not reachable on the
-- iCE40's power-of-two output divider: the solver maps 19.875 MHz
-- (VCO 636 MHz, feedback 53, divider 32) and that is the rate the host must
-- read back.
--
-- The green LED shows PLL lock.
entity main is
  port (
    clock_i : in std_ulogic;
    led_o: out std_ulogic;

    spi_sck_i : in std_ulogic;
    spi_mosi_i : in std_ulogic;
    spi_miso_o : out std_ulogic;
    spi_cs_n_i : in std_ulogic
  );
end main;

architecture arch of main is

  constant clock_i_hz_c: integer := 12e6;

  -- The reference comes through fabric, not straight off the
  -- package pin, so this is the SB_PLL40_CORE flavour.
  constant cfg_c : pll_config_t := pll_config(
    input_hz => clock_i_hz_c,
    o0 => pll_output(20_000_000, tolerance_ppm => 20_000),
    reference_input => nsl_clocking.pll_backend.pll_reference_id("CORE"));

  signal pll_clock_s : std_ulogic_vector(0 to cfg_c.output_count-1);
  signal locked_s : std_ulogic;

  signal reset_count_s : unsigned(7 downto 0) := (others => '0');
  signal reset_n_s : std_ulogic;

  signal spi_s: nsl_spi.spi.spi_slave_io;

begin

  -- The rack runs off the board oscillator and must answer whatever
  -- the PLL under test does, lock included.
  startup: process(clock_i)
  begin
    if rising_edge(clock_i) then
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
      clock_i => clock_i,
      clock_o => pll_clock_s,
      reset_n_i => '1',
      locked_o => locked_s
      );

  led_o <= locked_s;

  rack: gatecap_generated.pll_rates_ist.pll_rates_core
    generic map(
      clock_frequency_c => clock_i_hz_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_s,

      spi_i => spi_s.i,
      spi_o => spi_s.o,

      rates_ref_i => clock_i,
      rates_out0_i => pll_clock_s(0)
      );

  spi_s.i.sck <= spi_sck_i;
  spi_s.i.mosi <= spi_mosi_i;
  spi_s.i.cs_n <= spi_cs_n_i;

  -- Nothing else drives the header pin, so the tristate enable is
  -- ignored and the value goes out as is.
  spi_miso_o <= spi_s.o.miso.v;

end arch;
