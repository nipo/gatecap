"""Instrument plugins: parsing, contributions, and the rack they build.

The plugin here is the test's own, registered for the duration of a test and
dropped afterwards, so what it exercises is the contract a third-party
instrument sees and not gatecap's own logic analyzer.

Run: python3.13 -m pytest host/tests/test_generator_instruments.py
"""

import pytest

from acrobe_plugin.gatecap.generator import (DescriptionError,
                                             DescriptionParser, Generator,
                                             InstrumentPlugin,
                                             InstrumentRegistry, RackAssembly)
from acrobe_plugin.gatecap.generator.vhdl import (Constant, Emitter, Expr,
                                                  Generic, Instance, Port)


class ClockCounter(InstrumentPlugin):
    """One counter per measured clock, behind one APB port."""

    TAG = "!clock-counter"
    KEYS = ("clocks",)
    UNIT = "myext.clock_counter.clock_counter"
    UUID = "myext.clock_counter.CLOCK_COUNTER_UUID_C"
    # Register-file words, hence the address bits the entity decodes and the
    # footprint its envelope declares.
    REG_COUNT_L2 = 10

    @classmethod
    def parse(cls, payload, path):
        clocks = payload.get("clocks")
        if not isinstance(clocks, int) or isinstance(clocks, bool) \
           or clocks < 1:
            raise DescriptionError(
                f"clocks must be a positive integer, got {clocks!r}", path)
        return {"clocks": clocks}

    @classmethod
    def channels(cls, instrument):
        return instrument.params["clocks"]

    @classmethod
    def ports(cls, instrument):
        return tuple(Port(instrument.port(f"clock_{i}_i"), "in", "std_ulogic")
                     for i in range(cls.channels(instrument)))

    @classmethod
    def generics(cls, instrument):
        return (Generic(instrument.constant("window"), "natural", "1024"),)

    @classmethod
    def clocks(cls, instrument):
        return {f"clock{i}": instrument.port(f"clock_{i}_i")
                for i in range(cls.channels(instrument))}

    @classmethod
    def constants(cls, context):
        instrument = context.instrument
        return (Constant(instrument.constant("channels"), "natural",
                         str(cls.channels(instrument))),
                Constant(instrument.constant("size_l2"), "natural",
                         f"{cls.REG_COUNT_L2} + {context.data_bus_width_l2}"))

    @classmethod
    def envelope(cls, context):
        instrument = context.instrument
        return Expr.wrapped_call(
            "instrument_envelope",
            type_uuid=cls.UUID,
            size_l2=instrument.constant("size_l2"),
            name=Expr.string(instrument.name),
            t0=Expr.call("cbor_positive", instrument.constant("channels")))

    @classmethod
    def instance(cls, context):
        instrument = context.instrument
        port_map = {"clock_i": context.clock,
                    "reset_n_i": context.reset_n,
                    "apb_i": context.apb_master,
                    "apb_o": context.apb_slave}
        for i in range(cls.channels(instrument)):
            port_map[f"measured_i({i})"] = instrument.port(f"clock_{i}_i")
        return Instance(
            instrument.label("counter"), cls.UNIT,
            generic_map={"apb_config_c": context.apb_config,
                         "size_l2_c": instrument.constant("size_l2"),
                         "channel_count_c": instrument.constant("channels"),
                         "window_c": instrument.constant("window")},
            port_map=port_map)

    @classmethod
    def deps(cls, instrument):
        return ("myext.clock_counter",)


class HostClockCounter(ClockCounter):
    """A plugin naming a port after nothing in particular, which is how an
    instrument collides with the rest of the rack."""

    TAG = "!host-clock-counter"

    @classmethod
    def ports(cls, instrument):
        return (Port("clock_i", "in", "std_ulogic"),)


class WideCounter(ClockCounter):
    """The same instrument with a register file decoding more than a segment's
    floor, so allocation has to align it on its own size."""

    TAG = "!wide-counter"
    REG_COUNT_L2 = 12


class MuteCounter(ClockCounter):
    """The same instrument exporting no clock at all."""

    TAG = "!mute-counter"

    @classmethod
    def clocks(cls, instrument):
        return {}


