"""The Vivado seam: a rack seen as a block-design IP.

A rack's boundary carries NSL records and is named the way gatecap names
things. A Vivado IP's boundary carries flat pins and says what they mean
through ``X_INTERFACE_INFO`` attributes. This module is the wrapper between
the two, and the two hooks that let a plugin have its say about it.

``VivadoIoRegistry`` is keyed by the rack-side VHDL type: one entry says how
that type becomes pins, which packer does the packing, and which Xilinx bus it
is. A type is registered once and every plugin that puts it on the boundary
benefits. A record type with no entry is a generation error, never a dropped
pin.

``vivado_interfaces`` on the transport, instrument and signal-type bases says
which of the plugin's own ports form one interface, what role it plays and
what it is called. A clock or a reset is declared there too -- nothing in a
port's type says it is one -- and is renamed to the pin its interface states.
Every other port crosses as a plain pin of its own type and its own name,
which is what a probed vector is.

Everything the wrapper needs is known before it starts: which ports the
transport put on the boundary, which clock each domain runs on and how fast,
and what type each port has. Nothing here is a choice the user has to make,
which is why the wrapper is generated rather than written.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .errors import DescriptionError
from .checks import Check
from .vhdl import (Architecture, AttributeDecl, AttributeSpec, Constant,
                   DesignFile, Entity, Group, Instance, Port, SignalDecl)

# The two attributes Vivado's packager reads off a port.
INFO = "X_INTERFACE_INFO"
PARAMETER = "X_INTERFACE_PARAMETER"


def boundary_name(port):
    """A rack port's name as a clock or reset interface takes it: its own,
    minus the direction suffix, which a block design has no use for.

    Only a renamed pin goes through this. A plain pin keeps the rack's name
    whole, because stripping it would collide a control with a status of the
    same name."""
    for suffix in ("_i", "_o"):
        if port.endswith(suffix):
            return port[:-len(suffix)]
    return port


def type_base(type_name):
    """A port type without its index constraint."""
    return type_name.split("(", 1)[0].strip()


# Interfaces, as a plugin declares them


@dataclass(frozen=True)
class BusInterface:
    """Ports of one Xilinx bus interface. The bus itself is the registry's
    business: what is declared here is which ports form it, which way it
    faces, and which clock it runs on."""

    name: str      # boundary prefix, and the interface's name in Vivado
    ports: tuple   # rack port names forming it
    mode: str      # "slave" or "master", from the rack's point of view
    clock: str | None  # rack port of the clock it runs on, None for the host's
    params: dict = field(default_factory=dict)  # what the binding reads
    # Constants the wrapper declares for this interface, for a geometry the
    # plugin knows and the wrapper has no other way to name.
    declarations: tuple = ()


@dataclass(frozen=True)
class ClockInterface:
    """One clock pin. The buses it clocks are derived, not declared: they are
    the ones naming this clock's port."""

    name: str
    port: str
    frequency: int = 0    # Hz, 0 when the description states none
    reset: str | None = None   # rack port of the reset of the same domain


@dataclass(frozen=True)
class ResetInterface:
    """One reset pin, and every rack reset it drives. Merging two resets that
    are the same reset is what the tuple is for."""

    name: str
    ports: tuple
    active_low: bool = True


@dataclass(frozen=True)
class Unbound:
    """Rack ports the IP boundary leaves out.

    A port a vendor's own primitive already answers for has nothing to do on
    an IP boundary: the TAP pins are routed to that primitive inside the
    fabric, so a pin for each of them would be a pin nothing may drive. They
    are left open, which is what their default assignments are for."""

    ports: tuple


# What a binding answers with


@dataclass(frozen=True)
class Exposure:
    """One interface, turned into wrapper boundary and wrapper innards."""

    ports: tuple = ()          # flat Port on the wrapper boundary
    attributes: tuple = ()     # AttributeSpec over those ports
    declarations: tuple = ()   # Constant / SignalDecl the glue needs
    statements: tuple = ()     # the packer instances
    bindings: dict = field(default_factory=dict)  # rack formal -> actual
    deps: tuple = ()           # gbs partitions the glue pulls in


