"""Assembly of a description into a rack: the package functions the map is
allocated from, the backplane, the rack wrapper and the emitted directory.

What sits behind an instrument's APB port is the instrument's own business and
is tested with the instrument (see test_generator_logic_analyzer.py).

Run: python3.13 -m pytest host/tests/test_generator_assembly.py
"""

import pathlib

import pytest

from acrobe_plugin.gatecap.generator import (DescriptionError,
                                             DescriptionParser, RackAssembly)
from acrobe_plugin.gatecap.generator.vhdl import Emitter


DESCRIPTIONS = pathlib.Path(__file__).parent / "descriptions"


def described(name):
    return DescriptionParser.load_file(DESCRIPTIONS / f"{name}.yaml")


def assembled(name):
    return RackAssembly(described(name))


def rendered(node):
    return Emitter.render(node)


def package(rack):
    return rendered(rack.package_file())


def backplane(rack):
    return rendered(rack.backplane_architecture())


def statement_of(statements, label):
    """The rendered statement carrying ``label``, comment included."""
    for statement in statements:
        text = rendered(statement)
        if any(line.startswith(f"{label}:") for line in text.split("\n")):
            return text
    raise AssertionError(f"no statement labelled {label}")


# The package: where the map comes from


def test_the_envelope_is_a_function_of_the_instance_generics():
    # A probed stream carries its width in a generic, so the instrument's
    # footprint is only known once the rack elaborates: the envelope cannot be
    # a package constant.
    text = package(assembled("two_domains"))
    assert ("function la_envelope(\n"
            "    la_control_command_config_c : "
            "nsl_amba.axi4_stream.config_t;\n"
            "    la_control_response_config_c : "
            "nsl_amba.axi4_stream.config_t) return byte_string;") in text
    # Nothing dimensions the analyzer of a fixed-width description, and the
    # function takes no argument at all.
    assert "function la_envelope return byte_string;" in package(
        assembled("single_domain"))


def test_the_descriptor_keys_every_envelope_by_its_allocated_base():
    text = package(assembled("single_domain"))
    assert ("constant envelopes_c : byte_string := probe_capture_envelopes;\n"
            "    constant segment_base_c : base_vector := "
            "segment_bases(envelopes_c, rom_size_l2_c);") in text
    assert ("return\n"
            "      rack_compose(\n"
            "        instrument_entry(segment_base_c(0), "
            "envelope_nth(envelopes_c, 0)));") in text


def test_the_default_configuration_spans_the_allocated_extent():
    text = package(assembled("single_domain"))
    assert ("address_width => nsl_math.arith.max(24, "
            "nsl_math.arith.log2(segment_extent(envelopes_c, "
            "rom_size_l2_c))),") in text
    assert "data_bus_width => 8 * 2**data_bus_width_l2_c," in text


def test_the_package_declares_what_the_backplane_instantiates():
    text = package(assembled("two_domains"))
    for component in ("component link_capture_la is",
                      "component link_capture_backplane is",
                      "component link_capture is"):
        assert component in text
    # A stream configuration generic has no default: a value that happens to
    # elaborate would hide a mismatch with the probed bus.
    assert "config_t :=" not in text


# The backplane


def test_the_backplane_allocates_and_reports_the_map():
    text = backplane(assembled("single_domain"))
    assert ("constant envelopes_c : byte_string := "
            "probe_capture_envelopes;") in text
    assert ("constant segment_base_c : base_vector := "
            "segment_bases(envelopes_c, rom_size_l2_c);") in text
    assert ("constant allocation_c : base_vector := "
            "reported(envelopes_c, segment_base_c);") in text
    assert 'report "rack segment "' in text


def test_one_routing_prefix_per_segment():
    text = backplane(assembled("single_domain"))
    assert "segment_prefix(apb_config_c.address_width, 0, rom_size_l2_c)" \
        in text
    assert ("segment_prefix(\n"
            "        apb_config_c.address_width,\n"
            "        allocation_c(0),\n"
            "        envelope_size_l2(envelope_nth(envelopes_c, 0)))") in text


def test_the_backplane_checks_what_it_was_granted():
    text = backplane(assembled("single_domain"))
    for message in ("the descriptor must fit the ROM segment",
                    "the APB configuration must span the allocated map",
                    "the APB configuration must carry the rack's word width"):
        assert f'report "{message}"' in text
        assert f'message_c => "{message}"' in text
    assert "descriptor_fits_check: nsl_synthesis.assertion.synth_assert" in text


def test_the_fingerprint_is_taken_over_the_composed_blob():
    text = backplane(assembled("single_domain"))
    assert ("constant fingerprint_c : unsigned(31 downto 0) := "
            "descriptor_fingerprint(descriptor_c);") in text
    assert "fingerprint_c => fingerprint_c" in text


