"""The `!bus-explorer` instrument: what a description accepts, the entity it
becomes, the crossings it makes and the envelope it publishes.

Run: python3.13 -m pytest host/tests/test_generator_bus_explorer.py
"""

import textwrap

import pytest

from acrobe_plugin.gatecap.generator import (DescriptionError,
                                             DescriptionParser,
                                             InstrumentRegistry, RackAssembly)
from acrobe_plugin.gatecap.generator.vhdl import Emitter
from acrobe_plugin.gatecap.instrument.bus_explorer.generator import (BusExplorer,
                                                                    Explorer)


# The reference description: one explorer mastering a narrow target bus on a
# clock of its own, and one on the rack's clock taking every default.
REFERENCE = """
name: explorer_pkg.explorer_rack

communication:
  mode: apb

instruments:
  gt0: !bus-explorer
    clock: drpclk
    address-width: 10
    data-width: 16
    slots: 4
    map: xilinx-gtye4-drp
    timeout: 64

  cfg: !bus-explorer
    address-width: 12
    data-width: 32
"""


def parse(text):
    return DescriptionParser.load(text)


def rejects(text, fragment):
    with pytest.raises(DescriptionError) as raised:
        parse(text)
    assert fragment in str(raised.value), str(raised.value)
    return str(raised.value)


def minimal(body, communication="mode: apb"):
    """A rack holding one explorer, whose body is the only thing under
    test."""
    return (f"name: p.e\n\ncommunication:\n  {communication}\n\n"
            f"instruments:\n  gt0: !bus-explorer\n"
            + textwrap.indent(body, "    ",
                              lambda line: line.strip() != ""))


BODY = "address-width: 10\ndata-width: 16\n"


def params(description, name="gt0"):
    return description.instrument(name).params


def explored(text, name="gt0"):
    """The explorer as the rack contributes it: with the exported clock the
    rack runs on, when it runs on this instance's."""
    rack = RackAssembly(parse(text))
    entry = rack.description.instrument(name)
    return Explorer(entry, rack.host_clock(entry))


def architecture(explorer):
    return Emitter.render(explorer.architecture("core"))


def entity(explorer):
    return Emitter.render(explorer.entity("core"))


def declarations(elements):
    return [x.declaration() for x in elements]


# The description


def test_the_tag_is_registered():
    assert InstrumentRegistry.PLUGINS["!bus-explorer"] is BusExplorer


def test_reference_description_parses():
    description = parse(REFERENCE)
    assert [x.name for x in description.instruments] == ["gt0", "cfg"]
    assert params(description) == {
        "clock": "drpclk", "address_width": 10, "data_width": 16,
        "slots": 4, "map": "xilinx-gtye4-drp", "timeout": 64}


def test_everything_but_the_target_dimensions_has_a_default():
    assert params(parse(minimal(BODY))) == {
        "clock": None, "address_width": 10, "data_width": 16,
        "slots": 8, "map": "", "timeout": 65536}


def test_the_target_dimensions_are_required():
    rejects(minimal("data-width: 16\n"), "address-width is required")
    rejects(minimal("address-width: 10\n"), "data-width is required")


def test_a_target_dimension_is_one_to_thirty_two_bits():
    rejects(minimal("address-width: 0\ndata-width: 16\n"),
            "address-width must be at least 1, got 0")
    rejects(minimal("address-width: 33\ndata-width: 16\n"),
            "address-width must be at most 32, got 33")
    rejects(minimal("address-width: 10\ndata-width: 33\n"),
            "data-width must be at most 32, got 33")
    assert params(parse(minimal("address-width: 32\ndata-width: 1\n")))[
        "data_width"] == 1


def test_slots_are_one_to_thirty_two():
    rejects(minimal(BODY + "slots: 0\n"), "slots must be at least 1, got 0")
    rejects(minimal(BODY + "slots: 33\n"), "slots must be at most 32, got 33")


def test_a_timeout_is_a_positive_count_of_host_cycles():
    rejects(minimal(BODY + "timeout: 0\n"),
            "timeout must be at least 1, got 0")
    assert params(parse(minimal(BODY + "timeout: 3\n")))["timeout"] == 3


def test_an_unknown_key_is_refused():
    rejects(minimal(BODY + "width: 8\n"), "unknown key 'width'")
    rejects(minimal("address_width: 10\ndata-width: 16\n"),
            "unknown key 'address_width'")


