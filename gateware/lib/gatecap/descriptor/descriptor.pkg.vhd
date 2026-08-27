library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_data;
use nsl_data.bytestream.all;
use nsl_data.cbor.all;
use nsl_data.uuid.all;
use nsl_data.crc.all;

-- CBOR self-description for a gatecap rack.
--
-- A typed object is a CBOR array whose first element is the type UUID
-- (tag 37, 16-byte string) and whose remaining elements are the type's
-- positional fields. The field layout is fixed by the UUID (any change
-- is a new UUID), so no map keys are needed. The bridge identify is served
-- by the stream-to-APB bridge; the descriptor lives in a ROM at
-- descriptor-base.
--
-- The root descriptor maps allocated segment bases to instrument envelopes,
-- each envelope carrying the instrument's type, address-space footprint,
-- instance name and children map, then the fields its own type defines.
-- Blocks are the children of one instrument, so their names and their
-- cross-references scope to it: a control names its buffer and its trigger by
-- the key they take in the very same children map. Segment bases come out of
-- segment_bases, which allocates from the very envelopes the descriptor
-- carries.
package descriptor is

  -- apb bridge
  constant BRIDGE_UUID_C : uuid_t := uuid("51b5af74-0733-4ddb-9899-158ad7bde322");
  -- trace buffer
  constant BUFFER_UUID_C : uuid_t := uuid("0f9d2ab1-afb1-44f4-b8d1-a35e6244e339");
  -- control block (raw / segmented multi-window)
  constant CONTROL_UUID_C : uuid_t := uuid("bf023668-f44d-46f0-a318-03aa06223021");
  -- control block (run-length-encoded capture)
  constant RLE_CONTROL_UUID_C : uuid_t := uuid("5d3f8a21-9e74-4c60-b1d2-6f0a83e5c497");
  -- trigger block (value/mask compare on its own signal vector)
  constant TRIGGER_UUID_C : uuid_t := uuid("2a7c4e19-8b53-4f0a-9d61-7e2c5b048f36");
  -- edge/transition trigger block (independent old/new value/mask compare)
  constant EDGE_TRIGGER_UUID_C : uuid_t := uuid("9f4e2c17-6a3b-4d8e-b1c5-7e0a2f6d3b94");
  -- logic-analyzer instrument: capture domains behind one APB port
  constant LOGIC_ANALYZER_UUID_C : uuid_t := uuid("ce4e395e-1439-4ab7-9cee-cfb4f3257f3d");
  -- control/status instrument: a front panel of plain wires
  constant CONTROL_STATUS_UUID_C : uuid_t := uuid("dd241b36-f1b0-4418-b6b8-23223e5a93ff");
  -- control/status register block, the instrument's only child
  constant CONTROL_STATUS_BLOCK_UUID_C : uuid_t := uuid("2ee04b40-1620-438f-a783-0989ef7e19d3");
  -- rack root: instrument envelopes keyed by segment base
  constant RACK_UUID_C : uuid_t := uuid("aff98e3f-ce7f-483b-acc6-738464439eec");

  -- One address per instrument, indexed in envelope order.
  type base_vector is array(natural range <>) of natural;

  -- A 32-bit fingerprint of the descriptor bytes: a stable per-instance UID
  -- the host polls to notice the gateware changed under it (e.g. the FPGA was
  -- reprogrammed with a different capture config). Any descriptor change --
  -- signal count/names, buffer size, block set -- changes it. Not a standard
  -- CRC-32; the host only compares the value, never recomputes it.
  function descriptor_fingerprint(descriptor : byte_string) return unsigned;

  -- Bridge identify reply (the bridge appends its status byte).
  function bridge_identify(
    addr_bits : natural;
    data_bytes_l2 : natural;
    burst_length_l2 : natural;
    descriptor_base : natural) return byte_string;

  -- Signature the SPI discovery blob opens with. Four ASCII bytes, not a
  -- CBOR item: a host that reads them has a rack in front of it and may
  -- decode what follows, and one that does not has something else entirely.
  constant SPI_SIGNATURE_C : string := "GCAP";

  -- Discovery is answered on the SFDP opcode, in the SFDP layout: the opcode,
  -- a three-byte address, then eight dummy clocks -- one byte time -- before
  -- the first data byte. The controller underneath knows none of that; it
  -- streams the payload from the byte slot right after the opcode. So the
  -- blob is prefixed with as many filler bytes as the layout puts between
  -- opcode and data, and the signature lands exactly where an SFDP read
  -- expects its first data byte.
  --
  -- The address field is on the wire and ignored: this payload cannot be
  -- sought yet, and a host reads it from its first byte. It exists so that
  -- seeking is a change of behaviour behind an unchanged wire format.
  constant SPI_DISCOVERY_COMMAND_C : byte := x"5a";
  constant SPI_DISCOVERY_ADDRESS_BYTES_C : natural := 3;
  constant SPI_DISCOVERY_DUMMY_BYTES_C : natural := 1;
  constant SPI_DISCOVERY_FILLER_C : natural :=
    SPI_DISCOVERY_ADDRESS_BYTES_C + SPI_DISCOVERY_DUMMY_BYTES_C;

  -- SPI discovery blob, answered raw on the discovery opcode: the filler
  -- above, the signature, then one CBOR array:
  --
  --   [ addr-bits, data-bytes-l2, descriptor-base ]
  --
  -- which is the bridge identify's geometry without the type UUID: the
  -- descriptor root carries a UUID of its own, and it is read as soon as the
  -- base is known. Burst length is absent because this link has none -- a
  -- burst ends when the master releases the chip select, so its length is the
  -- master's choice and nothing the target can state.
  function spi_discovery(
    addr_bits : natural;
    data_bytes_l2 : natural;
    descriptor_base : natural) return byte_string;

  -- Typed trace-buffer object: [ type, sample-stride, total-size-l2 ].
  function buffer_desc(
    sample_stride : natural;
    buffer_size_l2 : natural) return byte_string;

  -- Typed control-block object. The RLE control is a distinct type (own
  -- UUID and driver): no max-length or window-count, its readback is a
  -- decode-from-zero of the whole region. buffer_name and trigger_name are
  -- the children-map names of the buffer this control fills and of the
  -- trigger block that arms it. signal_names is one text: the grouping spec
  -- the host expands to one name per probe bit (comma list, with brace
  -- groups "p.{a,b}" and array ranges "bus[7:0]").
  -- integration_latency is the wiring/CDC delay between the trigger and this
  -- control's core, in capture cycles: 0 for a single-domain direct wire, the
  -- interdomain_tick depth for a shared/cross-domain trigger. It is separate
  -- from the trigger block's intrinsic latency (a host driver constant); the
  -- host adds the two for markers.
  function control_desc(
    buffer_name : string;
    trigger_name : string;
    signal_count : natural;
    capture_len_width : natural;
    window_count : natural := 1;
    rle : boolean := false;
    signal_names : string := "";
    capture_clock_hz : natural := 0;
    integration_latency : natural := 0) return byte_string;

  -- Typed trigger-block object. Its own signal vector and names (disjoint
  -- from the captured probes): [ type, signal-count, signal-names ].
  function trigger_desc(
    signal_count : natural;
    signal_names : string := "";
    edge : boolean := false) return byte_string;

  -- Name-keyed map entry: name, register offset relative to the instrument's
  -- segment, and the block's typed object. The result is a name/value pair
  -- for child_map, not a standalone item.
  function sibling_entry(
    name : string;
    offset : natural;
    object : byte_string) return byte_string;

  -- Children-map entry for a block with no registers: the offset is CBOR
  -- null. Only its typed object carries information.
  function baseless_sibling_entry(
    name : string;
    object : byte_string) return byte_string;

  -- Children map of an instrument envelope, from up to 16 entries built with
  -- sibling_entry / baseless_sibling_entry, in the order they are given; the
  -- first empty entry ends the map. Child offsets are relative to the
  -- instrument's segment base, and child names scope to the instrument, so
  -- the cross-references a block holds resolve in this map alone.
  function child_map(
    c0, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c14, c15
    : byte_string := null_byte_string) return byte_string;

  -- Instrument envelope: [ type, size_l2, name, children, tail... ]. The
  -- prefix through children is framework-owned and frozen by RACK_UUID_C;
  -- the tail fields t0 to t7 are the instrument's own, frozen by its type
  -- UUID, and the first empty one ends the array. children is a child_map
  -- result, empty for an instrument with no child. size_l2 is the
  -- instrument's address-space footprint as a power of two, and the segment
  -- it is allocated is aligned on it.
  function instrument_envelope(
    type_uuid : uuid_t;
    size_l2 : natural;
    name : string;
    children : byte_string := null_byte_string;
    t0, t1, t2, t3, t4, t5, t6, t7
    : byte_string := null_byte_string) return byte_string;

  -- Root-map entry: an instrument's allocated segment base, relative to
  -- descriptor-base, and its envelope.
  function instrument_entry(
    base : natural;
    envelope : byte_string) return byte_string;

  -- Rack root descriptor (ROM contents) from up to 16 instrument entries, in
  -- the order they are given; the first empty entry ends the map. Keys are
  -- allocated bases, so the map is unsorted. next-offset chains to a further
  -- descriptor blob, 0 for none.
  function rack_compose(
    e0, e1, e2, e3, e4, e5, e6, e7, e8, e9, e10, e11, e12, e13, e14, e15
    : byte_string := null_byte_string;
    next_offset : natural := 0) return byte_string;

  -- The walk over a concatenation of envelopes. CBOR items are
  -- self-delimiting, so envelopes need neither a count nor an index: the
  -- concatenation is walked item by item. This is the shape allocation works
  -- from, VHDL-93 having no array of unconstrained byte_string.
  function envelope_count(envelopes : byte_string) return natural;
  function envelope_nth(
    envelopes : byte_string;
    index : natural) return byte_string;

  -- Fields the framework reads back from an envelope.
  function envelope_size_l2(envelope : byte_string) return natural;
  function envelope_name(envelope : byte_string) return string;

  -- Segment base of every envelope, in envelope order. Segments are laid out
  -- ascending by size, ties in envelope order, each aligned to its own size,
  -- above the descriptor ROM segment pinned at address 0. Ascending order
  -- back-fills the small segments below the first large alignment boundary,
  -- which minimises the extent -- and the extent is what sets the address
  -- width.
  function segment_bases(
    envelopes : byte_string;
    rom_size_l2 : natural := 12) return base_vector;

  -- First address above the last allocated segment.
  function segment_extent(
    envelopes : byte_string;
    rom_size_l2 : natural := 12) return natural;

  -- Envelope of a logic-analyzer instrument:
  --
  --   [ type, size_l2, name, children, [ member control names ] ]
  --
  -- The four leading fields are the framework's. The tail is this type's: the
  -- children-map names of the capture controls one arm covers, in domain
  -- order, which is what makes a multi-domain analyzer one correlated group.
  -- A single-domain analyzer names its one control. Names are passed as one
  -- comma-separated list ("ctrl.control,rx.control"), taken verbatim between
  -- commas; each control carries its own trigger integration latency in its
  -- own object.
  --
  -- children holds the control, trace-buffer and trigger blocks, at offsets
  -- relative to the instrument's segment.
  function logic_analyzer_envelope(
    size_l2 : natural;
    name : string;
    children : byte_string;
    control_names : string) return byte_string;

  -- Typed control/status register-block object: [ type ]. The block's
  -- register map follows from the inventory the instrument's envelope
  -- carries, so the object holds nothing beyond the type a host driver binds
  -- on.
  function control_status_block_desc return byte_string;

  -- Envelope of a control/status instrument:
  --
  --   [ type, size_l2, name, children,
  --     control-names, status-names, tick-out-names, tick-in-names,
  --     counter-width ]
  --
  -- The four leading fields are the framework's. The tail is this type's and
  -- carries the whole inventory, so a host builds the panel from the
  -- descriptor alone:
  --
  --   control-names   one text: the grouping spec of the control registers,
  --                   one item per register in register order, the item's
  --                   width giving the register's declared width and a <...>
  --                   suffix binding an enumeration table
  --                   ("led,level[0:11],mode[0:1]<idle,run>")
  --   status-names    same form, for the status registers
  --   tick-out-names  an array of texts, one per packed tick word, each a
  --                   comma list of the ticks that strobe together. Word
  --                   boundaries are the simultaneity groups, so they are
  --                   what the array preserves; the argument spells them
  --                   with a semicolon ("start,stop;soft_reset")
  --   tick-in-names   same form. Tick inputs are numbered word-major, which
  --                   is the order of their sticky bits and counters
  --   counter-width   bits of every tick-input counter; they wrap at it
  --
  -- children holds the register block at offset 0.
  function control_status_envelope(
    size_l2 : natural;
    name : string;
    children : byte_string;
    control_names : string;
    status_names : string;
    tick_out_names : string;
    tick_in_names : string;
    counter_width : natural) return byte_string;

