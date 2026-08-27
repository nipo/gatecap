"""The `!control-status` instrument: what a panel description accepts, the
entity it becomes, the crossings it makes and the inventory it publishes.

Run: python3.13 -m pytest host/tests/test_generator_control_status.py
"""

import textwrap

import pytest

from acrobe_plugin.gatecap.generator import (DescriptionError,
                                             DescriptionParser,
                                             InstrumentRegistry, RackAssembly)
from acrobe_plugin.gatecap.generator.vhdl import Emitter
from acrobe_plugin.gatecap.instrument.control_status import Panel


# The reference description: one panel on a clock of its own, holding all four
# kinds, and one on the rack's clock holding two.
REFERENCE = """
name: panel_pkg.panel_rack

communication:
  mode: apb

instruments:
  panel: !control-status
    clock: clk
    tick-counter-width: 4

    control:
      led: 1
      dac_level: 12
      mode:
        width: 2
        enum:
          0: idle
          1: run
          2: test

    status:
      state:
        width: 4
        enum:
          0: reset
          1: idle
          2: busy
      done: 1

    tick-out:
      - [ start, stop ]
      - [ soft_reset ]

    tick-in:
      - [ overflow, underflow ]

  mini: !control-status
    control:
      gate: 1

    tick-in:
      - [ pulse ]
"""


def parse(text):
    return DescriptionParser.load(text)


def rejects(text, fragment):
    with pytest.raises(DescriptionError) as raised:
        parse(text)
    assert fragment in str(raised.value), str(raised.value)
    return str(raised.value)


def minimal(body, communication="mode: apb"):
    """A rack holding one panel, whose body is the only thing under test."""
    return (f"name: p.e\n\ncommunication:\n  {communication}\n\n"
            f"instruments:\n  panel: !control-status\n"
            + textwrap.indent(body, "    ",
                              lambda line: line.strip() != ""))


ONE_CONTROL = "control:\n  led: 1\n"


def body(description, name="panel"):
    return description.instrument(name).params["panel"]


def paneled(text, name="panel"):
    """The panel as the rack contributes it: with the exported clock the rack
    runs on, when it runs on this instance's."""
    rack = RackAssembly(parse(text))
    entry = rack.description.instrument(name)
    return Panel(entry, rack.host_clock(entry))


def architecture(panel):
    return Emitter.render(panel.architecture("core"))


def entity(panel):
    return Emitter.render(panel.entity("core"))


# The description


def test_the_tag_is_registered():
    assert "!control-status" in InstrumentRegistry.tags()


def test_reference_description_parses():
    d = parse(REFERENCE)
    assert [x.name for x in d.instruments] == ["panel", "mini"]
    panel = body(d)
    assert [(level.name, level.width) for level in panel.controls] == [
        ("led", 1), ("dac_level", 12), ("mode", 2)]
    assert [(level.name, level.width) for level in panel.statuses] == [
        ("state", 4), ("done", 1)]
    assert [word.names for word in panel.tick_out] == [("start", "stop"),
                                                       ("soft_reset",)]
    assert [word.names for word in panel.tick_in] == [("overflow",
                                                       "underflow")]
    assert panel.counter_width == 4
    assert panel.counter_count() == 2


def test_a_panel_defaults_to_the_host_clock_and_a_32_bit_counter():
    panel = body(parse(REFERENCE), "mini")
    assert panel.clock is None
    assert panel.counter_width == 32
    assert panel.statuses == () and panel.tick_out == ()


def test_the_declared_clock_is_exported_under_its_own_name():
    d = parse(REFERENCE)
    assert d.exported_clocks() == {"panel.clk": "panel_clk_i"}


def test_ports_follow_the_instance_and_signal_names():
    d = parse(REFERENCE)
    panel = d.instrument("panel")
    assert [(port.name, port.direction, port.type)
            for port in panel.plugin.ports(panel)] == [
        ("panel_clk_i", "in", "std_ulogic"),
        ("panel_reset_n_i", "in", "std_ulogic"),
        ("panel_led_o", "out", "std_ulogic"),
        ("panel_dac_level_o", "out", "unsigned(11 downto 0)"),
        ("panel_mode_o", "out", "unsigned(1 downto 0)"),
        ("panel_state_i", "in", "unsigned(3 downto 0)"),
        ("panel_done_i", "in", "std_ulogic"),
        ("panel_start_o", "out", "std_ulogic"),
        ("panel_stop_o", "out", "std_ulogic"),
        ("panel_soft_reset_o", "out", "std_ulogic"),
        ("panel_overflow_i", "in", "std_ulogic"),
        ("panel_underflow_i", "in", "std_ulogic")]


def test_a_clockless_panel_takes_no_clock_port():
    d = parse(REFERENCE)
    mini = d.instrument("mini")
    assert [port.name for port in mini.plugin.ports(mini)] == [
        "mini_gate_o", "mini_pulse_i"]
    assert mini.plugin.clocks(mini) == {}


# What the description alone rejects


