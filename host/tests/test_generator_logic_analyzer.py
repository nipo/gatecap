"""The `!logic-analyzer` instrument: its internal map, its descriptor
envelope, the crossings it makes and the shared-trigger topology it wires.

Run: python3.13 -m pytest host/tests/test_generator_logic_analyzer.py
"""

import pathlib

import pytest

from acrobe_plugin.gatecap.generator import (DescriptionError,
                                             DescriptionParser, RackAssembly)
from acrobe_plugin.gatecap.generator.vhdl import Emitter
from acrobe_plugin.gatecap.instrument.la import Analyzer
from acrobe_plugin.gatecap.instrument.la.generator.layout import BlockLayout


DESCRIPTIONS = pathlib.Path(__file__).parent / "descriptions"


def described(name):
    return DescriptionParser.load_file(DESCRIPTIONS / f"{name}.yaml")


def analyzed(name, instrument="la"):
    """The analyzer as the rack contributes it: with the exported clock the
    rack runs on, when it runs on one of this instance's domains."""
    rack = RackAssembly(described(name))
    entry = rack.description.instrument(instrument)
    return Analyzer(entry, rack.host_clock(entry))


def rendered(node):
    return Emitter.render(node)


def architecture(analyzer):
    return rendered(analyzer.architecture("core"))


def envelope(analyzer):
    """The envelope function body as the rack emits it, declarations and
    all."""
    return "\n".join(rendered(item) for item in analyzer.geometry()
                     + analyzer.offsets()) + "\n" + analyzer.envelope()


def statement_of(analyzer, label):
    for statement in analyzer.statements():
        text = rendered(statement)
        if any(line.startswith(f"{label}:") for line in text.split("\n")):
            return text
    raise AssertionError(f"no statement labelled {label}")


# The internal map


def test_blocks_follow_the_description_order():
    layout = analyzed("two_domains").layout
    assert [block.key for block in layout.blocks] == [
        "control.control", "control.buffer", "control.trigger",
        "transceiver_rx.control", "transceiver_rx.buffer"]
    assert layout.index_bits() == 3


def test_a_trigger_only_domain_takes_a_single_region():
    blocks = analyzed("mixed_storage").layout.of("sync")
    assert blocks.control is None and blocks.buffer is None
    assert blocks.trigger.key == "sync.trigger"
    assert blocks.trigger.master() == "dout_s(0)"


def test_region_size_covers_every_buffer():
    text = architecture(analyzed("two_domains"))
    assert ("constant region_l2_c : natural :=\n"
            "    nsl_math.arith.max(\n"
            "      nsl_math.arith.max(12, control_buffer_size_l2_c),\n"
            "      transceiver_rx_buffer_size_l2_c);") in text
    # Offsets are the region index scaled by that size, so widening a buffer
    # moves every block together.
    assert ("constant transceiver_rx_buffer_offset_c : natural := "
            "4 * 2**region_l2_c;") in envelope(analyzed("two_domains"))


def test_the_footprint_is_the_map_the_analyzer_decodes():
    text = architecture(analyzed("single_domain"))
    assert ("constant map_size_l2_c : natural := region_l2_c + index_bits_c;"
            in text)
    # The declared footprint is a generic; the two are asserted equal, in
    # simulation and under synthesis alike.
    assert ("assert size_l2_c = map_size_l2_c\n"
            "    report \"the declared footprint must match the map the "
            "analyzer decodes\"\n"
            "    severity failure;") in text
    assert "footprint_check: nsl_synthesis.assertion.synth_assert" in text
    assert "assert apb_config_c.address_width >= map_size_l2_c" in text


def test_one_routing_prefix_per_block():
    text = architecture(analyzed("single_domain"))
    assert text.count("block_prefix(apb_config_c.address_width, "
                      "map_size_l2_c, index_bits_c, ") == 3


def test_too_many_blocks_for_the_routing_table():
    domains = "".join(f"""      d{i}:
        signals:
          a{i}:
            trigger: true
""" for i in range(6))
    description = ("name: p.e\ncommunication:\n  mode: apb\ninstruments:\n"
                   "  la: !logic-analyzer\n    domains:\n" + domains)
    with pytest.raises(DescriptionError) as raised:
        RackAssembly(DescriptionParser.load(description))
    assert "18 blocks need one APB region each" in str(raised.value)
    assert BlockLayout.MAX_BLOCKS == 16


# The envelope


def test_the_envelope_names_every_block_by_domain():
    text = envelope(analyzed("two_domains"))
    for entry in ('"control.buffer"', '"control.control"',
                  '"control.trigger"', '"transceiver_rx.buffer"',
                  '"transceiver_rx.control"'):
        assert f"sibling_entry(\n      {entry}," in text
    # A subscribing control points at the domain hosting the trigger.
    assert ('      control_desc(\n'
            '        buffer_name => "transceiver_rx.buffer",\n'
            '        trigger_name => "control.trigger",') in text


def test_the_envelope_tail_lists_the_controls_one_arm_covers():
    assert analyzed("two_domains").envelope().endswith(
        'control_names => "control.control,transceiver_rx.control")')
    assert analyzed("single_domain").envelope().endswith(
        'control_names => "sample.control")')