class RatedCounter(ClockCounter):
    """An instrument stating the rate of the clocks it exports, so a transport
    riding one takes the rate from the description."""

    TAG = "!rated-counter"
    RATE = 48_000_000

    @classmethod
    def clock_rates(cls, instrument):
        return {name: cls.RATE for name in cls.clocks(instrument)}


class WrittenCounter(ClockCounter):
    """An instrument generated rather than taken from a library: it writes its
    own entity next to the rack and declares the component for it."""

    TAG = "!written-counter"
    SOURCE = "-- generated counter\n"

    @classmethod
    def unit(cls, context):
        return f"{context.entity}_{context.instrument.name}"

    @classmethod
    def files(cls, context):
        return {f"{cls.unit(context)}.vhd": cls.SOURCE}

    @classmethod
    def components(cls, context):
        return (Instance(cls.unit(context), cls.unit(context)),)


@pytest.fixture(autouse=True)
def registered():
    plugins = (ClockCounter, HostClockCounter, WideCounter, MuteCounter,
               RatedCounter, WrittenCounter)
    for plugin in plugins:
        InstrumentRegistry.register(plugin)
    yield
    for plugin in plugins:
        del InstrumentRegistry.PLUGINS[plugin.TAG]


INSTRUMENT_ONLY = """
name: p.e
communication:
  mode: apb
instruments:
  rates: !clock-counter
    clocks: 3
"""

TWO_INSTRUMENTS = """
name: p.e
communication:
  mode: axi4_stream
instruments:
  rates: !clock-counter
    clocks: 2
  wide: !wide-counter
    clocks: 1
"""


def parse(text):
    return DescriptionParser.load(text)


def racked(text):
    return Generator.of(parse(text))


def declarations(elements):
    return [x.declaration() for x in elements]


def rejects(text, fragment):
    with pytest.raises(DescriptionError) as raised:
        parse(text)
    assert fragment in str(raised.value), str(raised.value)


# Registry and parsing


def test_the_registry_is_keyed_by_tag():
    assert InstrumentRegistry.PLUGINS["!clock-counter"] is ClockCounter
    assert "!clock-counter" in InstrumentRegistry.tags()
    with pytest.raises(AssertionError):
        InstrumentRegistry.register(ClockCounter)


def test_an_entry_names_its_plugin_by_tag():
    description = parse(INSTRUMENT_ONLY)
    instrument, = description.instruments
    assert (instrument.name, instrument.tag) == ("rates", "!clock-counter")
    assert instrument.plugin is ClockCounter
    assert instrument.params == {"clocks": 3}
    assert instrument.path() == "instruments.rates"
    assert instrument.port("clock_0_i") == "rates_clock_0_i"
    assert instrument.signal("count") == "rates_count_s"
    assert instrument.constant("channels") == "rates_channels_c"
    assert instrument.label("counter") == "rates_counter"


def test_instruments_keep_description_order():
    description = parse("""
name: p.e
communication:
  mode: apb
instruments:
  second: !clock-counter
    clocks: 1
  first: !clock-counter
    clocks: 1
""")
    assert [i.name for i in description.instruments] == ["second", "first"]


def test_an_unknown_tag_is_a_description_error():
    with pytest.raises(DescriptionError) as raised:
        parse(INSTRUMENT_ONLY.replace("!clock-counter", "!absent"))
    error = raised.value
    assert "unknown instrument '!absent'" in str(error)
    assert "known: " in str(error) and "!clock-counter" in str(error)
    assert error.path == "instruments.rates"
    assert error.line == 6


def test_an_untagged_entry_names_no_plugin():
    rejects("""
name: p.e
communication:
  mode: apb
instruments:
  rates:
    clocks: 2
""", "an instrument entry is tagged with the instrument it holds")


def test_a_plugin_validates_its_own_keys():
    rejects(INSTRUMENT_ONLY.replace("clocks: 3", "clocks: 0"),
            "clocks must be a positive integer, got 0")
    rejects(INSTRUMENT_ONLY.replace("clocks: 3", "period: 3"),
            "unknown key 'period' (known: clocks)")


