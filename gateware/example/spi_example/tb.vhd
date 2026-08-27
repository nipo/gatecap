library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_bnoc, nsl_spi, nsl_io, nsl_memory, nsl_data;
use nsl_amba.axi4_stream.all;
use nsl_data.bytestream.all;
use work.spi_probe.all;

-- Usage example: a generated gatecap capture rack and an SPI transactor
-- driving an SPI memory target, each behind its own UDP-to-stream bridge. The
-- capture core probes the SPI bus wires and both the command and response
-- streams of the transactor, so a host can arm the capture, run real SPI
-- transactions through the second bridge, and read back a trace that a
-- protocol analyser (sigrok) decodes into those very transactions.
--
--   udp:4242 -- spi_stream_capture ------------- 26 probes
--   udp:4243 -- axi/framed -- spi_transactor --SPI--> spi_memory + ram
--                              cmd/rsp streams and SPI wires tapped above
entity tb is
end entity;

architecture sim of tb is

  constant cfg_c : config_t := config(1, last => true);
  constant addr_bytes_c : natural := 2;
  constant data_bytes_c : natural := 2;

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';

  -- Capture bridge (udp 4242)
  signal cap_cmd_m : master_t;
  signal cap_cmd_s : slave_t;
  signal cap_rsp_m : master_t;
  signal cap_rsp_s : slave_t;

  -- SPI bridge (udp 4243)
  signal spi_cmd_m : master_t;
  signal spi_cmd_s : slave_t;
  signal spi_rsp_m : master_t;
  signal spi_rsp_s : slave_t;

  -- Framed command/response. A framed FIFO on each side buffers a whole
  -- frame so the transactor -- which interleaves command consumption and
  -- response emission -- never stalls on the half-duplex UDP transport.
  signal ada_cmd_s : nsl_bnoc.framed.framed_bus_t;  -- adapter -> cmd fifo
  signal cmd_s : nsl_bnoc.framed.framed_bus_t;      -- cmd fifo -> transactor
  signal rsp_s : nsl_bnoc.framed.framed_bus_t;      -- transactor -> rsp fifo
  signal fifo_rsp_s : nsl_bnoc.framed.framed_bus_t; -- rsp fifo -> adapter

  -- SPI wires
  signal sck_s : std_ulogic;
  signal cs_n_s : nsl_io.io.opendrain_vector(0 to 0);
  signal mosi_s : nsl_io.io.tristated;
  signal miso_s : std_ulogic;
  signal slave_i_s : nsl_spi.spi.spi_slave_i;
  signal slave_o_s : nsl_spi.spi.spi_slave_o;

  -- Memory target
  signal mem_addr_s : unsigned(addr_bytes_c*8-1 downto 0);
  signal mem_wdata_s : byte_string(0 to data_bytes_c-1);
  signal mem_wvalid_s : std_ulogic;
  signal mem_rready_s, mem_rvalid_s : std_ulogic;
  signal ram_wdata_s, ram_rdata_s : std_ulogic_vector(8*data_bytes_c-1 downto 0);