class VivadoIo:
    """Base class of a type binding: how one rack-side VHDL type reaches a
    Vivado boundary."""

    # Rack-side port types this binding claims, without index constraint.
    TYPES = ()
    # The IP-XACT bus these ports form, or None for plain pins.
    BUS = None
    # gbs partitions the glue needs.
    DEPS = ()

    @classmethod
    def expose(cls, interface, ports):
        """``ports`` are the rack :class:`Port` objects, in rack declaration
        order. ``interface`` is the :class:`BusInterface` that claimed them,
        or None for a port nothing claimed."""
        raise NotImplementedError


class VivadoIoRegistry:
    """Rack-side VHDL type -> binding."""

    BINDINGS = {}

    @classmethod
    def register(cls, binding):
        for name in binding.TYPES:
            assert name not in cls.BINDINGS, \
                f"port type {name!r} is already bound"
            cls.BINDINGS[name] = binding
        return binding

    @classmethod
    def get(cls, port):
        base = type_base(port.type)
        try:
            return cls.BINDINGS[base]
        except KeyError:
            raise DescriptionError(
                f"port {port.name!r} of type {base} has no Vivado binding "
                f"(bound: {', '.join(cls.types())})") from None

    @classmethod
    def types(cls):
        return tuple(sorted(cls.BINDINGS))


@VivadoIoRegistry.register
class ScalarIo(VivadoIo):
    """Logic types, which cross unchanged: the IP's pin is the rack's port,
    with its name and its type. Clocks, resets and probed vectors are all
    this."""

    TYPES = ("std_ulogic", "std_ulogic_vector", "std_logic",
             "std_logic_vector", "unsigned", "signed")

    @classmethod
    def expose(cls, interface, ports):
        if interface is not None:
            raise DescriptionError(
                f"interface {interface.name!r} groups logic ports "
                + ", ".join(port.name for port in ports)
                + ", which form no bus of their own")
        port, = ports
        return Exposure(ports=(port,), bindings={port.name: port.name})


@dataclass(frozen=True)
class StreamGeometry:
    """An AXI4-Stream boundary as the wrapper fixes it. Presence is part of
    the wrapper's shape, so it is stated here rather than read back from a
    generic: a pin cannot appear and disappear with a parameter."""

    data_bytes: int = 1
    id_width: int = 0
    dest_width: int = 0
    user_width: int = 0
    last: bool = False
    keep: bool = False
    strb: bool = False

    def config(self):
        """The ``nsl_amba.axi4_stream.config`` call building it."""
        arguments = [f"bytes => {self.data_bytes}"]
        for name, width in (("id", self.id_width), ("dest", self.dest_width),
                            ("user", self.user_width)):
            if width:
                arguments.append(f"{name} => {width}")
        for name, present in (("last", self.last), ("keep", self.keep),
                              ("strb", self.strb)):
            if present:
                arguments.append(f"{name} => true")
        return f"nsl_amba.axi4_stream.config({', '.join(arguments)})"

    def signals(self):
        """Logical signal name -> width in bits, None for a scalar, in the
        order the wrapper declares them."""
        signals = {"tdata": 8 * self.data_bytes}
        if self.strb:
            signals["tstrb"] = self.data_bytes
        if self.keep:
            signals["tkeep"] = self.data_bytes
        if self.last:
            signals["tlast"] = None
        for name, width in (("tid", self.id_width),
                            ("tdest", self.dest_width),
                            ("tuser", self.user_width)):
            if width:
                signals[name] = width
        signals["tvalid"] = None
        return signals


