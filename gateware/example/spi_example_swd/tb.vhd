library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_bnoc, nsl_simulation, nsl_spi, nsl_io,
  nsl_memory, nsl_data, nsl_coresight;
use nsl_amba.axi4_stream.all;
use nsl_data.bytestream.all;
use nsl_coresight.swd.all;
library gatecap_generated;
use gatecap_generated.spi_probe.all;

-- A generated capture core reached over SWD, probing an SPI link.
--
-- Two UDP servers bracket the platform:
--
--   udp:4249 -- framed DP transactor commands -- dp_framed_transactor --SWD--+
--                                                                            |
--                        spi_swd_capture (its own DP and Mem-AP) ------------+
--                            | probes: SPI wires, and the transactor's
--                            | command/response framed buses whole
--   udp:4250 -- framed SPI transactor commands -- spi_transactor --SPI--> memory
--
-- The host drives capture and SPI traffic through two separate connections.
-- The capture core carries a whole debug port, so it is reached the way any
-- debug target is:
--
--   udp/127.0.0.1:4249/nsl_swd(fin=100M)/dp(ap_probe=0)
--
-- so a trace of one SPI transaction is
--
--   acrobe gatecap -r <that path> capture spi.control \
--     --trigger cs_n=falling --count 4096 --pretrigger 64 -o trace.csv
--
-- The core's access port carries an identification register of its own, which
-- is what the host matches to know it found a capture core rather than a CPU.
--
-- UDP port 4250, reached as udp/127.0.0.1:4250/nsl_spi/cs0, carries the framed
-- SPI transactor commands that produce the traffic being probed.
--
-- Neither server ever terminates: the simulation runs until killed.
entity tb is
end entity;

architecture sim of tb is

  -- SPI wires
  signal sck_s : std_ulogic;
  signal cs_n_s : nsl_io.io.opendrain_vector(0 to 0);
  signal mosi_s : nsl_io.io.tristated;
  signal miso_s : std_ulogic;

  signal master_swd_s : nsl_coresight.swd.swd_master_bus;
  signal slave_swd_s : nsl_coresight.swd.swd_slave_bus;

