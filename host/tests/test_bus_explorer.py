"""Unit tests for the host-side halves of the bus-explorer instrument that
touch no hardware: the SVD decoder, the library that resolves a descriptor's
map identifier against it, and the journal with its two exports.

The document under test is the one the example bench's stub device implements
(``tests/data/demo_device.svd``), written in more than one SVD style on
purpose; the rest is synthesised inline, one document per construct.

Run: python3.13 -m pytest host/tests/test_bus_explorer.py
"""

import json
import os

import pytest

from acrobe_plugin.gatecap.instrument.bus_explorer.journal import (Journal,
                                                                  JournalEntry)
from acrobe_plugin.gatecap.instrument.bus_explorer.svd import (MapLibrary,
                                                              SvdDocument,
                                                              SvdError)

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_SVD = os.path.join(HERE, "data", "demo_device.svd")


def device(body, name="D"):
    """One peripheral at address zero, holding `body`."""
    return f"""<?xml version="1.0"?>
    <device><name>{name}</name><peripherals><peripheral>
      <name>P</name><baseAddress>0x1000</baseAddress>
      <registers>{body}</registers>
    </peripheral></peripherals></device>"""


# -- the decoder ---------------------------------------------------------


def test_the_demo_document_decodes_whole():
    doc = SvdDocument.parse_file(DEMO_SVD)
    assert doc.name == "GATECAP_DEMO_DEVICE"
    assert len(doc) == 7
    assert [r.name for r in doc.registers] == \
        ["ID", "CTRL", "STATUS", "SCRATCH", "MIRROR", "FAULT", "SLOW"]
    # The address a register is reached at is baseAddress + addressOffset,
    # which is what the engine drives onto paddr.
    assert doc.register_at(0x004).name == "CTRL"
    assert doc.register_at(0x010).name == "MIRROR"
    assert doc.register_at(0x014) is None
    # Read-only registers stay distinguishable from read/write ones.
    assert doc.register("ID").writable() is False
    assert doc.register("SCRATCH").writable() is True


def test_the_three_bit_range_spellings_all_read():
    """CTRL's three fields are written the three ways SVD allows, and must come
    out identical in kind: a decoder that reads only one spelling silently
    loses two thirds of a real document."""
    ctrl = SvdDocument.parse_file(DEMO_SVD).register("CTRL")
    assert (ctrl.field("ENABLE").lsb, ctrl.field("ENABLE").msb) == (0, 0)
    assert (ctrl.field("MODE").lsb, ctrl.field("MODE").msb) == (1, 2)
    assert (ctrl.field("GAIN").lsb, ctrl.field("GAIN").msb) == (4, 11)
    assert ctrl.field("GAIN").mask == 0xFF0
    assert ctrl.field("MODE").mask == 0x6


def test_a_value_breaks_down_into_its_fields():
    ctrl = SvdDocument.parse_file(DEMO_SVD).register("CTRL")
    fields = {f["name"]: f for f in ctrl.decode(0x0A3)}
    assert fields["ENABLE"]["value"] == 1
    assert fields["MODE"]["value"] == 1 and fields["MODE"]["label"] == "RUN"
    assert fields["GAIN"]["value"] == 0x0A
    # Low bits first, so a rendered breakdown reads like the word does.
    assert [f["name"] for f in ctrl.decode(0)] == ["ENABLE", "MODE", "GAIN"]


def test_a_field_write_computes_its_own_mask():
    ctrl = SvdDocument.parse_file(DEMO_SVD).register("CTRL")
    mode = ctrl.field("MODE")
    assert mode.encode("TEST") == 2
    assert mode.place(mode.encode("TEST")) == 0x4
    assert mode.mask == 0x6
    # A value that does not fit the field would reach into its neighbours,
    # which the mask says are not being written.
    with pytest.raises(ValueError):
        mode.place(4)
    with pytest.raises(ValueError):
        mode.encode("SPIN")