def test_an_unknown_key_is_refused():
    rejects(minimal("panels: 3\n" + ONE_CONTROL), "unknown key 'panels'")


def test_a_signal_name_must_be_a_vhdl_identifier():
    rejects(minimal("control:\n  dac-level: 12\n"),
            "control name 'dac-level' illegal character")
    rejects(minimal("tick-in:\n  - [ over-flow ]\n"),
            "tick-in name 'over-flow' illegal character")


def test_a_width_must_fit_a_register():
    rejects(minimal("control:\n  wide: 33\n"), "width must be in 1 to 32")
    rejects(minimal("status:\n  none: 0\n"), "width must be in 1 to 32")
    rejects(minimal("control:\n  wide: { width: 33 }\n"),
            "width must be at most 32")
    rejects(minimal("control:\n  named: { enum: { 0: a } }\n"),
            "needs a width")
    rejects(minimal("control:\n  odd: hello\n"),
            "a control is a width or a mapping")


def test_an_enum_must_fit_the_field_it_labels():
    rejects(minimal("status:\n  s: { width: 2, enum: { 4: too_far } }\n"),
            "enum maps value 4 beyond the 2-bit status")


def test_a_tick_word_is_a_non_empty_list_of_at_most_32_names():
    rejects(minimal("tick-out:\n  - []\n"),
            "a tick-out word is a non-empty list")
    rejects(minimal("tick-out:\n  - start\n"),
            "a tick-out word is a non-empty list")
    rejects(minimal("tick-in: 3\n"), "tick-in is a list of words")
    names = ", ".join(f"t{i}" for i in range(33))
    rejects(minimal(f"tick-out:\n  - [ {names} ]\n"),
            "packs at most 32 ticks, got 33")


def test_a_signal_is_named_once_whatever_its_kind():
    rejects(minimal("control:\n  go: 1\ntick-out:\n  - [ go ]\n"),
            "signal 'go' is declared twice")


def test_a_panel_needs_a_signal():
    rejects(minimal("tick-counter-width: 8\n"),
            "needs at least one control, status or tick")


def test_the_counter_width_must_fit_a_register():
    rejects(minimal("tick-counter-width: 33\n" + ONE_CONTROL),
            "tick-counter-width must be at most 32")


def test_the_clock_is_the_panel_s_own_name():
    rejects(minimal("clock: sys.clk\n" + ONE_CONTROL),
            "must be a plain name")


def test_each_region_holds_64_words():
    words = "\n".join(f"  - [ t{i} ]" for i in range(33))
    rejects(minimal(f"tick-in:\n{words}\n"), "the action region holds 64 words")
    words = "\n".join(f"  - [ t{i} ]" for i in range(31))
    statuses = "".join(f"  s{i}: 1\n" for i in range(32))
    rejects(minimal(f"tick-in:\n{words}\nstatus:\n{statuses}"),
            "the status region holds 64 words")
    controls = "".join(f"  c{i}: 1\n" for i in range(65))
    rejects(minimal(f"control:\n{controls}"), "the array region holds 64 words")


# The entity


def test_the_entity_is_named_after_the_rack_and_the_instance():
    panel = paneled(REFERENCE)
    assert panel.entity_name("core") == "core_panel"
    text = entity(panel)
    assert "entity core_panel is" in text
    assert "panel_dac_level_o : out unsigned(11 downto 0);" in text


def test_the_boundary_slices_each_word_to_its_declared_width():
    text = architecture(paneled(REFERENCE))
    assert "panel_led_o <= core_control_s(0)(0);" in text
    assert ("panel_dac_level_o <= unsigned(core_control_s(1)(11 downto 0));"
            in text)
    assert "core_status_in_s(0) <= panel_masked(panel_state_i, 4);" in text
    assert ("core_status_in_s(1) <= panel_masked(unsigned'(0 => panel_done_i), 1);"
            in text)
    assert "panel_start_o <= core_tick_out_s(0)(0);" in text
    assert "panel_stop_o <= core_tick_out_s(0)(1);" in text
    assert "panel_soft_reset_o <= core_tick_out_s(1)(0);" in text
    assert ("core_tick_in_s(0) <= (0 => panel_overflow_i, "
            "1 => panel_underflow_i, others => '0');" in text)


def test_the_boundary_generics_carry_the_declared_widths():
    text = architecture(paneled(REFERENCE))
    assert ("constant control_width_c : nsl_math.int_ext.integer_vector := "
            "(0 => 1, 1 => 12, 2 => 2);" in text)
    assert ("constant tick_out_count_c : nsl_math.int_ext.integer_vector := "
            "(0 => 2, 1 => 1);" in text)
    assert "constant counter_width_c : natural := 4;" in text


def test_an_absent_kind_is_an_empty_boundary_generic():
    text = architecture(paneled(REFERENCE, "mini"))
    assert ("constant status_width_c : nsl_math.int_ext.integer_vector := "
            "no_panel_signals_c;" in text)
    assert "signal core_status_in_s : panel_word_vector(0 to -1);" in text


