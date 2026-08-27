library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_data, nsl_simulation, nsl_amba;
use nsl_data.bytestream.all;
use nsl_data.prbs.all;
use nsl_amba.axi4_stream.all;
use nsl_simulation.logging.all;

entity tb is
end tb;

architecture arch of tb is

  constant cfg_c: config_t := config(1, last => true);

  signal enable_s, ping_s : std_ulogic;
  signal mode_s: unsigned(1 downto 0);
  signal panel_clock_s, panel_reset_n_s: std_ulogic;
  
begin
  
  capture: block is
    constant stream_cfg_c : config_t
      := config(1, last => true);

    signal clock_s, reset_n_s : std_ulogic;

    signal rx_s, tx_s : bus_t;
    signal rx_rdy_s : slave_t;
    signal tx_rsp_s : master_t;
    signal tx_rdy_s : slave_t;

  begin
    net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
      generic map(
        config_c => stream_cfg_c,
        bind_port_c => 4242
        )
      port map(
        clock_i => clock_s,
        reset_n_i => reset_n_s,
        tx_i => tx_s.m,
        tx_o => tx_s.s,
        rx_o => rx_s.m,
        rx_i => rx_s.s
        );

    inst: work.cs.demo
      generic map(
        stream_config_c => stream_cfg_c,
        burst_length_l2_c => 6
        )
      port map(
        clock_i => clock_s,
        reset_n_i => reset_n_s,
        rx_i => rx_s.m,
        rx_o => rx_s.s,
        tx_o => tx_s.m,
        tx_i => tx_s.s,

        panel_clock_i => panel_clock_s,
        panel_reset_n_i => panel_reset_n_s,

        panel_enable_o => enable_s,
        panel_mode_o => mode_s,
        panel_enabled_i => enable_s,
        panel_state_i(3 downto 2) => "00",
        panel_state_i(1 downto 0) => mode_s,
        panel_ping_o => ping_s,
        panel_ping_count_i => ping_s
        );
    
    simdrv: nsl_simulation.driver.simulation_driver
      generic map(
        clock_count => 1,
        reset_count => 1,
        done_count => 1
        )
      port map(
        clock_period(0) => 8 ns,
        reset_duration => (others => 44 ns),
        clock_o(0) => clock_s,
        reset_n_o(0) => reset_n_s,
        done_i => "0"
        );
    
  end block;

  simdrv: nsl_simulation.driver.simulation_driver
    generic map(
      clock_count => 1,
      reset_count => 1,
      done_count => 1
      )
    port map(
      clock_period(0) => 10 ns,
      reset_duration => (others => 32 ns),
      clock_o(0) => panel_clock_s,
      reset_n_o(0) => panel_reset_n_s,
      done_i => "0"
      );
  
end;
