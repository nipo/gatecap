"""Exact contributions of the signal-type and communication plugins.

Run: python3.13 -m pytest host/tests/test_generator_plugins.py
"""

import dataclasses

import pytest

from acrobe_plugin.gatecap.communication import (ApbCommunication,
                                                 Axi4StreamCommunication,
                                                 JtagCommunication,
                                                 SerialHdlcCommunication,
                                                 SpiCommunication,
                                                 SwdCommunication,
                                                 UsbCommunication)
from acrobe_plugin.gatecap.generator import (CommunicationContext,
                                             CommunicationRegistry,
                                             DescriptionError,
                                             DescriptionParser, HostClock,
                                             SignalTypeRegistry)
from acrobe_plugin.gatecap.instrument.la import (Axi4StreamSignal,
                                                 BnocFramedSignal,
                                                 BnocPipeSignal, BusSignal,
                                                 ScalarSignal)
from acrobe_plugin.gatecap.generator.vhdl import Emitter
from acrobe_plugin.gatecap.names import SignalNames


CONTEXT = CommunicationContext(clock="control_clock_i",
                               reset_n="control_reset_n_i",
                               apb_config="apb_config_c",
                               apb_master="apb_m_s",
                               apb_slave="apb_s_s",
                               descriptor_base="0",
                               clock_frequency=100_000_000)

# A transport with a clock of its own, or riding a domain that states no
# frequency: nothing in the description gives the host clock a rate.
RATELESS = CommunicationContext(clock="clock_i", reset_n="reset_n_i",
                                apb_config="apb_config_c",
                                apb_master="apb_m_s",
                                apb_slave="apb_s_s",
                                descriptor_base="0",
                                clock_frequency=0)


def usb_context(context):
    """A context carrying the fingerprint expression a rack hands a transport
    whose link states an identity of its own."""
    return dataclasses.replace(
        context, fingerprint="descriptor_fingerprint(e_descriptor)")


def spi_context(context, max_rate=10_000_000, spi_mode=0):
    """A context carrying what the spi mode's own keys parse to."""
    return dataclasses.replace(
        context, params={"max_rate": max_rate, "spi_mode": spi_mode})


# One domain, on a clock of its own name, for the host-clock resolution below.
DOMAIN = """\
      ctrl:
        clock: sysclk
        signals:
          a:
            trigger: true
"""


def described(body, communication="mode: apb"):
    return DescriptionParser.load(
        f"name: p.e\ncommunication:\n  {communication}\n\n"
        f"instruments:\n  la: !logic-analyzer\n    domains:\n{body}")


def probes(signals, domain="ctrl", communication="mode: apb"):
    """The probes of one domain, as the plugin parsed them."""
    indented = "".join("    " + line + "\n"
                       for line in signals.rstrip("\n").split("\n"))
    description = described(f"      {domain}:\n        signals:\n{indented}",
                            communication)
    domains = description.instrument("la").params["domains"]
    entry, = [d for d in domains if d.name == domain]
    return {p.name: p for p in entry.probes}


def declarations(elements):
    return [x.declaration() for x in elements]


def test_registry_keys():
    assert SignalTypeRegistry.PLUGINS[None] is ScalarSignal
    assert SignalTypeRegistry.PLUGINS["!bus"] is BusSignal
    assert SignalTypeRegistry.PLUGINS["!axi4-stream"] is Axi4StreamSignal
    assert SignalTypeRegistry.PLUGINS["!bnoc-framed"] is BnocFramedSignal
    assert SignalTypeRegistry.PLUGINS["!bnoc-pipe"] is BnocPipeSignal
    assert set(CommunicationRegistry.modes()) == {"apb", "swd", "spi",
                                                  "axi4_stream", "jtag",
                                                  "serial_hdlc", "usb"}
    assert CommunicationRegistry.PLUGINS["swd"] is SwdCommunication