def test_the_clock_is_the_explorer_s_own_name():
    rejects(minimal(BODY + "clock: other.drpclk\n"),
            "clock 'other.drpclk' must be a plain name")
    rejects(minimal(BODY + "clock: 2clk\n"),
            "clock name '2clk' must start with a letter")


def test_the_map_identifier_is_free_but_printable_text():
    assert params(parse(minimal(BODY + 'map: "vendor gt/drp v2"\n')))["map"] \
        == "vendor gt/drp v2"
    rejects(minimal(BODY + "map: 42\n"), "map must be a string, got 42")
    rejects(minimal(BODY + 'map: "a\\tb"\n'), "must be printable ASCII")


# The boundary


def test_ports_follow_the_instance_name():
    assert declarations(explored(REFERENCE).ports()) == [
        "gt0_drpclk_i : in std_ulogic",
        "gt0_reset_n_i : in std_ulogic",
        "gt0_target_o : out nsl_amba.apb.master_t",
        "gt0_target_i : in nsl_amba.apb.slave_t"]


def test_a_clockless_explorer_takes_no_clock_port():
    assert declarations(explored(REFERENCE, "cfg").ports()) == [
        "cfg_target_o : out nsl_amba.apb.master_t",
        "cfg_target_i : in nsl_amba.apb.slave_t"]
    assert explored(REFERENCE, "cfg").exported_clocks() == {}


def test_the_declared_clock_is_exported_under_its_own_name():
    assert parse(REFERENCE).exported_clocks() == {"gt0.drpclk": "gt0_drpclk_i"}


def test_the_target_bus_is_bound_straight_to_the_boundary():
    text = architecture(explored(REFERENCE))
    assert "apb_o => gt0_target_o," in text
    assert "apb_i => gt0_target_i" in text


def test_the_entity_is_named_after_the_rack_and_the_instance():
    explorer = explored(REFERENCE)
    assert explorer.entity_name("core") == "core_gt0"
    assert "entity core_gt0 is" in entity(explorer)


# The geometry


def test_the_description_becomes_the_constants_the_halves_share():
    text = architecture(explored(REFERENCE))
    assert "constant target_address_width_c : natural := 10;" in text
    assert "constant target_data_width_c : natural := 16;" in text
    assert "constant slot_count_c : natural := 4;" in text
    assert "constant timeout_c : positive := 64;" in text
    assert ("constant command_width_c : natural := command_width("
            "target_address_width_c, target_data_width_c);" in text)
    assert ("constant response_width_c : natural := response_width("
            "target_data_width_c);" in text)


def test_the_footprint_is_the_register_map_and_is_asserted():
    text = architecture(explored(REFERENCE))
    assert ("constant map_size_l2_c : natural := "
            "gatecap.bus_explorer.bus_explorer_size_l2("
            "apb_config_c.data_bus_width_l2);" in text)
    assert "assert size_l2_c = map_size_l2_c" in text
    assert "footprint_check: nsl_synthesis.assertion.synth_assert" in text


def test_the_halves_take_their_own_generics():
    text = architecture(explored(REFERENCE))
    assert "shell: gatecap.bus_explorer.bus_explorer_shell" in text
    assert "core: gatecap.bus_explorer.bus_explorer_core" in text
    # The shell owns the register file and the timeout; the core owns neither.
    assert text.count("timeout_c => timeout_c") == 1
    assert text.count("fingerprint_c => fingerprint_c") == 1


# The crossings


def test_each_stream_crosses_in_its_own_direction():
    text = architecture(explored(REFERENCE))
    assert """\
  command_cdc: nsl_clocking.interdomain.interdomain_fifo_slice
    generic map(
      data_width_c => command_width_c
      )
    port map(
      reset_n_i => reset_n_i,
      clock_i(0) => clock_i,
      clock_i(1) => gt0_drpclk_i,
      in_data_i => shell_command_s,
      in_valid_i => shell_command_valid_s,
      in_ready_o => shell_command_ready_s,
      out_data_o => core_command_s,
      out_valid_o => core_command_valid_s,
      out_ready_i => core_command_ready_s
      );
""" in text
    assert """\
  response_cdc: nsl_clocking.interdomain.interdomain_fifo_slice
    generic map(
      data_width_c => response_width_c
      )
    port map(
      reset_n_i => reset_n_i,
      clock_i(0) => gt0_drpclk_i,
      clock_i(1) => clock_i,
      in_data_i => core_response_s,
      in_valid_i => core_response_valid_s,
      in_ready_o => core_response_ready_s,
      out_data_o => shell_response_s,
      out_valid_o => shell_response_valid_s,
      out_ready_i => shell_response_ready_s
      );
""" in text


