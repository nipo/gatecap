"""Description parsing and validation: the rack the framework owns, and the
body the logic-analyzer plugin validates under it.

Run: python3.13 -m pytest host/tests/test_generator_description.py
"""

import textwrap

import pytest

from acrobe_plugin.gatecap.generator import (Description, DescriptionError,
                                             DescriptionParser)
from acrobe_plugin.gatecap.instrument.la.generator.schema import EnumSpec


# The reference description for every good case: one rack, one analyzer.
REFERENCE = """
name: my_pkg.my_capture

communication:
  mode: axi4_stream
  clock: la.control

instruments:
  la: !logic-analyzer
    storage:
      buffer_depth_l2: 12
      rle: false
      packed: false

    capture:
      max_windows: 1

    trigger:
      capabilities: edge

    domains:
      control:
        clock: clock
        frequency: 25_000_000

        signals:
          command: !axi4-stream
            trace: idskouvlr
            trigger: vlr
          response: !axi4-stream
            trace: idskouvlr
          state: !bus
            width: 2
            trigger: true
            enum:
              0: RESET
              1: IDLE
              2: COMMAND
              3: RESPONSE

      transceiver_rx:
        clock: clock
        frequency: 125_000_000
        storage:
          buffer_depth_l2: 10
        trigger:
          from: control

        signals:
          word: !bus
            width: 8
          k: {}
"""


def parse(text):
    return DescriptionParser.load(text)


def rejects(text, fragment):
    with pytest.raises(DescriptionError) as raised:
        parse(text)
    assert fragment in str(raised.value), str(raised.value)
    return str(raised.value)


def minimal(domains, communication="mode: apb", top=""):
    """A rack holding one analyzer: the dimensioning sections and the domains
    sit in the instrument's body, where the plugin validates them."""
    body = textwrap.indent(top + "domains:\n" + domains, "    ",
                           lambda line: line.strip() != "")
    return (f"name: p.e\n\ncommunication:\n  {communication}\n\n"
            f"instruments:\n  la: !logic-analyzer\n{body}")


def instrument(description, name="la"):
    return description.instrument(name).params


def domains(description, name="la"):
    return instrument(description, name)["domains"]


def domain(description, domain_name, name="la"):
    for entry in domains(description, name):
        if entry.name == domain_name:
            return entry
    raise KeyError(domain_name)


ONE_DOMAIN = """  d:
    signals:
      a:
        trigger: true
"""


def test_reference_description_parses():
    d = parse(REFERENCE)
    assert isinstance(d, Description)
    assert (d.name.package, d.name.entity) == ("my_pkg", "my_capture")
    assert d.communication.mode == "axi4_stream"
    assert d.communication.clock_export == "la.control"
    assert [x.name for x in d.instruments] == ["la"]
    assert [x.name for x in domains(d)] == ["control", "transceiver_rx"]


def test_ports_follow_instance_domain_and_signal_names():
    d = parse(REFERENCE)
    # The instance name prefixes every boundary port, so two analyzers may
    # both hold a domain called "control".
    control = domain(d, "control").scoped("la")
    assert control.clock_port() == "la_control_clock_i"
    assert control.reset_port() == "la_control_reset_n_i"
    assert [p.port_name() for p in control.probes] == [
        "la_control_command_i", "la_control_response_i", "la_control_state_i"]
    assert [port.name for port in d.instruments[0].plugin.ports(
        d.instruments[0])][:2] == ["la_control_clock_i",
                                   "la_control_reset_n_i"]


def test_domain_overrides_dimensioning_defaults():
    d = parse(REFERENCE)
    assert instrument(d)["storage"].buffer_depth_l2 == 12
    assert domain(d, "control").storage.buffer_depth_l2 == 12
    assert domain(d, "transceiver_rx").storage.buffer_depth_l2 == 10
    # Unset keys of an overriding section keep the instrument-level value.
    assert domain(d, "transceiver_rx").storage.rle is False
    assert domain(d, "transceiver_rx").capture.max_windows == 1
    assert domain(d, "transceiver_rx").trigger.capabilities == "edge"