def test_scalar_contribution():
    probe = probes("      k:\n        trigger: true\n")["k"]
    plugin = probe.plugin
    assert plugin is ScalarSignal
    assert declarations(plugin.ports(probe)) == ["ctrl_k_i : in std_ulogic"]
    assert plugin.generics(probe) == ()
    assert plugin.deps(probe) == ()
    assert plugin.trace_length(probe) == "1"
    assert plugin.trace_pack(probe) == "std_ulogic_vector'(0 => ctrl_k_i)"
    assert plugin.trace_names(probe) == '"k"'
    assert plugin.trigger_pack(probe) == "std_ulogic_vector'(0 => ctrl_k_i)"
    assert plugin.trigger_names(probe) == '"k"'
    assert plugin.trace_width(probe) == 1


def test_scalar_enum_becomes_a_name_spec_suffix():
    probe = probes("      busy:\n        trigger: true\n"
                   "        enum: {0: RUN, 1: STOP}\n")["busy"]
    assert probe.plugin.trace_names(probe) == '"busy<RUN,STOP>"'


def test_bus_contribution():
    probe = probes("      state: !bus\n        width: 2\n"
                   "        trigger: true\n"
                   "        enum:\n          0: RESET\n          1: IDLE\n"
                   "          2: COMMAND\n          3: RESPONSE\n")["state"]
    plugin = probe.plugin
    assert plugin is BusSignal
    assert declarations(plugin.ports(probe)) == [
        "ctrl_state_i : in std_ulogic_vector(1 downto 0)"]
    assert plugin.generics(probe) == ()
    assert plugin.deps(probe) == ()
    assert plugin.trace_length(probe) == "2"
    assert plugin.trace_pack(probe) == "ctrl_state_i"
    assert plugin.trace_names(probe) == \
        '"state[0:1]<RESET,IDLE,COMMAND,RESPONSE>"'
    assert plugin.trigger_names(probe) == plugin.trace_names(probe)
    assert plugin.trace_width(probe) == 2


def test_bus_width_is_never_a_generic():
    probe = probes("      word: !bus\n        width: 8\n"
                   "        trigger: true\n")["word"]
    assert probe.plugin.generics(probe) == ()
    assert probe.params == {"width": 8}
    assert probe.plugin.trace_names(probe) == '"word[0:7]"'


def test_axi4_stream_contribution():
    probe = probes("      command: !axi4-stream\n"
                   "        trace: idskouvlr\n        trigger: vlr\n")["command"]
    plugin = probe.plugin
    assert plugin is Axi4StreamSignal
    assert declarations(plugin.ports(probe)) == [
        "ctrl_command_i : in nsl_amba.axi4_stream.bus_t"]
    generic, = plugin.generics(probe)
    # No default value, deliberately: a default that happens to elaborate
    # would hide a mismatch between the probed bus and the capture geometry.
    assert generic.default is None
    assert generic.declaration() == \
        "ctrl_command_config_c : nsl_amba.axi4_stream.config_t"
    assert plugin.deps(probe) == ("nsl_amba.axi4_stream",
                                  "gatecap.axi4_stream_packer")
    assert plugin.trace_length(probe) == (
        'gatecap.axi4_stream_packer.axis_length(ctrl_command_config_c, '
        '"idskouvlr")')
    assert plugin.trace_pack(probe) == (
        'gatecap.axi4_stream_packer.axis_pack(ctrl_command_config_c, '
        '"idskouvlr", ctrl_command_i)')
    assert plugin.trace_names(probe) == (
        '"command.{" & gatecap.axi4_stream_packer.axis_names'
        '(ctrl_command_config_c, "idskouvlr") & "}"')
    assert plugin.trigger_length(probe) == (
        'gatecap.axi4_stream_packer.axis_length(ctrl_command_config_c, "vlr")')
    assert plugin.trigger_pack(probe) == (
        'gatecap.axi4_stream_packer.axis_pack(ctrl_command_config_c, "vlr", '
        'ctrl_command_i)')
    assert plugin.trigger_names(probe) == (
        '"command.{" & gatecap.axi4_stream_packer.axis_names'
        '(ctrl_command_config_c, "vlr") & "}"')
    # Widths live in the stream config generic, not in the description.
    assert plugin.trace_width(probe) is None
    assert plugin.trigger_width(probe) is None


def test_axi4_stream_traces_the_whole_alphabet_by_default():
    probe = probes("      s: !axi4-stream\n        trigger: v\n")["s"]
    assert probe.trace_selection == "idskouvlr"
    assert probe.plugin.trace_names(probe).endswith('"idskouvlr") & "}"')


