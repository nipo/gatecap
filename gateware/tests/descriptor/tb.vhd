library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_data, gatecap;
use nsl_data.bytestream.all;
use nsl_data.cbor.all;
use nsl_data.text.all;
use gatecap.descriptor.all;
use gatecap.bus_explorer.all;

entity tb is
end entity;

architecture sim of tb is

  constant ident_c : byte_string := bridge_identify(
    addr_bits => 12,
    data_bytes_l2 => 2,
    burst_length_l2 => 8,
    descriptor_base => 0);

  -- A rack of one single-domain analyzer, spelled out envelope by envelope:
  -- the smallest descriptor a host enumerates, and the reference the
  -- fingerprint checks below are run against.
  function single_domain(signal_names : string) return byte_string is
  begin
    return rack_compose(
      instrument_entry(0, logic_analyzer_envelope(
        size_l2 => 14,
        name => "la",
        children => child_map(
          sibling_entry("buffer", 16#2000#,
                        buffer_desc(sample_stride => 8, buffer_size_l2 => 12)),
          sibling_entry("control", 16#1000#,
                        control_desc(buffer_name => "buffer",
                                     trigger_name => "trigger",
                                     signal_count => 8,
                                     capture_len_width => 16,
                                     signal_names => signal_names,
                                     capture_clock_hz => 100_000_000)),
          sibling_entry("trigger", 16#3000#,
                        trigger_desc(signal_count => 8,
                                     signal_names => "s0,s1,s2,s3,s4,s5,s6,s7"))),
        control_names => "control")));
  end function;

  constant desc_c : byte_string := single_domain("s0,s1,s2,s3,s4,s5,s6,s7");

  -- Same as desc_c but one signal renamed: the fingerprint must differ.
  constant desc2_c : byte_string := single_domain("s0,s1,s2,s3,s4,s5,s6,sX");

  -- Two capture domains sharing the trigger hosted by "ctrl": one instrument,
  -- five children under domain-prefixed names -- the subscribing domain hosts
  -- no trigger of its own -- and a tail naming both controls as the members of
  -- the group an arm covers.
  constant multi_c : byte_string := logic_analyzer_envelope(
    size_l2 => 15,
    name => "la",
    children => child_map(
      sibling_entry("ctrl.buffer", 16#0000#,
                    buffer_desc(sample_stride => 32, buffer_size_l2 => 12)),
      sibling_entry("ctrl.control", 16#1000#,
                    control_desc(buffer_name => "ctrl.buffer",
                                 trigger_name => "ctrl.trigger",
                                 signal_count => 16,
                                 capture_len_width => 12,
                                 signal_names => "cmd[7:0],state[7:0]",
                                 capture_clock_hz => 25_000_000)),
      sibling_entry("ctrl.trigger", 16#2000#,
                    trigger_desc(signal_count => 8,
                                 signal_names => "state[7:0]")),
      sibling_entry("rx.buffer", 16#3000#,
                    buffer_desc(sample_stride => 8, buffer_size_l2 => 10)),
      sibling_entry("rx.control", 16#4000#,
                    control_desc(buffer_name => "rx.buffer",
                                 trigger_name => "ctrl.trigger",
                                 signal_count => 9,
                                 capture_len_width => 10,
                                 rle => true,
                                 signal_names => "word[7:0],k",
                                 capture_clock_hz => 125_000_000,
                                 integration_latency => 2))),
    control_names => "ctrl.control,rx.control");

  -- A control/status panel: one child, the register block at offset 0, and a
  -- tail carrying the whole inventory -- a name-spec text for the controls and
  -- one for the statuses, an array of texts per tick kind (one text per packed
  -- word, so the simultaneity groups survive), and the counter width.
  constant panel_c : byte_string := control_status_envelope(
    size_l2 => 10,
    name => "panel",
    children => child_map(
      sibling_entry("registers", 0, control_status_block_desc)),
    control_names => "led,dac_level[0:11],mode[0:1]<idle,run,test>",
    status_names => "state[0:3]<reset,idle,busy>,done",
    tick_out_names => "start,stop;soft_reset",
    tick_in_names => "overflow,underflow",
    counter_width => 4);

  -- A panel leaving two kinds out: an absent kind is an empty text and an
  -- empty array, never a missing field.
  constant mini_c : byte_string := control_status_envelope(
    size_l2 => 10,
    name => "mini",
    children => child_map(
      sibling_entry("registers", 0, control_status_block_desc)),
    control_names => "gate",
    status_names => "",
    tick_out_names => "",
    tick_in_names => "pulse",
    counter_width => 32);

  -- A bus explorer: one child, the engine block at offset 0, and a tail
  -- carrying the target's dimensions and the identifier of its register map,
  -- which the host resolves against its own library of SVD documents.
  constant explorer_c : byte_string := bus_explorer_envelope(
    size_l2 => 10,
    name => "gt0",
    children => child_map(
      sibling_entry("engine", 0, bus_explorer_block_desc)),
    address_width => 10,
    data_width => 16,
    slot_count => 4,
    map_id => "xilinx-gtye4-drp");

  -- An explorer of a map the host has no name for: an empty text, never a
  -- missing field.
  constant unnamed_map_c : byte_string := bus_explorer_envelope(
    size_l2 => 10,
    name => "regs",
    children => child_map(
      sibling_entry("engine", 0, bus_explorer_block_desc)),
    address_width => 32,
    data_width => 32,
    slot_count => 8,
    map_id => "");

  -- Tells whether needle appears in haystack.
  function contains(haystack, needle : byte_string) return boolean is
    alias h : byte_string(0 to haystack'length-1) is haystack;
    alias n : byte_string(0 to needle'length-1) is needle;
  begin
    if n'length = 0 or n'length > h'length then
      return false;
    end if;
    for i in 0 to h'length - n'length loop
      if h(i to i + n'length - 1) = n then
        return true;
      end if;
    end loop;
    return false;
  end function;

begin

  check: process
  begin
    report "identify diag: " & cbor_diag(ident_c);
    report "descriptor diag: " & cbor_diag(desc_c);
    report "multi-domain diag: " & cbor_diag(multi_c);
    report "panel diag: " & cbor_diag(panel_c);

    -- Each payload is a typed array: identify has 5 elements (0x85),
    -- the root has 3 (type, next-offset, segment map) (0x83).
    assert ident_c(ident_c'low) = x"85"
      report "identify is not a 5-element array" severity failure;
    assert desc_c(desc_c'low) = x"83"
      report "descriptor is not a 3-element array" severity failure;

    -- Fingerprint is deterministic and sensitive to any descriptor change.
    assert descriptor_fingerprint(desc_c) = descriptor_fingerprint(desc_c)
      report "fingerprint not deterministic" severity failure;
    assert descriptor_fingerprint(desc_c) /= descriptor_fingerprint(desc2_c)
      report "fingerprint did not change with the descriptor" severity failure;

    -- Children entries are name/value pairs, based or baseless.
    assert sibling_entry("b", 5, buffer_desc(8, 12))
      = cbor_tstr("b") & x"82" & cbor_positive(5) & buffer_desc(8, 12)
      report "based child entry mis-encoded" severity failure;
    assert baseless_sibling_entry("a", buffer_desc(8, 12))
      = cbor_tstr("a") & x"82" & x"f6" & buffer_desc(8, 12)
      report "baseless child entry mis-encoded" severity failure;

    -- A logic-analyzer envelope is the framework prefix plus one tail field:
    -- the controls one arm covers.
    assert multi_c(multi_c'low) = x"85"
      report "logic-analyzer envelope is not a 5-element array" severity failure;
    assert envelope_size_l2(multi_c) = 15
      report "envelope footprint mis-encoded" severity failure;
    assert envelope_name(multi_c) = "la"
      report "envelope name mis-encoded" severity failure;
    assert contains(multi_c, cbor_array(cbor_tstr("ctrl.control"),
                                        cbor_tstr("rx.control")))
      report "envelope tail does not list the member controls" severity failure;
    assert contains(desc_c, cbor_array(cbor_tstr("control")))
      report "single-domain envelope tail does not list its one control"
      severity failure;

    -- Five children, and the map keys are the domain-prefixed names.
    assert contains(multi_c, cbor_map_hdr(length => 5))
      report "logic analyzer does not hold a 5-entry children map"
      severity failure;
    assert contains(multi_c, cbor_tstr("ctrl.control"))
      report "domain-prefixed child name missing" severity failure;
    assert contains(multi_c, cbor_tstr("rx.buffer"))
      report "domain-prefixed child name missing" severity failure;
    assert contains(multi_c,
                    cbor_tstr("rx.buffer") & x"82" & cbor_positive(16#3000#))
      report "child entry lost its offset" severity failure;

    -- The cross-domain control references its own buffer and the shared
    -- trigger by the names they take in the same children map.
    assert contains(multi_c, cbor_tstr("rx.buffer") & cbor_tstr("ctrl.trigger"))
      report "control sink/trigger references are not the children names"
      severity failure;

    -- A control/status envelope is the framework prefix plus five tail
    -- fields, and every field is spelled out here: this encoding is what a
    -- host decoder is written against.
    assert panel_c(panel_c'low) = x"89"
      report "control/status envelope is not a 9-element array"
      severity failure;
    assert control_status_block_desc
      = cbor_array(cbor_tagged(37, cbor_bstr(CONTROL_STATUS_BLOCK_UUID_C)))
      report "control/status block object is not its type alone"
      severity failure;
    assert panel_c = cbor_array(
      cbor_tagged(37, cbor_bstr(CONTROL_STATUS_UUID_C)),
      cbor_positive(10),
      cbor_tstr("panel"),
      cbor_map_hdr(length => 1)
        & cbor_tstr("registers")
        & cbor_array(cbor_positive(0), control_status_block_desc),
      cbor_tstr("led,dac_level[0:11],mode[0:1]<idle,run,test>"),
      cbor_tstr("state[0:3]<reset,idle,busy>,done"),
      cbor_array(cbor_tstr("start,stop"), cbor_tstr("soft_reset")),
      cbor_array(cbor_tstr("overflow,underflow")),
      cbor_positive(4))
      report "control/status envelope mis-encoded" severity failure;
    assert mini_c = cbor_array(
      cbor_tagged(37, cbor_bstr(CONTROL_STATUS_UUID_C)),
      cbor_positive(10),
      cbor_tstr("mini"),
      cbor_map_hdr(length => 1)
        & cbor_tstr("registers")
        & cbor_array(cbor_positive(0), control_status_block_desc),
      cbor_tstr("gate"),
      cbor_tstr(""),
      cbor_array_hdr(length => 0),
      cbor_array(cbor_tstr("pulse")),
      cbor_positive(32))
      report "control/status envelope with absent kinds mis-encoded"
      severity failure;

    -- A bus-explorer envelope is the framework prefix plus four tail fields,
    -- spelled out here: this encoding is what a host decoder is written
    -- against.
    report "bus explorer diag: " & cbor_diag(explorer_c);
    assert explorer_c(explorer_c'low) = x"88"
      report "bus-explorer envelope is not an 8-element array" severity failure;
    assert bus_explorer_block_desc
      = cbor_array(cbor_tagged(37, cbor_bstr(BUS_EXPLORER_ENGINE_UUID_C)))
      report "bus-explorer block object is not its type alone" severity failure;
    assert explorer_c = cbor_array(
      cbor_tagged(37, cbor_bstr(BUS_EXPLORER_UUID_C)),
      cbor_positive(10),
      cbor_tstr("gt0"),
      cbor_map_hdr(length => 1)
        & cbor_tstr("engine")
        & cbor_array(cbor_positive(0), bus_explorer_block_desc),
      cbor_positive(10),
      cbor_positive(16),
      cbor_positive(4),
      cbor_tstr("xilinx-gtye4-drp"))
      report "bus-explorer envelope mis-encoded" severity failure;
    assert unnamed_map_c = cbor_array(
      cbor_tagged(37, cbor_bstr(BUS_EXPLORER_UUID_C)),
      cbor_positive(10),
      cbor_tstr("regs"),
      cbor_map_hdr(length => 1)
        & cbor_tstr("engine")
        & cbor_array(cbor_positive(0), bus_explorer_block_desc),
      cbor_positive(32),
      cbor_positive(32),
      cbor_positive(8),
      cbor_tstr(""))
      report "bus-explorer envelope without a map identifier mis-encoded"
      severity failure;

    report "descriptor testbench PASSED" severity note;
    wait;
  end process;

end architecture;