def test_bit_range_and_msb_lsb_agree_with_bit_offset():
    doc = SvdDocument.parse_text(device("""
      <register><name>R</name><addressOffset>0</addressOffset><fields>
        <field><name>A</name><bitOffset>3</bitOffset><bitWidth>4</bitWidth></field>
        <field><name>B</name><bitRange>[10:7]</bitRange></field>
        <field><name>C</name><lsb>11</lsb><msb>14</msb></field>
      </fields></register>"""))
    register = doc.register("R")
    assert [(f.lsb, f.msb) for f in register.fields] == \
        [(3, 6), (7, 10), (11, 14)]
    assert register.address == 0x1000


def test_a_dim_array_expands_into_one_register_per_index():
    doc = SvdDocument.parse_text(device("""
      <register><dim>4</dim><dimIncrement>4</dimIncrement>
        <name>CH%s</name><addressOffset>0x20</addressOffset></register>"""))
    assert [r.name for r in doc.registers] == ["CH0", "CH1", "CH2", "CH3"]
    assert [r.address for r in doc.registers] == \
        [0x1020, 0x1024, 0x1028, 0x102C]


def test_a_derived_peripheral_repeats_the_map_at_its_own_base():
    doc = SvdDocument.parse_text("""<?xml version="1.0"?>
    <device><name>D</name><peripherals>
      <peripheral><name>A</name><baseAddress>0x100</baseAddress><registers>
        <register><name>R</name><addressOffset>8</addressOffset></register>
      </registers></peripheral>
      <peripheral derivedFrom="A"><name>B</name>
        <baseAddress>0x200</baseAddress></peripheral>
    </peripherals></device>""")
    assert doc.register("A.R").address == 0x108
    assert doc.register("B.R").address == 0x208
    assert doc.register_at(0x208).qualified == "B.R"


def test_numbers_read_in_every_spelling_svd_writes_them():
    doc = SvdDocument.parse_text(device("""
      <register><name>A</name><addressOffset>0x10</addressOffset></register>
      <register><name>B</name><addressOffset>32</addressOffset></register>
      <register><name>C</name><addressOffset>#0110000</addressOffset></register>
      """))
    assert [r.address - 0x1000 for r in doc.registers] == [0x10, 32, 0x30]


def test_a_construct_this_decoder_does_not_implement_is_refused():
    """Named, not skipped: a map that quietly lost half its registers decodes
    addresses to the wrong names, which is worse than having no map."""
    with pytest.raises(SvdError, match="cluster"):
        SvdDocument.parse_text("""<?xml version="1.0"?>
        <device><name>D</name><peripherals><peripheral>
          <name>P</name><baseAddress>0</baseAddress>
          <registers><cluster><name>C</name></cluster></registers>
        </peripheral></peripherals></device>""")
    with pytest.raises(SvdError, match="derivedFrom"):
        SvdDocument.parse_text(device(
            """<register derivedFrom="X"><name>R</name>
               <addressOffset>0</addressOffset></register>"""))
    with pytest.raises(SvdError, match="not an SVD document"):
        SvdDocument.parse_text("<peripherals/>")
    with pytest.raises(SvdError, match="well-formed"):
        SvdDocument.parse_text("<device>")


# -- the library ---------------------------------------------------------


def test_a_map_identifier_resolves_to_a_registered_document(tmp_path,
                                                            monkeypatch):
    monkeypatch.setenv("GATECAP_CONFIG_DIR", str(tmp_path))
    library = MapLibrary()
    assert library.registered() == {}
    # An identifier nothing is registered under is the raw-hex case, not an
    # error: an explorer with no map still explores.
    assert library.resolve("gatecap-demo-device") is None
    assert library.resolve("") is None

    document = library.add("gatecap-demo-device", DEMO_SVD)
    assert len(document) == 7
    assert library.registered() == {"gatecap-demo-device": DEMO_SVD}
    assert len(library.resolve("gatecap-demo-device")) == 7
    # A second library over the same store sees it: `bus map add` in one
    # process and a pane in another is the normal case.
    assert MapLibrary().path("gatecap-demo-device") == DEMO_SVD

    library.remove("gatecap-demo-device")
    assert library.registered() == {}
    with pytest.raises(KeyError):
        library.remove("gatecap-demo-device")


