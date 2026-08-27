"""Enumeration tables attached to a described field.

A field of a name-spec may carry a ``value: label`` table, which the descriptor
publishes as the ``<...>`` suffix of the item that names it (see
``architecture.md``, "Signal names"). The grammar is the framework's, not one
instrument's: a logic-analyzer probe and a control/status register bind a table
the same way, and the host decodes both with one parser.

A table may splice a well-known one under the reserved ``base`` key, which
renders as the ``+ns.name`` entry the reader resolves against
:class:`acrobe_plugin.gatecap.enums.EnumRegistry`. The name is checked here,
where a typo can still be pointed at a line.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..enums import EnumRegistry
from .errors import DescriptionError


@dataclass(frozen=True)
class EnumSpec:
    """A value -> label table, rendered as the ``<...>`` name-spec suffix."""

    labels: tuple
    base: str | None = None

    ILLEGAL = set(",<>{}[]:+")
    BASE_KEY = "base"

    @classmethod
    def parse(cls, payload, path):
        if not isinstance(payload, dict) or not payload:
            raise DescriptionError(
                "enum must be a non-empty value: label mapping", path)
        base = None
        labels = []
        for value, label in payload.items():
            if value == cls.BASE_KEY:
                base = cls.__base(label, path)
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise DescriptionError(
                    f"enum key {value!r} must be a non-negative integer or "
                    f"{cls.BASE_KEY!r}", path)
            if not isinstance(label, str) or not label:
                raise DescriptionError(
                    f"enum label for {value} must be a non-empty string", path)
            bad = sorted(cls.ILLEGAL.intersection(label))
            if bad:
                raise DescriptionError(
                    f"enum label {label!r} must not contain "
                    f"{''.join(bad)!r}", path)
            labels.append((value, label))
        labels.sort()
        return cls(labels=tuple(labels), base=base)

    @classmethod
    def __base(cls, name, path):
        if not isinstance(name, str) or not name:
            raise DescriptionError(
                f"enum {cls.BASE_KEY} must name a well-known table", path)
        try:
            EnumRegistry.get(name)
        except KeyError:
            raise DescriptionError(
                f"unknown enum base {name!r} (known: "
                f"{', '.join(sorted(EnumRegistry.TABLES))})", path) from None
        return name

    def spec(self):
        """The enum body: consecutive labels ride the running index, others
        state their value (see the host's enum grammar)."""
        parts = []
        index = 0
        if self.base is not None:
            parts.append(f"+{self.base}")
        for value, label in self.labels:
            # Past a spliced base the running index is the base table's, which
            # only the reader resolves, so every value is stated.
            if value == index and self.base is None:
                parts.append(label)
            else:
                parts.append(f"{value}:{label}")
            index = value + 1
        return ",".join(parts)

    def suffix(self):
        return f"<{self.spec()}>"

    def widest(self):
        values = [value for value, _ in self.labels]
        if self.base is not None:
            values += list(EnumRegistry.get(self.base))
        return max(values)
