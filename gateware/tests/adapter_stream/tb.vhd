library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, gatecap;
use nsl_amba.axi4_stream.all;
use nsl_data.bytestream.all;
use gatecap.testing.all;

-- adapter_stream against the conformance sequence: the command frames go
-- straight onto the link, one byte per beat, since the link is the byte
-- stream the bridge speaks.
entity tb is
end entity;

architecture sim of tb is

  constant stream_config_c : config_t := config(bytes => 1, last => true);

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal cmd_s : nsl_amba.axi4_stream.bus_t;
  signal rsp_s : nsl_amba.axi4_stream.bus_t;
  signal apb_s : nsl_amba.apb.bus_t;

begin

  dut: gatecap.adapter_stream.stream_adapter
    generic map(
      apb_config_c => adapter_apb_config_c,
      stream_config_c => stream_config_c,
      burst_length_l2_c => adapter_burst_length_l2_c,
      descriptor_base_c => adapter_descriptor_base_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      rx_i => cmd_s.m,
      rx_o => cmd_s.s,
      tx_o => rsp_s.m,
      tx_i => rsp_s.s,
      apb_o => apb_s.m,
      apb_i => apb_s.s
      );

  completer: nsl_amba.ram.apb_ram
    generic map(
      config_c => adapter_apb_config_c,
      byte_size_l2_c => adapter_completer_size_l2_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_s.m,
      apb_o => apb_s.s
      );

  clock_gen: process
  begin
    while not done_s loop
      clock_s <= '0';
      wait for 5 ns;
      clock_s <= '1';
      wait for 5 ns;
    end loop;
    wait;
  end process;

  stim: process
  begin
    cmd_s.m <= transfer_defaults(stream_config_c);
    rsp_s.s <= accept(stream_config_c, false);
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);

    for step in 0 to conformance_step_count_c - 1 loop
      packet_send(stream_config_c, clock_s, cmd_s.s, cmd_s.m,
                  conformance_command(step));
      packet_check(stream_config_c, clock_s, rsp_s.m, rsp_s.s,
                   conformance_response(step));
    end loop;

    report "adapter_stream testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
