library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_data, nsl_simulation, nsl_amba;
library gatecap_generated;
use nsl_data.bytestream.all;
use nsl_data.prbs.all;
use nsl_amba.axi4_stream.all;
use nsl_simulation.logging.all;

entity tb is
end tb;

architecture arch of tb is

  constant cfg_c: config_t := config(1, last => true);

  signal gen_clock_s, gen_reset_n_s : std_ulogic;
  signal lb_clock_s, lb_reset_n_s : std_ulogic;

  signal gen_tx_s, lb_rx_s, lb_tx_s, gen_rx_s : bus_t;
  shared variable gen_tx_q, gen_rx_q : frame_queue_root_t;

begin

  generator: process
    variable state_v : prbs_state(30 downto 0) := x"7777777"&"111";
  begin
    frame_queue_init(gen_tx_q);
    frame_queue_init(gen_rx_q);

    wait for 100 ns;

    loop
      log_info("Sending frame...");
      frame_queue_check_io(
        root_master => gen_tx_q, 
        root_slave  => gen_rx_q, 
        data => prbs_byte_string(state_v, prbs31, 32),
        timeout => 0 ps);
      state_v := prbs_forward(state_v, prbs31, 32 * 8);

      wait for 1 ms;
    end loop;

    wait;
  end process;

  gen_tx: process is
  begin
    gen_tx_s.m <= transfer_defaults(cfg_c);
    wait for 40 ns;
    frame_queue_master(cfg_c, gen_tx_q, gen_clock_s, gen_tx_s.s, gen_tx_s.m);
  end process;

--  gen_tx_dump: nsl_amba.axi4_stream.axi4_stream_dumper
--    generic map(
--      config_c => cfg_c,
--      prefix_c => "gen_tx"
--      )
--    port map(
--      clock_i => gen_clock_s,
--      reset_n_i => gen_reset_n_s,
--
--      bus_i => gen_tx_s
--      );

  gen2lb: nsl_amba.stream_fifo.axi4_stream_fifo
    generic map(
      depth_c => 16,
      config_c => cfg_c,
      clock_count_c => 2
      )
    port map(
      clock_i(0) => gen_clock_s,
      clock_i(1) => lb_clock_s,
      reset_n_i => gen_reset_n_s,

      in_i => gen_tx_s.m,
      in_o => gen_tx_s.s,

      out_o => lb_rx_s.m,
      out_i => lb_rx_s.s
      );

--  lb_rx_dump: nsl_amba.axi4_stream.axi4_stream_dumper
--    generic map(
--      config_c => cfg_c,
--      prefix_c => "lb_rx"
--      )
--    port map(
--      clock_i => lb_clock_s,
--      reset_n_i => lb_reset_n_s,
--
--      bus_i => lb_rx_s
--      );

  lb: nsl_amba.stream_fifo.axi4_stream_fifo_atomic
    generic map(
      depth_c => 64,
      config_c => cfg_c,
      clk_count_c => 1
      )
    port map(
      clock_i(0) => lb_clock_s,
      reset_n_i => lb_reset_n_s,

      in_i => lb_rx_s.m,
      in_o => lb_rx_s.s,

      out_o => lb_tx_s.m,
      out_i => lb_tx_s.s
      );

--  lb_tx_dump: nsl_amba.axi4_stream.axi4_stream_dumper
--    generic map(
--      config_c => cfg_c,
--      prefix_c => "lb_tx"
--      )
--    port map(
--      clock_i => lb_clock_s,
--      reset_n_i => lb_reset_n_s,
--
--      bus_i => lb_tx_s
--      );

  lb2gen: nsl_amba.stream_fifo.axi4_stream_fifo
    generic map(
      depth_c => 16,
      config_c => cfg_c,
      clock_count_c => 2
      )
    port map(
      clock_i(0) => lb_clock_s,
      clock_i(1) => gen_clock_s,
      reset_n_i => gen_reset_n_s,

      in_i => lb_tx_s.m,
      in_o => lb_tx_s.s,

      out_o => gen_rx_s.m,
      out_i => gen_rx_s.s
      );

--  gen_rx_dump: nsl_amba.axi4_stream.axi4_stream_dumper
--    generic map(
--      config_c => cfg_c,
--      prefix_c => "gen_rx"
--      )
--    port map(
--      clock_i => gen_clock_s,
--      reset_n_i => gen_reset_n_s,
--
--      bus_i => gen_rx_s
--      );
  
  gen_rx: process is
  begin
    gen_rx_s.s <= accept(cfg_c, false);
    wait for 40 ns;
    frame_queue_slave(cfg_c, gen_rx_q, gen_clock_s, gen_rx_s.m, gen_rx_s.s);
  end process;

  simdrv: nsl_simulation.driver.simulation_driver
    generic map(
      clock_count => 2,
      reset_count => 2,
      done_count => 1
      )
    port map(
      clock_period(0) => 10 ns,
      clock_period(1) => 5 ns,
      reset_duration => (others => 44 ns),
      clock_o(0) => gen_clock_s,
      clock_o(1) => lb_clock_s,
      reset_n_o(0) => gen_reset_n_s,
      reset_n_o(1) => lb_reset_n_s,
      done_i => "0"
      );
  
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

    inst: gatecap_generated.multidomain.multidomain_capture
      generic map(
        stream_config_c => stream_cfg_c,
        burst_length_l2_c => 6,
        la_gen_tx_config_c => cfg_c,
        la_gen_rx_config_c => cfg_c,
        la_lb_tx_config_c => cfg_c,
        la_lb_rx_config_c => cfg_c
        )
      port map(
        clock_i => clock_s,
        reset_n_i => reset_n_s,
        rx_i => rx_s.m,
        rx_o => rx_s.s,
        tx_o => tx_s.m,
        tx_i => tx_s.s,

        la_gen_clock_i => gen_clock_s,
        la_gen_reset_n_i => gen_reset_n_s,
        la_gen_tx_i => gen_tx_s,
        la_gen_rx_i => gen_rx_s,

        la_lb_clock_i => lb_clock_s,
        la_lb_reset_n_i => lb_reset_n_s,
        la_lb_tx_i => lb_tx_s,
        la_lb_rx_i => lb_rx_s
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

end;
