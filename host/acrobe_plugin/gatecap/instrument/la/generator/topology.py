"""Trigger topology of a generated core.

A domain whose probes carry trigger markings hosts a trigger block watching
them. Other domains may subscribe to it instead of hosting one of their own,
which correlates their captures: the hosting block's enable is the AND of
every subscribing core's ready, so no trigger fires until every capture has
its pre-trigger context, and its tick fans out to every subscriber.

Crossing a domain boundary costs cycles in both directions: a subscriber's
ready is resynchronised into the hosting domain, and the tick is retimed into
the subscriber's domain. The retiming depth is what each control reports as
its integration latency, so the host can place the trigger marker on the right
sample of every buffer.
"""

from __future__ import annotations

from acrobe_plugin.gatecap.generator import (Assignment, Cdc, Check,
                                             Constant, Expr, Instance,
                                             SignalDecl)
from .clusters import ProbeVector


class TriggerSource:
    """The trigger one capturing domain waits on."""

    LATENCY = "gatecap.control.trigger_control_latency_c"
    EDGE_LATENCY = "gatecap.control.trigger_control_edge_latency_c"
    TICK_LATENCY = "interdomain_tick_latency_c"

    def __init__(self, key, tick, edge, crossed):
        self.key = key
        self.crossed = crossed
        self.__tick = tick
        self.__edge = edge

    def tick(self):
        return self.__tick

    def latency_constant(self):
        return self.EDGE_LATENCY if self.__edge else self.LATENCY

    def integration_constant(self):
        return self.TICK_LATENCY if self.crossed else "0"


class TriggerHost:
    """The trigger block of one hosting domain and its fan-out.

    Its registers sit on the host clock like every other register file; the
    match runs on the hosting domain's capture clock, which is where its
    signal vector and its enable live."""

    UNIT = "gatecap.control.trigger_control"
    EDGE_UNIT = "gatecap.control.trigger_control_edge"
    DEP = "gatecap.control"
    TRIGGER_WIDTH_MAX = 32

    def __init__(self, domain, host, capture, block):
        self.domain = domain
        self.host = host
        self.capture = capture
        self.block = block
        self.vector = ProbeVector(domain.trigger_probes(), ProbeVector.TRIGGER)
        self.edge = domain.trigger.edge()
        self.subscribers = []
        self.fanout = []
        self.enables = []
        self.constants = []
        self.declarations = []
        self.statements = []

    def name(self, suffix):
        return f"{self.domain.name}_{suffix}"

    def signal(self, suffix):
        return self.name(f"{suffix}_s")

    def constant(self, suffix):
        return self.name(f"{suffix}_c")

    def key(self):
        return self.block.key

    def subscribe(self, domain, capture):
        """Wire domain's core to this trigger and return what it sees of it."""
        own = domain.name == self.domain.name
        if not own:
            self.subscribers.append(domain.name)
        if own:
            tick = self.signal("trigger")
        else:
            crossing = Cdc(self.capture, capture)
            tick = crossing.tick(f"{domain.name}_trigger_s",
                                 self.signal("trigger"))
            self.fanout.append(crossing)
        # Every subscriber's ready gates the trigger, each brought into the
        # hosting domain where the match runs.
        ready = f"{domain.name}_ready_s"
        if own:
            self.enables.append(ready)
        else:
            crossing = Cdc(capture, self.capture)
            self.enables.append(
                crossing.flag(f"{domain.name}_ready_in_{self.domain.name}_s",
                              ready))
            self.fanout.append(crossing)
        return TriggerSource(self.key(), tick, self.edge, not own)

    def enable(self):
        """The trigger's enable: every subscribing core ready at once."""
        if not self.enables:
            # A trigger nobody waits on still needs a defined enable; hold it
            # armed so its tick is observable.
            return "'1'"
        if len(self.enables) == 1:
            return self.enables[0]
        self.declarations.append(SignalDecl(self.signal("trigger_enable"),
                                            "std_ulogic"))
        self.statements.append(Assignment(self.signal("trigger_enable"),
                                          " and ".join(self.enables)))
        return self.signal("trigger_enable")

    def deps(self):
        deps = [self.DEP]
        for crossing in self.fanout:
            deps += list(crossing.deps())
        return tuple(deps)

    def crossed(self):
        return any(crossing.statements for crossing in self.fanout)

    def checks(self):
        return [Check(
            self.name("trigger_width_check"),
            f"domain {self.domain.name}: a trigger vector is at most "
            f"{self.TRIGGER_WIDTH_MAX} bits",
            f"{self.constant('trigger_signal_count')} <= "
            f"{self.TRIGGER_WIDTH_MAX}")]

    def contribute(self):
        """Declarations and statements, once every subscriber is known."""
        enable = self.enable()
        self.constants[:0] = [
            Constant(self.constant("trigger_signal_count"), "natural",
                     self.vector.count(),
                     comment=f"Trigger vector of domain {self.domain.name}, "
                             "independent of what the domain captures."),
            Constant(self.constant("trigger_signal_names"), "string",
                     self.vector.names()),
            ]
        self.declarations[:0] = [
            SignalDecl(self.signal("trigger_signals"),
                       "std_ulogic_vector("
                       f"{self.constant('trigger_signal_count')}-1 downto 0)"),
            SignalDecl(self.signal("trigger"), "std_ulogic"),
            ]
        for crossing in self.fanout:
            self.declarations += crossing.declarations
        heading = (f"Trigger of domain {self.domain.name}, watching its own "
                   "signal vector: a capture may trigger on signals it does "
                   "not store.")
        if self.subscribers:
            heading += (" It fires " + ", ".join(self.subscribers)
                        + " as well, so their buffers are cut on the same "
                        "event; it stays disabled until every one of them is "
                        "ready.")
        self.statements[:0] = [
            Assignment(self.signal("trigger_signals"), self.vector.pack(),
                       comment=heading),
            Instance(
                self.name("trigger"),
                self.EDGE_UNIT if self.edge else self.UNIT,
                generic_map={
                    "apb_config_c": "apb_config_c",
                    "signal_count_c": self.constant("trigger_signal_count"),
                    "async_c": Expr.boolean(
                        self.capture.clock != self.host.clock),
                    },
                port_map={
                    "clock_i": self.host.clock,
                    "reset_n_i": self.host.reset_n,
                    "apb_i": self.block.master(),
                    "apb_o": self.block.slave(),
                    "capture_clock_i": self.capture.clock,
                    "capture_reset_n_i": self.capture.reset_n,
                    "signals_i": self.signal("trigger_signals"),
                    "enable_i": enable,
                    "trigger_o": self.signal("trigger"),
                    }),
            ]
        for crossing in self.fanout:
            self.statements += crossing.statements

    def descriptor_object(self):
        return Expr.call("trigger_desc", self.constant("trigger_signal_count"),
                         self.constant("trigger_signal_names"),
                         Expr.boolean(self.edge))


