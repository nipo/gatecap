"""Rack descriptor decoding: reference blobs built with cbor2, the error
paths of the framework-owned fields, and a cross-check against the gateware.

The cross-check builds the `rack_descriptor` unit bench, which composes a
descriptor with the VHDL functions and prints it as hex; parsing those exact
bytes here pins the two implementations to each other.

Run: python3.13 -m pytest host/tests/test_rack_descriptor.py
"""

import os
import re
import subprocess
import uuid

import cbor2
import pytest

from acrobe_plugin.gatecap.rack import (
    RACK_UUID, DescriptorError, Rack)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "gateware", "tests", "rack_descriptor")

ANALYZER_UUID = uuid.UUID("0786abe7-3599-4074-b0d7-8c727847a08d")
COUNTER_UUID = uuid.UUID("778010a3-b4ff-4156-aa92-2eaca97ecfe4")
BARE_UUID = uuid.UUID("f493c40e-934c-4f2d-a54d-4c0deeaafc67")
BLOCK_UUID = uuid.UUID("0f9d2ab1-afb1-44f4-b8d1-a35e6244e339")


class Reference:
    """Builders for descriptors the gateware would compose."""

    @staticmethod
    def envelope(type_uuid, size_l2, name, children=None, tail=()):
        return [type_uuid, size_l2, name, children or {}, *tail]

    @staticmethod
    def root(segments, next_offset=0):
        return cbor2.dumps([RACK_UUID, next_offset, segments])

    @classmethod
    def two_instruments(cls):
        return cls.root({
            0x4000: cls.envelope(
                ANALYZER_UUID, 14, "la",
                children={
                    "buffer": [0x0000, [BLOCK_UUID, 8, 12]],
                    "group": [None, [BLOCK_UUID, "buffer"]],
                },
                tail=["control", 1]),
            0x1000: cls.envelope(COUNTER_UUID, 12, "clkmon",
                                 tail=[48_000_000]),
        })


def test_instruments_carry_base_footprint_and_tail():
    rack = Rack.parse(Reference.two_instruments())
    assert rack.next_offset == 0
    assert [i.name for i in rack] == ["la", "clkmon"]

    la, clkmon = rack.instruments
    assert (la.type_uuid, la.base, la.size_l2, la.size) == (
        ANALYZER_UUID, 0x4000, 14, 0x4000)
    assert la.tail == ["control", 1]
    assert (clkmon.type_uuid, clkmon.base, clkmon.size) == (
        COUNTER_UUID, 0x1000, 0x1000)
    assert clkmon.tail == [48_000_000]
    assert clkmon.children == {}


def test_child_addresses_stack_descriptor_segment_and_offset():
    la = Rack.parse(Reference.two_instruments()).instruments[0]
    assert set(la.children) == {"buffer", "group"}
    assert la.children["buffer"].type_uuid == BLOCK_UUID
    assert la.child_address("buffer") == 0x4000
    assert la.child_address("buffer", descriptor_base=0x8000) == 0xc000
    # A reference-only child owns no register file.
    assert la.children["group"].offset is None
    assert la.child_address("group") is None


def test_trailing_bytes_are_ignored():
    # The host reads whole words, so the blob comes back padded.
    rack = Rack.parse(Reference.two_instruments() + b"\x00" * 64)
    assert len(rack) == 2


def test_rejects_foreign_root_type():
    blob = cbor2.dumps([uuid.uuid4(), 0, {}])
    with pytest.raises(DescriptorError, match="is not a rack"):
        Rack.parse(blob)


def test_rejects_flat_descriptor():
    # The first-generation format: siblings keyed by name, no envelope.
    flat = cbor2.dumps([uuid.UUID("9710ce59-f5e6-403c-83e1-c86c553b608f"), 0,
                        {"buffer": [0x100, [BLOCK_UUID, 8, 12]]}])
    with pytest.raises(DescriptorError, match="is not a rack"):
        Rack.parse(flat)


def test_rejects_truncated_blob():
    with pytest.raises(DescriptorError, match="does not decode"):
        Rack.parse(Reference.two_instruments()[:8])


def test_rejects_envelope_missing_framework_fields():
    blob = Reference.root({0x1000: [COUNTER_UUID, 12, "clkmon"]})
    with pytest.raises(DescriptorError, match="shorter than the four fields"):
        Rack.parse(blob)


def test_rejects_envelope_with_untyped_head():
    blob = Reference.root({0x1000: Reference.envelope("counter", 12, "clkmon")})
    with pytest.raises(DescriptorError, match="not typed by a UUID"):
        Rack.parse(blob)


def test_rejects_envelope_without_children_map():
    blob = Reference.root({0x1000: [COUNTER_UUID, 12, "clkmon", ["rate"]]})
    with pytest.raises(DescriptorError, match="children map"):
        Rack.parse(blob)


def test_rejects_child_that_is_not_an_offset_object_pair():
    blob = Reference.root({0x1000: Reference.envelope(
        COUNTER_UUID, 12, "clkmon", children={"rate": [0, BLOCK_UUID, 2]})})
    with pytest.raises(DescriptorError, match="not an .offset, object. pair"):
        Rack.parse(blob)


def test_rejects_child_without_a_typed_object():
    blob = Reference.root({0x1000: Reference.envelope(
        COUNTER_UUID, 12, "clkmon", children={"rate": [0, ["rate", 2]]})})
    with pytest.raises(DescriptorError, match="not typed by a UUID"):
        Rack.parse(blob)


def test_rejects_root_that_is_not_a_segment_map():
    blob = cbor2.dumps([RACK_UUID, 0, [Reference.envelope(BARE_UUID, 12, "aux")]])
    with pytest.raises(DescriptorError, match="base-keyed segment map"):
        Rack.parse(blob)


@pytest.fixture(scope="module")
def gateware_descriptor():
    build = subprocess.run(["gbs", "project", "build"], cwd=BENCH_DIR,
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    with open(os.path.join(BENCH_DIR, "tb.log")) as log_file:
        log = log_file.read()
    assert "PASSED" in log, log
    printed = re.search(r"RACK DESCRIPTOR HEX: ([0-9a-f]+)", log)
    assert printed is not None, "the bench printed no descriptor"
    return bytes.fromhex(printed.group(1))


def test_gateware_composition_walks_back(gateware_descriptor):
    rack = Rack.parse(gateware_descriptor)
    assert rack.next_offset == 0
    assert [(i.name, i.base, i.size_l2) for i in rack] == [
        ("la", 0x4000, 14),
        ("clkmon", 0x1000, 12),
        ("aux", 0x2000, 12),
    ]

    la, clkmon, aux = rack.instruments
    assert la.type_uuid == ANALYZER_UUID
    assert {name: child.offset for name, child in la.children.items()} == {
        "buffer": 0x0000, "control": 0x1000, "trigger": 0x2000}
    assert la.children["buffer"].type_uuid == BLOCK_UUID
    assert la.tail == ["control", 1]

    assert clkmon.type_uuid == COUNTER_UUID
    assert clkmon.child_address("rate") == 0x1000
    assert clkmon.tail == [48_000_000]

    assert (aux.type_uuid, aux.children, aux.tail) == (BARE_UUID, {}, [])