@VivadoIoRegistry.register
class Axi4StreamIo(VivadoIo):
    """An AXI4-Stream pair: a ``master_t`` and the ``slave_t`` answering it,
    packed into pins by the NSL packers.

    The rack's own type carries the whole channel in one record, so which way
    the interface faces follows from the direction of its master half: a
    master the rack takes in is a slave interface on the IP."""

    TYPES = ("nsl_amba.axi4_stream.master_t", "nsl_amba.axi4_stream.slave_t")
    BUS = "xilinx.com:interface:axis:1.0"
    DEPS = ("nsl_amba.packer",)
    BUS_TYPE = "nsl_amba.axi4_stream.bus_t"
    PACKERS = {"slave": "nsl_amba.packer.axi4_stream_slave_packer",
               "master": "nsl_amba.packer.axi4_stream_master_packer"}
    # tready runs against the channel, so it is the one pin whose direction is
    # not the interface's.
    READY = "tready"

    @classmethod
    def expose(cls, interface, ports):
        master, slave = cls.__halves(interface, ports)
        mode = "slave" if master.direction == "in" else "master"
        if mode != interface.mode:
            raise DescriptionError(
                f"interface {interface.name!r} is declared {interface.mode} "
                f"but its master half {master.name!r} is {master.direction}")
        geometry = interface.params["geometry"]
        config = interface.params["config"]

        forward = "in" if mode == "slave" else "out"
        backward = "out" if mode == "slave" else "in"
        boundary, port_map = [], {}
        for name, width in geometry.signals().items():
            pin = f"{interface.name}_{name}"
            boundary.append(Port(pin, forward, cls.__type(width)))
            port_map[name] = pin
        ready = f"{interface.name}_{cls.READY}"
        boundary.append(Port(ready, backward, "std_logic"))
        port_map[cls.READY] = ready

        signal = f"{interface.name}_s"
        port_map["stream_i"] = f"{signal}.m" if mode == "master" \
            else f"{signal}.s"
        port_map["stream_o"] = f"{signal}.s" if mode == "master" \
            else f"{signal}.m"

        return Exposure(
            ports=tuple(boundary),
            attributes=tuple(
                AttributeSpec(INFO, port.name,
                              f"{cls.BUS} {interface.name} "
                              f"{port.name[len(interface.name) + 1:].upper()}")
                for port in boundary),
            declarations=(SignalDecl(signal, cls.BUS_TYPE),),
            statements=(Instance(f"{interface.name}_packer",
                                 cls.PACKERS[mode],
                                 generic_map={"config_c": config},
                                 port_map=port_map),),
            bindings={master.name: f"{signal}.m", slave.name: f"{signal}.s"},
            deps=cls.DEPS)

    @classmethod
    def __halves(cls, interface, ports):
        halves = {}
        for port in ports:
            halves[type_base(port.type)] = port
        try:
            return (halves["nsl_amba.axi4_stream.master_t"],
                    halves["nsl_amba.axi4_stream.slave_t"])
        except KeyError:
            raise DescriptionError(
                f"interface {interface.name!r} needs one "
                "nsl_amba.axi4_stream.master_t port and one slave_t, got "
                + ", ".join(f"{p.name}: {p.type}" for p in ports)) from None

    @staticmethod
    def __type(width):
        if width is None:
            return "std_logic"
        return f"std_logic_vector({width - 1} downto 0)"


@dataclass(frozen=True)
class ApbGeometry:
    """An APB boundary as the wrapper fixes it.

    ``config`` names the configuration constant the packer is given, which the
    architecture declares.  The widths are stated apart from it, as plain
    numbers: the packager works IP-XACT expressions out of the port clause
    itself and evaluates no VHDL, so a width it cannot read as a literal is a
    width it refuses.  What the rack really elaborated to is checked against
    them rather than read off them.  Presence is the wrapper's shape and is
    stated here too."""

    config: str          # configuration constant, for the packer
    address_width: int   # bits on the boundary
    data_bytes: int      # bytes on the boundary
    prot: bool = False
    strb: bool = False

    def signals(self):
        """Logical signal name -> (direction from the master, width in bits
        or None for a scalar), in declaration order."""
        data = 8 * self.data_bytes
        signals = {"paddr": ("out", self.address_width),
                   "psel": ("out", None),
                   "penable": ("out", None),
                   "pwrite": ("out", None),
                   "pwdata": ("out", data)}
        if self.prot:
            signals["pprot"] = ("out", 3)
        if self.strb:
            signals["pstrb"] = ("out", self.data_bytes)

        signals["pready"] = ("in", None)
        signals["prdata"] = ("in", data)
        signals["pslverr"] = ("in", None)
        return signals