def test_defaults_apply_when_sections_are_absent():
    d = parse(minimal(ONE_DOMAIN))
    storage = instrument(d)["storage"]
    assert storage.buffer_depth_l2 == 10
    assert storage.rle is False and storage.packed is False
    assert instrument(d)["capture"].max_windows == 1
    assert instrument(d)["trigger"].capabilities == "value"
    assert domain(d, "d").clock == "clock"
    assert domain(d, "d").frequency == 0


def test_domain_overrides_trigger_capabilities():
    d = parse(minimal("""  d:
    trigger:
      capabilities: edge
    signals:
      a:
        trigger: true
""", top="trigger:\n  capabilities: value\n"))
    assert instrument(d)["trigger"].capabilities == "value"
    assert domain(d, "d").trigger.capabilities == "edge"
    assert domain(d, "d").trigger.edge()


def test_trigger_topology_hosting_and_subscribing():
    d = parse(REFERENCE)
    control, rx = domains(d)
    assert control.hosts_trigger() and not control.subscribes()
    assert [p.name for p in control.trigger_probes()] == ["command", "state"]
    assert rx.subscribes() and rx.trigger_from == "control"
    assert not rx.hosts_trigger()
    assert rx.captures()


def test_trace_false_keeps_a_probe_out_of_the_capture_vector():
    d = parse(minimal("""  d:
    signals:
      a:
        trigger: true
        trace: false
      b: {}
"""))
    probes = domain(d, "d").probes
    assert [p.name for p in domain(d, "d").traced_probes()] == ["b"]
    assert probes[0].triggered() and not probes[0].traced


def test_deps_collect_the_instrument_plugin_keys():
    # The transport's own keys are not here: the rack adds the partition it
    # instantiates the adapter from.
    deps = parse(REFERENCE).deps()
    assert "nsl_amba.axi4_stream" in deps
    assert "gatecap.axi4_stream_packer" in deps
    assert "gatecap.capture" in deps and "gatecap.descriptor" in deps
    assert "gatecap.axi4_stream_packer" not in parse(minimal(ONE_DOMAIN)).deps()


def test_bnoc_widths_are_static_and_count_towards_the_trigger_cap():
    d = parse(minimal("""  d:
    signals:
      command: !bnoc-framed
        trigger: dvl
      rx: !bnoc-pipe
        trigger: dv
"""))
    command, rx = domain(d, "d").probes
    assert command.plugin.trigger_width(command) == 10
    assert command.plugin.trace_width(command) == 11
    assert rx.plugin.trigger_width(rx) == 9
    assert rx.plugin.trace_width(rx) == 10
    # Both fit, and the parser knows they do without emitting a check.
    assert command.plugin.deps(command) == ("nsl_bnoc.framed",
                                            "gatecap.bnoc_packer")
    assert rx.plugin.deps(rx) == ("nsl_bnoc.pipe", "gatecap.bnoc_packer")


def test_reject_over_wide_bnoc_trigger():
    # Whole framed buses are 11 bits each: three of them overflow the trigger
    # vector, and the parser says so where a stream's width would only be
    # known at elaboration.
    parse(minimal("""  d:
    signals:
      a: !bnoc-framed
        trigger: dvlr
      b: !bnoc-framed
        trigger: dvlr
      c: !bnoc-pipe
        trigger: dvr
"""))
    rejects(minimal("""  d:
    signals:
      a: !bnoc-framed
        trigger: dvlr
      b: !bnoc-framed
        trigger: dvlr
      c: !bnoc-framed
        trigger: dvlr
"""), "trigger vector is 33 bits, at most 32 are supported")


