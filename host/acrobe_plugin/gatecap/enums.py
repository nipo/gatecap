"""Enumerated value decoding for gatecap probe fields.

A bus (or scalar) in the descriptor's grouping spec may carry an enum table in
angle brackets, ``resp[1:0]<...>``, mapping the field's integer value to a
symbolic label. The table is a comma list of entries, applied left to right to
a ``value -> label`` map:

  * ``label``            -- assign the running index, then advance it
  * ``N:label`` / ``0xNN:label`` -- assign N, set the running index to N+1
  * ``+ns.name``         -- splice a well-known table from ``EnumRegistry``,
                            then set the running index past the highest value
                            it assigned
  * (empty)              -- leave the running index's value unmapped, advance

Later entries win, so ``+axi.resp,2:MY_SLVERR`` reuses the whole base table but
overrides value 2, and ``+base,0x2a:custom0,custom1`` extends a base above it.
Unmapped values render as their number.
"""

from __future__ import annotations


class EnumRegistry:
    """Well-known value->label tables referenced by ``+ns.name`` in an enum."""

    TABLES = {
        "axi.resp": {0: "OKAY", 1: "EXOKAY", 2: "SLVERR", 3: "DECERR"},
        "axi.burst": {0: "FIXED", 1: "INCR", 2: "WRAP"},
        "axi.size": {0: "1B", 1: "2B", 2: "4B", 3: "8B",
                     4: "16B", 5: "32B", 6: "64B", 7: "128B"},
        "axi.lock": {0: "NORMAL", 1: "EXCLUSIVE"},
        # Base used by the socket_enum synthetic example to exercise splice +
        # extend + override + undefined values.
        "demo.phase": {0: "IDLE", 1: "START", 2: "RUN"},
    }

    @classmethod
    def get(cls, name):
        try:
            return dict(cls.TABLES[name])
        except KeyError:
            raise KeyError(f"unknown enum registry entry {name!r}") from None


class EnumTable:
    @classmethod
    def parse(cls, body):
        """Resolve a ``<...>`` body to a ``{value: label}`` dict."""
        table = {}
        index = 0
        for entry in body.split(","):
            entry = entry.strip()
            if entry == "":
                index += 1
                continue
            if entry.startswith("+"):
                base = EnumRegistry.get(entry[1:])
                table.update(base)
                if base:
                    index = max(base) + 1
                continue
            key, sep, label = entry.partition(":")
            if sep and cls.__is_int(key):
                value = int(key, 0)
                table[value] = label.strip()
                index = value + 1
            else:
                table[index] = entry
                index += 1
        return table

    @staticmethod
    def __is_int(text):
        try:
            int(text.strip(), 0)
            return True
        except ValueError:
            return False
