"""The signal-type seam: what a probe-type plugin is, and where they are kept.

A plugin owns everything type-specific about one probe: the ports and generics
it adds to the generated entity, the elaboration-time expressions that pack it
into the capture and trigger vectors, name and length, and the gbs
dependencies the generated partition needs.

A selection string is what the description marked: element letters for an
abstract bus type, the empty string for a whole-signal type. ``None`` means
the probe does not take part in that vector.

Only the base class and the registry live here. Probes are the logic
analyzer's vocabulary, so the types that ship with gatecap live with it
(:mod:`acrobe_plugin.gatecap.instrument.la.generator.signal_types`), and a
third-party type registers the same way from its own plugin.
"""

from __future__ import annotations

from .errors import DescriptionError



class SignalTypePlugin:
    """Base class and contribution interface of a signal type."""

    # YAML tag selecting the plugin; None is the untagged bare scalar.
    TAG = None
    # Keys the type accepts on top of the common trace/trigger/enum.
    KEYS = ()
    # Whether an enum table may be attached.
    ENUM = True

    @classmethod
    def parse(cls, payload, path):
        """Validate the type-specific keys and return them as params."""
        return {}

    @classmethod
    def parse_trace(cls, value, path):
        """``(traced, selection)`` for a ``trace`` value (None when absent)."""
        if value is None:
            return True, ""
        if value is False:
            return False, None
        if value is True:
            raise DescriptionError(
                "trace: true is redundant, signals are traced by default; "
                "use trace: false to exclude a probe from the capture vector",
                path)
        raise DescriptionError(
            f"trace for {cls.label()} must be false or absent, got {value!r}",
            path)

    @classmethod
    def parse_trigger(cls, value, path):
        """Selection for a ``trigger`` value, or None when not a trigger."""
        if value is None or value is False:
            return None
        if value is True:
            return ""
        raise DescriptionError(
            f"trigger for {cls.label()} must be true or false, got {value!r}",
            path)

    @classmethod
    def label(cls):
        return cls.TAG or "a bare scalar"

    @classmethod
    def ports(cls, probe):
        raise NotImplementedError

    @classmethod
    def generics(cls, probe):
        return ()

    @classmethod
    def deps(cls, probe):
        return ()

    @classmethod
    def length(cls, probe, selection):
        raise NotImplementedError

    @classmethod
    def pack(cls, probe, selection):
        raise NotImplementedError

    @classmethod
    def names(cls, probe, selection):
        raise NotImplementedError

    @classmethod
    def static_width(cls, probe, selection):
        """Bit count when it is known from the description, else None."""
        return None

    @classmethod
    def trace_length(cls, probe):
        return cls.length(probe, probe.trace_selection)

    @classmethod
    def trace_pack(cls, probe):
        return cls.pack(probe, probe.trace_selection)

    @classmethod
    def trace_names(cls, probe):
        return cls.names(probe, probe.trace_selection)

    @classmethod
    def trace_width(cls, probe):
        return cls.static_width(probe, probe.trace_selection)

    @classmethod
    def trigger_length(cls, probe):
        return cls.length(probe, probe.trigger_selection)

    @classmethod
    def trigger_pack(cls, probe):
        return cls.pack(probe, probe.trigger_selection)

    @classmethod
    def trigger_names(cls, probe):
        return cls.names(probe, probe.trigger_selection)

    @classmethod
    def trigger_width(cls, probe):
        return cls.static_width(probe, probe.trigger_selection)

    @staticmethod
    def enum_suffix(probe):
        return "" if probe.enum is None else probe.enum.suffix()

    @classmethod
    def check_enum(cls, probe, path):
        """Reject an enum wider than the field it labels."""
        if probe.enum is None:
            return
        width = cls.static_width(probe, "")
        if width is not None and probe.enum.widest() >> width:
            raise DescriptionError(
                f"enum maps value {probe.enum.widest()} beyond the "
                f"{width}-bit signal", path)


class SignalTypeRegistry:
    """YAML tag -> signal-type plugin."""

    PLUGINS = {}

    @classmethod
    def register(cls, plugin):
        assert plugin.TAG not in cls.PLUGINS, \
            f"signal type {plugin.TAG!r} is already registered"
        cls.PLUGINS[plugin.TAG] = plugin
        return plugin

    @classmethod
    def get(cls, tag, path):
        try:
            return cls.PLUGINS[tag]
        except KeyError:
            raise DescriptionError(
                f"unknown signal type {tag!r} (known: {', '.join(cls.tags())})",
                path) from None

    @classmethod
    def tags(cls):
        return tuple(tag or "untagged" for tag in cls.PLUGINS)

