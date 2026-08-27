"""Parsing and validation of a ``!logic-analyzer`` instrument body.

Everything decidable from the description alone is checked here, before any
VHDL exists: identifier legality, dangling trigger references, the trigger
topology, storage sanity, and the trigger-vector cap where the widths are
static. Geometry that depends on a stream configuration generic is not
knowable in Python and becomes an elaboration-time assertion in the emitted
entity instead.

Every path reported is rooted at the instrument the body belongs to, so a
rack holding two analyzers points at the offending one.
"""

from __future__ import annotations

from acrobe_plugin.gatecap.generator import (DescriptionError, Field,
                                             SignalTypeRegistry, Tagged)
from .schema import (CaptureParams, Defaults, Domain, EnumSpec, Probe,
                     StorageParams, TriggerParams)


class BodyParser:
    """The ``!logic-analyzer`` mapping -> dimensioning defaults and domains."""

    KEYS = ("storage", "capture", "trigger", "domains")
    DOMAIN_KEYS = ("clock", "frequency", "storage", "capture", "trigger",
                   "signals")
    STORAGE_KEYS = ("buffer_depth_l2", "rle", "packed")
    CAPTURE_KEYS = ("max_windows",)
    TRIGGER_KEYS = ("capabilities",)
    DOMAIN_TRIGGER_KEYS = ("capabilities", "from")
    SIGNAL_KEYS = ("trace", "trigger", "enum")
    TRIGGER_WIDTH_MAX = 32
    BUFFER_DEPTH_L2_MAX = 30

    @classmethod
    def parse(cls, payload, path):
        storage = StorageParams(**dict(
            StorageParams.DEFAULTS,
            **cls.__storage(Field.mapping(payload, "storage", path),
                            f"{path}.storage")))
        capture = CaptureParams(**dict(
            CaptureParams.DEFAULTS,
            **cls.__capture(Field.mapping(payload, "capture", path),
                            f"{path}.capture")))
        trigger = TriggerParams(**dict(
            TriggerParams.DEFAULTS,
            **cls.__trigger(Field.mapping(payload, "trigger", path),
                            f"{path}.trigger", cls.TRIGGER_KEYS)[0]))

        section = Field.mapping(payload, "domains", path, required=True)
        if not section:
            raise DescriptionError(
                "a logic analyzer needs at least one capture domain",
                f"{path}.domains")
        domains = tuple(
            cls.__domain(name, body, path, storage, capture, trigger)
            for name, body in section.items())
        cls.__check_topology(domains, path)
        return {"storage": storage, "capture": capture, "trigger": trigger,
                "domains": domains}

    @classmethod
    def __domain(cls, name, body, root, storage, capture, trigger):
        path = f"{root}.domains.{name}"
        Field.identifier(name, f"{root}.domains", "domain name")
        if isinstance(body, Tagged):
            raise DescriptionError(
                f"domain must be an untagged mapping, got {body.tag}", path,
                body.line)
        if not isinstance(body, dict):
            raise DescriptionError("domain must be a mapping", path)
        Field.known_keys(body, cls.DOMAIN_KEYS, path)

        clock = Field.string(body, "clock", path, default="clock")
        Field.identifier(clock, f"{path}.clock", "clock name")
        frequency = Field.integer(body, "frequency", path, default=0, minimum=0)

        domain_storage = Defaults.merged(storage, cls.__storage(
            Field.mapping(body, "storage", path), f"{path}.storage"))
        domain_capture = Defaults.merged(capture, cls.__capture(
            Field.mapping(body, "capture", path), f"{path}.capture"))
        given, source = cls.__trigger(Field.mapping(body, "trigger", path),
                                      f"{path}.trigger",
                                      cls.DOMAIN_TRIGGER_KEYS)
        domain_trigger = Defaults.merged(trigger, given)

        cls.__check_storage(domain_storage, domain_capture, path)

        signals = Field.mapping(body, "signals", path, required=True)
        if not signals:
            raise DescriptionError("at least one signal is required",
                                   f"{path}.signals")
        probes = tuple(cls.__probe(name, signal, entry, path)
                       for signal, entry in signals.items())

        return Domain(name=name, clock=clock, frequency=frequency,
                      storage=domain_storage, capture=domain_capture,
                      trigger=domain_trigger, trigger_from=source,
                      probes=probes)

    @classmethod
    def __storage(cls, section, path):
        Field.known_keys(section, cls.STORAGE_KEYS, path)
        given = {}
        depth = Field.integer(section, "buffer_depth_l2", path, minimum=1,
                              maximum=cls.BUFFER_DEPTH_L2_MAX)
        if depth is not None:
            given["buffer_depth_l2"] = depth
        for key in ("rle", "packed"):
            value = Field.boolean(section, key, path)
            if value is not None:
                given[key] = value
        return given

    @classmethod
    def __capture(cls, section, path):
        Field.known_keys(section, cls.CAPTURE_KEYS, path)
        windows = Field.integer(section, "max_windows", path, minimum=1)
        return {} if windows is None else {"max_windows": windows}

    @classmethod
    def __trigger(cls, section, path, allowed):
        Field.known_keys(section, allowed, path)
        given = {}
        capabilities = Field.string(section, "capabilities", path)
        if capabilities is not None:
            if capabilities not in TriggerParams.CAPABILITIES:
                raise DescriptionError(
                    f"capabilities must be one of "
                    f"{', '.join(TriggerParams.CAPABILITIES)}, got "
                    f"{capabilities!r}", f"{path}.capabilities")
            given["capabilities"] = capabilities
        source = Field.string(section, "from", path)
        return given, source

    @classmethod
    def __check_storage(cls, storage, capture, path):
        if storage.rle and storage.packed:
            raise DescriptionError(
                "rle and packed storage are mutually exclusive",
                f"{path}.storage")
        if storage.rle and capture.max_windows != 1:
            raise DescriptionError(
                "rle storage has a single window, max_windows must be 1",
                f"{path}.capture")

    @classmethod
    def __probe(cls, domain, name, entry, root):
        path = f"{root}.signals.{name}"
        Field.identifier(name, f"{root}.signals", "signal name")
        tag, payload, line = cls.__entry(entry, path)
        plugin = SignalTypeRegistry.get(tag, path)
        Field.known_keys(payload, cls.SIGNAL_KEYS + plugin.KEYS, path)

        enum = None
        if "enum" in payload:
            if not plugin.ENUM:
                raise DescriptionError(
                    f"{plugin.label()} names its own fields, an enum cannot "
                    "attach to it", path, line)
            enum = EnumSpec.parse(payload["enum"], f"{path}.enum")

        params = plugin.parse(payload, path)
        traced, trace_selection = plugin.parse_trace(payload.get("trace"),
                                                     f"{path}.trace")
        trigger_selection = plugin.parse_trigger(payload.get("trigger"),
                                                 f"{path}.trigger")
        if not traced and trigger_selection is None:
            raise DescriptionError(
                "signal is neither traced nor a trigger source", path)

        probe = Probe(domain=domain, name=name, tag=tag, plugin=plugin,
                      traced=traced, trace_selection=trace_selection,
                      trigger_selection=trigger_selection, enum=enum,
                      params=params)
        plugin.check_enum(probe, f"{path}.enum")
        return probe

    @staticmethod
    def __entry(entry, path):
        """``(tag, payload, line)`` for a signal entry: an untagged mapping (or
        nothing at all) is the bare scalar."""
        if entry is None:
            return None, {}, None
        if isinstance(entry, Tagged):
            return entry.tag, entry.mapping(path), entry.line
        if isinstance(entry, dict):
            return None, entry, None
        raise DescriptionError(
            f"signal must be a tagged or empty mapping, got {entry!r}", path)

    @classmethod
    def __check_topology(cls, domains, root):
        names = [domain.name for domain in domains]
        by_name = {domain.name: domain for domain in domains}
        for domain in domains:
            path = f"{root}.domains.{domain.name}.trigger"
            if domain.subscribes():
                if domain.hosts_trigger():
                    raise DescriptionError(
                        "a domain either hosts a trigger (signals marked "
                        "trigger) or subscribes to another domain's, not both",
                        path)
                if domain.trigger_from == domain.name:
                    raise DescriptionError(
                        "a domain cannot subscribe to its own trigger", path)
                if domain.trigger_from not in by_name:
                    raise DescriptionError(
                        f"from {domain.trigger_from!r} is not a domain "
                        f"(known: {', '.join(names)})", path)
                if not by_name[domain.trigger_from].hosts_trigger():
                    raise DescriptionError(
                        f"domain {domain.trigger_from!r} hosts no trigger "
                        "(none of its signals is marked trigger)", path)
            elif domain.captures() and not domain.hosts_trigger():
                raise DescriptionError(
                    "a capturing domain needs a trigger: mark signals with "
                    "trigger, or subscribe with trigger: {from: <domain>}",
                    path)
            cls.__check_trigger_width(domain, root)

    @classmethod
    def __check_trigger_width(cls, domain, root):
        static = 0
        for probe in domain.trigger_probes():
            width = probe.plugin.trigger_width(probe)
            if width is not None:
                static += width
        if static > cls.TRIGGER_WIDTH_MAX:
            raise DescriptionError(
                f"trigger vector is {static} bits, at most "
                f"{cls.TRIGGER_WIDTH_MAX} are supported",
                f"{root}.domains.{domain.name}")