def test_an_explorer_on_the_host_clock_crosses_nothing():
    explorer = explored(REFERENCE, "cfg")
    text = architecture(explorer)
    assert "interdomain" not in text
    assert "core_command_s <= shell_command_s;" in text
    assert "core_command_valid_s <= shell_command_valid_s;" in text
    assert "shell_command_ready_s <= core_command_ready_s;" in text
    assert "shell_response_s <= core_response_s;" in text
    assert "shell_response_valid_s <= core_response_valid_s;" in text
    assert "core_response_ready_s <= shell_response_ready_s;" in text
    # The core takes the host clock and the host reset.
    assert """\
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      command_i => core_command_s,""" in text


def test_an_explorer_whose_clock_the_rack_rides_crosses_nothing():
    text = REFERENCE.replace("mode: apb", "mode: apb\n  clock: gt0.drpclk")
    explorer = explored(text)
    rendered = architecture(explorer)
    assert "interdomain" not in rendered
    # The clock port stays: it is where the rack takes its own clock from.
    assert "gt0_drpclk_i : in std_ulogic;" in entity(explorer)
    assert """\
    port map(
      clock_i => clock_i,
      reset_n_i => gt0_reset_n_i,
      command_i => core_command_s,""" in rendered


def test_crossings_bring_their_dependency_and_nothing_else_does():
    assert "nsl_clocking.interdomain" in explored(REFERENCE).deps()
    collapsed = explored(REFERENCE, "cfg").deps()
    assert "nsl_clocking.interdomain" not in collapsed
    assert "gatecap.bus_explorer" in collapsed


# The descriptor


def test_the_envelope_publishes_the_target_and_its_map():
    assert explored(REFERENCE).envelope("data_bus_width_l2_c") == """\
gatecap.bus_explorer.bus_explorer_envelope(
  size_l2 => gatecap.bus_explorer.bus_explorer_size_l2(data_bus_width_l2_c),
  name => "gt0",
  children => child_map(
    sibling_entry("engine", 0, gatecap.bus_explorer.bus_explorer_block_desc)),
  address_width => 10,
  data_width => 16,
  slot_count => 4,
  map_id => "xilinx-gtye4-drp")"""


def test_an_unnamed_map_is_an_empty_text():
    envelope = explored(REFERENCE, "cfg").envelope("data_bus_width_l2_c")
    assert 'map_id => ""' in envelope
    assert "slot_count => 8" in envelope


# The rack around it


def test_the_rack_emits_one_file_per_explorer_and_instantiates_them():
    files = RackAssembly(parse(REFERENCE)).files()
    assert "explorer_rack_gt0.vhd" in files
    assert "explorer_rack_cfg.vhd" in files
    backplane = files["explorer_rack_backplane.vhd"]
    assert "gt0_explorer: explorer_rack_gt0" in backplane
    assert "cfg_explorer: explorer_rack_cfg" in backplane
    # Two instances, two segments, each on its own APB leg.
    assert "apb_i => dout_s(1)" in backplane
    assert "apb_i => dout_s(2)" in backplane
    assert "gt0_target_o => gt0_target_o" in backplane
    package = files["explorer_pkg.pkg.vhd"]
    assert "function gt0_envelope return byte_string;" in package
    assert "component explorer_rack_gt0 is" in package
    assert "gatecap.bus_explorer" in files["explorer_pkg.gbs.yaml"]


def test_the_rack_boundary_carries_every_instance_s_ports():
    rack = RackAssembly(parse(REFERENCE))
    assert declarations(rack.ports())[-6:] == [
        "gt0_drpclk_i : in std_ulogic",
        "gt0_reset_n_i : in std_ulogic",
        "gt0_target_o : out nsl_amba.apb.master_t",
        "gt0_target_i : in nsl_amba.apb.slave_t",
        "cfg_target_o : out nsl_amba.apb.master_t",
        "cfg_target_i : in nsl_amba.apb.slave_t"]


def test_the_manifest_pulls_the_gateware_in(tmp_path):
    rack = RackAssembly(parse(REFERENCE))
    assert "gatecap.bus_explorer" in rack.deps()
    assert "nsl_clocking.interdomain" in rack.deps()
    rack.write(tmp_path)
    assert "  - gatecap.bus_explorer\n" \
        in (tmp_path / "explorer_pkg.gbs.yaml").read_text()
