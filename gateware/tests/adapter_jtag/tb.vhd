library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_bnoc, nsl_clocking, nsl_data, nsl_jtag, nsl_simulation,
  gatecap;
use nsl_bnoc.testing.all;
use nsl_data.bytestream.all;
use nsl_data.endian.all;
use nsl_jtag.continuous_transport.all;
use nsl_jtag.jtag.all;
use nsl_jtag.transactor.all;
use nsl_simulation.assertions.all;
use gatecap.testing.all;

-- jtag_adapter against the conformance sequence.
--
-- The simulation master is a JTAG ATE shifting a simulated chain, with the
-- host half of nsl_jtag.continuous_transport on top of it: each batch is one
-- Shift-DR run carrying a TX budget grant, the command frame when the TAP's
-- advertised RX credit fits it, and idle padding leaving the TAP room to
-- answer in. Responses come back as protocol bytes on TDO and are reassembled
-- into frames.
--
-- The adapter's chip pins stay unbound: its TAP primitive is the simulation
-- one, which takes its register interface from the chain below rather than
-- from the boundary.
entity tb is
end entity;

architecture sim of tb is

  -- Hijacked from a Lattice ECP-5, as the socket examples do.
  constant idcode_c : std_ulogic_vector(31 downto 0) := x"41111043";
  constant idcode_instruction_c : std_ulogic_vector(7 downto 0) := x"e0";
  constant user0_instruction_c : std_ulogic_vector(7 downto 0) := x"32";

  -- TX budget granted per batch, and the idle bytes left after the command:
  -- both directions need room for the longest response, the identify reply.
  constant tx_budget_c : natural := 200;
  constant idle_count_c : natural := 128;
  -- A batch is a Shift-DR run; a response that needs more than this many of
  -- them is a stuck link, not a slow one.
  constant batch_max_c : natural := 64;

  signal ate_clock_s, ate_reset_n_s : std_ulogic;
  signal dut_clock_s, dut_reset_n_s : std_ulogic;
  signal done_s : std_ulogic_vector(0 to 0);

  signal ate_o : nsl_jtag.jtag.jtag_ate_o;
  signal ate_i : nsl_jtag.jtag.jtag_ate_i;
  signal tap_o : nsl_jtag.jtag.jtag_tap_o;
  signal tap_i : nsl_jtag.jtag.jtag_tap_i;

  signal ate_cmd_s, ate_rsp_s : nsl_bnoc.framed.framed_bus_t;
  signal apb_s : nsl_amba.apb.bus_t;

  shared variable command_q, response_q : framed_queue_root;

  -- Preamble and SOF in wire-bit order: what a batch opens with.
  function sync_pattern return std_ulogic_vector is
  begin
    return std_ulogic_vector(from_le(byte_string'(x"55", x"d5")));
  end function;

  -- First wire-bit index after the preamble-to-SOF transition in v, or -1.
  function find_sof(v : std_ulogic_vector; len : integer) return integer is
    constant pat : std_ulogic_vector := sync_pattern;
    variable ok : boolean;
  begin
    for k in 0 to len - pat'length loop
      ok := true;
      for j in 0 to pat'length - 1 loop
        if v(k + j) /= pat(j) then
          ok := false;
        end if;
      end loop;
      if ok then
        return k + pat'length;
      end if;
    end loop;
    return -1;
  end function;

  -- Byte at wire-bit position pos, LSB first.
  function byte_at(v : std_ulogic_vector; pos : integer) return byte is
    variable b : byte;
  begin
    for j in 0 to 7 loop
      b(j) := v(pos + j);
    end loop;
    return b;
  end function;

begin

  ate_i <= transport to_ate(tap_o);
  tap_i <= transport to_tap(ate_o);

  -- The chain the ATE shifts. It hands the selected user register over to
  -- whichever block claims it inside the design -- here, the adapter's TAP.
  tap: nsl_simulation.jtag.jtag_sim_tap
    generic map(
      idcode_c => idcode_c,
      idcode_instruction_c => idcode_instruction_c,
      user0_instruction_c => user0_instruction_c
      )
    port map(
      tck_i => tap_i.tck,
      tms_i => tap_i.tms,
      tdi_i => tap_i.tdi,
      tdo_o => tap_o.tdo.v
      );
  tap_o.tdo.en <= '1';
  tap_o.rtck <= '0';

  ate: nsl_jtag.transactor.framed_ate
    port map(
      clock_i => ate_clock_s,
      reset_n_i => ate_reset_n_s,
      cmd_i => ate_cmd_s.req,
      cmd_o => ate_cmd_s.ack,
      rsp_o => ate_rsp_s.req,
      rsp_i => ate_rsp_s.ack,
      jtag_o => ate_o,
      jtag_i => ate_i,
      system_reset_n_o => open
      );

  dut: gatecap.adapter_jtag.jtag_adapter
    generic map(
      apb_config_c => adapter_apb_config_c,
      burst_length_l2_c => adapter_burst_length_l2_c,
      descriptor_base_c => adapter_descriptor_base_c
      )
    port map(
      clock_i => dut_clock_s,
      reset_n_i => dut_reset_n_s,
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

  host: process
    -- The response frame being reassembled, and it once complete.
    variable partial : byte_stream := null;
    variable response : byte_stream := null;
    variable complete : boolean;
    -- Free space in the TAP's RX FIFO, as its last credit frame stated it.
    variable credit : integer := 0;
    variable batch : natural;

    procedure do_io(rsp_data : out byte_stream; command : in byte_string) is
      variable r : byte_stream;
    begin
      framed_queue_put(command_q, command);
      framed_queue_get(response_q, r);
      rsp_data := r;
    end procedure;

    procedure chain_reset(div : integer range 1 to 256) is
      variable r : byte_stream;
    begin
      do_io(r, cmd_reset(5) & cmd_divisor(div) & cmd_reset(5) & cmd_run(1));
      deallocate(r);
    end procedure;

    procedure ir_set(ir : std_ulogic_vector) is
      variable command, r : byte_stream;
    begin
      command := null;
      write(command, cmd_capture_ir);
      write(command, cmd_shift(ir, false));
      write(command, cmd_run(1));
      do_io(r, command.all);
      rsp(r);
      rsp_shift(r, ir'length);
      rsp(r);
      deallocate(command);
      deallocate(r);
    end procedure;

    -- Prefix preamble and SOF to protocol_data, shift the batch, and return
    -- every byte after the TAP's own SOF on TDO.
    procedure exchange(protocol_data : in byte_string;
                       rx_bytes : out byte_stream) is
      constant framing_c : byte_string := (x"55", x"55", x"d5");
      constant payload_c : byte_string := framing_c & protocol_data;
      constant command_c : byte_string :=
        cmd_capture_dr
        & cmd_shift(std_ulogic_vector(from_le(payload_c)), true)
        & cmd_run(1);
      variable tdo_bits : std_ulogic_vector(payload_c'length * 8 - 1 downto 0);
      variable r : byte_stream;
      variable sof, pos : integer;
      variable acc : byte_stream := null;
    begin
      do_io(r, command_c);
      rsp(r);
      rsp_shift(r, tdo_bits);
      rsp(r);
      deallocate(r);

      sof := find_sof(tdo_bits, tdo_bits'length);
      if sof >= 0 then
        pos := sof;
        while pos + 8 <= tdo_bits'length loop
          write(acc, byte_at(tdo_bits, pos));
          pos := pos + 8;
        end loop;
      end if;
      rx_bytes := acc;
    end procedure;

    -- One frame as data frames, split at the protocol's 64-byte maximum; the
    -- last one carries the end-of-packet bit.
    procedure append_data_frames(stream : inout byte_stream;
                                 packet : in byte_string) is
      variable offset : integer := 0;
      variable remaining : integer := packet'length;
      variable chunk : integer;
      variable last_bit : std_ulogic;
      variable header : byte;
    begin
      while remaining > 0 loop
        chunk := remaining;
        if chunk > data_bytes_max_c then
          chunk := data_bytes_max_c;
        end if;
        if chunk = remaining then
          last_bit := '1';
        else
          last_bit := '0';
        end if;
        header := "0" & last_bit & std_ulogic_vector(to_unsigned(chunk - 1, 6));
        write(stream, header);
        write(stream, packet(packet'left + offset
                             to packet'left + offset + chunk - 1));
        offset := offset + chunk;
        remaining := remaining - chunk;
      end loop;
    end procedure;

    -- Walk protocol bytes: accumulate data into partial, publish a complete
    -- frame, and follow the TAP's credit. Control frames truncated by the end
    -- of a batch are skipped -- they carry absolute state, so the next one
    -- restates it.
    procedure deframe(data : in byte_string) is
      variable pos : integer := data'left;
      variable header : byte;
      variable length : integer;
      variable whole : boolean;
    begin
      while pos <= data'right loop
        header := data(pos);
        pos := pos + 1;
        if std_match(header, data_header_mask_c) then
          length := to_integer(unsigned(header(5 downto 0))) + 1;
          whole := true;
          for i in 0 to length - 1 loop
            if pos > data'right then
              whole := false;
              exit;
            end if;
            write(partial, data(pos));
            pos := pos + 1;
          end loop;
          if whole and header(hdr_last_bit_c) = '1' then
            response := partial;
            partial := null;
            complete := true;
          end if;
        elsif header = ctl_credit_c then
          if pos + 1 <= data'right then
            credit := to_integer(unsigned(data(pos)))
                      + 256 * to_integer(unsigned(data(pos + 1)));
          end if;
          pos := pos + 2;
        elsif header = ctl_tx_level_c then
          pos := pos + 2;
        else
          null;
        end if;
      end loop;
    end procedure;

    -- Push one command frame and collect the response, one batch at a time:
    -- the TAP opens a batch with no TX budget and grants RX credit as it goes,
    -- so neither direction fits in a single Shift-DR run by construction.
    procedure transact(command : in byte_string; reply : out byte_stream) is
      variable protocol : byte_stream;
      variable received : byte_stream;
      variable sent : boolean := false;
    begin
      complete := false;
      batch := 0;
      while not complete loop
        protocol := null;
        write(protocol, byte_string'(0 => ctl_credit_c)
              & to_le(to_unsigned(tx_budget_c, 16)));
        if not sent and credit >= command'length then
          append_data_frames(protocol, command);
          credit := credit - command'length;
          sent := true;
        end if;
        for i in 0 to idle_count_c - 1 loop
          write(protocol, ctl_idle_c);
        end loop;

        exchange(protocol.all, received);
        deallocate(protocol);
        if received /= null then
          deframe(received.all);
          deallocate(received);
        end if;

        batch := batch + 1;
        assert batch < batch_max_c
          report "no response after " & integer'image(batch_max_c) & " batches"
          severity failure;
      end loop;
      reply := response;
      response := null;
    end procedure;

    variable reply : byte_stream;
  begin
    done_s(0) <= '0';
    framed_queue_init(command_q);
    framed_queue_init(response_q);

    wait for 100 ns;

    chain_reset(3);
    ir_set(user0_instruction_c);

    for step in 0 to conformance_step_count_c - 1 loop
      transact(conformance_command(step), reply);
      assert_equal("jtag", "response frame",
                   reply.all, conformance_response(step), failure);
      deallocate(reply);
    end loop;

    report "adapter_jtag testbench PASSED" severity note;
    done_s(0) <= '1';
    wait;
  end process;

  command_writer: process
    variable data : byte_stream;
  begin
    ate_cmd_s.req <= nsl_bnoc.framed.framed_req_idle_c;
    wait for 40 ns;
    framed_queue_master_worker(ate_cmd_s.req, ate_cmd_s.ack, ate_clock_s,
                               command_q);
  end process;

  response_reader: process
  begin
    ate_rsp_s.ack <= nsl_bnoc.framed.framed_ack_idle_c;
    wait for 40 ns;
    framed_queue_slave_worker(ate_rsp_s.req, ate_rsp_s.ack, ate_clock_s,
                              response_q);
  end process;

  driver: nsl_simulation.driver.simulation_driver
    generic map(
      clock_count => 2,
      reset_count => 2,
      done_count => done_s'length
      )
    port map(
      clock_period(0) => 8 ns,
      clock_period(1) => 10 ns,
      reset_duration(0) => 42 ns,
      reset_duration(1) => 42 ns,
      reset_n_o(0) => ate_reset_n_s,
      reset_n_o(1) => dut_reset_n_s,
      clock_o(0) => ate_clock_s,
      clock_o(1) => dut_clock_s,
      done_i => done_s
      );

end architecture;