begin

  slave_swd_s.i <= to_slave(master_swd_s.o);
  master_swd_s.i <= to_master(slave_swd_s.o);

  swd_transactor: block is
    constant clock_period_c : time := 10 ns;
    signal clock_s : std_ulogic;
    signal reset_n_s : std_ulogic;

    constant cfg_c : config_t := nsl_bnoc.axi_adapter.axi4_stream_framed_config_c;
    signal udp_rx_s, udp_tx_s: nsl_amba.axi4_stream.bus_t;

    type framed_io is
    record
      cmd, rsp: nsl_bnoc.framed.framed_bus_t;
    end record;

    signal dp_s : framed_io;
  begin
    
    net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
      generic map(
        config_c => cfg_c,
        bind_port_c => 4249
        )
      port map(
        clock_i => clock_s,
        reset_n_i => reset_n_s,

        tx_i => udp_tx_s.m,
        tx_o => udp_tx_s.s,

        rx_o => udp_rx_s.m,
        rx_i => udp_rx_s.s
        );

    cmd_adapter: nsl_bnoc.axi_adapter.axi4_stream_to_framed
      port map(
        clock_i => clock_s,
        reset_n_i => reset_n_s,
        axi_i => udp_rx_s.m,
        axi_o => udp_rx_s.s,
        framed_o => dp_s.cmd.req,
        framed_i => dp_s.cmd.ack
        );

    rsp_adapter: nsl_bnoc.axi_adapter.framed_to_axi4_stream
      port map(
        clock_i => clock_s,
        reset_n_i => reset_n_s,
        framed_i => dp_s.rsp.req,
        framed_o => dp_s.rsp.ack,
        axi_o => udp_tx_s.m,
        axi_i => udp_tx_s.s
        );
    
    dp: nsl_coresight.transactor.dp_framed_transactor
      port map(
        clock_i  => clock_s,
        reset_n_i => reset_n_s,
        
        cmd_i => dp_s.cmd.req,
        cmd_o => dp_s.cmd.ack,
        rsp_o => dp_s.rsp.req,
        rsp_i => dp_s.rsp.ack,

        swd_o => master_swd_s.o,
        swd_i => master_swd_s.i
        );

    driver: nsl_simulation.driver.simulation_driver
      generic map(
        clock_count => 1,
        reset_count => 1,
        done_count => 1
        )
      port map(
        clock_period(0) => clock_period_c,
        reset_duration(0) => 42 ns,
        reset_n_o(0) => reset_n_s,
        clock_o(0) => clock_s,
        done_i => "0"
        );
  end block;

  spi_transactor: block is
    signal clock_s : std_ulogic := '0';
    signal reset_n_s : std_ulogic := '0';

    constant cfg_c : config_t := nsl_bnoc.axi_adapter.axi4_stream_framed_config_c;

    -- SPI bridge (udp 4250)
    signal spi_cmd_m : master_t;
    signal spi_cmd_s : slave_t;
    signal spi_rsp_m : master_t;
    signal spi_rsp_s : slave_t;

    -- Framed command/response. A framed FIFO on each side buffers a whole
    -- frame so the transactor -- which interleaves command consumption and
    -- response emission -- never stalls on the half-duplex UDP transport.
    signal ada_cmd_s : nsl_bnoc.framed.framed_bus_t;  -- adapter -> cmd fifo
    signal fifo_rsp_s : nsl_bnoc.framed.framed_bus_t; -- rsp fifo -> adapter

    signal cmd_s : nsl_bnoc.framed.framed_bus_t;      -- cmd fifo -> transactor
    signal rsp_s : nsl_bnoc.framed.framed_bus_t;      -- transactor -> rsp fifo
  begin
    -- Capture core, wired to the SWD pins the probe drives. Everything
    -- between those two wires and the core's register map -- the debug port,
    -- the access port and the bridge down to APB -- is inside the core.
    capture: spi_swd_capture
      port map(
        reset_n_i => reset_n_s,
        swd_i => slave_swd_s.i,
        swd_o => slave_swd_s.o,

        la_spi_clock_i => clock_s,
        la_spi_reset_n_i => reset_n_s,
        la_spi_sck_i => sck_s,
        la_spi_cs_n_i => cs_n_s(0).drain_n,
        la_spi_mosi_i => nsl_io.io.to_logic(mosi_s),
        la_spi_miso_i => miso_s,
        la_spi_command_i => cmd_s,
        la_spi_response_i => rsp_s
        );

    spi_net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
      generic map(
        config_c => cfg_c,
        bind_port_c => 4250
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

    simdrv: nsl_simulation.driver.simulation_driver
      generic map(
        clock_count => 1,
        reset_count => 1,
        done_count => 1
        )
      port map(
        clock_period(0) => 10 ns,
        reset_duration => (others => 32 ns),
        clock_o(0) => clock_s,
        reset_n_o(0) => reset_n_s,
        done_i => "0"
        );
  end block;

  spi_target: block is
    constant addr_bytes_c : natural := 2;
    constant data_bytes_c : natural := 2;

    signal slave_i_s : nsl_spi.spi.spi_slave_i;
    signal slave_o_s : nsl_spi.spi.spi_slave_o;

    -- Memory target
    signal mem_addr_s : unsigned(addr_bytes_c*8-1 downto 0);
    signal mem_wdata_s : byte_string(0 to data_bytes_c-1);
    signal mem_wvalid_s : std_ulogic;
    signal mem_rready_s, mem_rvalid_s : std_ulogic;
    signal ram_wdata_s, ram_rdata_s : std_ulogic_vector(8*data_bytes_c-1 downto 0);

    signal clock_s : std_ulogic := '0';
    signal reset_n_s : std_ulogic := '0';
  begin
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

    simdrv: nsl_simulation.driver.simulation_driver
      generic map(
        clock_count => 1,
        reset_count => 1,
        done_count => 1
        )
      port map(
        clock_period(0) => 20 ns,
        reset_duration => (others => 32 ns),
        clock_o(0) => clock_s,
        reset_n_o(0) => reset_n_s,
        done_i => "0"
        );
  end block;

end architecture;
