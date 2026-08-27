library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, gatecap;
use nsl_data.bytestream.all;
use nsl_data.endian.all;
use gatecap.descriptor.all;

-- Conformance harness for the communication adapters.
--
-- Every adapter presents the same inner contract -- an APB requester of a
-- given configuration, a clock and a reset, and a descriptor base it
-- publishes -- so one sequence of accesses exercises all of them. The
-- sequence is stated here once, in the two shapes the links come in: as
-- bridge command frames for the byte-stream adapters (stream, JTAG, serial
-- HDLC), and as a plain address and payload for the ones whose link carries
-- memory accesses of its own (SWD).
--
-- A bench elaborates its adapter on adapter_apb_config_c, attaches an
-- nsl_amba.ram.apb_ram of adapter_completer_size_l2_c bytes to its requester,
-- and drives the sequence from the link side with the simulation master that
-- link has.
package testing is

  -- Bytes per APB word, log2, and the address bits the completer decodes.
  constant adapter_data_bus_width_l2_c : natural := 2;
  constant adapter_address_width_c : natural := 12;
  -- Words in one read command, log2. Small enough that the count field is
  -- one byte, large enough that a multi-word read is a burst.
  constant adapter_burst_length_l2_c : natural := 4;
  -- Where the descriptor sits, as an adapter publishes it. Zero is the only
  -- value every link can carry.
  constant adapter_descriptor_base_c : natural := 0;

  constant adapter_apb_config_c : nsl_amba.apb.config_t :=
    nsl_amba.apb.config(
      address_width => adapter_address_width_c,
      data_bus_width => 8 * 2**adapter_data_bus_width_l2_c,
      err => true);

  -- Bytes of the completer behind the adapter, log2.
  constant adapter_completer_size_l2_c : natural := adapter_address_width_c;

  -- Byte address the sequence writes, and the words it puts there. Two words,
  -- so a read spans a burst and a single-word read can pick the second one.
  constant conformance_address_c : natural := 16#40#;
  constant conformance_data_c : byte_string := from_hex("0f1e2d3c4b5a6978");

  -- The identify reply an adapter serving one answers with, status byte
  -- excluded.
  constant conformance_identify_c : byte_string := bridge_identify(
    addr_bits => adapter_apb_config_c.address_width,
    data_bytes_l2 => adapter_apb_config_c.data_bus_width_l2,
    burst_length_l2 => adapter_burst_length_l2_c,
    descriptor_base => adapter_descriptor_base_c);

  -- The sequence as bridge command frames: identify, write, read back the
  -- whole payload, read one word from the middle of it.
  constant conformance_step_count_c : natural := 4;
  function conformance_command(step : natural) return byte_string;
  function conformance_response(step : natural) return byte_string;

  -- Word count of the payload, and the byte address of its second word.
  function conformance_word_count return natural;
  function conformance_second_word_address return natural;

end package;

package body testing is

  constant word_bytes_c : natural := 2**adapter_data_bus_width_l2_c;
  constant address_bytes_c : natural := (adapter_address_width_c + 7) / 8;
  constant count_bytes_c : natural := (adapter_burst_length_l2_c + 7) / 8;
  -- Opcodes of the stream-to-APB bridge (nsl_amba.stream_apb).
  constant write_opcode_c : byte := x"00";
  constant read_opcode_c : byte := x"80";
  constant identify_opcode_c : byte_string := (x"ff", x"00");
  -- Every response ends with a status byte; zero is success.
  constant ok_c : byte_string := (0 => x"00");

  function conformance_word_count return natural is
  begin
    return conformance_data_c'length / word_bytes_c;
  end function;

  function conformance_second_word_address return natural is
  begin
    return conformance_address_c + word_bytes_c;
  end function;

  function address_field(address : natural) return byte_string is
  begin
    return to_le(to_unsigned(address, address_bytes_c * 8));
  end function;

  -- The read count field carries the word count less one, so zero reads one
  -- word.
  function count_field(words : natural) return byte_string is
  begin
    return to_le(to_unsigned(words - 1, count_bytes_c * 8));
  end function;

  function conformance_command(step : natural) return byte_string is
  begin
    case step is
      when 0 =>
        return identify_opcode_c;
      when 1 =>
        return write_opcode_c & address_field(conformance_address_c)
          & conformance_data_c;
      when 2 =>
        return read_opcode_c & address_field(conformance_address_c)
          & count_field(conformance_word_count);
      when 3 =>
        return read_opcode_c
          & address_field(conformance_second_word_address) & count_field(1);
      when others =>
        assert false
          report "conformance step out of range" severity failure;
        return ok_c;
    end case;
  end function;

  function conformance_response(step : natural) return byte_string is
  begin
    case step is
      when 0 =>
        return conformance_identify_c & ok_c;
      when 1 =>
        return ok_c;
      when 2 =>
        return conformance_data_c & ok_c;
      when 3 =>
        return conformance_data_c(conformance_data_c'left + word_bytes_c
                                  to conformance_data_c'right) & ok_c;
      when others =>
        assert false
          report "conformance step out of range" severity failure;
        return ok_c;
    end case;
  end function;

end package body;