def test_the_instrument_is_told_the_footprint_its_envelope_declares():
    # The instrument lays its own map out and asserts it against this, so the
    # published footprint and the decoded one cannot drift apart.
    text = backplane(assembled("single_domain"))
    assert ("size_l2_c => envelope_size_l2(envelope_nth(envelopes_c, 0)),"
            in text)


def test_too_many_instruments_for_the_routing_table():
    body = "".join(f"""  la{i}: !logic-analyzer
    domains:
      d:
        signals:
          a:
            trigger: true
""" for i in range(16))
    description = "name: p.e\ncommunication:\n  mode: apb\ninstruments:\n" + body
    with pytest.raises(DescriptionError) as raised:
        RackAssembly(DescriptionParser.load(description))
    assert "16 instruments need one APB segment each" in str(raised.value)
    assert RackAssembly.MAX_INSTRUMENTS == 15


# The rack wrapper


def test_a_passthrough_rack_hands_its_completer_out():
    rack = assembled("single_domain")
    assert rack.passthrough()
    text = rendered(rack.entity())
    # The geometry is a generic whose default spans the allocated map, so an
    # instantiating design may take it as it stands.
    assert ("apb_config_c : nsl_amba.apb.config_t := "
            "probe_capture_apb_config") in text
    assert "backplane: probe_capture_backplane" in statement_of(
        rack.rack_statements(), "backplane")


def test_an_adapter_rack_dimensions_its_own_completer():
    rack = assembled("two_domains")
    assert not rack.passthrough()
    text = rendered(rack.rack_architecture())
    assert ("constant apb_config_c : nsl_amba.apb.config_t :=\n"
            "    link_capture_apb_config(\n"
            "      la_control_command_config_c,\n"
            "      la_control_response_config_c);") in text
    adapter = statement_of(rack.rack_statements(), "adapter")
    assert "adapter: gatecap.adapter_stream.stream_adapter" in adapter
    assert "descriptor_base_c => 0" in adapter


def test_every_instrument_port_crosses_the_rack_untouched():
    rack = assembled("two_domains")
    text = statement_of(rack.rack_statements(), "backplane")
    for port in ("la_control_clock_i", "la_transceiver_rx_clock_i",
                 "la_control_command_i", "la_transceiver_rx_k_i"):
        assert f"{port} => {port}" in text


def test_a_rack_on_an_exported_clock_keeps_only_its_reset():
    rack = assembled("single_domain")
    names = [port.name for port in rack.ports()]
    assert names[0] == "reset_n_i" and "clock_i" not in names
    # The clock is the analyzer's own domain port, bound structurally.
    assert rack.clocks.clock == "la_sample_clock_i"


def test_a_rack_with_no_exported_clock_adds_its_own_ports():
    rack = assembled("mixed_storage")
    names = [port.name for port in rack.ports()]
    assert names[:2] == ["clock_i", "reset_n_i"]


# Transports


def test_a_jtag_rack_carries_the_tap_pins_and_their_tie_offs(tmp_path):
    assembled("jtag_transport").write(tmp_path)
    text = (tmp_path / "probe_pkg.pkg.vhd").read_text()
    assert """\
      chip_tck_i : in std_ulogic := '0';
      chip_tms_i : in std_ulogic := '0';
      chip_tdi_i : in std_ulogic := '0';
      chip_tdo_o : out std_ulogic;""" in text
    # No clock key in the description: the transport has ports of its own.
    assert "clock_i : in std_ulogic;\n      reset_n_i : in std_ulogic;" in text
    manifest = (tmp_path / "probe_pkg.gbs.yaml").read_text()
    assert "  - gatecap.adapter_jtag\n" in manifest


def test_a_swd_rack_carries_its_two_pins(tmp_path):
    rack = assembled("swd_transport")
    rack.write(tmp_path)
    text = (tmp_path / "probe_pkg.pkg.vhd").read_text()
    assert """\
      swd_i : in nsl_coresight.swd.swd_slave_i;
      swd_o : out nsl_coresight.swd.swd_slave_o;""" in text
    # The transport rides the analyzer's clock, so the rack has no clock port
    # of its own, and the adapter asks for no generic at all.
    entity = rendered(rack.entity())
    assert "clock_i : in std_ulogic;\n    reset_n_i : in std_ulogic;" \
        not in entity
    assert rack.generics() == ()
    manifest = (tmp_path / "probe_pkg.gbs.yaml").read_text()
    for dep in ("gatecap.adapter_swd", "nsl_coresight.swd"):
        assert f"  - {dep}\n" in manifest