def test_reject_bad_bnoc_markings():
    rejects(minimal("  d:\n    signals:\n      a: !bnoc-framed\n"),
            "!bnoc-framed takes a mapping of keys")
    rejects(minimal("  d:\n    signals:\n      a: !bnoc-pipe {trigger: true}\n"),
            "trigger for !bnoc-pipe must be a non-empty element string over "
            "[dvr]")
    rejects(minimal("  d:\n    signals:\n      a: !bnoc-pipe {trace: dvl}\n"),
            "trace element 'l' is not one of [dvr]")
    rejects(minimal("  d:\n    signals:\n      a: !bnoc-framed {trace: dvv}\n"),
            "trace element 'v' is repeated")
    rejects(minimal("  d:\n    signals:\n      a: !bnoc-framed {trace: ''}\n"),
            "trace for !bnoc-framed must be a non-empty element string")
    rejects(minimal("""  d:
    signals:
      a: !bnoc-framed
        trace: dv
        enum: {0: A}
"""), "!bnoc-framed names its own fields, an enum cannot attach to it")


def test_enum_spec_rendering():
    d = parse(REFERENCE)
    state = domain(d, "control").probes[2]
    assert state.enum.spec() == "RESET,IDLE,COMMAND,RESPONSE"
    assert state.enum.suffix() == "<RESET,IDLE,COMMAND,RESPONSE>"
    # A gap in the values pins the running index of the entries after it.
    sparse = EnumSpec.parse({0: "A", 4: "B", 5: "C"}, "x")
    assert sparse.spec() == "A,4:B,C"


def test_enum_splices_a_well_known_base():
    # A base renders as the +ns.name entry the reader resolves; the running
    # index past it is the base table's, so every own label states its value.
    spliced = EnumSpec.parse({"base": "demo.phase", 3: "custom0",
                              4: "custom1", 5: "STOP"}, "x")
    assert spliced.spec() == "+demo.phase,3:custom0,4:custom1,5:STOP"
    # The base's own values count toward the width the table needs.
    assert EnumSpec.parse({"base": "axi.size"}, "x").widest() == 7
    rejects(minimal("""  d:
    signals:
      a: !bus
        width: 2
        enum: {base: nope}
"""), "unknown enum base 'nope'")
    rejects(minimal("""  d:
    signals:
      a: !bus
        width: 2
        enum: {base: axi.size}
"""), "enum maps value 7 beyond the 2-bit signal")


def test_bare_scalar_forms_are_equivalent():
    signals = ["      k: {trigger: true}\n",
               "      k:\n        trigger: true\n",
               "      k: {trace: false, trigger: true}\n",
               "      k:\n      t:\n        trigger: true\n"]
    for entry in signals:
        d = parse(minimal("  d:\n    signals:\n" + entry))
        probe = domain(d, "d").probes[0]
        assert probe.tag is None
        assert probe.plugin.static_width(probe, "") == 1
        assert probe.plugin.trace_names(probe) == '"k"'


def test_reject_unknown_top_level_key():
    rejects(minimal(ONE_DOMAIN) + "\nextra: 1\n", "unknown key 'extra'")


def test_reject_bad_name():
    for name, message in (("nodot", "must be a dotted package.entity pair"),
                          ("p.1e", "must start with a letter"),
                          ("p.p", "package and entity must differ")):
        rejects(minimal(ONE_DOMAIN).replace("name: p.e", f"name: {name}"),
                message)


def test_reject_illegal_identifiers():
    rejects(minimal("  1st:\n    signals:\n      a: {}\n"),
            "domain name '1st' must start with a letter")
    rejects(minimal("  d:\n    signals:\n      signal: {}\n"),
            "signal name 'signal' is a VHDL reserved word")
    rejects(minimal("  d:\n    clock: clk_\n    signals:\n      a: {}\n"),
            "clock name 'clk_' must not end with an underscore")


def test_reject_wrong_tag_payload():
    rejects(minimal("  d:\n    signals:\n      a: !bus 5\n"),
            "!bus takes a mapping of keys")
    rejects(minimal("  d:\n    signals:\n      a: !axi4-stream\n"),
            "!axi4-stream takes a mapping of keys")
    rejects(minimal("  d:\n    signals:\n      a: !frobnicate {}\n"),
            "unknown signal type '!frobnicate'")