def test_bnoc_framed_contribution():
    probe = probes("      command: !bnoc-framed\n"
                   "        trace: dvl\n        trigger: vl\n")["command"]
    plugin = probe.plugin
    assert plugin is BnocFramedSignal
    assert declarations(plugin.ports(probe)) == [
        "ctrl_command_i : in nsl_bnoc.framed.framed_bus_t"]
    # A bnoc bus has one geometry, so nothing about it is a generic.
    assert plugin.generics(probe) == ()
    assert plugin.deps(probe) == ("nsl_bnoc.framed", "gatecap.bnoc_packer")
    assert plugin.trace_length(probe) == \
        'gatecap.bnoc_packer.framed_length("dvl")'
    assert plugin.trace_pack(probe) == \
        'gatecap.bnoc_packer.framed_pack(ctrl_command_i, "dvl")'
    assert plugin.trace_names(probe) == (
        '"command.{" & gatecap.bnoc_packer.framed_names("dvl") & "}"')
    assert plugin.trigger_length(probe) == \
        'gatecap.bnoc_packer.framed_length("vl")'
    assert plugin.trigger_pack(probe) == \
        'gatecap.bnoc_packer.framed_pack(ctrl_command_i, "vl")'
    assert plugin.trigger_names(probe) == (
        '"command.{" & gatecap.bnoc_packer.framed_names("vl") & "}"')
    # 8-bit data plus one bit per handshake line, known here and not only at
    # elaboration.
    assert plugin.trace_width(probe) == 10
    assert plugin.trigger_width(probe) == 2


def test_bnoc_pipe_contribution():
    probe = probes("      rx: !bnoc-pipe\n        trigger: r\n")["rx"]
    plugin = probe.plugin
    assert plugin is BnocPipeSignal
    assert declarations(plugin.ports(probe)) == [
        "ctrl_rx_i : in nsl_bnoc.pipe.pipe_bus_t"]
    assert plugin.generics(probe) == ()
    assert plugin.deps(probe) == ("nsl_bnoc.pipe", "gatecap.bnoc_packer")
    # A pipe has no frame boundary: its alphabet is the traced default.
    assert probe.trace_selection == "dvr"
    assert plugin.trace_length(probe) == 'gatecap.bnoc_packer.pipe_length("dvr")'
    assert plugin.trace_pack(probe) == \
        'gatecap.bnoc_packer.pipe_pack(ctrl_rx_i, "dvr")'
    assert plugin.trace_names(probe) == (
        '"rx.{" & gatecap.bnoc_packer.pipe_names("dvr") & "}"')
    assert plugin.trigger_names(probe) == (
        '"rx.{" & gatecap.bnoc_packer.pipe_names("r") & "}"')
    assert plugin.trace_width(probe) == 10
    assert plugin.trigger_width(probe) == 1


def test_bnoc_names_parse_back_through_the_host_grammar():
    # What framed_names("dvlr") returns, spliced the way the descriptor splices
    # it, expands to one dotted name per probe bit.
    names, enums = SignalNames.parse("command.{data[0:7],valid,last,ready}")
    assert names == [f"command.data[{i}]" for i in range(8)] + [
        "command.valid", "command.last", "command.ready"]
    assert enums == {}


def test_name_fragments_parse_back_through_the_host_grammar():
    # What the emitted expressions evaluate to at elaboration, spliced the way
    # the descriptor will splice them, must expand to one name per probe bit.
    packed = "data[0:7],valid,last"          # axis_names(cfg, "dvl")
    spec = ",".join(['command.{' + packed + '}',
                     "state[0:1]<RESET,IDLE,COMMAND,RESPONSE>", "k"])
    names, enums = SignalNames.parse(spec)
    assert names == [f"command.data[{i}]" for i in range(8)] + [
        "command.valid", "command.last",
        "state[0]", "state[1]", "k"]
    assert enums == {"state": {0: "RESET", 1: "IDLE", 2: "COMMAND",
                               3: "RESPONSE"}}


# What a rack takes from a transport plugin: the adapter it instantiates, the
# generics it binds on it, and the partitions it pulls. Nothing of the link
# itself is emitted.