# The crossings


def test_every_word_of_the_contract_crosses_in_its_own_direction():
    text = architecture(paneled(REFERENCE))
    for label, unit, clock in (
            ("control_1_cdc", "interdomain_reg", "clock_i => panel_clk_i"),
            ("status_0_cdc", "interdomain_reg", "clock_i => clock_i"),
            ("sticky_0_cdc", "interdomain_reg", "clock_i => clock_i"),
            ("tick_out_mask_1_cdc", "interdomain_static_reg",
             "input_clock_i => clock_i"),
            ("sticky_clear_0_cdc", "interdomain_static_reg",
             "input_clock_i => clock_i"),
            ("core_tick_out_strobe_cdc", "interdomain_tick",
             "output_clock_i => panel_clk_i"),
            ("core_sticky_clear_strobe_cdc", "interdomain_tick",
             "output_clock_i => panel_clk_i")):
        assert f"{label}: nsl_clocking.interdomain.{unit}" in text, label
        assert clock in text, label
    # One control word per crossing, never a slice of one.
    assert "data_i => shell_control_s(2)," in text
    assert "control_3_cdc" not in text


def test_a_counter_crosses_over_its_own_width_and_rides_a_word():
    text = architecture(paneled(REFERENCE))
    assert "counter_0_cdc: nsl_clocking.interdomain.interdomain_counter" in text
    assert "data_width_c => counter_width_c" in text
    assert ("data_i => unsigned(core_counter_s(1)(counter_width_c-1 downto 0))"
            in text)
    assert ("shell_counter_s(1) <= std_ulogic_vector(resize("
            "crossed_counter_s(1), 32));" in text)


def test_a_panel_on_the_host_clock_crosses_nothing():
    panel = paneled(REFERENCE, "mini")
    text = architecture(panel)
    assert "interdomain" not in text
    assert "core_control_s <= shell_control_s;" in text
    assert "shell_sticky_s <= core_sticky_s;" in text
    assert "shell_counter_s <= core_counter_s;" in text
    # The core takes the host clock, and the strobes reach it unretimed.
    assert "tick_out_strobe_i => shell_tick_out_strobe_s," in text
    assert "nsl_clocking.interdomain" not in panel.deps()


def test_a_panel_whose_clock_the_rack_rides_crosses_nothing():
    text = REFERENCE.replace("mode: apb", "mode: apb\n  clock: panel.clk")
    panel = paneled(text)
    architecture_text = architecture(panel)
    assert "interdomain" not in architecture_text
    # The clock port stays: it is where the rack takes its own clock from.
    assert "panel_clk_i : in std_ulogic;" in entity(panel)
    assert "clock_i => clock_i," in architecture_text
    assert "reset_n_i => panel_reset_n_i," in architecture_text


def test_crossings_bring_their_dependency_and_nothing_else_does():
    assert "nsl_clocking.interdomain" in paneled(REFERENCE).deps()
    assert "gatecap.control_status" in paneled(REFERENCE, "mini").deps()


# The descriptor


def test_the_envelope_publishes_the_whole_inventory():
    assert paneled(REFERENCE).envelope() == """\
control_status_envelope(
  size_l2 => 10,
  name => "panel",
  children => child_map(
    sibling_entry("registers", 0, control_status_block_desc)),
  control_names => "led,dac_level[0:11],mode[0:1]<idle,run,test>",
  status_names => "state[0:3]<reset,idle,busy>,done",
  tick_out_names => "start,stop;soft_reset",
  tick_in_names => "overflow,underflow",
  counter_width => 4)"""


def test_an_absent_kind_is_an_empty_inventory_text():
    envelope = paneled(REFERENCE, "mini").envelope()
    assert 'control_names => "gate"' in envelope
    assert 'status_names => ""' in envelope
    assert 'tick_out_names => ""' in envelope
    assert "counter_width => 32" in envelope


def test_the_footprint_is_the_register_map_and_is_asserted():
    text = architecture(paneled(REFERENCE))
    assert "constant map_size_l2_c : natural := 10;" in text
    assert "assert size_l2_c = map_size_l2_c" in text
    assert "footprint_check: nsl_synthesis.assertion.synth_assert" in text
    assert "size_l2 => 10" in paneled(REFERENCE).envelope()


# The rack around it


def test_the_rack_emits_one_file_per_panel_and_instantiates_them():
    files = RackAssembly(parse(REFERENCE)).files()
    assert "panel_rack_panel.vhd" in files
    assert "panel_rack_mini.vhd" in files
    backplane = files["panel_rack_backplane.vhd"]
    assert "panel_panel: panel_rack_panel" in backplane
    assert "mini_panel: panel_rack_mini" in backplane
    assert "panel_overflow_i => panel_overflow_i" in backplane
    package = files["panel_pkg.pkg.vhd"]
    assert "function panel_envelope return byte_string;" in package
    assert "gatecap.control_status" in files["panel_pkg.gbs.yaml"]
