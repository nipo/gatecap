library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_bnoc, nsl_io, nsl_spi;
use nsl_amba.axi4_stream.all;
use work.spi_pkg.all;

-- UDP harness for a generated rack reached over plain SPI.
--
-- The rack's four SPI pins hang off a framed SPI transactor, and one UDP
-- server carries that transactor's command and response frames:
--
--   udp:4254 -- framed SPI transactor commands -- spi_framed_transactor
--                                                       |
--                                     SPI --------------+
--                                      |
--                                   spi_rack (logic analyzer + panel)
--
-- so the host reaches the rack as
--
--   udp/127.0.0.1:4254/nsl_spi(fin=100M,fmax=10M)/cs0/gatecap
--
-- The transactor is the whole master: it holds the chip select for one
-- transaction and shifts the bytes the host handed it, which is all the
-- protocol asks of a master. Its divisor divides the 100 MHz host clock down
-- to the rate asked for, so asking for 10 MHz is asking for the highest rate
-- the rack was elaborated for.
--
-- The panel's wires are looped back here, so a host can observe what it drove:
-- every control feeds the status of the same width, and the tick output feeds
-- the tick input. The analyzer's stimulus is the usual free-running counter
-- and a state bus asserting DONE for one cycle every 64.
--
-- Driven externally by the acrobe host; runs until killed.
entity tb is
end entity;

architecture sim of tb is

  constant stream_cfg_c : config_t
    := nsl_bnoc.axi_adapter.axi4_stream_framed_config_c;

  constant event_period_c : natural := 64;
  constant period_bits_c : natural := 6;

  signal host_clock_s : std_ulogic := '0';
  signal host_reset_n_s : std_ulogic := '0';
  signal panel_clock_s : std_ulogic := '0';
  signal panel_reset_n_s : std_ulogic := '0';

  signal udp_rx_s, udp_tx_s : nsl_amba.axi4_stream.bus_t;
  signal cmd_s, rsp_s : nsl_bnoc.framed.framed_bus_t;

  -- SPI wires between the transactor and the rack.
  signal sck_s : std_ulogic;
  signal cs_n_s : nsl_io.io.opendrain_vector(0 to 0);
  signal mosi_s : nsl_io.io.tristated;
  signal miso_s : std_ulogic;

  signal spi_i_s : nsl_spi.spi.spi_slave_i;
  signal spi_o_s : nsl_spi.spi.spi_slave_o;

  signal count_s : unsigned(7 downto 0) := (others => '0');
  signal state_s : std_ulogic_vector(1 downto 0) := "01";

  signal led_s : std_ulogic;
  signal level_s : unsigned(11 downto 0);
  signal start_s : std_ulogic;

begin

  net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
    generic map(
      config_c => stream_cfg_c,
      bind_port_c => 4254
      )
    port map(
      clock_i => host_clock_s,
      reset_n_i => host_reset_n_s,
      tx_i => udp_tx_s.m,
      tx_o => udp_tx_s.s,
      rx_o => udp_rx_s.m,
      rx_i => udp_rx_s.s
      );

  cmd_adapter: nsl_bnoc.axi_adapter.axi4_stream_to_framed
    port map(
      clock_i => host_clock_s,
      reset_n_i => host_reset_n_s,
      axi_i => udp_rx_s.m,
      axi_o => udp_rx_s.s,
      framed_o => cmd_s.req,
      framed_i => cmd_s.ack
      );

  rsp_adapter: nsl_bnoc.axi_adapter.framed_to_axi4_stream
    port map(
      clock_i => host_clock_s,
      reset_n_i => host_reset_n_s,
      framed_i => rsp_s.req,
      framed_o => rsp_s.ack,
      axi_o => udp_tx_s.m,
      axi_i => udp_tx_s.s
      );

  master: nsl_spi.transactor.spi_framed_transactor
    generic map(
      slave_count_c => 1
      )
    port map(
      clock_i => host_clock_s,
      reset_n_i => host_reset_n_s,
      sck_o => sck_s,
      cs_n_o => cs_n_s,
      mosi_o => mosi_s,
      miso_i => miso_s,
      cmd_i => cmd_s.req,
      cmd_o => cmd_s.ack,
      rsp_o => rsp_s.req,
      rsp_i => rsp_s.ack
      );

  spi_i_s.sck <= sck_s;
  spi_i_s.cs_n <= cs_n_s(0).drain_n;
  spi_i_s.mosi <= nsl_io.io.to_logic(mosi_s);
  miso_s <= nsl_io.io.to_logic(spi_o_s.miso);

  dut: spi_rack
    port map(
      reset_n_i => host_reset_n_s,
      spi_i => spi_i_s,
      spi_o => spi_o_s,

      la_main_clock_i => host_clock_s,
      la_main_reset_n_i => host_reset_n_s,
      la_main_state_i => state_s,
      la_main_count_i => std_ulogic_vector(count_s),

      panel_clock_i => panel_clock_s,
      panel_reset_n_i => panel_reset_n_s,
      panel_led_o => led_s,
      panel_level_o => level_s,
      -- Loopback: what the host wrote comes back as the panel's statuses.
      panel_led_echo_i => led_s,
      panel_level_echo_i => level_s,
      panel_start_o => start_s,
      -- Loopback: the strobed tick output is one event on its counter.
      panel_started_i => start_s
      );

  host_clock_s <= not host_clock_s after 5 ns;      -- 100 MHz
  panel_clock_s <= not panel_clock_s after 3500 ps; -- ~143 MHz, unrelated

  reset: process
  begin
    host_reset_n_s <= '0';
    panel_reset_n_s <= '0';
    wait for 42 ns;
    host_reset_n_s <= '1';
    panel_reset_n_s <= '1';
    wait;
  end process;

  -- Analyzer stimulus: a free-running counter, and the state asserted for the
  -- single cycle where the counter reaches a multiple of the event period.
  bench: process(host_clock_s)
  begin
    if rising_edge(host_clock_s) then
      count_s <= count_s + 1;
      if count_s(period_bits_c-1 downto 0) = event_period_c - 1 then
        state_s <= "11";                -- DONE
      else
        state_s <= "01";                -- BUSY
      end if;
    end if;
  end process;

end architecture;