def test_every_transport_names_the_adapter_it_instantiates():
    assert [CommunicationRegistry.PLUGINS[mode].UNIT
            for mode in ("apb", "swd", "spi", "axi4_stream", "jtag",
                         "serial_hdlc", "usb")] \
        == [None,
            "gatecap.adapter_swd.swd_adapter",
            "gatecap.adapter_spi.spi_adapter",
            "gatecap.adapter_stream.stream_adapter",
            "gatecap.adapter_jtag.jtag_adapter",
            "gatecap.adapter_serial_hdlc.serial_hdlc_adapter",
            "gatecap.adapter_usb.usb_adapter"]


def test_an_adapter_takes_the_geometry_and_the_descriptor_base():
    assert SwdCommunication.generic_map(CONTEXT) == {
        "apb_config_c": "apb_config_c",
        "descriptor_base_c": "0"}
    assert JtagCommunication.generic_map(CONTEXT) == {
        "apb_config_c": "apb_config_c",
        "burst_length_l2_c": "burst_length_l2_c",
        "descriptor_base_c": "0"}
    assert Axi4StreamCommunication.generic_map(CONTEXT) == {
        "apb_config_c": "apb_config_c",
        "stream_config_c": "stream_config_c",
        "burst_length_l2_c": "burst_length_l2_c",
        "descriptor_base_c": "0"}


def test_the_serial_adapter_is_told_the_clock_rate_it_divides():
    # Stated by the description: a literal, and no generic for it.
    assert SerialHdlcCommunication.generic_map(CONTEXT)["clock_frequency_c"] \
        == "100_000_000"
    assert "clock_frequency_c" not in \
        [g.name for g in SerialHdlcCommunication.generics(CONTEXT)]
    # Stated by nothing: the boundary generic, forwarded.
    assert SerialHdlcCommunication.generic_map(RATELESS)["clock_frequency_c"] \
        == "clock_frequency_c"
    assert "clock_frequency_c" in \
        [g.name for g in SerialHdlcCommunication.generics(RATELESS)]


def test_the_spi_adapter_is_told_both_rates_the_ratio_is_made_of():
    # The rate the description states, and the clock it is checked against:
    # what makes the elaboration assert an arithmetic fact rather than a hope.
    context = spi_context(CONTEXT)
    assert SpiCommunication.generic_map(context) == {
        "apb_config_c": "apb_config_c",
        "clock_frequency_c": "100_000_000",
        "sck_max_rate_c": "10_000_000",
        "spi_mode_c": "0",
        "descriptor_base_c": "0"}
    assert SpiCommunication.generics(context) == ()
    # Riding a clock whose rate nothing states, the rack asks the design for
    # it -- the wire rate alone says nothing about the ratio.
    rateless = spi_context(RATELESS)
    assert [g.name for g in SpiCommunication.generics(rateless)] == [
        "clock_frequency_c"]
    assert SpiCommunication.generic_map(rateless)["clock_frequency_c"] \
        == "clock_frequency_c"


def test_the_spi_mode_reaches_the_adapter():
    context = spi_context(CONTEXT, spi_mode=3)
    assert SpiCommunication.generic_map(context)["spi_mode_c"] == "3"


def test_the_spi_pins_are_the_boundary_and_the_adapter_formals():
    assert declarations(SpiCommunication.ports()) == [
        "spi_i : in nsl_spi.spi.spi_slave_i",
        "spi_o : out nsl_spi.spi.spi_slave_o"]


def test_the_usb_adapter_carries_the_fingerprint_it_publishes_as_a_serial():
    # The rack's identity reaches the bus, so the descriptor's fingerprint is
    # a generic of the adapter and not a thing the host is told afterwards.
    assert UsbCommunication.generic_map(usb_context(RATELESS)) == {
        "apb_config_c": "apb_config_c",
        "fingerprint_c": "descriptor_fingerprint(e_descriptor)",
        "burst_length_l2_c": "burst_length_l2_c",
        "descriptor_base_c": "0"}


def test_the_usb_adapter_is_told_a_host_clock_rate_the_description_states():
    context = usb_context(dataclasses.replace(RATELESS,
                                              clock_frequency=60_000_000))
    assert UsbCommunication.generic_map(context)["clock_frequency_c"] \
        == "60_000_000"
    # Nothing states one: the adapter's own default, which is the rate the
    # phy is built for.
    assert "clock_frequency_c" not in \
        UsbCommunication.generic_map(usb_context(RATELESS))