def test_reject_bad_bus_width():
    rejects(minimal("  d:\n    signals:\n      a: !bus {}\n"),
            "!bus requires a width")
    rejects(minimal("  d:\n    signals:\n      a: !bus {width: 0}\n"),
            "!bus width must be a positive integer")


def test_reject_wrong_marking_shape():
    rejects(minimal("  d:\n    signals:\n      a: !bus {width: 2, trigger: dv}\n"),
            "trigger for !bus must be true or false, got 'dv'")
    rejects(minimal("  d:\n    signals:\n      a: !axi4-stream {trigger: true}\n"),
            "trigger for !axi4-stream must be a non-empty element string")
    rejects(minimal("  d:\n    signals:\n      a: !axi4-stream {trace: dq}\n"),
            "trace element 'q' is not one of [idskouvlr]")
    rejects(minimal("  d:\n    signals:\n      a: !axi4-stream {trace: dd}\n"),
            "trace element 'd' is repeated")
    rejects(minimal("  d:\n    signals:\n      a: {trace: true}\n"),
            "trace: true is redundant")


def test_reject_signal_in_neither_vector():
    rejects(minimal("  d:\n    signals:\n      a: {trace: false}\n"),
            "signal is neither traced nor a trigger source")


def test_reject_enum_misuse():
    rejects(minimal("""  d:
    signals:
      a: !axi4-stream
        trace: dv
        enum: {0: A}
"""), "!axi4-stream names its own fields, an enum cannot attach to it")
    rejects(minimal("""  d:
    signals:
      a: !bus
        width: 2
        enum: {4: TOOBIG}
"""), "enum maps value 4 beyond the 2-bit signal")
    rejects(minimal("""  d:
    signals:
      a: !bus
        width: 2
        enum: {0: "A,B"}
"""), "must not contain")


def test_reject_dangling_trigger_source():
    rejects(minimal("""  d:
    trigger:
      from: nowhere
    signals:
      a: {}
"""), "from 'nowhere' is not a domain")
    rejects(minimal("""  d:
    trigger:
      from: d
    signals:
      a: {}
"""), "a domain cannot subscribe to its own trigger")
    # A chain: only the domain that owns the trigger signals may be a source.
    rejects(minimal("""  top:
    signals:
      a:
        trigger: true
  middle:
    trigger:
      from: top
    signals:
      b: {}
  bottom:
    trigger:
      from: middle
    signals:
      c: {}
"""), "domain 'middle' hosts no trigger")


def test_reject_missing_trigger_source():
    rejects(minimal("  d:\n    signals:\n      a: {}\n"),
            "a capturing domain needs a trigger")


def test_reject_hosting_and_subscribing():
    rejects(minimal("""  host:
    signals:
      a:
        trigger: true
  both:
    trigger:
      from: host
    signals:
      b:
        trigger: true
"""), "either hosts a trigger (signals marked trigger) or subscribes")


def test_trigger_only_domain_needs_no_trace():
    d = parse(minimal("""  d:
    signals:
      a:
        trace: false
        trigger: true
"""))
    entry = domain(d, "d")
    assert entry.hosts_trigger() and not entry.captures()


def test_reject_over_wide_static_trigger():
    rejects(minimal("""  d:
    signals:
      a: !bus
        width: 24
        trigger: true
      b: !bus
        width: 16
        trigger: true
"""), "trigger vector is 40 bits, at most 32 are supported")


def test_stream_trigger_width_is_not_checked_in_python():
    # The width is a stream-config generic, so the cap becomes an emitted
    # elaboration-time assertion instead.
    d = parse(minimal("""  d:
    signals:
      a: !axi4-stream
        trigger: idskouvlr
"""))
    probe = domain(d, "d").probes[0]
    assert probe.plugin.trigger_width(probe) is None


