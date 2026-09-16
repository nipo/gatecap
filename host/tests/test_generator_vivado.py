"""The Vivado IP topcell: the boundary a rack is packaged behind.

What is tested here is the wrapper alone -- which pins it has, what the
interface attributes say about them, and how the records behind them are
packed. The rack itself is tested in test_generator_assembly.py.

Run: python3.13 -m pytest host/tests/test_generator_vivado.py
"""

import pathlib

import pytest

from acrobe_plugin.gatecap.generator import (BusInterface, DescriptionError,
                                             DescriptionParser, Port,
                                             RackAssembly, StreamGeometry,
                                             StreamParameters, Unbound,
                                             VivadoIoRegistry, VivadoIpWrapper)

# A UART with a clock of its own: the host rate is a generic, and no default
# is right for it -- a wrong one divides down to a baud rate nothing speaks.
OWN_CLOCK_UART = """
name: probe_pkg.serial_capture

communication:
  mode: serial_hdlc

instruments:
  la: !logic-analyzer
    storage:
      buffer_depth_l2: 8
    domains:
      sample:
        clock: clock
        frequency: 50_000_000
        signals:
          state: !bus
            width: 2
            trigger: true
"""


# Two bus explorers over the plain APB completer: the rack's own APB reaches
# the boundary, and so does each explorer's target bus.
EXPLORERS = """
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


# A probed bnoc bus: a record the wrapper has no packer for, where a probed
# AXI4-Stream has one.
BNOC_PROBE = """
name: bnoc_pkg.bnoc_capture

communication:
  mode: apb

instruments:
  la: !logic-analyzer
    storage:
      buffer_depth_l2: 8
    domains:
      link:
        clock: clock
        frequency: 50_000_000
        signals:
          command: !bnoc-framed
            trigger: vlr