def test_a_usb_rack_refuses_a_clock_the_phy_cannot_recover_bits_with():
    with pytest.raises(DescriptionError):
        UsbCommunication.check(usb_context(CONTEXT))
    UsbCommunication.check(usb_context(
        dataclasses.replace(RATELESS, clock_frequency=48_000_000)))
    UsbCommunication.check(usb_context(RATELESS))


def test_the_usb_link_is_two_records_and_a_status_wire():
    assert declarations(UsbCommunication.ports()) == [
        "usb_o : out nsl_usb.io.usb_io_c",
        "usb_i : in nsl_usb.io.usb_io_s",
        "online_o : out std_ulogic"]


def test_a_transport_pulls_its_adapter_partition():
    assert JtagCommunication.deps() == ("gatecap.adapter_jtag",
                                        "nsl_amba.apb")
    # The link types the boundary ports name come along.
    assert SwdCommunication.deps() == ("gatecap.adapter_swd", "nsl_amba.apb",
                                       "nsl_coresight.swd")
    assert SpiCommunication.deps() == ("gatecap.adapter_spi", "nsl_amba.apb",
                                       "nsl_spi.spi")
    assert UsbCommunication.deps() == ("gatecap.adapter_usb", "nsl_amba.apb",
                                       "nsl_usb.io")
    assert ApbCommunication.deps() == ("nsl_amba.apb",)


def test_swd_rides_an_exported_clock_like_any_other_transport():
    # The DP synchronises the wire into the rack's own clock, so the mode is
    # clocked and takes the clock key.
    assert SwdCommunication.CLOCKED
    clock = HostClock.resolve(described(
        DOMAIN, "mode: swd\n  clock: la.ctrl"))
    assert (clock.clock, clock.reset_n) == ("la_ctrl_sysclk_i", "reset_n_i")
    assert declarations(clock.ports) == ["reset_n_i : in std_ulogic"]
    # And without the key the rack gets ports of its own.
    bare = HostClock.resolve(described(DOMAIN, "mode: swd"))
    assert declarations(bare.ports) == ["clock_i : in std_ulogic",
                                        "reset_n_i : in std_ulogic"]


def test_jtag_is_clocked_like_any_other_transport():
    # The TAP crosses TCK to the host clock in its own FIFOs, and its framed
    # side runs on the host clock: the mode takes the clock key.
    assert JtagCommunication.CLOCKED
    clock = HostClock.resolve(described(
        DOMAIN, "mode: jtag\n  clock: la.ctrl"))
    assert (clock.clock, clock.reset_n) == ("la_ctrl_sysclk_i", "reset_n_i")


def test_the_host_clock_rate_comes_from_the_clock_it_rides():
    # The analyzer states its domain's rate, so a transport riding it needs no
    # generic for it.
    stated = HostClock.resolve(described(
        "      ctrl:\n        frequency: 100_000_000\n        signals:\n"
        "          a:\n            trigger: true\n",
        "mode: serial_hdlc\n  clock: la.ctrl"))
    assert stated.frequency == 100_000_000
    assert HostClock.resolve(described(
        DOMAIN, "mode: serial_hdlc\n  clock: la.ctrl")).frequency == 0


def test_host_clock_falls_back_to_dedicated_ports():
    clock = HostClock.resolve(described(DOMAIN, "mode: axi4_stream"))
    assert (clock.clock, clock.reset_n) == ("clock_i", "reset_n_i")
    assert declarations(clock.ports) == ["clock_i : in std_ulogic",
                                         "reset_n_i : in std_ulogic"]


def test_clockless_transport_rejects_the_clock_key():
    class ProbeCommunication(Axi4StreamCommunication):
        MODE = "jtag_probe"
        CLOCKED = False

    CommunicationRegistry.register(ProbeCommunication)
    try:
        with pytest.raises(Exception) as raised:
            described(DOMAIN, "mode: jtag_probe\n  clock: la.ctrl")
        assert "clockless and takes no clock key" in str(raised.value)
    finally:
        del CommunicationRegistry.PLUGINS["jtag_probe"]
