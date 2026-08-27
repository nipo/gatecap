library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_bnoc, nsl_coresight, nsl_data, nsl_simulation, gatecap;
use nsl_bnoc.testing.all;
use nsl_coresight.swd.all;
use nsl_coresight.testing.all;
use nsl_data.bytestream.all;
use gatecap.testing.all;

-- swd_adapter against the conformance sequence. This link carries memory
-- accesses of its own, so there is no command frame to build: a stock debug
-- probe -- a DP transactor behind a Mem-AP mapper, driven by the procedures
-- nsl_coresight.testing offers -- writes and reads the conformance payload
-- through the adapter's access port.
entity tb is
end entity;

architecture sim of tb is

  constant ate_clock_period_c : time := 10 ns;
  constant dut_clock_period_c : time := 20 ns;

  -- The DP identity the adapter answers with. A probe that does not find it
  -- is not talking to a gatecap rack.
  constant dp_idr_c : unsigned(31 downto 0) := x"0ba00477";
  -- Mem-AP control/status: 32-bit accesses, address auto-incrementing.
  constant memap_csw_c : unsigned(23 downto 0) := x"800000";

  type framed_io is
  record
    cmd, rsp : nsl_bnoc.framed.framed_bus_t;
  end record;

  signal dp_s, memap_s : framed_io;
  signal master_swd_s : nsl_coresight.swd.swd_master_bus;
  signal slave_swd_s : nsl_coresight.swd.swd_slave_bus;

  signal apb_s : nsl_amba.apb.bus_t;

  signal ate_clock_s, ate_reset_n_s : std_ulogic;
  signal dut_clock_s, dut_reset_n_s : std_ulogic;
  signal done_s : std_ulogic_vector(0 to 0);

  shared variable cmd_q, rsp_q : framed_queue_root;

begin

  slave_swd_s.i <= to_slave(master_swd_s.o);
  master_swd_s.i <= to_master(slave_swd_s.o);

  dut: gatecap.adapter_swd.swd_adapter
    generic map(
      apb_config_c => adapter_apb_config_c,
      descriptor_base_c => adapter_descriptor_base_c
      )
    port map(
      clock_i => dut_clock_s,
      reset_n_i => dut_reset_n_s,
      swd_i => slave_swd_s.i,
      swd_o => slave_swd_s.o,
      apb_o => apb_s.m,
      apb_i => apb_s.s
      );

  completer: nsl_amba.ram.apb_ram
    generic map(
      config_c => adapter_apb_config_c,
      byte_size_l2_c => adapter_completer_size_l2_c
      )
    port map(
      clock_i => dut_clock_s,
      reset_n_i => dut_reset_n_s,
      apb_i => apb_s.m,
      apb_o => apb_s.s
      );

  memap: nsl_coresight.memap_mapper.framed_memap_transactor
    port map(
      clock_i => ate_clock_s,
      reset_n_i => ate_reset_n_s,
      cmd_i => memap_s.cmd.req,
      cmd_o => memap_s.cmd.ack,
      rsp_o => memap_s.rsp.req,
      rsp_i => memap_s.rsp.ack,
      dp_cmd_o => dp_s.cmd.req,
      dp_cmd_i => dp_s.cmd.ack,
      dp_rsp_i => dp_s.rsp.req,
      dp_rsp_o => dp_s.rsp.ack
      );

  dp: nsl_coresight.transactor.dp_framed_transactor
    port map(
      clock_i => ate_clock_s,
      reset_n_i => ate_reset_n_s,
      cmd_i => dp_s.cmd.req,
      cmd_o => dp_s.cmd.ack,
      rsp_o => dp_s.rsp.req,
      rsp_i => dp_s.rsp.ack,
      swd_o => master_swd_s.o,
      swd_i => master_swd_s.i
      );

  stim: process
  begin
    done_s(0) <= '0';
    wait for 100 ns;

    memap_dp_swd_init("swd", cmd_q, rsp_q, dp_idr_c);
    memap_param_set("swd", cmd_q, rsp_q, memap_csw_c, 4);
    memap_write("swd", cmd_q, rsp_q,
                to_unsigned(conformance_address_c, 32), conformance_data_c);
    memap_read_check("swd", cmd_q, rsp_q,
                     to_unsigned(conformance_address_c, 32),
                     conformance_data_c);
    memap_read_check("swd", cmd_q, rsp_q,
                     to_unsigned(conformance_second_word_address, 32),
                     conformance_data_c(conformance_data_c'left + 4
                                        to conformance_data_c'right));

    report "adapter_swd testbench PASSED" severity note;
    done_s(0) <= '1';
    wait;
  end process;

  command_writer: process
    variable data : byte_stream;
  begin
    framed_queue_init(cmd_q);
    loop
      framed_wait(memap_s.cmd.req, memap_s.cmd.ack, ate_clock_s, 1);
      framed_queue_get(cmd_q, data);
      framed_put(memap_s.cmd.req, memap_s.cmd.ack, ate_clock_s, data.all);
      deallocate(data);
    end loop;
  end process;

  response_reader: process
    variable data : byte_stream;
  begin
    framed_queue_init(rsp_q);
    loop
      framed_get(memap_s.rsp.req, memap_s.rsp.ack, ate_clock_s, data);
      framed_queue_put(rsp_q, data.all);
      deallocate(data);
    end loop;
  end process;

  driver: nsl_simulation.driver.simulation_driver
    generic map(
      clock_count => 2,
      reset_count => 2,
      done_count => done_s'length
      )
    port map(
      clock_period(0) => ate_clock_period_c,
      clock_period(1) => dut_clock_period_c,
      reset_duration(0) => 42 ns,
      reset_duration(1) => 42 ns,
      reset_n_o(0) => ate_reset_n_s,
      reset_n_o(1) => dut_reset_n_s,
      clock_o(0) => ate_clock_s,
      clock_o(1) => dut_clock_s,
      done_i => done_s
      );

end architecture;