@VivadoIoRegistry.register
class ApbIo(VivadoIo):
    """An APB pair: a ``master_t`` and the ``slave_t`` answering it, packed
    into pins by the NSL packers.

    Which way the interface faces follows from the direction of its master
    half, as it does for a stream: a requester the rack drives out is a master
    interface on the IP, and the completer it offers is a slave one."""

    TYPES = ("nsl_amba.apb.master_t", "nsl_amba.apb.slave_t")
    BUS = "xilinx.com:interface:apb:1.0"
    DEPS = ("nsl_amba.packer",)
    BUS_TYPE = "nsl_amba.apb.bus_t"
    PACKERS = {"slave": "nsl_amba.packer.apb_slave_packer",
               "master": "nsl_amba.packer.apb_master_packer"}

    @classmethod
    def expose(cls, interface, ports):
        master, slave = cls.__halves(interface, ports)
        mode = "slave" if master.direction == "in" else "master"
        if mode != interface.mode:
            raise DescriptionError(
                f"interface {interface.name!r} is declared {interface.mode} "
                f"but its master half {master.name!r} is {master.direction}")
        geometry = interface.params["geometry"]

        boundary, port_map = [], {}
        for name, (towards, width) in geometry.signals().items():
            pin = f"{interface.name}_{name}"
            direction = towards if mode == "master" else cls.__back(towards)
            boundary.append(Port(pin, direction, cls.__type(width)))
            port_map[name] = pin

        signal = f"{interface.name}_s"
        port_map["apb_i"] = f"{signal}.m" if mode == "master" \
            else f"{signal}.s"
        port_map["apb_o"] = f"{signal}.s" if mode == "master" \
            else f"{signal}.m"

        return Exposure(
            ports=tuple(boundary),
            attributes=tuple(
                AttributeSpec(INFO, port.name,
                              f"{cls.BUS} {interface.name} "
                              f"{port.name[len(interface.name) + 1:].upper()}")
                for port in boundary),
            declarations=(SignalDecl(signal, cls.BUS_TYPE),),
            statements=cls.__checks(interface, geometry)
            + (Instance(f"{interface.name}_packer",
                        cls.PACKERS[mode],
                        generic_map={"config_c": geometry.config},
                        port_map=port_map),),
            bindings={master.name: f"{signal}.m", slave.name: f"{signal}.s"},
            deps=cls.DEPS + (Check.DEP,))

    @classmethod
    def __checks(cls, interface, geometry):
        """The pins are fixed and the configuration behind them is not, so
        what the rack elaborated to is checked against the boundary it was
        packaged with rather than read off it."""
        checks = (
            Check(f"{interface.name}_address_width_check",
                  f"{interface.name} carries {geometry.address_width} address "
                  "bit(s) on the IP boundary, and the bus behind it does not",
                  f"{geometry.config}.address_width = "
                  f"{geometry.address_width}"),
            Check(f"{interface.name}_data_width_check",
                  f"{interface.name} carries {geometry.data_bytes} data "
                  "byte(s) on the IP boundary, and the bus behind it does not",
                  f"2**{geometry.config}.data_bus_width_l2 = "
                  f"{geometry.data_bytes}"),
            )
        statements = []
        for check in checks:
            statements += list(check.statements())
        return tuple(statements)

    @classmethod
    def __halves(cls, interface, ports):
        halves = {type_base(port.type): port for port in ports}
        try:
            return (halves["nsl_amba.apb.master_t"],
                    halves["nsl_amba.apb.slave_t"])
        except KeyError:
            raise DescriptionError(
                f"interface {interface.name!r} needs one nsl_amba.apb.master_t "
                "port and one slave_t, got "
                + ", ".join(f"{p.name}: {p.type}" for p in ports)) from None

    @staticmethod
    def __back(direction):
        return "in" if direction == "out" else "out"

    @staticmethod
    def __type(width):
        if width is None:
            return "std_logic"
        return f"std_logic_vector({width - 1} downto 0)"


