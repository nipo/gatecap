"""The !clock-measurer instrument plugin: schema validation and emission.

An instrument-only description is a rack, so what comes out is a package, a
backplane and a rack entity. No hardware and no simulator here -- a
description in, VHDL text out, which is where every description error is
supposed to die.

Run: python3.13 -m pytest host/tests/test_generator_clock_measurer.py
"""

import pytest

from acrobe_plugin.gatecap.generator import (DescriptionError,
                                             DescriptionParser, Generator,
                                             InstrumentRegistry)
from acrobe_plugin.gatecap.instrument.clock_measurer.generator import \
    ClockMeasurer

DESCRIPTION = """
name: p.e
communication:
  mode: apb
instruments:
  rates: !clock-measurer
    reference: ref
    frequency: 100_000_000
    clocks: [fast, slow, odd]
    max_rate: 200_000_000
    update_hz_l2: 14
"""


def parse(text=DESCRIPTION):
    return DescriptionParser.load(text)


def racked(text=DESCRIPTION):
    return Generator.of(parse(text))


def package(text=DESCRIPTION):
    return racked(text).files()["p.pkg.vhd"]


def backplane(text=DESCRIPTION):
    return racked(text).files()["e_backplane.vhd"]


def declarations(elements):
    return [x.declaration() for x in elements]


def rejects(text, fragment):
    with pytest.raises(DescriptionError) as raised:
        parse(text)
    assert fragment in str(raised.value), str(raised.value)


def without(key):
    """The description with one key of the instrument body removed."""
    return "\n".join(line for line in DESCRIPTION.split("\n")
                     if not line.strip().startswith(f"{key}:"))


# Registration and parsing


def test_the_plugin_is_registered_under_its_tag():
    assert InstrumentRegistry.PLUGINS["!clock-measurer"] is ClockMeasurer


def test_the_body_becomes_the_instrument_params():
    instrument, = parse().instruments
    assert instrument.name == "rates"
    assert instrument.params == {
        "reference": "ref", "frequency": 100_000_000,
        "clocks": ("fast", "slow", "odd"), "max_rate": 200_000_000,
        # 200 MHz needs 28 bits; the description does not state that itself.
        "rate_width": 28, "update_hz_l2": 14}


def test_the_update_rate_defaults_to_once_per_second():
    instrument, = parse(without("update_hz_l2")).instruments
    assert instrument.params["update_hz_l2"] == 0


def test_required_keys_are_required():
    rejects(without("reference"), "reference is required")
    rejects(without("frequency"), "frequency is required")
    rejects(without("clocks"),
            "clocks must be a non-empty list of clock names")
    rejects(without("max_rate"), "max_rate is required")


def test_an_unknown_key_is_rejected_by_the_parser():
    rejects(DESCRIPTION.replace("max_rate:", "maxrate:"),
            "unknown key 'maxrate'")


def test_a_clock_name_must_be_a_vhdl_identifier():
    rejects(DESCRIPTION.replace("reference: ref", "reference: 2ref"),
            "reference '2ref' must start with a letter")
    rejects(DESCRIPTION.replace("[fast, slow, odd]", "[fast, sl-ow]"),
            "clock 'sl-ow' illegal character '-'")


def test_the_reference_is_not_one_of_the_measured_clocks():
    rejects(DESCRIPTION.replace("[fast, slow, odd]", "[fast, ref]"),
            "clock 'ref' is the reference clock")


def test_a_clock_is_not_listed_twice():
    rejects(DESCRIPTION.replace("[fast, slow, odd]", "[fast, slow, fast]"),
            "clock 'fast' is listed twice")


def test_a_rate_must_fit_one_apb_word():
    rejects(DESCRIPTION.replace("max_rate: 200_000_000",
                                "max_rate: 4_294_967_296"),
            "needs more than 32 bits, and a rate is published in one APB word")
    # One below the limit is fine, and takes the whole word.
    instrument, = parse(DESCRIPTION.replace("max_rate: 200_000_000",
                                            "max_rate: 4_294_967_295")
                        ).instruments
    assert instrument.params["rate_width"] == 32


def test_the_update_rate_must_leave_counting_bits():
    rejects(DESCRIPTION.replace("update_hz_l2: 14", "update_hz_l2: 28"),
            "leaves no counting bits in the 28-bit rate")
    rejects(DESCRIPTION.replace("update_hz_l2: 14", "update_hz_l2: -1"),
            "update_hz_l2 must be at least 0")


def test_the_update_rate_must_leave_a_window_to_measure_in():
    rejects(DESCRIPTION.replace("frequency: 100_000_000", "frequency: 20_000"),
            "shorter than two cycles of the 20000 Hz reference clock")


# Emission