def test_a_document_is_parsed_before_it_is_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("GATECAP_CONFIG_DIR", str(tmp_path))
    bad = tmp_path / "bad.svd"
    bad.write_text("<device><name>D</name></device>")
    with pytest.raises(SvdError):
        MapLibrary().add("bad", str(bad))
    assert MapLibrary().registered() == {}


# -- the journal ---------------------------------------------------------


def journal():
    j = Journal("dut", address_digits=3, value_digits=8)
    j.observe(op="read", address=0x004)
    j.observe(op="write", address=0x00C, value=0xA5A5A5A5,
              register="DEMO.SCRATCH")
    j.observe(op="masked-write", address=0x004, value=0x2, mask=0x6,
              register="DEMO.CTRL", field="MODE")
    j.observe(op="write", address=0x020, value=1, register="DEMO.FAULT",
              error="slverr")
    return j


def test_the_journal_keeps_the_writes_and_only_the_writes():
    j = journal()
    assert len(j) == 3 and j.reads == 1
    assert [e.op for e in j.entries] == ["write", "masked-write", "write"]
    assert j.entries[1].decoded() == "DEMO.CTRL.MODE"
    assert j.entries[0].decoded() == "DEMO.SCRATCH"
    assert j.entries[2].error == "slverr"
    # A plain write carries no mask; a masked one carries the mask that was
    # driven, which is what makes it replayable.
    assert j.entries[0].mask is None and j.entries[1].mask == 0x6


def test_the_listing_names_every_write_in_order():
    text = journal().listing()
    lines = text.strip().split("\n")
    assert lines[0].startswith("# gatecap bus explorer dut: 3 write(s), "
                               "1 read(s)")
    assert "[00c]" in lines[1] and "a5a5a5a5" in lines[1]
    assert "DEMO.SCRATCH" in lines[1]
    assert "mask 00000006" in lines[2] and "DEMO.CTRL.MODE" in lines[2]
    assert "!! slverr" in lines[3]


def test_the_recipe_replays_without_a_map():
    recipe = journal().recipe("gatecap-demo-device")
    assert recipe["gatecap-bus-explorer-recipe"] == Journal.VERSION
    assert recipe["instrument"] == "dut"
    steps = Journal.steps_of(recipe)
    assert len(steps) == 3
    # A step says what to drive; the decoded name rides along as commentary,
    # so the same recipe replays on a host with no map registered.
    assert steps[1] == {"op": "masked-write", "address": 0x004, "value": 0x2,
                        "mask": 0x6, "name": "DEMO.CTRL.MODE"}
    assert Journal.steps_of(json.dumps(recipe)) == steps
    assert Journal.steps_of([{"op": "write", "address": 0, "value": 0}])


def test_a_recipe_that_is_not_one_is_refused():
    with pytest.raises(ValueError, match="version"):
        Journal.steps_of({"steps": []})
    with pytest.raises(ValueError, match="not one of"):
        Journal.steps_of([{"op": "read", "address": 0, "value": 0}])
    with pytest.raises(ValueError, match="integer address"):
        Journal.steps_of([{"op": "write", "value": 0}])


def test_clearing_the_journal_forgets_the_session():
    j = journal()
    assert j.clear() == 3
    assert len(j) == 0 and j.reads == 0
    assert j.recipe()["steps"] == []


def test_an_entry_renders_what_it_holds():
    entry = JournalEntry(time=0.0, op="write", address=4, value=1, mask=None,
                         register=None, field=None, error=None)
    assert entry.decoded() is None
    assert "[00000004]" in entry.text()
    assert entry.step() == {"op": "write", "address": 4, "value": 1}