end package;

package body descriptor is

  function uuid(u : uuid_t) return byte_string is
  begin
    return cbor_tagged(37, cbor_bstr(u));
  end function;

  function descriptor_fingerprint(descriptor : byte_string) return unsigned is
    constant params_c : crc_params_t := crc_params(
      poly => x"04c11db7",
      init => x"ffffffff",
      complement_state => false,
      complement_input => false,
      byte_bit_order => BIT_ORDER_DESCENDING,
      spill_order => EXP_ORDER_DESCENDING,
      byte_order => BYTE_ORDER_INCREASING);
    constant state_c : crc_state_t :=
      crc_update(params_c, crc_init(params_c), descriptor);
  begin
    return resize(unsigned(crc_spill_vector(params_c, state_c)), 32);
  end function;

  function bridge_identify(
    addr_bits : natural;
    data_bytes_l2 : natural;
    burst_length_l2 : natural;
    descriptor_base : natural) return byte_string is
  begin
    -- [ type, addr-bits, data-bytes-l2, burst-length-l2, descriptor-base ]
    return cbor_array(
      uuid(BRIDGE_UUID_C),
      cbor_positive(addr_bits),
      cbor_positive(data_bytes_l2),
      cbor_positive(burst_length_l2),
      cbor_positive(descriptor_base));
  end function;

  function spi_discovery(
    addr_bits : natural;
    data_bytes_l2 : natural;
    descriptor_base : natural) return byte_string is
    constant filler_c : byte_string(0 to SPI_DISCOVERY_FILLER_C-1) :=
      (others => x"00");
  begin
    return filler_c
      & to_byte_string(SPI_SIGNATURE_C)
      & cbor_array(
        cbor_positive(addr_bits),
        cbor_positive(data_bytes_l2),
        cbor_positive(descriptor_base));
  end function;

  function buffer_desc(
    sample_stride : natural;
    buffer_size_l2 : natural) return byte_string is
  begin
    return cbor_array(
      uuid(BUFFER_UUID_C),
      cbor_positive(sample_stride),
      cbor_positive(buffer_size_l2));
  end function;

  function control_desc(
    buffer_name : string;
    trigger_name : string;
    signal_count : natural;
    capture_len_width : natural;
    window_count : natural := 1;
    rle : boolean := false;
    signal_names : string := "";
    capture_clock_hz : natural := 0;
    integration_latency : natural := 0) return byte_string is
  begin
    if rle then
      -- [ type, sink, trigger, signal-count, signal-names, capture-clock-hz,
      --   integration-latency ]
      return cbor_array(
        uuid(RLE_CONTROL_UUID_C),
        cbor_tstr(buffer_name),
        cbor_tstr(trigger_name),
        cbor_positive(signal_count),
        cbor_tstr(signal_names),
        cbor_positive(capture_clock_hz),
        cbor_positive(integration_latency));
    else
      -- [ type, sink, trigger, signal-count, signal-names,
      --   max-capture-length, window-count, capture-clock-hz (0 = unknown),
      --   integration-latency ]
      return cbor_array(
        uuid(CONTROL_UUID_C),
        cbor_tstr(buffer_name),
        cbor_tstr(trigger_name),
        cbor_positive(signal_count),
        cbor_tstr(signal_names),
        cbor_positive(2**capture_len_width - 1),
        cbor_positive(window_count),
        cbor_positive(capture_clock_hz),
        cbor_positive(integration_latency));
    end if;
  end function;

  function trigger_desc(
    signal_count : natural;
    signal_names : string := "";
    edge : boolean := false) return byte_string is
    variable type_uuid : uuid_t := TRIGGER_UUID_C;
  begin
    if edge then
      type_uuid := EDGE_TRIGGER_UUID_C;
    end if;
    return cbor_array(
      uuid(type_uuid),
      cbor_positive(signal_count),
      cbor_tstr(signal_names));
  end function;

  -- Count of the separated items in a list; an empty text holds none.
  function item_count(items : string; separator : character) return natural is
    variable count : natural := 1;
  begin
    if items'length = 0 then
      return 0;
    end if;
    for i in items'range loop
      if items(i) = separator then
        count := count + 1;
      end if;
    end loop;
    return count;
  end function;

  -- Encoded text strings, one per separated item, concatenated.
  function items_encoded(items : string; separator : character)
    return byte_string is
  begin
    if items'length = 0 then
      return null_byte_string;
    end if;
    for i in items'range loop
      if items(i) = separator then
        return cbor_tstr(items(items'left to i-1))
          & items_encoded(items(i+1 to items'right), separator);
      end if;
    end loop;
    return cbor_tstr(items);
  end function;

  -- A CBOR array of texts, one per item of a separated list.
  function text_array(items : string; separator : character := ',')
    return byte_string is
  begin
    return cbor_array_hdr(length => item_count(items, separator))
      & items_encoded(items, separator);
  end function;

  function name_array(names : string) return byte_string is
  begin
    return text_array(names);
  end function;

  function sibling_entry(
    name : string;
    offset : natural;
    object : byte_string) return byte_string is
  begin
    return cbor_tstr(name) & cbor_array(cbor_positive(offset), object);
  end function;

  function baseless_sibling_entry(
    name : string;
    object : byte_string) return byte_string is
  begin
    return cbor_tstr(name) & cbor_array(cbor_null, object);
  end function;

  -- Rank of the last non-empty entry of a prefix-filled entry list.
  function entry_count(
    e0, e1, e2, e3, e4, e5, e6, e7, e8, e9, e10, e11, e12, e13, e14, e15
    : byte_string := null_byte_string) return natural is
    variable last : natural := 0;
    variable used : natural := 0;
  begin
    if e0'length /= 0 then last := 1; used := used + 1; end if;
    if e1'length /= 0 then last := 2; used := used + 1; end if;
    if e2'length /= 0 then last := 3; used := used + 1; end if;
    if e3'length /= 0 then last := 4; used := used + 1; end if;
    if e4'length /= 0 then last := 5; used := used + 1; end if;
    if e5'length /= 0 then last := 6; used := used + 1; end if;
    if e6'length /= 0 then last := 7; used := used + 1; end if;
    if e7'length /= 0 then last := 8; used := used + 1; end if;
    if e8'length /= 0 then last := 9; used := used + 1; end if;
    if e9'length /= 0 then last := 10; used := used + 1; end if;
    if e10'length /= 0 then last := 11; used := used + 1; end if;
    if e11'length /= 0 then last := 12; used := used + 1; end if;
    if e12'length /= 0 then last := 13; used := used + 1; end if;
    if e13'length /= 0 then last := 14; used := used + 1; end if;
    if e14'length /= 0 then last := 15; used := used + 1; end if;
    if e15'length /= 0 then last := 16; used := used + 1; end if;

    assert last = used
      report "Entry list has a hole: entries must be given without a gap"
      severity failure;

    return last;
  end function;

  function child_map(
    c0, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c14, c15
    : byte_string := null_byte_string) return byte_string is
  begin
    return cbor_map_hdr(
      length => entry_count(c0, c1, c2, c3, c4, c5, c6, c7,
                            c8, c9, c10, c11, c12, c13, c14, c15))
      & c0 & c1 & c2 & c3 & c4 & c5 & c6 & c7
      & c8 & c9 & c10 & c11 & c12 & c13 & c14 & c15;
  end function;

  -- An envelope always carries a children map, empty ones included.
  function populated_child_map(children : byte_string) return byte_string is
  begin
    if children'length = 0 then
      return child_map;
    end if;

    return children;
  end function;

  function instrument_envelope(
    type_uuid : uuid_t;
    size_l2 : natural;
    name : string;
    children : byte_string := null_byte_string;
    t0, t1, t2, t3, t4, t5, t6, t7
    : byte_string := null_byte_string) return byte_string is
    constant tail_count_c : natural :=
      entry_count(t0, t1, t2, t3, t4, t5, t6, t7);
    constant children_c : byte_string := populated_child_map(children);
  begin
    return cbor_array_hdr(length => 4 + tail_count_c)
      & uuid(type_uuid)
      & cbor_positive(size_l2)
      & cbor_tstr(name)
      & children_c
      & t0 & t1 & t2 & t3 & t4 & t5 & t6 & t7;
  end function;

  function instrument_entry(
    base : natural;
    envelope : byte_string) return byte_string is
  begin
    return cbor_positive(base) & envelope;
  end function;

  function rack_compose(
    e0, e1, e2, e3, e4, e5, e6, e7, e8, e9, e10, e11, e12, e13, e14, e15
    : byte_string := null_byte_string;
    next_offset : natural := 0) return byte_string is
  begin
    return cbor_array(
      uuid(RACK_UUID_C),
      cbor_positive(next_offset),
      cbor_map_hdr(
        length => entry_count(e0, e1, e2, e3, e4, e5, e6, e7,
                              e8, e9, e10, e11, e12, e13, e14, e15))
      & e0 & e1 & e2 & e3 & e4 & e5 & e6 & e7
      & e8 & e9 & e10 & e11 & e12 & e13 & e14 & e15);
  end function;

  function envelope_count(envelopes : byte_string) return natural is
    alias e : byte_string(0 to envelopes'length-1) is envelopes;
    variable index : natural := 0;
    variable count : natural := 0;
  begin
    while index < e'length loop
      index := index + cbor_item_length(e(index to e'right));
      count := count + 1;
    end loop;

    return count;
  end function;

  function envelope_nth(
    envelopes : byte_string;
    index : natural) return byte_string is
    alias e : byte_string(0 to envelopes'length-1) is envelopes;
    variable start : natural := 0;
  begin
    for i in 0 to index loop
      assert start < e'length
        report "Envelope index past the end of the concatenation"
        severity failure;
      exit when i = index;
      start := start + cbor_item_length(e(start to e'right));
    end loop;

    return e(start to start + cbor_item_length(e(start to e'right)) - 1);
  end function;

  function envelope_size_l2(envelope : byte_string) return natural is
    alias e : byte_string(0 to envelope'length-1) is envelope;
    constant array_c : parser_t := cbor_parse(e);
    constant uuid_c : natural := cbor_header_length(e);
    constant start_c : natural :=
      uuid_c + cbor_item_length(e(uuid_c to e'right));
    constant size_c : parser_t := cbor_parse(e(start_c to e'right));
  begin
    assert kind(array_c) = KIND_ARRAY and arg_int(array_c) >= 4
      report "Not an instrument envelope"
      severity failure;
    assert kind(size_c) = KIND_POSITIVE
      report "Envelope size_l2 is not an unsigned integer"
      severity failure;

    return arg_int(size_c);
  end function;

  function envelope_name(envelope : byte_string) return string is
    alias e : byte_string(0 to envelope'length-1) is envelope;
    constant uuid_c : natural := cbor_header_length(e);
    constant size_c : natural :=
      uuid_c + cbor_item_length(e(uuid_c to e'right));
    constant start_c : natural :=
      size_c + cbor_item_length(e(size_c to e'right));
    constant name_c : parser_t := cbor_parse(e(start_c to e'right));
  begin
    assert kind(name_c) = KIND_TSTR
      report "Envelope name is not a text string"
      severity failure;

    return to_character_string(
      e(start_c + cbor_header_length(e(start_c to e'right))
        to start_c + cbor_item_length(e(start_c to e'right)) - 1));
  end function;

  function segment_bases(
    envelopes : byte_string;
    rom_size_l2 : natural := 12) return base_vector is
    constant count_c : natural := envelope_count(envelopes);
    variable size_l2 : base_vector(0 to count_c-1);
    variable base : base_vector(0 to count_c-1);
    variable placed : bit_vector(0 to count_c-1) := (others => '0');
    variable next_index : integer;
    variable size : natural;
    variable addr : natural := 2**rom_size_l2;
  begin
    for i in 0 to count_c-1 loop
      size_l2(i) := envelope_size_l2(envelope_nth(envelopes, i));
    end loop;

    for k in 0 to count_c-1 loop
      next_index := -1;
      for i in 0 to count_c-1 loop
        if placed(i) = '0' then
          if next_index < 0 then
            next_index := i;
          elsif size_l2(i) < size_l2(next_index) then
            next_index := i;
          end if;
        end if;
      end loop;

      placed(next_index) := '1';
      size := 2**size_l2(next_index);
      addr := ((addr + size - 1) / size) * size;
      base(next_index) := addr;
      addr := addr + size;
    end loop;

    return base;
  end function;

  function segment_extent(
    envelopes : byte_string;
    rom_size_l2 : natural := 12) return natural is
    constant base_c : base_vector := segment_bases(envelopes, rom_size_l2);
    variable top : natural;
    variable extent : natural := 2**rom_size_l2;
  begin
    for i in base_c'range loop
      top := base_c(i) + 2**envelope_size_l2(envelope_nth(envelopes, i));
      if top > extent then
        extent := top;
      end if;
    end loop;

    return extent;
  end function;

  function logic_analyzer_envelope(
    size_l2 : natural;
    name : string;
    children : byte_string;
    control_names : string) return byte_string is
  begin
    return instrument_envelope(
      type_uuid => LOGIC_ANALYZER_UUID_C,
      size_l2 => size_l2,
      name => name,
      children => children,
      t0 => name_array(control_names));
  end function;

  function control_status_block_desc return byte_string is
  begin
    return cbor_array(uuid(CONTROL_STATUS_BLOCK_UUID_C));
  end function;

  function control_status_envelope(
    size_l2 : natural;
    name : string;
    children : byte_string;
    control_names : string;
    status_names : string;
    tick_out_names : string;
    tick_in_names : string;
    counter_width : natural) return byte_string is
  begin
    return instrument_envelope(
      type_uuid => CONTROL_STATUS_UUID_C,
      size_l2 => size_l2,
      name => name,
      children => children,
      t0 => cbor_tstr(control_names),
      t1 => cbor_tstr(status_names),
      t2 => text_array(tick_out_names, ';'),
      t3 => text_array(tick_in_names, ';'),
      t4 => cbor_positive(counter_width));
  end function;

end package body;