def test_an_instrument_name_is_a_vhdl_identifier():
    rejects(INSTRUMENT_ONLY.replace("rates:", "2rates:"),
            "instrument name '2rates' must start with a letter")


def test_instrument_ports_are_claimed_like_any_other():
    rejects("""
name: p.e
communication:
  mode: apb
instruments:
  a: !host-clock-counter
    clocks: 1
""", "port clock_i is claimed by both the host clock and instrument a")


def test_a_rack_needs_an_instrument():
    rejects("name: p.e\ncommunication:\n  mode: apb\ninstruments: {}\n",
            "a rack needs at least one instrument")


# Exported clocks


def test_exported_clocks_are_named_instance_dot_clock():
    description = parse(INSTRUMENT_ONLY)
    assert description.exported_clocks() == {
        "rates.clock0": "rates_clock_0_i",
        "rates.clock1": "rates_clock_1_i",
        "rates.clock2": "rates_clock_2_i"}


def test_an_unknown_exported_clock_is_a_description_error():
    rejects(INSTRUMENT_ONLY.replace("  mode: apb",
                                    "  mode: apb\n  clock: rates.absent"),
            "clock 'rates.absent' is not an exported clock")
    rejects(INSTRUMENT_ONLY.replace("!clock-counter", "!mute-counter")
            .replace("  mode: apb", "  mode: apb\n  clock: rates.clock0"),
            "no instrument of this rack exports one")


def test_an_exported_clock_carries_the_rate_the_description_states():
    text = INSTRUMENT_ONLY.replace("!clock-counter", "!rated-counter") \
        .replace("  mode: apb", "  mode: serial_hdlc\n  clock: rates.clock1")
    rack = racked(text)
    assert parse(text).exported_clock_rates()["rates.clock1"] \
        == RatedCounter.RATE
    # Stated, so the adapter is given a literal and asks for no generic.
    assert [generic.name for generic in rack.generics()] == [
        "rates_window_c", "baud_rate_c", "burst_length_l2_c"]
    assert "clock_frequency_c => 48_000_000" in rack.files()["e.vhd"]


# The package: what both entities read the map from


def test_a_description_is_always_a_rack():
    assert isinstance(racked(INSTRUMENT_ONLY), RackAssembly)


def test_a_rack_emits_a_package_a_backplane_and_a_rack():
    rack = racked(INSTRUMENT_ONLY)
    assert list(rack.files()) == ["p.pkg.vhd", "e_backplane.vhd", "e.vhd",
                                  "p.gbs.yaml"]
    assert rack.file_names() == ("p.pkg.vhd", "e_backplane.vhd", "e.vhd")


def test_the_package_holds_the_map_both_entities_read():
    text = racked(TWO_INSTRUMENTS).files()["p.pkg.vhd"]
    assert "constant data_bus_width_l2_c : natural := 2;" in text
    assert "constant rom_size_l2_c : natural := 12;" in text
    assert "constant rates_size_l2_c : natural := 10 + data_bus_width_l2_c;" \
        in text
    # One envelope function per instrument, taking that instance's generics.
    assert """\
  function rates_envelope(
    rates_window_c : natural) return byte_string;""" in text
    assert """\
    return
      instrument_envelope(
        type_uuid => myext.clock_counter.CLOCK_COUNTER_UUID_C,
        size_l2 => rates_size_l2_c,
        name => "rates",
        t0 => cbor_positive(rates_channels_c));""" in text
    assert ("    return rates_envelope(rates_window_c) & "
            "wide_envelope(wide_window_c);") in text
    assert """\
    constant envelopes_c : byte_string := \
e_envelopes(rates_window_c, wide_window_c);
    constant segment_base_c : base_vector := \
segment_bases(envelopes_c, rom_size_l2_c);""" in text
    assert """\
    return
      rack_compose(
        instrument_entry(segment_base_c(0), envelope_nth(envelopes_c, 0)),
        instrument_entry(segment_base_c(1), envelope_nth(envelopes_c, 1)));""" \
        in text
    # Both entities are declared, so a design names either of them.
    assert "component e_backplane is" in text
    assert "component e is" in text