begin

  capture: spi_stream_capture
    generic map(
      stream_config_c => cfg_c,
      burst_length_l2_c => 8
      )
    port map(
      reset_n_i => reset_n_s,
      rx_i => cap_cmd_m,
      rx_o => cap_cmd_s,
      tx_o => cap_rsp_m,
      tx_i => cap_rsp_s,
      la_spi_clock_i => clock_s,
      la_spi_reset_n_i => reset_n_s,
      la_spi_sck_i => sck_s,
      la_spi_cs_n_i => cs_n_s(0).drain_n,
      la_spi_mosi_i => nsl_io.io.to_logic(mosi_s),
      la_spi_miso_i => miso_s,
      la_spi_command_i => cmd_s,
      la_spi_response_i => rsp_s
      );

  cap_net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
    generic map(
      config_c => cfg_c,
      bind_port_c => 4242
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      tx_i => cap_rsp_m,
      tx_o => cap_rsp_s,
      rx_o => cap_cmd_m,
      rx_i => cap_cmd_s
      );

  spi_net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
    generic map(
      config_c => cfg_c,
      bind_port_c => 4243
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      tx_i => spi_rsp_m,
      tx_o => spi_rsp_s,
      rx_o => spi_cmd_m,
      rx_i => spi_cmd_s
      );

  cmd_adapter: nsl_bnoc.axi_adapter.axi4_stream_to_framed
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      axi_i => spi_cmd_m,
      axi_o => spi_cmd_s,
      framed_o => ada_cmd_s.req,
      framed_i => ada_cmd_s.ack
      );

  cmd_fifo: nsl_bnoc.framed.framed_fifo
    generic map(depth => 64, clk_count => 1)
    port map(
      p_resetn => reset_n_s,
      p_clk(0) => clock_s,
      p_in_val => ada_cmd_s.req,
      p_in_ack => ada_cmd_s.ack,
      p_out_val => cmd_s.req,
      p_out_ack => cmd_s.ack
      );

  rsp_fifo: nsl_bnoc.framed.framed_fifo
    generic map(depth => 64, clk_count => 1)
    port map(
      p_resetn => reset_n_s,
      p_clk(0) => clock_s,
      p_in_val => rsp_s.req,
      p_in_ack => rsp_s.ack,
      p_out_val => fifo_rsp_s.req,
      p_out_ack => fifo_rsp_s.ack
      );

  rsp_adapter: nsl_bnoc.axi_adapter.framed_to_axi4_stream
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      framed_i => fifo_rsp_s.req,
      framed_o => fifo_rsp_s.ack,
      axi_o => spi_rsp_m,
      axi_i => spi_rsp_s
      );

  transactor: nsl_spi.transactor.spi_framed_transactor
    generic map(
      slave_count_c => 1
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      sck_o => sck_s,
      cs_n_o => cs_n_s,
      mosi_o => mosi_s,
      miso_i => miso_s,
      cmd_i => cmd_s.req,
      cmd_o => cmd_s.ack,
      rsp_o => rsp_s.req,
      rsp_i => rsp_s.ack
      );

  -- SPI bus: transactor master to memory slave.
  slave_i_s.sck <= sck_s;
  slave_i_s.cs_n <= cs_n_s(0).drain_n;
  slave_i_s.mosi <= nsl_io.io.to_logic(mosi_s);
  miso_s <= nsl_io.io.to_logic(slave_o_s.miso);

  target: nsl_spi.slave.spi_memory_controller
    generic map(
      addr_bytes_c => addr_bytes_c,
      data_bytes_c => data_bytes_c,
      write_opcode_c => x"0b"
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      spi_i => slave_i_s,
      spi_o => slave_o_s,
      cpol_i => '0',
      cpha_i => '0',
      selected_o => open,
      addr_o => mem_addr_s,
      rdata_i => from_suv(ram_rdata_s),
      rready_o => mem_rready_s,
      rvalid_i => mem_rvalid_s,
      wdata_o => mem_wdata_s,
      wvalid_o => mem_wvalid_s
      );

  -- ram_1p has a one-cycle registered read: the data for an accepted read
  -- request is valid the next cycle.
  ram_rvalid: process(clock_s)
  begin
    if rising_edge(clock_s) then
      mem_rvalid_s <= mem_rready_s;
    end if;
  end process;

  -- Big-endian word, matching the read side's from_suv, so a read returns
  -- the written value.
  ram_wdata_s <= std_ulogic_vector(mem_wdata_s(0)) & std_ulogic_vector(mem_wdata_s(1));

  ram: nsl_memory.ram.ram_1p
    generic map(
      addr_size_c => 8*addr_bytes_c,
      data_size_c => 8*data_bytes_c
      )
    port map(
      clock_i => clock_s,
      write_en_i => mem_wvalid_s,
      address_i => mem_addr_s,
      write_data_i => ram_wdata_s,
      read_data_o => ram_rdata_s
      );

  clock_s <= not clock_s after 5 ns;

  reset: process
  begin
    reset_n_s <= '0';
    wait for 42 ns;
    reset_n_s <= '1';
    wait;
  end process;

end architecture;
