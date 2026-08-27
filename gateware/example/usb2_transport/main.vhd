library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_usb, nsl_memory, nsl_clocking, nsl_hwdep, nsl_bnoc, nsl_spi, nsl_io, nsl_indication, nsl_amba, gatecap;

entity main is
  port (
    usb_dxp_io : inout std_logic;
    usb_dxn_io : inout std_logic;
    usb_rxdp_i : in std_logic;
    usb_rxdn_i : in std_logic;
    usb_pullup_en_o : out std_logic;
    usb_term_dp_io : inout std_logic;
    usb_term_dn_io : inout std_logic;

    s_n_i: in std_ulogic_vector(1 to 2);
    done_led_o: inout std_logic;
    ready_led_o: inout std_logic;
    clk_i: in std_ulogic;

    mspi_sck_o : out std_ulogic;
    mspi_cs_n_o, mspi_mosi_io: inout std_logic;
    mspi_miso_i : in std_ulogic
  );
end main;

architecture arch of main is

  constant clock_ext_s_hz_c : integer := 50e6;
  constant clock_usb_s_hz_c : integer := 60e6;
  constant max_txn_length_l2_c : integer := 5;

  signal usb_o : nsl_usb.io.usb_io_c;
  signal usb_i : nsl_usb.io.usb_io_s;
  signal tx_valid, tx_ready, rx_valid, rx_ready : std_ulogic;
  signal tx_data, rx_data : std_ulogic_vector(7 downto 0);

  signal utmi_s : nsl_usb.utmi.utmi8_bus;

  signal app_reset_n_s, reset_merged_n_s, reset_n_s,
    spi_reset_n_s, gatecap_reset_n_s: std_ulogic;
  signal online : std_ulogic;

  signal clock_usb_s, clock_ext_s : std_ulogic;

  type pipe_io is
  record
    cmd, rsp : nsl_bnoc.pipe.pipe_bus_t;
  end record;

  type framed_io is
  record
    cmd, rsp : nsl_bnoc.framed.framed_bus_t;
  end record;

  signal spi_pipe_s: pipe_io;
  signal gatecap_pipe_s: pipe_io;
  signal gatecap_framed_s: framed_io;
  signal spi_framed_s: framed_io;

  -- SPI wires
  signal sck_s : std_ulogic;
  signal cs_n_s : nsl_io.io.opendrain_vector(0 to 0);
  signal mosi_s : nsl_io.io.tristated;
  signal miso_s : std_ulogic;