class VivadoIpWrapper:
    """The topcell of a rack packaged as a Vivado IP.

    One entity whose boundary is flat pins carrying interface attributes, one
    packer per record interface, and the rack behind them. Everything on that
    boundary comes from the rack's own: the transport says which ports form
    its stream pair, the instruments say which of their ports are clocks and
    how fast they run, and the port types say the rest."""

    SUFFIX = "_vivado_ip"
    ARCHITECTURE = "rtl"
    HEADER = ("Vivado IP topcell generated by acrobe gatecap generate.\n"
              "Regenerate from the description; edits here are lost.")
    CLOCK = "aclk"
    RESET = "aresetn"
    CLOCK_BUS = "xilinx.com:signal:clock:1.0"
    RESET_BUS = "xilinx.com:signal:reset:1.0"
    RACK_LABEL = "rack"

    OPEN = "open"

    def __init__(self, rack):
        self.rack = rack
        self.clocks, self.resets, self.buses, self.unbound = \
            self.__interfaces()
        self.exposures = self.__exposures()
        self.generics, self.generic_map, self.constants = self.__generics()

    def name(self):
        return f"{self.rack.entity_name()}{self.SUFFIX}"

    def file_name(self):
        return f"{self.name()}.vhd"

    # Interfaces

    def __interfaces(self):
        """Every interface the rack's contributors declare, with the host
        clock folded into the domain it rides."""
        clocks, resets, buses, unbound = [], [], [], []
        for declared in self.__declared():
            if isinstance(declared, ClockInterface):
                clocks.append(declared)
            elif isinstance(declared, ResetInterface):
                resets.append(declared)
            elif isinstance(declared, BusInterface):
                buses.append(declared)
            elif isinstance(declared, Unbound):
                unbound += list(declared.ports)
            else:
                raise AssertionError(
                    f"{declared!r} is not a Vivado interface")
        clocks, resets = self.__merged(clocks, resets)
        # A bus an instrument rides the host clock with names no clock of its
        # own; the host's is the one it runs on.
        host = clocks[0].port
        buses = [bus if bus.clock is not None
                 else replace(bus, clock=host) for bus in buses]
        return clocks, resets, tuple(buses), frozenset(unbound)

    def __declared(self):
        declared = list(self.rack.communication.vivado_interfaces(
            self.rack.communication_context))
        for instrument in self.rack.description.instruments:
            declared += list(instrument.plugin.vivado_interfaces(instrument))
        return declared

    def __merged(self, clocks, resets):
        """The rack's own host clock and reset, and the merge that follows
        from the description: a transport riding an instrument's clock runs in
        that instrument's domain, so the reset the rack takes for its host
        side and the reset of that domain are one pin."""
        export = self.rack.description.communication.clock_export
        if export is None:
            clocks.insert(0, ClockInterface(
                self.CLOCK, self.rack.clocks.clock,
                frequency=self.rack.clocks.frequency,
                reset=self.rack.clocks.reset_n))
            resets.insert(0, ResetInterface(
                self.RESET, (self.rack.clocks.reset_n,)))
            return tuple(clocks), tuple(resets)

        host = self.rack.clocks.clock
        ridden = [clock for clock in clocks if clock.port == host]
        if len(ridden) != 1:
            raise DescriptionError(
                f"exported clock {export!r} is port {host!r}, which "
                f"{len(ridden)} instrument clock interface(s) claim: a "
                "Vivado IP needs exactly one", "communication.clock")
        clock, = ridden
        host_reset = self.rack.clocks.reset_n
        clocks[clocks.index(clock)] = ClockInterface(
            self.CLOCK, host, frequency=clock.frequency, reset=host_reset)
        merged = False
        for index, reset in enumerate(resets):
            if clock.reset is not None and clock.reset in reset.ports:
                resets[index] = ResetInterface(
                    self.RESET, (host_reset,) + reset.ports,
                    active_low=reset.active_low)
                merged = True
        if not merged:
            # The clock comes from an instrument with no reset of its own --
            # a rate measurer's reference, say -- so there is nothing to
            # merge and the rack's own reset is the pin.
            resets.insert(0, ResetInterface(self.RESET, (host_reset,)))
        return tuple(clocks), tuple(resets)

    # Ports

    def __claimed(self):
        """Rack port name -> the bus interface claiming it."""
        claimed = {}
        for bus in self.buses:
            for port in bus.ports:
                if port in claimed:
                    raise DescriptionError(
                        f"port {port!r} is claimed by interfaces "
                        f"{claimed[port].name!r} and {bus.name!r}")
                claimed[port] = bus
        return claimed

    def __exposures(self):
        """One exposure per interface and per unclaimed port. Clock and reset
        pins come first, the way a block design reads them; the rest keeps the
        rack's own port order."""
        ports = {port.name: port for port in self.rack.ports()}
        claimed = self.__claimed()
        unknown = (set(claimed) | self.unbound) - set(ports)
        if unknown:
            raise DescriptionError(
                "interface(s) claim port(s) the rack does not have: "
                + ", ".join(sorted(unknown)))
        both = set(claimed) & self.unbound
        if both:
            raise DescriptionError(
                "port(s) both claimed by an interface and left unbound: "
                + ", ".join(sorted(both)))
        clocks, resets, rest, done = [], [], [], set()
        for name, port in ports.items():
            if name in self.unbound:
                rest.append(self.__open(port))
                continue
            bus = claimed.get(name)
            if bus is None:
                exposure = self.__logic(port)
                if any(clock.port == name for clock in self.clocks):
                    clocks.append(exposure)
                elif any(name in reset.ports for reset in self.resets):
                    resets.append(exposure)
                else:
                    rest.append(exposure)
                continue
            if bus.name in done:
                continue
            done.add(bus.name)
            group = tuple(ports[member] for member in bus.ports)
            binding = VivadoIoRegistry.get(group[0])
            rest.append(binding.expose(bus, group))
        return tuple(clocks) + tuple(resets) + tuple(rest)

    def __open(self, port):
        """A port the boundary leaves out, bound to nothing."""
        if port.direction == "in" and port.default is None:
            raise DescriptionError(
                f"port {port.name!r} is left off the Vivado boundary but has "
                "no default, so nothing would drive it")
        return Exposure(bindings={port.name: self.OPEN})

    def __logic(self, port):
        """An unclaimed port, under the boundary name and the attributes of
        the clock or reset role it was declared in."""
        binding = VivadoIoRegistry.get(port)
        if binding.BUS is not None:
            raise DescriptionError(
                f"port {port.name!r} of type {port.type} is half of a "
                f"{binding.BUS} interface, and no plugin says which")
        exposure = binding.expose(None, (port,))
        pin, = exposure.ports
        name = self.__pin(port.name, pin.name)
        if self.__secondary(port.name):
            # A reset several rack ports share is one pin, declared once.
            return Exposure(bindings={port.name: name})
        if name == pin.name:
            return Exposure(ports=(pin,),
                            attributes=self.__role(port.name),
                            bindings={port.name: name})
        # A renamed pin drops the comment, which described the rack's port.
        return Exposure(
            ports=(Port(name, pin.direction, pin.type, default=pin.default),),
            attributes=self.__role(port.name),
            bindings={port.name: name})

    def __secondary(self, rack_port):
        """Whether a port shares a reset pin already declared for another."""
        return any(rack_port in reset.ports[1:] for reset in self.resets)

    def __pin(self, rack_port, default):
        """The boundary name of a rack port: the one its clock or reset
        interface states, or the one the binding derived."""
        for clock in self.clocks:
            if clock.port == rack_port:
                return clock.name
        for reset in self.resets:
            if rack_port in reset.ports:
                return reset.name
        return default

    def __role(self, rack_port):
        for clock in self.clocks:
            if clock.port == rack_port:
                return self.__clock_attributes(clock)
        for reset in self.resets:
            if rack_port in reset.ports:
                return self.__reset_attributes(reset)
        return ()

    def __clock_attributes(self, clock):
        parameters = []
        associated = [bus.name for bus in self.buses
                      if bus.clock == clock.port]
        if associated:
            parameters.append(f"ASSOCIATED_BUSIF {':'.join(associated)}")
        if clock.reset is not None:
            parameters.append(f"ASSOCIATED_RESET {self.__pin_of(clock.reset)}")
        if clock.frequency:
            parameters.append(f"FREQ_HZ {clock.frequency}")
        attributes = [AttributeSpec(
            INFO, clock.name, f"{self.CLOCK_BUS} {clock.name} CLK")]
        if parameters:
            attributes.append(
                AttributeSpec(PARAMETER, clock.name, ", ".join(parameters)))
        return tuple(attributes)

    def __reset_attributes(self, reset):
        polarity = "ACTIVE_LOW" if reset.active_low else "ACTIVE_HIGH"
        return (AttributeSpec(INFO, reset.name,
                              f"{self.RESET_BUS} {reset.name} RST"),
                AttributeSpec(PARAMETER, reset.name, f"POLARITY {polarity}"))

    def __pin_of(self, rack_port):
        for reset in self.resets:
            if rack_port in reset.ports:
                return reset.name
        raise DescriptionError(
            f"reset port {rack_port!r} has no reset interface")

    # Generics

    def __generics(self):
        """The rack's generics as the IP takes them.

        A generic the declaring plugin says nothing about crosses as it
        stands, and must be a scalar with a default: an IP parameter with no
        value is one the block design cannot elaborate. A plugin that knows
        better replaces it -- with a :class:`Constant` the wrapper declares,
        which is how a record generic reaches a boundary that cannot carry
        one, or with a :class:`Generic` of its own carrying the default the
        rack itself has no business fixing."""
        bound = dict(self.rack.communication.vivado_generics(
            self.rack.communication_context))
        for instrument in self.rack.description.instruments:
            bound.update(instrument.plugin.vivado_generics(instrument))
        generics, generic_map, constants = [], {}, []
        for generic in self.rack.generics():
            replacement = bound.get(generic.name, generic)
            if isinstance(replacement, Constant):
                constants.append(replacement)
                generic_map[generic.name] = replacement.name
                continue
            if type_base(replacement.type) not in self.SCALARS:
                if replacement.default is None:
                    raise DescriptionError(
                        f"generic {generic.name} of type {replacement.type} "
                        "cannot cross a Vivado IP boundary, and the plugin "
                        "declaring it binds it to no constant")
                # The rack states a value an instantiating design may
                # override; the IP takes the rack's own.
                constant = Constant(replacement.name, replacement.type,
                                    replacement.default,
                                    comment=replacement.comment)
                constants.append(constant)
                generic_map[generic.name] = constant.name
                continue
            if replacement.default is None:
                raise DescriptionError(
                    f"generic {generic.name} has no default, so the packaged "
                    "IP would have a parameter with no value")
            generics.append(replacement)
            generic_map[generic.name] = replacement.name
        return tuple(generics), generic_map, tuple(constants)

    SCALARS = ("natural", "positive", "integer", "boolean", "string", "real")

    # Emission

    def ports(self):
        ports = []
        seen = set()
        for exposure in self.exposures:
            for port in exposure.ports:
                if port.name in seen:
                    raise DescriptionError(
                        f"two Vivado boundary pins are named {port.name!r}")
                seen.add(port.name)
                ports.append(port)
        return tuple(ports)

    def deps(self):
        """gbs partitions the wrapper needs on top of the rack's own: the
        packers, and whatever a third-party binding pulls in."""
        deps = []
        for exposure in self.exposures:
            deps += list(exposure.deps)
        return tuple(sorted(set(deps)))

    def libraries(self):
        """Libraries the file names, in first-seen order. The rack is a unit
        of this very library, so it is reached through work and states
        nothing."""
        libraries = []
        for name in self.__qualified():
            base = type_base(name)
            if "." not in base:
                continue
            library = base.split(".", 1)[0]
            if library not in libraries and library not in ("work", "ieee"):
                libraries.append(library)
        return tuple(libraries)

    def __qualified(self):
        for generic in self.generics:
            yield generic.type
        for port in self.ports():
            yield port.type
        for bus in self.buses:
            for constant in bus.declarations:
                yield constant.type
                yield constant.value
        for constant in self.constants:
            yield constant.type
            yield constant.value
        for statement in self.statements():
            # Only an instance names a unit; a check's assert names nothing.
            yield getattr(statement, "unit", "")
        for exposure in self.exposures:
            for declaration in exposure.declarations:
                yield declaration.type

    def entity(self):
        return Entity(self.name(), self.generics, self.ports(),
                      comment=self.__entity_comment())

    def __entity_comment(self):
        return "\n".join([
            f"Rack {self.rack.entity_name()} as a Vivado IP: the same core, "
            "behind a boundary of flat pins carrying the interface attributes "
            "the packager reads.",
            "",
            "This is the unit the IP is packaged from. It holds one packer "
            "per record interface and the rack itself; nothing of the rack's "
            "own geometry is settled here."])

    def declarations(self):
        attributes = [AttributeDecl(INFO), AttributeDecl(PARAMETER)]
        specifications, declarations = [], []
        for exposure in self.exposures:
            specifications += list(exposure.attributes)
            declarations += list(exposure.declarations)
        constants = list(self.constants)
        for bus in self.buses:
            constants += list(bus.declarations)
        return ((Group(tuple(attributes)), Group(tuple(specifications)))
                + tuple(constants) + tuple(declarations))

    def statements(self):
        statements = []
        for exposure in self.exposures:
            statements += list(exposure.statements)
        return tuple(statements) + (self.instance(),)

    def instance(self):
        port_map = {}
        for exposure in self.exposures:
            port_map.update(exposure.bindings)
        return Instance(
            self.RACK_LABEL,
            f"work.{self.rack.package_name()}.{self.rack.entity_name()}",
            generic_map=self.generic_map,
            port_map={port.name: port_map[port.name]
                      for port in self.rack.ports()})

    def architecture(self):
        return Architecture(self.ARCHITECTURE, self.name(),
                            self.declarations(), self.statements())

    def file(self):
        return DesignFile(
            header=self.HEADER,
            clauses=DesignFile.context(
                self.libraries(),
                (f"work.{self.rack.package_name()}.all",)),
            units=(self.entity(), self.architecture()))

    def files(self):
        return {self.file_name(): self.file().render()}