class TriggerTopology:
    """Every trigger block of a core and who waits on which."""

    LATENCY_COMMENT = (
        "Destination-clock cycles from a tick entering "
        "nsl_clocking.interdomain.interdomain_tick to it leaving: two\n"
        "resynchroniser stages, then the register that turns the "
        "resynchronised toggle back into a pulse.")

    def __init__(self, domains, clocks, layout):
        self.hosts = {}
        self.sources = {}
        for domain in domains:
            if domain.hosts_trigger():
                self.hosts[domain.name] = TriggerHost(
                    domain, clocks.host, clocks.of(domain.name),
                    layout.of(domain.name).trigger)
        for domain in domains:
            if not domain.captures():
                continue
            host = self.hosts[domain.trigger_from or domain.name]
            self.sources[domain.name] = host.subscribe(
                domain, clocks.of(domain.name))
        for host in self.hosts.values():
            host.contribute()

    def source(self, name):
        return self.sources[name]

    def crossed(self):
        return any(host.crossed() for host in self.hosts.values())

    def constants(self):
        constants = []
        if self.crossed():
            constants.append(Constant(TriggerSource.TICK_LATENCY, "natural",
                                      str(Cdc.TICK_LATENCY),
                                      comment=self.LATENCY_COMMENT))
        for host in self.hosts.values():
            constants += host.constants
        return constants

    def declarations(self):
        declarations = []
        for host in self.hosts.values():
            declarations += host.declarations
        return declarations

    def statements(self):
        statements = []
        for host in self.hosts.values():
            statements += host.statements
        return statements

    def checks(self):
        checks = []
        for host in self.hosts.values():
            checks += host.checks()
        return checks

    def deps(self):
        deps = []
        for host in self.hosts.values():
            deps += list(host.deps())
        return tuple(deps)