def test_a_spi_rack_carries_its_four_pins_and_both_rates(tmp_path):
    rack = assembled("spi_transport")
    rack.write(tmp_path)
    text = (tmp_path / "probe_pkg.pkg.vhd").read_text()
    assert """\
      spi_i : in nsl_spi.spi.spi_slave_i;
      spi_o : out nsl_spi.spi.spi_slave_o;""" in text
    # The clock the transport rides is the analyzer's, so the rack takes no
    # clock port and no generic of its own at all.
    entity = rendered(rack.entity())
    assert "clock_i : in std_ulogic;\n    reset_n_i : in std_ulogic;" \
        not in entity
    assert rack.generics() == ()
    adapter = statement_of(rack.rack_statements(), "adapter")
    assert "clock_frequency_c => 100_000_000" in adapter
    assert "sck_max_rate_c => 10_000_000" in adapter
    manifest = (tmp_path / "probe_pkg.gbs.yaml").read_text()
    for dep in ("gatecap.adapter_spi", "nsl_spi.spi"):
        assert f"  - {dep}\n" in manifest


def test_a_usb_rack_is_a_device_on_the_bus(tmp_path):
    rack = assembled("usb_transport")
    rack.write(tmp_path)
    text = (tmp_path / "probe_pkg.pkg.vhd").read_text()
    for line in ("      usb_o : out nsl_usb.io.usb_io_c;",
                 "      usb_i : in nsl_usb.io.usb_io_s;",
                 "      online_o : out std_ulogic;"):
        assert line in text
    # The transport rides the capture domain's clock, so the rack's only
    # generic is the host's read budget.
    assert [generic.name for generic in rack.generics()] == [
        "burst_length_l2_c"]
    adapter = statement_of(rack.rack_statements(), "adapter")
    # The rack's identity on the bus is its own descriptor's fingerprint.
    assert "fingerprint_c => descriptor_fingerprint(usb_capture_descriptor)" \
        in adapter
    assert "clock_frequency_c => 60_000_000" in adapter
    manifest = (tmp_path / "probe_pkg.gbs.yaml").read_text()
    for dep in ("gatecap.adapter_usb", "nsl_usb.io"):
        assert f"  - {dep}\n" in manifest


def test_a_usb_rack_refuses_a_clock_the_phy_cannot_use():
    with pytest.raises(DescriptionError):
        RackAssembly(DescriptionParser.load("""
name: p.e
communication:
  mode: usb
  clock: la.sample
instruments:
  la: !logic-analyzer
    domains:
      sample:
        clock: clock
        frequency: 100_000_000
        signals:
          a:
            trigger: true
"""))


def test_a_serial_rack_takes_the_rate_from_the_clock_it_rides():
    rack = assembled("serial_transport")
    # The transport rides a domain whose description states its rate, so the
    # rate is a literal and only the baud rate is asked of the design.
    assert [generic.name for generic in rack.generics()] == [
        "baud_rate_c", "burst_length_l2_c"]
    adapter = statement_of(rack.rack_statements(), "adapter")
    assert "clock_frequency_c => 100_000_000" in adapter


def test_a_serial_rack_on_a_rateless_clock_takes_the_rate_as_a_generic():
    rack = RackAssembly(DescriptionParser.load("""
name: p.e
communication:
  mode: serial_hdlc
instruments:
  la: !logic-analyzer
    domains:
      ctrl:
        signals:
          a:
            trigger: true
"""))
    assert [generic.name for generic in rack.generics()] == [
        "clock_frequency_c", "baud_rate_c", "burst_length_l2_c"]
    assert "clock_frequency_c => clock_frequency_c" in statement_of(
        rack.rack_statements(), "adapter")


# Emission


def test_generated_directory_holds_the_rack_and_what_it_instantiates(tmp_path):
    rack = assembled("two_domains")
    written = rack.write(tmp_path / "core")
    assert [pathlib.Path(path).name for path in written] == [
        "link_capture_la.vhd", "link_pkg.pkg.vhd",
        "link_capture_backplane.vhd", "link_capture.vhd", "link_pkg.gbs.yaml"]
    manifest = (tmp_path / "core" / "link_pkg.gbs.yaml").read_text()
    assert manifest.startswith("sources:\n  - file_type: vhdl\n    files:\n"
                               "      - link_capture_la.vhd\n"
                               "      - link_pkg.pkg.vhd\n"
                               "      - link_capture_backplane.vhd\n"
                               "      - link_capture.vhd\n")
    for dep in ("gatecap.adapter_stream", "gatecap.axi4_stream_packer",
                "gatecap.capture", "gatecap.control", "gatecap.descriptor",
                "gatecap.trace", "nsl_amba.apb_routing",
                "nsl_clocking.interdomain", "nsl_synthesis.assertion"):
        assert f"  - {dep}\n" in manifest


def test_emission_is_deterministic(tmp_path):
    first = assembled("two_domains").files()
    second = RackAssembly(described("two_domains")).files()
    assert first == second
    assembled("two_domains").write(tmp_path)
    for name, contents in first.items():
        assert (tmp_path / name).read_text() == contents