def test_every_clock_becomes_a_port_named_after_the_instance():
    rack = racked()
    ports = declarations(rack.ports())
    assert ports[-4:] == ["rates_ref_i : in std_ulogic",
                          "rates_fast_i : in std_ulogic",
                          "rates_slow_i : in std_ulogic",
                          "rates_odd_i : in std_ulogic"]
    # Everything is fixed by the description, so nothing but the APB
    # configuration of the apb mode is left to the instantiating design.
    assert declarations(rack.generics()) == [
        "apb_config_c : nsl_amba.apb.config_t := e_apb_config"]


def test_the_reference_clock_is_exported_with_its_rate():
    assert parse().exported_clocks() == {"rates.ref": "rates_ref_i"}
    assert parse().exported_clock_rates() == {"rates.ref": 100_000_000}
    # And a rack may run on it, which leaves it its only clock port.
    rack = racked(DESCRIPTION.replace("  mode: apb",
                                      "  mode: apb\n  clock: rates.ref"))
    assert declarations(rack.ports())[0] == "reset_n_i : in std_ulogic"
    assert "clock_i => rates_ref_i" in rack.files()["e.vhd"]


def test_the_description_becomes_package_constants():
    text = package()
    assert "constant rates_reference_hz_c : natural := 100000000;" in text
    assert "constant rates_rate_width_c : natural := 28;" in text
    assert "constant rates_update_hz_l2_c : natural := 14;" in text
    assert "constant rates_measured_count_c : natural := 3;" in text
    assert 'constant rates_measured_names_c : string := "fast,slow,odd";' \
        in text
    assert "constant rates_size_l2_c : natural := " \
        "gatecap.clock_measurer.clock_measurer_size_l2(" \
        "data_bus_width_l2_c);" in text


def test_the_instrument_is_instantiated_on_its_own_segment():
    assert """\
  rates_measurer: gatecap.clock_measurer.clock_rate_block
    generic map(
      apb_config_c => apb_config_c,
      size_l2_c => rates_size_l2_c,
      measured_count_c => rates_measured_count_c,
      reference_hz_c => rates_reference_hz_c,
      rate_width_c => rates_rate_width_c,
      update_hz_l2_c => rates_update_hz_l2_c,
      fingerprint_c => fingerprint_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      apb_i => dout_s(1),
      apb_o => din_s(1),
      reference_clock_i => rates_ref_i,
      measured_clock_i(0) => rates_fast_i,
      measured_clock_i(1) => rates_slow_i,
      measured_clock_i(2) => rates_odd_i
      );
""" in backplane()


def test_the_instrument_carries_its_own_envelope():
    # The envelope is a function of the instance's generics; this instrument
    # takes none, so the function takes none either.
    assert "function rates_envelope return byte_string;" in package()
    assert '''\
    return
      gatecap.clock_measurer.clock_measurer_envelope(
        name => "rates",
        size_l2 => rates_size_l2_c,
        reference_name => "ref",
        reference_hz => rates_reference_hz_c,
        update_hz_l2 => rates_update_hz_l2_c,
        measured_names => rates_measured_names_c);''' in package()
    # And the rack allocates it a segment out of that very envelope.
    assert ("instrument_entry(segment_base_c(0), "
            "envelope_nth(envelopes_c, 0))") in package()


def test_the_constants_are_declared_before_the_envelope_uses_them():
    text = package()
    assert text.index("constant rates_measured_names_c") \
        < text.index("function rates_envelope")


def test_the_manifest_pulls_the_gateware_in(tmp_path):
    rack = racked()
    assert "gatecap.clock_measurer" in rack.deps()
    rack.write(tmp_path)
    assert "  - gatecap.clock_measurer\n" \
        in (tmp_path / "p.gbs.yaml").read_text()


def test_two_instances_do_not_collide():
    rack = racked("""
name: p.e
communication:
  mode: apb
instruments:
  host: !clock-measurer
    reference: sys
    frequency: 50_000_000
    clocks: [rx]
    max_rate: 100_000_000
  link: !clock-measurer
    reference: sys
    frequency: 50_000_000
    clocks: [rx]
    max_rate: 100_000_000
""")
    ports = declarations(rack.ports())
    assert ports[-4:] == ["host_sys_i : in std_ulogic",
                          "host_rx_i : in std_ulogic",
                          "link_sys_i : in std_ulogic",
                          "link_rx_i : in std_ulogic"]
    text = rack.files()["e_backplane.vhd"]
    assert "host_measurer: gatecap.clock_measurer.clock_rate_block" in text
    assert "link_measurer: gatecap.clock_measurer.clock_rate_block" in text
    # Two instances, two segments, each on its own APB leg.
    assert "apb_i => dout_s(1)" in text and "apb_i => dout_s(2)" in text