"""


DESCRIPTIONS = pathlib.Path(__file__).parent / "descriptions"


def assembled(name):
    return RackAssembly(DescriptionParser.load_file(DESCRIPTIONS
                                                    / f"{name}.yaml"))


def wrapper(name):
    return assembled(name).vivado_wrapper()


def wrapped(name):
    rack = assembled(name)
    text, = rack.vivado_files().values()
    return text


def pins(name):
    return [port.name for port in wrapper(name).ports()]


def refusal(name):
    with pytest.raises(DescriptionError) as raised:
        assembled(name).vivado_files()
    return str(raised.value)


# The boundary


def test_the_stream_pair_becomes_two_axi_stream_interfaces():
    text = wrapped("stream_ip")
    assert """\
    s_axis_tdata : in std_logic_vector(7 downto 0);
    s_axis_tlast : in std_logic;
    s_axis_tvalid : in std_logic;
    s_axis_tready : out std_logic;
    m_axis_tdata : out std_logic_vector(7 downto 0);
    m_axis_tlast : out std_logic;
    m_axis_tvalid : out std_logic;
    m_axis_tready : in std_logic;""" in text
    # The rack takes a master in on rx, so that half is the slave interface.
    assert ('attribute X_INTERFACE_INFO of s_axis_tdata : signal is '
            '"xilinx.com:interface:axis:1.0 s_axis TDATA";') in text


def test_clocks_and_resets_come_first():
    # A block design reads the timing pins before anything else, and the rack
    # states its reset before the transport's ports.
    assert pins("stream_ip")[:4] == [
        "aclk", "la_pixel_pixclk", "aresetn", "la_pixel_reset_n"]


def test_a_probed_vector_crosses_under_its_own_name():
    # Stripping the direction suffix would collide a control with a status of
    # the same name, so only the pins an interface renames lose it.
    text = wrapped("stream_ip")
    assert "la_main_command_i : in std_ulogic_vector(7 downto 0);" in text
    assert "la_pixel_vsync_i : in std_ulogic" in text


def test_the_wrapper_has_no_pin_the_rack_does_not_explain():
    rack = assembled("stream_ip")
    bound = rack.vivado_wrapper().instance().port_map
    assert set(bound) == {port.name for port in rack.ports()}


# Clocks, resets and the merge


def test_the_ridden_domain_shares_one_reset_pin_with_the_host():
    # communication.clock is la.main, so the rack's host side and that domain
    # are one domain, and their two resets are one pin.
    text = wrapped("stream_ip")
    assert "reset_n_i => aresetn," in text
    assert "la_main_reset_n_i => aresetn," in text
    assert "la_main_clock_i => aclk," in text
    assert len([pin for pin in pins("stream_ip") if pin == "aresetn"]) == 1


def test_the_host_clock_carries_its_buses_its_reset_and_its_rate():
    assert ('attribute X_INTERFACE_PARAMETER of aclk : signal is '
            '"ASSOCIATED_BUSIF s_axis:m_axis, ASSOCIATED_RESET aresetn, '
            'FREQ_HZ 100000000";') in wrapped("stream_ip")


def test_a_domain_of_its_own_keeps_its_own_pair():
    text = wrapped("stream_ip")
    assert ('attribute X_INTERFACE_PARAMETER of la_pixel_pixclk : signal is '
            '"ASSOCIATED_RESET la_pixel_reset_n, FREQ_HZ 74250000";') in text
    assert ('attribute X_INTERFACE_PARAMETER of la_pixel_reset_n : signal is '
            '"POLARITY ACTIVE_LOW";') in text


def test_a_rack_with_a_clock_of_its_own_names_it_aclk():
    # No communication.clock: the rack's own clock_i and reset_n_i are the
    # pair, and the capture domain keeps its own beside them.
    text = wrapped("jtag_transport")
    assert "clock_i => aclk," in text
    assert "reset_n_i => aresetn," in text
    assert "la_sample_clock_i => la_sample_clock," in text
    assert "la_sample_reset_n_i => la_sample_reset_n," in text


# Ports the boundary leaves out


def test_the_tap_pins_stay_off_the_boundary():
    # Xilinx wires the TAP internally, so the adapter's primitive reaches the
    # chip's own and a block design has nothing to connect here.
    text = wrapped("jtag_transport")
    for pin in ("chip_tck", "chip_tms", "chip_tdi", "chip_tdo"):
        assert pin not in [port.name for port in wrapper("jtag_transport")
                           .ports()]
        assert f"{pin}_i => open," in text or f"{pin}_o => open" in text


def test_an_unbound_port_with_nothing_to_drive_it_is_refused():
    rack = assembled("jtag_transport")
    plugin = rack.communication

    class Undefaulted(plugin):
        @classmethod
        def ports(cls):
            return (Port("chip_tck_i", "in", "std_ulogic"),) \
                + plugin.ports()[1:]

        @classmethod
        def vivado_interfaces(cls, context):
            return (Unbound(("chip_tck_i",)),)

    rack.communication = Undefaulted
    with pytest.raises(DescriptionError) as raised:
        VivadoIpWrapper(rack)
    assert "chip_tck_i" in str(raised.value)
    assert "no default" in str(raised.value)


# Generics


def test_a_record_generic_becomes_the_constant_its_plugin_states():
    text = wrapped("stream_ip")
    assert ("constant stream_config_c : nsl_amba.axi4_stream.config_t := "
            "nsl_amba.axi4_stream.config(bytes => 1, last => true);") in text
    assert "stream_config_c => stream_config_c" in text
    assert "stream_config_c :" not in text.split("end entity;")[0].split(
        "port (")[0].replace("constant ", "")


def test_a_scalar_generic_becomes_an_ip_parameter_with_a_default():
    # The rack's own has none -- a design instantiating it states one every
    # time -- and an IP parameter with no value is one Vivado cannot elaborate.
    rack = assembled("stream_ip")
    assert [(g.name, g.type, g.default) for g in rack.generics()] == [
        ("stream_config_c", "nsl_amba.axi4_stream.config_t", None),
        ("burst_length_l2_c", "natural", None)]
    assert [(g.name, g.default)
            for g in rack.vivado_wrapper().generics] == [
        ("burst_length_l2_c", "6")]


def test_the_transport_defaults_what_it_can_and_refuses_what_it_cannot():
    # serial_hdlc states a baud rate of its own -- 8n1 at 115200 is what a
    # host opens a port at -- but not a host clock rate the description leaves
    # out.
    rack = RackAssembly(DescriptionParser.load(OWN_CLOCK_UART))
    assert [(g.name, g.default) for g in rack.generics()] == [
        ("clock_frequency_c", None), ("baud_rate_c", None),
        ("burst_length_l2_c", None)]
    with pytest.raises(DescriptionError) as raised:
        rack.vivado_files()
    assert "clock_frequency_c has no default" in str(raised.value)


# Packing


def test_each_stream_is_packed_through_one_bus_signal():
    text = wrapped("stream_ip")
    assert """\
  s_axis_packer: nsl_amba.packer.axi4_stream_slave_packer
    generic map(
      config_c => stream_config_c
      )
    port map(
      tdata => s_axis_tdata,
      tlast => s_axis_tlast,
      tvalid => s_axis_tvalid,
      tready => s_axis_tready,
      stream_i => s_axis_s.s,
      stream_o => s_axis_s.m
      );""" in text
    assert "rx_i => s_axis_s.m,\n      rx_o => s_axis_s.s," in text
    assert "tx_o => m_axis_s.m,\n      tx_i => m_axis_s.s," in text


def test_the_wrapper_states_the_libraries_it_names_and_no_others():
    # The rack is a unit of this very library, so it is reached through work.
    text = wrapped("stream_ip")
    assert "\nlibrary nsl_amba;\n" in text
    assert "rack: work.ip_pkg.ip_capture" in text


# A probed stream


def test_a_probed_stream_becomes_a_monitor_interface():
    # The core drives nothing of an observed bus, tready included: what the
    # two ends answer each other is read and never answered back.
    text = wrapped("two_domains")
    assert ('attribute X_INTERFACE_MODE of la_control_command_tdata : signal '
            'is "monitor slave la_control_command";') in text
    monitor = [port for port in wrapper("two_domains").ports()
               if port.name.startswith("la_control_command_t")]
    assert [port.direction for port in monitor] == ["in"] * 9


def test_every_pin_of_a_probed_stream_exists_whatever_its_geometry():
    # A field the configuration turns off is a null range, not an absent pin,
    # and an optional pin carries the value a block design that has nothing
    # to drive it leaves behind.
    text = wrapped("two_domains")
    assert ("la_control_command_tdata : in std_logic_vector("
            "8 * la_control_command_data_bytes - 1 downto 0) "
            ":= (others => '0');") in text
    assert ("la_control_command_tuser : in std_logic_vector("
            "la_control_command_user_width - 1 downto 0) "
            ":= (others => '0');") in text
    assert "la_control_command_tvalid : in std_logic := '0';" in text
    assert "la_control_command_tready : in std_logic := '1';" in text


def test_a_probed_stream_states_its_geometry_as_ip_parameters():
    # The rack takes the geometry as a record with no default, on purpose; a
    # block design has nowhere to write one, so the IP asks for it in scalars.
    # ready and last default against the factory: an observed stream almost
    # always has both, and a selection over them silently loses bits without.
    generics = [(generic.name, generic.type, generic.default)
                for generic in wrapper("two_domains").generics]
    assert generics[0] == ("burst_length_l2_c", "natural", "6")
    assert generics[1:9] == [
        ("la_control_command_data_bytes", "natural", "1"),
        ("la_control_command_id_width", "natural", "0"),
        ("la_control_command_dest_width", "natural", "0"),
        ("la_control_command_user_width", "natural", "0"),
        ("la_control_command_has_keep", "boolean", "false"),
        ("la_control_command_has_strobe", "boolean", "false"),
        ("la_control_command_has_ready", "boolean", "true"),
        ("la_control_command_has_last", "boolean", "true")]


def test_the_probes_record_generic_is_rebuilt_from_those_parameters():
    text = wrapped("two_domains")
    assert """\
  constant la_control_command_config_c : nsl_amba.axi4_stream.config_t :=
    nsl_amba.axi4_stream.config(
      bytes => la_control_command_data_bytes,
      id => la_control_command_id_width,
      dest => la_control_command_dest_width,
      user => la_control_command_user_width,
      keep => la_control_command_has_keep,
      strobe => la_control_command_has_strobe,
      ready => la_control_command_has_ready,
      last => la_control_command_has_last);""" in text
    assert "la_control_command_config_c => la_control_command_config_c" in text


def test_a_probed_stream_reaches_the_rack_through_the_monitor_packer():
    text = wrapped("two_domains")
    assert """\
  la_control_command_packer: nsl_amba.packer.axi4_stream_monitor_packer
    generic map(
      config_c => la_control_command_config_c
      )
    port map(
      tdata => la_control_command_tdata,""" in text
    assert "stream_o => la_control_command_s" in text
    assert "la_control_command_i => la_control_command_s," in text


def test_a_probed_stream_is_clocked_by_the_domain_sampling_it():
    # A probe says what its bus is and not what clocks it; the domain does.
    assert ('attribute X_INTERFACE_PARAMETER of aclk : signal is '
            '"ASSOCIATED_BUSIF s_axis:m_axis:la_control_command:'
            'la_control_response, ASSOCIATED_RESET aresetn, '
            'FREQ_HZ 25000000";') in wrapped("two_domains")


def test_a_whole_stream_bus_declared_as_a_half_is_refused():
    # bus_t carries both halves, so nothing may drive one of them: an
    # interface over it is a monitor or it is a mistake.
    port = Port("probe_i", "in", "nsl_amba.axi4_stream.bus_t")
    interface = BusInterface("probe", (port.name,), "slave", clock=None,
                             params={"geometry": StreamParameters("probe"),
                                     "config": "probe_config_c"})
    with pytest.raises(DescriptionError) as raised:
        VivadoIoRegistry.get(port).expose(interface, (port,))
    assert "only ever a monitor" in str(raised.value)


def test_a_fixed_geometry_calls_the_factory_by_its_own_parameter_names():
    assert StreamGeometry(data_bytes=4, keep=True, strb=True).config() == \
        "nsl_amba.axi4_stream.config(bytes => 4, keep => true, strobe => true)"


# APB


def explorers():
    return RackAssembly(DescriptionParser.load(EXPLORERS))


def test_the_rack_completer_becomes_a_slave_apb_interface():
    text, = explorers().vivado_files().values()
    assert """\
    s_apb_paddr : in std_logic_vector(23 downto 0);
    s_apb_psel : in std_logic;
    s_apb_penable : in std_logic;
    s_apb_pwrite : in std_logic;
    s_apb_pwdata : in std_logic_vector(31 downto 0);
    s_apb_pready : out std_logic;
    s_apb_prdata : out std_logic_vector(31 downto 0);
    s_apb_pslverr : out std_logic;""" in text
    assert ('attribute X_INTERFACE_INFO of s_apb_paddr : signal is '
            '"xilinx.com:interface:apb:1.0 s_apb PADDR";') in text


def test_a_fixed_width_is_checked_against_what_elaborated():
    # The packager works IP-XACT expressions out of the port clause and
    # evaluates no VHDL, so the pins carry numbers; what the rack's map really
    # came out at is checked against them instead of sizing them.
    text, = explorers().vivado_files().values()
    assert """\
  assert apb_config_c.address_width = 24
    report "s_apb carries 24 address bit(s) on the IP boundary, and the bus \
behind it does not"
    severity failure;""" in text
    assert "assert 2**apb_config_c.data_bus_width_l2 = 4" in text
    # And once more as a synthesis assertion, which a synthesiser cannot skip.
    assert "s_apb_address_width_check: nsl_synthesis.assertion.synth_assert" \
        in text


def test_a_target_bus_becomes_a_master_apb_interface():
    text, = explorers().vivado_files().values()
    # The widths are the description's own, so they are literals.
    assert "gt0_target_paddr : out std_logic_vector(9 downto 0);" in text
    assert "gt0_target_pwdata : out std_logic_vector(15 downto 0);" in text
    assert "cfg_target_paddr : out std_logic_vector(11 downto 0);" in text
    assert "cfg_target_pwdata : out std_logic_vector(31 downto 0);" in text
    assert ("constant gt0_target_config_c : nsl_amba.apb.config_t := "
            "gatecap.bus_explorer.target_apb_config(10, 16);") in text
    assert "gt0_target_packer: nsl_amba.packer.apb_master_packer" in text
    # The library rounds a data width up to the bus it rides; the pins have to
    # agree with what it rounded to.
    assert "assert 2**gt0_target_config_c.data_bus_width_l2 = 2" in text


def test_a_target_bus_on_the_host_clock_is_clocked_by_it():
    # cfg states no clock of its own, so it rides the rack's, and that is the
    # clock its interface is associated with.
    text, = explorers().vivado_files().values()
    assert ('attribute X_INTERFACE_PARAMETER of aclk : signal is '
            '"ASSOCIATED_BUSIF s_apb:cfg_target, ASSOCIATED_RESET aresetn";'
            ) in text
    assert ('attribute X_INTERFACE_PARAMETER of gt0_drpclk : signal is '
            '"ASSOCIATED_BUSIF gt0_target, ASSOCIATED_RESET gt0_reset_n";'
            ) in text


# What it refuses


def test_a_record_port_with_no_binding_is_refused_by_name():
    rack = RackAssembly(DescriptionParser.load(BNOC_PROBE))
    with pytest.raises(DescriptionError) as raised:
        rack.vivado_files()
    message = str(raised.value)
    assert "la_link_command_i" in message
    assert "nsl_bnoc.framed.framed_bus_t has no Vivado binding" in message
    assert "nsl_amba.axi4_stream.bus_t" in message


def test_a_transport_with_no_binding_is_refused_too():
    # SPI pins are a record of their own, and nothing packs one.
    assert "'spi_i' of type nsl_spi.spi.spi_slave_i" in refusal(
        "spi_transport")
    assert "'swd_i' of type nsl_coresight.swd.swd_slave_i" in refusal(
        "swd_transport")


def test_every_logic_type_a_rack_puts_on_its_boundary_is_bound():
    assert set(VivadoIoRegistry.types()) >= {
        "std_ulogic", "std_ulogic_vector", "unsigned", "signed"}


# The partition


def test_the_wrapper_is_the_last_file_and_pulls_the_packers_in():
    rack = assembled("stream_ip")
    assert rack.file_names(vivado_ip=True)[-1] == "ip_capture_vivado_ip.vhd"
    assert rack.file_names() == rack.file_names(vivado_ip=True)[:-1]
    assert "nsl_amba.packer" not in rack.deps()
    assert "nsl_amba.packer" in rack.deps(vivado_ip=True)
    assert "  - nsl_amba.packer\n" in rack.manifest(vivado_ip=True).render()


def test_emission_is_deterministic():
    assert wrapped("stream_ip") == wrapped("stream_ip")


def test_the_wrapper_is_written_only_when_asked_for(tmp_path):
    rack = assembled("stream_ip")
    rack.write(tmp_path / "plain")
    assert not (tmp_path / "plain" / "ip_capture_vivado_ip.vhd").exists()
    rack.write(tmp_path / "ip", vivado_ip=True)
    assert (tmp_path / "ip" / "ip_capture_vivado_ip.vhd").exists()