def test_the_tail_skips_a_domain_that_only_triggers():
    assert analyzed("mixed_storage").control_names() == ["slow.control",
                                                         "fast.control"]


def test_the_envelope_declares_the_footprint_the_entity_decodes():
    text = analyzed("single_domain").envelope()
    assert "size_l2 => map_size_l2_c," in text
    assert 'name => "la",' in text


# Structural special cases


def test_a_domain_on_the_racks_clock_has_no_crossing():
    analyzer = analyzed("single_domain")
    cluster = analyzer.clusters["sample"]
    assert cluster.down.direct() and cluster.up.direct()
    assert cluster.down.statements == [] and cluster.up.statements == []
    text = architecture(analyzer)
    assert "nsl_clocking" not in text
    assert "interdomain_tick_latency_c" not in text
    # The control reads the capture domain's own signals.
    assert "state_i => sample_state_s," in text
    assert "head_i => sample_head_s," in text


def test_a_domain_off_the_racks_clock_crosses_every_signal():
    analyzer = analyzed("two_domains")
    assert statement_of(analyzer, "transceiver_rx_arm_cap_cdc") == """\
transceiver_rx_arm_cap_cdc: nsl_clocking.interdomain.interdomain_tick
  port map(
    input_clock_i => clock_i,
    output_clock_i => la_transceiver_rx_clock_i,
    input_reset_n_i => reset_n_i,
    tick_i => transceiver_rx_arm_s,
    tick_o => transceiver_rx_arm_cap_s
    );
"""
    assert statement_of(analyzer, "transceiver_rx_state_host_cdc") == """\
transceiver_rx_state_host_cdc: nsl_clocking.interdomain.interdomain_reg
  generic map(
    data_width_c => 2,
    stable_count_c => 2
    )
  port map(
    clock_i => clock_i,
    data_i => transceiver_rx_state_s,
    data_o => transceiver_rx_state_host_s
    );
"""
    # The head is held in the capture domain, then resynchronised alongside a
    # completion tick.
    body = statement_of(analyzer, "transceiver_rx_head_hold")
    assert "if rising_edge(la_transceiver_rx_clock_i) then" in body
    assert "transceiver_rx_head_hold_s <= transceiver_rx_head_s;" in body
    assert "head_i => unsigned(transceiver_rx_head_host_s)," \
        in architecture(analyzer)


def test_configuration_crosses_as_set_and_hold():
    assert statement_of(analyzed("two_domains"),
                        "transceiver_rx_capture_len_cap_cdc") == """\
transceiver_rx_capture_len_cap_cdc: \
nsl_clocking.interdomain.interdomain_static_reg
  generic map(
    data_width_c => transceiver_rx_capture_len_width_c
    )
  port map(
    input_clock_i => clock_i,
    data_i => std_ulogic_vector(transceiver_rx_capture_len_s),
    data_o => transceiver_rx_capture_len_cap_s
    );
"""


def test_a_subscribing_domain_gets_no_trigger_block():
    analyzer = analyzed("two_domains")
    assert set(analyzer.topology.hosts) == {"control"}
    text = architecture(analyzer)
    assert text.count(": gatecap.control.trigger_control_edge\n") == 1
    assert "transceiver_rx_trigger_signals_s" not in text


def test_a_trigger_only_domain_gets_no_control_or_buffer():
    analyzer = analyzed("mixed_storage")
    assert "sync" not in analyzer.clusters
    text = architecture(analyzer)
    assert "sync_control" not in text and "sync_buffer" not in text
    assert "sync_trigger: gatecap.control.trigger_control" in text


def test_storage_style_picks_the_block_pair():
    text = architecture(analyzed("mixed_storage"))
    assert "slow_control: gatecap.control.capture_control_rle" in text
    assert "slow_core: gatecap.capture.capture_core_rle" in text
    assert "fast_buffer: gatecap.trace.trace_buffer_packed" in text
    assert "fast_control: gatecap.control.capture_control\n" in text
    # The RLE tag rides with each line, so the buffer is one bit wider than
    # the probe vector.
    assert "constant slow_line_width_c : natural := slow_signal_count_c + 1;" \
        in text
    assert "constant fast_line_width_c : natural := fast_signal_count_c;" \
        in text


# Shared trigger


def test_the_hosting_trigger_waits_for_every_subscriber():
    analyzer = analyzed("two_domains")
    text = architecture(analyzer)
    assert ("control_trigger_enable_s <= control_ready_s and "
            "transceiver_rx_ready_in_control_s;") in text
    assert statement_of(analyzer, "transceiver_rx_ready_in_control_cdc") == """\
transceiver_rx_ready_in_control_cdc: nsl_clocking.interdomain.interdomain_reg
  generic map(
    data_width_c => 1
    )
  port map(
    clock_i => clock_i,
    data_i(0) => transceiver_rx_ready_s,
    data_o(0) => transceiver_rx_ready_in_control_s
    );
"""
    assert "enable_i => control_trigger_enable_s," in text