def test_reject_storage_conflicts():
    rejects(minimal(ONE_DOMAIN, top="storage:\n  rle: true\n  packed: true\n"),
            "rle and packed storage are mutually exclusive")
    rejects(minimal(ONE_DOMAIN,
                    top="storage:\n  rle: true\ncapture:\n  max_windows: 4\n"),
            "rle storage has a single window")
    rejects(minimal(ONE_DOMAIN, top="storage:\n  buffer_depth_l2: 0\n"),
            "buffer_depth_l2 must be at least 1")
    rejects(minimal(ONE_DOMAIN, top="trigger:\n  capabilities: fuzzy\n"),
            "capabilities must be one of value, edge")


def test_reject_communication_errors():
    rejects(minimal(ONE_DOMAIN, communication="mode: carrier_pigeon"),
            "unknown communication mode 'carrier_pigeon'")
    rejects(minimal("""  control:
    signals:
      a:
        trigger: true
""", communication="mode: apb\n  clock: control_clock"),
            "clock 'control_clock' is not an exported clock")
    rejects(minimal(ONE_DOMAIN, communication="mode: apb\n  clock: la.nope"),
            "known: la.d")
    rejects(minimal(ONE_DOMAIN, communication="clock: la.d"),
            "mode is required")
    # A key belongs to the mode that declares it, and nothing else takes it.
    rejects(minimal(ONE_DOMAIN, communication="mode: apb\n  max_rate: 1000"),
            "unknown key 'max_rate'")


def test_the_spi_mode_takes_a_rate_and_a_clock_polarity():
    described = parse(minimal(
        ONE_DOMAIN, communication="mode: spi\n  max_rate: 20_000_000"))
    assert described.communication.params == {"max_rate": 20_000_000,
                                              "spi_mode": 0}
    assert parse(minimal(
        ONE_DOMAIN,
        communication="mode: spi\n  max_rate: 1000\n  spi_mode: 3"
        )).communication.params["spi_mode"] == 3
    # The rate is what the oversampling assert is made of, so it has no
    # default to fall back on.
    rejects(minimal(ONE_DOMAIN, communication="mode: spi"),
            "max_rate is required")
    rejects(minimal(ONE_DOMAIN,
                    communication="mode: spi\n  max_rate: 0"),
            "max_rate must be at least 1")
    rejects(minimal(ONE_DOMAIN,
                    communication="mode: spi\n  max_rate: 1000\n"
                                  "  spi_mode: 4"),
            "spi_mode must be at most 3")


def test_reject_port_name_collisions():
    rejects(minimal("""  a:
    signals:
      b_c:
        trigger: true
  a_b:
    signals:
      c:
        trigger: true
"""), "port la_a_b_c_i is claimed by both")
    rejects(minimal("""  d:
    signals:
      reset_n:
        trigger: true
"""), "port la_d_reset_n_i is claimed by both")


def test_reject_malformed_documents():
    rejects("", "description is empty")
    rejects("- a\n- b\n", "description must be a mapping")
    rejects("name: p.e\ncommunication:\n  mode: apb\n",
            "a rack needs at least one instrument")
    rejects("name: p.e\ncommunication:\n  mode: apb\ninstruments: {}\n",
            "a rack needs at least one instrument")
    rejects("name: p.e\ncommunication:\n  mode: apb\ninstruments:\n  la: {}\n",
            "an instrument entry is tagged with the instrument it holds")
    rejects("name: p.e\ncommunication:\n  mode: apb\ninstruments:\n"
            "  la: !frobnicate {}\n", "unknown instrument '!frobnicate'")
    rejects(minimal("  d:\n    signals: {}\n"), "at least one signal is required")
    rejects("name: p.e\ncommunication:\n  mode: apb\ninstruments:\n"
            "  la: !logic-analyzer\n    domains: {}\n",
            "a logic analyzer needs at least one capture domain")
    with pytest.raises(DescriptionError) as raised:
        parse("name: p.e\ncommunication:\n mode: [\n")
    assert "YAML syntax error" in str(raised.value)


def test_error_carries_path_and_line():
    with pytest.raises(DescriptionError) as raised:
        parse(minimal("  d:\n    signals:\n      a: !bus 5\n"))
    error = raised.value
    assert error.path == "instruments.la.domains.d.signals.a"
    assert error.line == 11
