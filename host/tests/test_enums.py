"""Unit tests for enum decoding in the probe grouping spec.

Run: python3.13 -m pytest host/tests/test_enums.py
"""

import io

from vcd import VCDWriter

from acrobe_plugin.gatecap.enums import EnumTable, EnumRegistry
from acrobe_plugin.gatecap.instrument.la.signals import VcdLayout
from acrobe_plugin.gatecap.names import SignalNames
from acrobe_plugin.gatecap.instrument.la.blocks.trigger import trigger_fields, field_value


# -- EnumTable.parse --------------------------------------------------------

def test_positional():
    assert EnumTable.parse("OKAY,EXOKAY,SLVERR,DECERR") == {
        0: "OKAY", 1: "EXOKAY", 2: "SLVERR", 3: "DECERR"}


def test_registry_splice():
    assert EnumTable.parse("+axi.resp") == EnumRegistry.get("axi.resp")


def test_sparse_explicit_indices():
    assert EnumTable.parse("0x00:NOP,0x10:READ,0xFF:RESET") == {
        0: "NOP", 16: "READ", 255: "RESET"}


def test_gap_leaves_value_unmapped():
    assert EnumTable.parse("A,,C") == {0: "A", 2: "C"}


def test_extend_above_base():
    # splice sets the running index past the base, so bare labels land after it
    assert EnumTable.parse("+axi.burst,3:RESERVED,extra") == {
        0: "FIXED", 1: "INCR", 2: "WRAP", 3: "RESERVED", 4: "extra"}


def test_override_a_base_value():
    t = EnumTable.parse("+axi.resp,2:MY_SLVERR")
    assert t[2] == "MY_SLVERR"
    assert t[0] == "OKAY" and t[3] == "DECERR"


def test_splice_index_is_past_base_then_explicit_wins():
    # FIRST maps 0, then the splice overrides 0..2 and moves the index to 3.
    assert EnumTable.parse("FIRST,+axi.burst,0x10:hi,next") == {
        0: "FIXED", 1: "INCR", 2: "WRAP", 16: "hi", 17: "next"}


def test_unknown_registry_entry_raises():
    try:
        EnumTable.parse("+axi.nope")
    except KeyError:
        pass
    else:
        assert False, "expected KeyError"


# -- SignalNames.parse ------------------------------------------------------

def test_parse_bus_enum():
    names, enums = SignalNames.parse("resp[1:0]<+axi.resp>")
    assert names == ["resp[1]", "resp[0]"]
    assert enums == {"resp": {0: "OKAY", 1: "EXOKAY", 2: "SLVERR", 3: "DECERR"}}


def test_parse_scalar_enum():
    names, enums = SignalNames.parse("state<IDLE,BUSY>")
    assert names == ["state"]
    assert enums == {"state": {0: "IDLE", 1: "BUSY"}}


def test_parse_nested_scope_enum():
    names, enums = SignalNames.parse("pkt.{resp[1:0]<+axi.resp>,data[7:0]}")
    assert names == ["pkt.resp[1]", "pkt.resp[0]"] + [f"pkt.data[{n}]" for n in range(7, -1, -1)]
    assert set(enums) == {"pkt.resp"}
    assert enums["pkt.resp"][2] == "SLVERR"


def test_expand_strips_enum_backward_compatible():
    # expand() must ignore enums so existing callers are unaffected.
    assert SignalNames.expand("resp[1:0]<+axi.resp>") == ["resp[1]", "resp[0]"]
    assert SignalNames.expand("a,b,c") == ["a", "b", "c"]


def test_enum_group_is_rejected():
    try:
        SignalNames.parse("p.{a,b}<X,Y>")
    except ValueError:
        pass
    else:
        assert False, "expected ValueError for enum on a group"


# -- VcdLayout end to end ---------------------------------------------------

def test_vcd_layout_decodes_value_to_label():
    names, enums = SignalNames.parse("resp[1:0]<+axi.resp>")
    layout = VcdLayout(names, enums=enums)
    (var,) = layout.vars
    assert var.size == 2

    # names order is resp[1] (probe bit 0) then resp[0] (probe bit 1). A sample
    # with probe bit 0 set means resp[1]=1, i.e. value 2 -> SLVERR.
    assert var.value(0b01) == 2
    assert var.label(2) == "SLVERR"
    assert var.value(0b10) == 1
    assert var.label(1) == "EXOKAY"
    assert var.label(0) == "OKAY"


def test_vcd_layout_no_enum_labels_none():
    names, enums = SignalNames.parse("bus[1:0]")
    (var,) = VcdLayout(names, enums=enums).vars
    assert var.enum is None
    assert var.label(3) is None


def test_vcd_emits_enum_as_string_label():
    names, enums = SignalNames.parse("resp[1:0]<+axi.resp>")
    layout = VcdLayout(names, enums=enums)
    buf = io.StringIO()
    with VCDWriter(buf, timescale="1 ns", date="") as w:
        layout.register(w)
        layout.emit(w, 0, 0b01)   # value 2
    text = buf.getvalue()
    assert "$var string" in text     # enum bus is a string var
    assert "SLVERR" in text          # decoded label, not the number


# -- trigger by label -------------------------------------------------------

def test_trigger_field_carries_enum():
    names, enums = SignalNames.parse("resp[1:0]<+axi.resp>,valid")
    fields = {f["name"]: f for f in trigger_fields(names, enums)}
    assert fields["resp"]["kind"] == "bus"
    assert fields["resp"]["enum"][2] == "SLVERR"
    assert fields["valid"]["enum"] is None


def test_field_value_resolves_label_and_number():
    names, enums = SignalNames.parse("resp[1:0]<+axi.resp>,mode[1:0]")
    fields = {f["name"]: f for f in trigger_fields(names, enums)}
    assert field_value(fields["resp"], "SLVERR") == 2
    assert field_value(fields["resp"], "0x1") == 1        # numbers still work
    assert field_value(fields["mode"], "3") == 3          # non-enum bus