def test_the_package_states_the_default_apb_configuration():
    text = racked(INSTRUMENT_ONLY).files()["p.pkg.vhd"]
    assert """\
  function e_apb_config(
    rates_window_c : natural) return nsl_amba.apb.config_t;""" in text
    assert """\
    return
      nsl_amba.apb.config(
        address_width => nsl_math.arith.max(24, \
nsl_math.arith.log2(segment_extent(envelopes_c, rom_size_l2_c))),
        data_bus_width => 8 * 2**data_bus_width_l2_c,
        err => true);""" in text


# The backplane


def test_the_backplane_routes_by_segment_prefix():
    text = racked(TWO_INSTRUMENTS).files()["e_backplane.vhd"]
    assert "function segment_prefix(width, base, size_l2 : natural)" in text
    assert """\
  constant routing_table_c : nsl_amba.address.address_vector :=
    nsl_amba.address.routing_table(apb_config_c.address_width,
      segment_prefix(apb_config_c.address_width, 0, rom_size_l2_c),
      segment_prefix(
        apb_config_c.address_width,
        allocation_c(0),
        envelope_size_l2(envelope_nth(envelopes_c, 0))),
      segment_prefix(
        apb_config_c.address_width,
        allocation_c(1),
        envelope_size_l2(envelope_nth(envelopes_c, 1))));""" in text
    assert "signal dout_s : nsl_amba.apb.master_vector(0 to 2);" in text


def test_the_backplane_reports_the_allocation_at_elaboration():
    text = racked(INSTRUMENT_ONLY).files()["e_backplane.vhd"]
    assert 'report "rack segment " & envelope_name(' in text
    assert "constant allocation_c : base_vector := " \
        "reported(envelopes_c, segment_base_c);" in text


def test_the_backplane_pins_the_rom_and_checks_it_fits():
    text = racked(INSTRUMENT_ONLY).files()["e_backplane.vhd"]
    assert """\
  descriptor_rom: nsl_amba.rom.apb_rom
    generic map(
      implementation_c => nsl_memory.rom.ROM_DISTRIBUTED,
      config_c => apb_config_c,
      contents_c => descriptor_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      apb_i => dout_s(0),
      apb_o => din_s(0)
      );""" in text
    for condition in ("descriptor_c'length <= 2**rom_size_l2_c",
                      "apb_config_c.address_width >= address_bits_c",
                      "apb_config_c.data_bus_width_l2 = data_bus_width_l2_c"):
        assert f"assert {condition}" in text
        assert f"condition_c => {condition}" in text


def test_the_backplane_instantiates_the_instruments():
    text = racked(INSTRUMENT_ONLY).files()["e_backplane.vhd"]
    assert """\
  rates_counter: myext.clock_counter.clock_counter
    generic map(
      apb_config_c => apb_config_c,
      size_l2_c => rates_size_l2_c,
      channel_count_c => rates_channels_c,
      window_c => rates_window_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      apb_i => dout_s(1),
      apb_o => din_s(1),
      measured_i(0) => rates_clock_0_i,
      measured_i(1) => rates_clock_1_i,
      measured_i(2) => rates_clock_2_i
      );""" in text


def test_an_instrument_is_told_the_footprint_its_envelope_declares():
    rack = racked(INSTRUMENT_ONLY)
    context = rack.instruments  # contributions, keyed by instance
    assert set(context) == {"rates"}
    # The framework offers it; this plugin dimensions itself from its own
    # constant instead, which the entity then checks.
    assert "envelope_size_l2(envelope_nth(envelopes_c, 0))" in \
        rack.files()["e_backplane.vhd"]


# The rack wrapper


def test_an_apb_rack_is_a_passthrough_over_the_backplane():
    rack = racked(INSTRUMENT_ONLY)
    assert rack.passthrough()
    text = rack.files()["e.vhd"]
    assert ("apb_config_c : nsl_amba.apb.config_t := "
            "e_apb_config(rates_window_c)") in text
    assert """\
  backplane: e_backplane
    generic map(
      rates_window_c => rates_window_c,
      apb_config_c => apb_config_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      apb_i => apb_i,
      apb_o => apb_o,
      rates_clock_0_i => rates_clock_0_i,
      rates_clock_1_i => rates_clock_1_i,
      rates_clock_2_i => rates_clock_2_i
      );""" in text
    # A passthrough has nothing of its own between the two.
    assert "signal apb_m_s" not in text


