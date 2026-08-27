library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_data, gatecap;
use nsl_data.bytestream.all;
use nsl_data.cbor.all;
use nsl_data.text.all;
use nsl_data.uuid.all;
use gatecap.descriptor.all;

entity tb is
end entity;

architecture sim of tb is

  -- Instrument types are the plugins' own identities; these three stand for
  -- them here, so the bench depends on no instrument in particular.
  constant ANALYZER_UUID_C : uuid_t := uuid("0786abe7-3599-4074-b0d7-8c727847a08d");
  constant COUNTER_UUID_C : uuid_t := uuid("778010a3-b4ff-4156-aa92-2eaca97ecfe4");
  constant BARE_UUID_C : uuid_t := uuid("f493c40e-934c-4f2d-a54d-4c0deeaafc67");

  -- 16 KB, three children, two tail fields of its own.
  constant analyzer_c : byte_string := instrument_envelope(
    type_uuid => ANALYZER_UUID_C,
    size_l2 => 14,
    name => "la",
    children => child_map(
      sibling_entry("buffer", 16#0000#, buffer_desc(sample_stride => 8,
                                                    buffer_size_l2 => 12)),
      sibling_entry("control", 16#1000#,
                    control_desc(buffer_name => "buffer",
                                 trigger_name => "trigger",
                                 signal_count => 8,
                                 capture_len_width => 12,
                                 signal_names => "word[7:0]",
                                 capture_clock_hz => 100_000_000)),
      sibling_entry("trigger", 16#2000#,
                    trigger_desc(signal_count => 8,
                                 signal_names => "word[7:0]"))),
    t0 => cbor_tstr("control"),
    t1 => cbor_positive(1));

  -- 4 KB, one child, one tail field.
  constant counter_c : byte_string := instrument_envelope(
    type_uuid => COUNTER_UUID_C,
    size_l2 => 12,
    name => "clkmon",
    children => child_map(
      sibling_entry("rate", 16#0000#,
                    trigger_desc(signal_count => 2,
                                 signal_names => "sys,phy"))),
    t0 => cbor_positive(48_000_000));

  -- 4 KB, neither child nor tail.
  constant bare_c : byte_string := instrument_envelope(
    type_uuid => BARE_UUID_C,
    size_l2 => 12,
    name => "aux");

  -- What the elaboration walker is handed: envelopes back to back, in
  -- description order, no index and no count.
  constant envelopes_c : byte_string := analyzer_c & counter_c & bare_c;

  constant bases_c : base_vector := segment_bases(envelopes_c);
  constant extent_c : natural := segment_extent(envelopes_c);

  constant root_c : byte_string := rack_compose(
    instrument_entry(bases_c(0), analyzer_c),
    instrument_entry(bases_c(1), counter_c),
    instrument_entry(bases_c(2), bare_c));

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
    report "envelope diag: " & cbor_diag(analyzer_c);
    report "root diag: " & cbor_diag(root_c);
    report "allocation: la=" & to_string(bases_c(0))
      & " clkmon=" & to_string(bases_c(1))
      & " aux=" & to_string(bases_c(2))
      & " extent=" & to_string(extent_c);

    -- An envelope is an array of four framework fields plus its own tail.
    assert analyzer_c(analyzer_c'low) = x"86"
      report "envelope with two tail fields is not a 6-element array"
      severity failure;
    assert counter_c(counter_c'low) = x"85"
      report "envelope with one tail field is not a 5-element array"
      severity failure;
    assert bare_c(bare_c'low) = x"84"
      report "tailless envelope is not a 4-element array" severity failure;
    assert bare_c = cbor_array(cbor_tagged(37, cbor_bstr(BARE_UUID_C)),
                               cbor_positive(12),
                               cbor_tstr("aux"),
                               cbor_map_hdr(length => 0))
      report "tailless envelope mis-encoded" severity failure;

    -- The concatenation is self-delimiting: it walks back to the envelopes
    -- it was built from, with no length carried anywhere.
    assert envelope_count(envelopes_c) = 3
      report "concatenation does not walk back to three envelopes"
      severity failure;
    assert envelope_nth(envelopes_c, 0) = analyzer_c
      report "first envelope mis-walked" severity failure;
    assert envelope_nth(envelopes_c, 1) = counter_c
      report "second envelope mis-walked" severity failure;
    assert envelope_nth(envelopes_c, 2) = bare_c
      report "third envelope mis-walked" severity failure;

    -- The two fields the framework reads back out of an envelope.
    assert envelope_size_l2(envelope_nth(envelopes_c, 0)) = 14
      report "size_l2 mis-extracted" severity failure;
    assert envelope_size_l2(envelope_nth(envelopes_c, 1)) = 12
      report "size_l2 mis-extracted" severity failure;
    assert envelope_size_l2(envelope_nth(envelopes_c, 2)) = 12
      report "size_l2 mis-extracted" severity failure;
    assert envelope_name(envelope_nth(envelopes_c, 0)) = "la"
      report "name mis-extracted" severity failure;
    assert envelope_name(envelope_nth(envelopes_c, 1)) = "clkmon"
      report "name mis-extracted" severity failure;
    assert envelope_name(envelope_nth(envelopes_c, 2)) = "aux"
      report "name mis-extracted" severity failure;

    -- Allocation is ascending by size above the 4 KB ROM segment: both 4 KB
    -- instruments come first and back-fill below the 16 KB one's alignment
    -- boundary, ties in description order.
    assert bases_c(1) = 16#1000# and bases_c(2) = 16#2000#
      report "4 KB segments did not back-fill in description order"
      severity failure;
    assert bases_c(0) = 16#4000#
      report "16 KB segment is not aligned on its own size" severity failure;
    assert extent_c = 16#8000#
      report "extent does not cover the last segment" severity failure;

    -- The root keys the envelopes by their allocated base, in description
    -- order, so they come out unsorted.
    assert root_c(root_c'low) = x"83"
      report "root is not a 3-element array" severity failure;
    assert contains(root_c, cbor_positive(16#4000#) & analyzer_c)
      report "root entry does not key the envelope by its base"
      severity failure;
    assert root_c = cbor_array(
      cbor_tagged(37, cbor_bstr(RACK_UUID_C)),
      cbor_positive(0),
      cbor_map_hdr(length => 3)
        & cbor_positive(16#4000#) & analyzer_c
        & cbor_positive(16#1000#) & counter_c
        & cbor_positive(16#2000#) & bare_c)
      report "root composition mis-encoded" severity failure;

    -- The fingerprint covers bases and envelopes alike, so any change of
    -- allocation or of composition is a new one.
    assert descriptor_fingerprint(root_c)
      /= descriptor_fingerprint(rack_compose(
        instrument_entry(bases_c(1), counter_c),
        instrument_entry(bases_c(0), analyzer_c),
        instrument_entry(bases_c(2), bare_c)))
      report "fingerprint did not change with the entry order" severity failure;

    -- The host suite decodes these exact bytes and checks the same structure.
    report "RACK DESCRIPTOR HEX: " & to_hex_string(root_c) severity note;
    report "rack descriptor testbench PASSED" severity note;
    wait;
  end process;

end architecture;
