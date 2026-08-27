"""Unit tests for signal-name grouping and VCD layout.

Run: python3.13 -m pytest host/tests/test_signals.py
"""

import io

from vcd import VCDWriter

from acrobe_plugin.gatecap.instrument.la.signals import VcdLayout
from acrobe_plugin.gatecap.names import SignalNames


def test_expand_plain_comma_list():
    assert SignalNames.expand("a,b,c") == ["a", "b", "c"]
    assert SignalNames.expand("") == []
    assert SignalNames.expand("lone") == ["lone"]


def test_expand_brace_group_preserves_order():
    assert SignalNames.expand("p.{a,c,b}") == ["p.a", "p.c", "p.b"]


def test_expand_array_range_both_directions():
    assert SignalNames.expand("bus[7:0]") == [f"bus[{n}]" for n in range(7, -1, -1)]
    assert SignalNames.expand("bus[0:3]") == ["bus[0]", "bus[1]", "bus[2]", "bus[3]"]
    assert SignalNames.expand("bit[2]") == ["bit[2]"]


def test_expand_nested():
    assert SignalNames.expand("command.{valid,last,data[1:0]}") == [
        "command.valid", "command.last", "command.data[1]", "command.data[0]"]
    assert SignalNames.expand("a.{b,c[1:0]},x[0:1]") == [
        "a.b", "a.c[1]", "a.c[0]", "x[0]", "x[1]"]


def _vcd(names, samples, buses):
    layout = VcdLayout(names, buses=buses)
    buf = io.StringIO()
    with VCDWriter(buf, timescale="1 ns", date="") as writer:
        layout.register(writer)
        for t, sample in enumerate(samples):
            layout.emit(writer, t, sample)
    return buf.getvalue()


def test_vcd_hierarchy_and_bus():
    names = SignalNames.expand("sck,command.{valid,data[0:3]}")
    out = _vcd(names, [0], buses=True)
    assert "$scope module capture $end" in out
    assert "$scope module command $end" in out
    assert "$var wire 1 " in out and "sck $end" in out
    assert "$var wire 4 " in out and "data $end" in out  # 4-bit bus


def test_vcd_bus_value_assembly():
    # names: 0 sck, 1 command.valid, 2..5 command.data[0..3]
    names = SignalNames.expand("sck,command.{valid,data[0:3]}")
    # data = 0b1010 (data[3..0]); place at bits 2..5, valid=bit1, sck=bit0
    sample = (1 << 0) | (0 << 1) | (0b1010 << 2)
    out = _vcd(names, [sample], buses=True)
    assert "b1010 " in out  # bus value, MSB = data[3]


def test_vcd_scalar_mode_has_no_vectors():
    names = SignalNames.expand("sck,command.data[0:3]")
    out = _vcd(names, [0], buses=False)
    assert "wire 8" not in out and "wire 4" not in out
    # each array element is its own single-bit var, hierarchy kept
    assert "$scope module command $end" in out
    assert "data[3] $end" in out