def test_the_trigger_tick_fans_out_to_the_subscriber():
    analyzer = analyzed("two_domains")
    assert statement_of(analyzer, "transceiver_rx_trigger_cdc") == """\
transceiver_rx_trigger_cdc: nsl_clocking.interdomain.interdomain_tick
  port map(
    input_clock_i => clock_i,
    output_clock_i => la_transceiver_rx_clock_i,
    input_reset_n_i => la_control_reset_n_i,
    tick_i => control_trigger_s,
    tick_o => transceiver_rx_trigger_s
    );
"""
    # In its own domain the trigger reaches the core by wire.
    assert "trigger_i => control_trigger_s," in architecture(analyzer)


def test_integration_latency_is_the_crossing_depth():
    text = architecture(analyzed("two_domains"))
    assert "constant interdomain_tick_latency_c : natural := 3;" in text
    assert ("constant control_trigger_latency_c : natural := "
            "gatecap.control.trigger_control_edge_latency_c + 0;") in text
    assert ("constant transceiver_rx_trigger_latency_c : natural := "
            "gatecap.control.trigger_control_edge_latency_c + "
            "interdomain_tick_latency_c;") in text
    # The same depth is what the descriptor reports to the host.
    assert "integration_latency => interdomain_tick_latency_c)" in envelope(
        analyzed("two_domains"))


def test_a_self_triggering_domain_enables_the_trigger_directly():
    text = architecture(analyzed("single_domain"))
    assert "enable_i => sample_ready_s," in text
    assert "sample_trigger_enable_s" not in text


def test_the_trigger_watches_its_own_vector():
    text = architecture(analyzed("single_domain"))
    assert ("sample_trigger_signals_s <= std_ulogic_vector'(0 => "
            "la_sample_busy_i) & la_sample_state_i;") in text
    assert ("constant sample_trigger_signal_names_c : string := "
            '"state[0:2]<RESET,IDLE,RUN,DONE>" & "," & "busy";') in text
    assert "assert sample_trigger_signal_count_c <= 32" in text


# Probe vectors


def test_the_capture_vector_stacks_probes_low_bits_first():
    text = architecture(analyzed("single_domain"))
    assert ("sample_signals_s <=\n"
            "    la_sample_data_i & std_ulogic_vector'(0 => la_sample_busy_i) "
            "&\n    la_sample_state_i;") in text
    assert ("constant sample_signal_count_c : natural := 3 + 1 + 16;") in text
    assert ('constant sample_signal_names_c : string := '
            '"state[0:2]<RESET,IDLE,RUN,DONE>" & "," & "busy" & "," & '
            '"data[0:15]";') in text


def test_a_stream_contributes_expressions_not_numbers():
    text = architecture(analyzed("two_domains"))
    assert ("gatecap.axi4_stream_packer.axis_length("
            "la_control_command_config_c, \"idskouvlr\")") in text
    assert ("gatecap.axi4_stream_packer.axis_pack("
            "la_control_command_config_c, \"vlr\", la_control_command_i)") \
        in text
    assert ('"command.{" & gatecap.axi4_stream_packer.axis_names('
            'la_control_command_config_c, "vlr") & "}"') in text


# Several analyzers in one rack


TWO_ANALYZERS = """
name: p.e
communication:
  mode: apb
instruments:
  front: !logic-analyzer
    storage: {buffer_depth_l2: 6}
    domains:
      control:
        clock: clock
        frequency: 100_000_000
        signals:
          state: !bus
            width: 4
            trigger: true
  back: !logic-analyzer
    storage: {buffer_depth_l2: 8}
    domains:
      control:
        clock: sample
        frequency: 50_000_000
        signals:
          word: !bus
            width: 8
            trigger: true
"""


def test_two_analyzers_coexist_in_one_rack(tmp_path):
    rack = RackAssembly(DescriptionParser.load(TWO_ANALYZERS))
    # Two entities, one per instance, and both instantiated by the backplane.
    assert set(rack.plugin_files()) == {"e_front.vhd", "e_back.vhd"}
    text = Emitter.render(rack.backplane_architecture())
    assert "front_analyzer: e_front" in text
    assert "back_analyzer: e_back" in text
    assert "apb_i => dout_s(1)" in text and "apb_i => dout_s(2)" in text
    # Same domain name on both, distinct ports and distinct segments.
    names = [port.name for port in rack.ports()]
    assert "front_control_clock_i" in names and "back_control_sample_i" in names
    package = Emitter.render(rack.package_file())
    assert "function front_envelope return byte_string;" in package
    assert "function back_envelope return byte_string;" in package
    assert ("instrument_entry(segment_base_c(0), "
            "envelope_nth(envelopes_c, 0)),") in package
    assert ("instrument_entry(segment_base_c(1), "
            "envelope_nth(envelopes_c, 1)));") in package
    # Each analyzer names its own children, and they scope to it.
    front, back = (Analyzer(rack.description.instrument(name))
                   for name in ("front", "back"))
    assert front.control_names() == back.control_names() == ["control.control"]
    assert front.entity_name("e") == "e_front"
    rack.write(tmp_path)
    assert (tmp_path / "e_front.vhd").exists()
    assert (tmp_path / "e_back.vhd").exists()
