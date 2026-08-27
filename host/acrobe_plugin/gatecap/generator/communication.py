"""The communication seam: what a transport plugin is, and where they are kept.

A plugin owns the host-facing transport of a rack, and says so declaratively:
the ports and generics it puts on the rack's boundary, the library adapter that
terminates the link, how that adapter's generics are bound, and its gbs
dependencies. The transport logic itself lives in the gatecap library, one
entity per link, so nothing of it is emitted.

Only the base class, the registry and the context they share live here. The
modes that ship with gatecap are one module each under
:mod:`acrobe_plugin.gatecap.communication`, registered when that package is
imported, and a third-party mode registers the same way from its own plugin.

Host clock: when ``communication.clock`` names a domain or an exported clock,
that clock doubles as the host clock and no clock port is added; when the key
is absent the transport gets dedicated ``clock_i`` / ``reset_n_i`` ports. A
clockless transport rejects the key outright.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import DescriptionError
from .vhdl import Port


@dataclass(frozen=True)
class CommunicationContext:
    """What the generated unit offers a transport plugin."""

    clock: str            # host clock signal or port name
    reset_n: str          # host reset name
    apb_config: str       # expression naming the internal APB config constant
    apb_master: str       # signal the front end drives towards the blocks
    apb_slave: str        # signal the blocks drive back
    descriptor_base: str  # expression: byte address of the descriptor ROM
    clock_frequency: int  # host clock in Hz, 0 when the description omits it
    # Expression evaluating to the rack's descriptor fingerprint, for a
    # transport whose link carries an identity of its own (a USB serial-number
    # string, and nothing else so far). The value is an elaboration constant of
    # the package's making, so the expression is a call and not a name.
    fingerprint: str = ""
    # What the mode's own keys parsed to, as its plugin returned them.
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HostClock:
    """Where the host domain's clock and reset come from, and how fast it
    runs. The rate is known only when the transport rides a domain whose
    description states a frequency; it is zero otherwise, and a transport
    needing it takes it as a generic instead.

    A rack has no domain to ride: its clock is either its own port or one an
    instrument exports, and an exported clock names a clock alone, so the
    reset stays a port of the rack's. The rate comes from the instrument that
    exports the clock, when the description states one."""

    clock: str
    reset_n: str
    ports: tuple = ()
    frequency: int = 0

    CLOCK = "clock_i"
    RESET_N = "reset_n_i"

    @classmethod
    def resolve(cls, description):
        communication = description.communication
        if communication.clock_export is not None:
            export = communication.clock_export
            return cls(
                clock=description.exported_clocks()[export],
                reset_n=cls.RESET_N,
                ports=(Port(cls.RESET_N, "in", "std_ulogic",
                            comment="Host-side reset. The clock is exported "
                                    f"clock \"{export}\", which is a port of "
                                    "the instrument that exports it."),),
                frequency=description.exported_clock_rates().get(export, 0))
        return cls(clock=cls.CLOCK, reset_n=cls.RESET_N,
                   ports=(Port(cls.CLOCK, "in", "std_ulogic"),
                          Port(cls.RESET_N, "in", "std_ulogic")))


class CommunicationPlugin:
    """Base class and declaration interface of a transport.

    ``UNIT`` names the library adapter; ``ports`` and ``generics`` are the
    boundary interface, forwarded to that adapter formal to formal, and
    ``generic_map`` binds whatever else the adapter takes.

    ``KEYS`` are the keys the mode adds to the communication section, on top of
    the framework's own; :meth:`parse` validates them and returns what the
    plugin wants to read back from
    :attr:`CommunicationContext.params`."""

    MODE = None
    # False for a transport with no clock of its own (JTAG and friends): it
    # rejects communication.clock instead of reusing a domain's.
    CLOCKED = True
    # The gatecap library entity terminating the link, or None when the unit
    # hands its APB out and there is no adapter.
    UNIT = None
    # Formals every adapter takes: its requester geometry, and where it
    # publishes the descriptor.
    APB_CONFIG = "apb_config_c"
    DESCRIPTOR_BASE = "descriptor_base_c"
    # Keys of the communication section this mode owns, beyond mode and clock.
    KEYS = ()

    @classmethod
    def parse(cls, section, path):
        """Validate the mode's own keys and return them as the params the
        context carries."""
        return {}

    @classmethod
    def validate(cls, clock_domain, path):
        """Check the mode-specific part of the communication section."""
        if clock_domain is not None and not cls.CLOCKED:
            raise DescriptionError(
                f"communication mode {cls.MODE!r} is clockless and takes no "
                "clock key", path)

    @classmethod
    def check(cls, context):
        """Check what only the assembled context can tell: the host clock's
        rate above all, which comes from the instrument exporting the clock
        rather than from the communication section. Raise
        :class:`DescriptionError` on a rack the mode cannot serve."""

    @classmethod
    def ports(cls):
        """Boundary ports, bound to the adapter formal to formal."""
        raise NotImplementedError

    @classmethod
    def generics(cls, context):
        """Boundary generics, bound to the adapter formal to formal."""
        return ()

    @classmethod
    def generic_map(cls, context):
        """Adapter generics that are not boundary generics."""
        return {cls.APB_CONFIG: context.apb_config,
                cls.DESCRIPTOR_BASE: context.descriptor_base}

    @classmethod
    def deps(cls):
        raise NotImplementedError


class CommunicationRegistry:
    """Mode name -> transport plugin."""

    PLUGINS = {}

    @classmethod
    def register(cls, plugin):
        assert plugin.MODE not in cls.PLUGINS, \
            f"communication mode {plugin.MODE!r} is already registered"
        cls.PLUGINS[plugin.MODE] = plugin
        return plugin

    @classmethod
    def get(cls, mode, path):
        try:
            return cls.PLUGINS[mode]
        except KeyError:
            raise DescriptionError(
                f"unknown communication mode {mode!r} "
                f"(known: {', '.join(cls.modes())})", path) from None

    @classmethod
    def modes(cls):
        return tuple(cls.PLUGINS)