begin

  reset_merged_n_s <= s_n_i(1);

  clock_ext_buffer: nsl_hwdep.clock.clock_buffer
    port map(
      clock_i => clk_i,
      clock_o => clock_ext_s
      );

  pll: nsl_clocking.pll.pll_basic
    generic map(
      input_hz_c => clock_ext_s_hz_c,
      output_hz_c => clock_usb_s_hz_c,
      hw_variant_c => "ice40(out=global,in=core)"
      )
    port map(
      clock_i => clock_ext_s,
      reset_n_i => reset_merged_n_s,

      clock_o => clock_usb_s,
      locked_o => reset_n_s
      );
  
  hs_phy: work.softphy.gw_usb2_phy
    port map(
      clock_i => clock_usb_s,
      reset_n_i => reset_n_s,

      usb_dxp_io => usb_dxp_io,
      usb_dxn_io => usb_dxn_io,
      usb_rxdp_i => usb_rxdp_i,
      usb_rxdn_i => usb_rxdn_i,
      usb_pullup_en_o => usb_pullup_en_o,
      usb_term_dp_io => usb_term_dp_io,
      usb_term_dn_io => usb_term_dn_io,

      utmi_i => utmi_s.sie2phy,
      utmi_o => utmi_s.phy2sie
      );

  func: nsl_usb.func.serial_port_dual
    generic map(
      vendor_id_c => x"dead",
      product_id_c => x"beef",
      device_version_c => x"0100",
      manufacturer_c => "Nipo",
      product_c => "Gatecap example",
      serial_c => "lol",
      hs_supported_c => true,
      phy_clock_rate_c => clock_usb_s_hz_c,
      self_powered_c => false
      )
    port map(
      phy_system_o => utmi_s.sie2phy.system,
      phy_system_i => utmi_s.phy2sie.system,
      phy_data_o => utmi_s.sie2phy.data,
      phy_data_i => utmi_s.phy2sie.data,

      reset_n_i => reset_n_s,

      app_reset_n_o => app_reset_n_s,
      online_o => online,

      rx0_o => spi_pipe_s.cmd.req,
      rx0_i => spi_pipe_s.cmd.ack,
      tx0_i => spi_pipe_s.rsp.req,
      tx0_o => spi_pipe_s.rsp.ack,
      
      rx1_o => gatecap_pipe_s.cmd.req,
      rx1_i => gatecap_pipe_s.cmd.ack,
      tx1_i => gatecap_pipe_s.rsp.req,
      tx1_o => gatecap_pipe_s.rsp.ack
      );

  spi: block is
    signal spi_relax_s: framed_io;
  begin
    spi_unchunker: nsl_bnoc.chunked_link.framed_unchunker
      port map(
        reset_n_i => app_reset_n_s,
        clock_i => clock_usb_s,

        in_i => spi_pipe_s.cmd.req,
        in_o => spi_pipe_s.cmd.ack,

        reset_n_o => spi_reset_n_s,
        
        out_o => spi_framed_s.cmd.req,
        out_i => spi_framed_s.cmd.ack
        );

    cmd_relax: nsl_bnoc.framed.framed_fifo
      generic map(
        depth => 512,
        clk_count => 1
        )
      port map(
        p_resetn => spi_reset_n_s,
        p_clk(0) => clock_usb_s,
        p_in_val => spi_framed_s.cmd.req,
        p_in_ack => spi_framed_s.cmd.ack,
        p_out_val => spi_relax_s.cmd.req,
        p_out_ack => spi_relax_s.cmd.ack
        );
    
    spi_transactor: nsl_spi.transactor.spi_framed_transactor
      generic map(
        slave_count_c => 1
        )
      port map(
        clock_i => clock_usb_s,
        reset_n_i => spi_reset_n_s,
        sck_o => sck_s,
        cs_n_o => cs_n_s,
        mosi_o => mosi_s,
        miso_i => miso_s,
        cmd_i => spi_relax_s.cmd.req,
        cmd_o => spi_relax_s.cmd.ack,
        rsp_o => spi_relax_s.rsp.req,
        rsp_i => spi_relax_s.rsp.ack
        );

    rsp_relax: nsl_bnoc.framed.framed_fifo
      generic map(
        depth => 512,
        clk_count => 1
        )
      port map(
        p_resetn => spi_reset_n_s,
        p_clk(0) => clock_usb_s,
        p_in_val => spi_relax_s.rsp.req,
        p_in_ack => spi_relax_s.rsp.ack,
        p_out_val => spi_framed_s.rsp.req,
        p_out_ack => spi_framed_s.rsp.ack
        );

    spi_chunker: nsl_bnoc.chunked_link.framed_chunker
      generic map(
        max_txn_length_l2_c => max_txn_length_l2_c
        )
      port map(
        reset_n_i => spi_reset_n_s,
        clock_i => clock_usb_s,

        in_i => spi_framed_s.rsp.req,
        in_o => spi_framed_s.rsp.ack,

        out_o => spi_pipe_s.rsp.req,
        out_i => spi_pipe_s.rsp.ack
        );

  end block;

  mspi_sck_o <= sck_s;
  mosi_driver: nsl_io.io.tristated_io_driver
    port map(
      io_io => mspi_mosi_io,
      v_i => mosi_s);
  miso_s <= mspi_miso_i;
  csn_driver: nsl_io.io.opendrain_io_driver
    port map(
      io_io => mspi_cs_n_o,
      v_i => cs_n_s(0));
  
  ready_led_o <= online;
  
  monitor: nsl_indication.activity.activity_blinker
    generic map(
      clock_hz_c => real(clock_ext_s_hz_c)
      )
    port map(
      reset_n_i => reset_merged_n_s,
      clock_i => clock_ext_s,
      activity_i => spi_framed_s.cmd.req.valid,
      led_o => done_led_o
      );
  
  gatecap_unchunker: nsl_bnoc.chunked_link.framed_unchunker
    port map(
      reset_n_i => app_reset_n_s,
      clock_i => clock_usb_s,

      in_i => gatecap_pipe_s.cmd.req,
      in_o => gatecap_pipe_s.cmd.ack,

      reset_n_o => gatecap_reset_n_s,

      out_o => gatecap_framed_s.cmd.req,
      out_i => gatecap_framed_s.cmd.ack
      );

  gatecap_chunker: nsl_bnoc.chunked_link.framed_chunker
    generic map(
      max_txn_length_l2_c => max_txn_length_l2_c
      )
    port map(
      reset_n_i => gatecap_reset_n_s,
      clock_i => clock_usb_s,

      in_i => gatecap_framed_s.rsp.req,
      in_o => gatecap_framed_s.rsp.ack,

      out_o => gatecap_pipe_s.rsp.req,
      out_i => gatecap_pipe_s.rsp.ack
      );

  cap: block is
    use nsl_amba.axi4_stream.all;
    constant cfg_c : config_t := config(1, last => true);
    signal cap_cmd_s, cap_rsp_s: bus_t;
  begin
    rsp_adapter: nsl_bnoc.axi_adapter.framed_to_axi4_stream
      port map(
        clock_i => clock_usb_s,
        reset_n_i => gatecap_reset_n_s,
        framed_i => gatecap_framed_s.cmd.req,
        framed_o => gatecap_framed_s.cmd.ack,
        axi_o => cap_cmd_s.m,
        axi_i => cap_cmd_s.s
        );

    cmd_adapter: nsl_bnoc.axi_adapter.axi4_stream_to_framed
      port map(
        clock_i => clock_usb_s,
        reset_n_i => gatecap_reset_n_s,
        axi_i => cap_rsp_s.m,
        axi_o => cap_rsp_s.s,
        framed_o => gatecap_framed_s.rsp.req,
        framed_i => gatecap_framed_s.rsp.ack
        );

    capture: work.spi_probe.spi_usb2_capture
      generic map(
        stream_config_c => cfg_c,
        burst_length_l2_c => 8
        )
      port map(
        reset_n_i => gatecap_reset_n_s,
        rx_i => cap_cmd_s.m,
        rx_o => cap_cmd_s.s,
        tx_o => cap_rsp_s.m,
        tx_i => cap_rsp_s.s,

        la_spi_clock_i => clock_usb_s,
        la_spi_reset_n_i => gatecap_reset_n_s,

        la_spi_sck_i => sck_s,
        la_spi_cs_n_i => cs_n_s(0).drain_n,
        la_spi_mosi_i => nsl_io.io.to_logic(mosi_s),
        la_spi_miso_i => miso_s,
        la_spi_command_i => spi_framed_s.cmd,
        la_spi_response_i => spi_framed_s.rsp
        );
  end block;

end arch;