def test_a_transport_rack_holds_the_adapter_and_the_backplane():
    text = racked(TWO_INSTRUMENTS).files()["e.vhd"]
    assert ("constant apb_config_c : nsl_amba.apb.config_t := "
            "e_apb_config(rates_window_c, wide_window_c);") in text
    assert "signal apb_m_s : nsl_amba.apb.master_t;" in text
    # The link is a library entity: its generics and ports cross the boundary
    # formal to formal, and only the requester side is the rack's own.
    assert """\
  adapter: gatecap.adapter_stream.stream_adapter
    generic map(
      apb_config_c => apb_config_c,
      stream_config_c => stream_config_c,
      burst_length_l2_c => burst_length_l2_c,
      descriptor_base_c => 0
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      rx_i => rx_i,
      rx_o => rx_o,
      tx_o => tx_o,
      tx_i => tx_i,
      apb_o => apb_m_s,
      apb_i => apb_s_s
      );""" in text
    assert """\
      apb_i => apb_m_s,
      apb_o => apb_s_s,""" in text
    # The transport is internal, so the geometry is not the design's business.
    assert "apb_config_c : nsl_amba.apb.config_t :=" not in \
        text.split("entity e is")[1].split("end entity;")[0]


def test_an_exported_clock_is_bound_structurally():
    rack = racked(INSTRUMENT_ONLY.replace(
        "  mode: apb", "  mode: apb\n  clock: rates.clock1"))
    assert declarations(rack.ports())[:1] == ["reset_n_i : in std_ulogic"]
    text = rack.files()["e.vhd"]
    assert "clock_i : in std_ulogic" not in text.split("architecture")[0]
    assert """\
    port map(
      clock_i => rates_clock_1_i,
      reset_n_i => reset_n_i,""" in text


# Emission


def test_the_rack_manifest_lists_the_files_in_analysis_order(tmp_path):
    rack = racked(TWO_INSTRUMENTS)
    rack.write(tmp_path)
    manifest = (tmp_path / "p.gbs.yaml").read_text()
    assert """\
sources:
  - file_type: vhdl
    files:
      - p.pkg.vhd
      - e_backplane.vhd
      - e.vhd
""" in manifest
    assert "  - myext.clock_counter\n" in manifest
    assert "  - gatecap.descriptor\n" in manifest
    for name in ("p.pkg.vhd", "e_backplane.vhd", "e.vhd"):
        clause, = [line for line in (tmp_path / name).read_text().split("\n")
                   if line.startswith("library ") and "ieee" not in line]
        assert "myext" in clause


def test_an_instrument_may_write_its_own_entity(tmp_path):
    rack = racked(INSTRUMENT_ONLY.replace("!clock-counter", "!written-counter"))
    assert rack.file_names() == ("e_rates.vhd", "p.pkg.vhd",
                                 "e_backplane.vhd", "e.vhd")
    rack.write(tmp_path)
    assert (tmp_path / "e_rates.vhd").read_text() == WrittenCounter.SOURCE
    assert "      - e_rates.vhd\n" in (tmp_path / "p.gbs.yaml").read_text()
    # And the package declares it, so the backplane instantiates it by name.
    assert "e_rates: e_rates" in (tmp_path / "p.pkg.vhd").read_text()


def test_both_rack_units_read_the_package():
    for name in ("e_backplane.vhd", "e.vhd"):
        assert "use work.p.all;" in racked(INSTRUMENT_ONLY).files()[name]


def test_a_rack_holds_at_most_fifteen_instruments():
    body = "".join(f"  i{i}: !clock-counter\n    clocks: 1\n"
                   for i in range(16))
    with pytest.raises(DescriptionError) as raised:
        racked(f"name: p.e\ncommunication:\n  mode: apb\ninstruments:\n{body}")
    assert "16 instruments need one APB segment each" in str(raised.value)


def test_rack_emission_stays_deterministic():
    assert racked(TWO_INSTRUMENTS).files() == racked(TWO_INSTRUMENTS).files()
