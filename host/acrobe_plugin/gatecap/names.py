"""The name spec every gatecap descriptor names its signals with.

A name spec is one text -- a comma list of items, where an item may use these
shorthands, freely nested:

  * a brace group ``prefix.{a,b,c}`` expands to ``prefix.a prefix.b prefix.c``
  * an array range ``bus[7:0]`` expands to ``bus[7] bus[6] ... bus[0]`` (and
    ``bus[0:7]`` ascending); the brackets stay in the expanded names
  * an enum ``bus[1:0]<...>`` (or a scalar ``flag<lo,hi>``) attaches a
    value->label table to that field (see ``enums.EnumTable``)

``SignalNames.expand`` flattens the spec to one name per bit, in spec order,
which is the order the bits are numbered in. ``SignalNames.parse`` also
returns the resolved enum tables keyed by field name.

The grammar belongs to the framework, not to one instrument: a logic-analyzer
probe vector and a control/status register list are both written in it, and
both are read back with this parser.
"""

from __future__ import annotations

from .enums import EnumTable


class SignalNames:
    @classmethod
    def expand(cls, spec):
        """Flatten a grouping spec to the list of probe names it names."""
        return [name for name, _ in cls.__pairs(spec)]

    @classmethod
    def parse(cls, spec):
        """Return ``(names, enums)``: the flat probe names and a
        ``{bus_name: {value: label}}`` map for fields carrying a ``<...>``."""
        names = []
        raw = {}
        for name, enum_raw in cls.__pairs(spec):
            names.append(name)
            if enum_raw is not None:
                raw.setdefault(cls.__bus_name(name), enum_raw)
        enums = {bus: EnumTable.parse(body) for bus, body in raw.items()}
        return names, enums

    @classmethod
    def __pairs(cls, spec):
        # (name, enum_body|None) for every probe bit, in spec order.
        if not spec:
            return []
        out = []
        for item in cls.__split(spec):
            out += cls.__expand_item(item)
        return out

    @staticmethod
    def __split(text):
        # Split on commas at depth 0; commas inside a group, range or enum
        # belong to it.
        parts = []
        depth = 0
        start = 0
        for i, c in enumerate(text):
            if c in "{[<":
                depth += 1
            elif c in "}]>":
                depth -= 1
            elif c == "," and depth == 0:
                parts.append(text[start:i])
                start = i + 1
        parts.append(text[start:])
        return parts

    @staticmethod
    def __match(text, open_pos):
        # Index of the closer matching the brace/bracket opener at open_pos.
        depth = 0
        for i in range(open_pos, len(text)):
            if text[i] in "{[":
                depth += 1
            elif text[i] in "}]":
                depth -= 1
                if depth == 0:
                    return i
        raise ValueError(f"unbalanced group in signal spec: {text!r}")

    @staticmethod
    def __strip_enum(item):
        # Split a trailing balanced <...> enum block off an item.
        if not item.endswith(">"):
            return item, None
        depth = 0
        for i in range(len(item) - 1, -1, -1):
            if item[i] == ">":
                depth += 1
            elif item[i] == "<":
                depth -= 1
                if depth == 0:
                    return item[:i], item[i + 1:-1]
        raise ValueError(f"unbalanced <> in signal spec: {item!r}")

    @classmethod
    def __expand_item(cls, item):
        # Expand one item to (name, enum_body|None) pairs. The item's enum, if
        # any, tags every bit it produces.
        base, enum_body = cls.__strip_enum(item)
        if enum_body is not None and "{" in base:
            raise ValueError(f"enum <...> applies to a field, not a group: {item!r}")
        for i, c in enumerate(base):
            if c == "{":
                close = cls.__match(base, i)
                options = []
                for alt in cls.__split(base[i + 1:close]):
                    options += cls.__expand_item(alt)
                return cls.__product(base[:i], options, base[close + 1:])
            if c == "[":
                close = cls.__match(base, i)
                options = [(o, None) for o in cls.__range(base[i + 1:close])]
                pairs = cls.__product(base[:i], options, base[close + 1:])
                if enum_body is not None:
                    pairs = [(name, enum_body) for name, _ in pairs]
                return pairs
        return [(base, enum_body)]

    @classmethod
    def __product(cls, prefix, options, rest):
        tails = cls.__expand_item(rest)
        out = []
        for oname, oenum in options:
            for tname, tenum in tails:
                out.append((prefix + oname + tname,
                            oenum if oenum is not None else tenum))
        return out

    @staticmethod
    def __range(inner):
        # "7:0" -> ["[7]","[6]",...,"[0]"]; a bare index "3" -> ["[3]"].
        if ":" in inner:
            lo, hi = inner.split(":", 1)
            a, b = int(lo), int(hi)
            step = 1 if b >= a else -1
            return [f"[{n}]" for n in range(a, b + step, step)]
        if inner.isdigit():
            return [f"[{inner}]"]
        raise ValueError(f"array range must be numeric, got [{inner}]")

    @staticmethod
    def __bus_name(name):
        # Drop a trailing [index] so every bit of a bus shares one key.
        if name.endswith("]") and "[" in name:
            head, _, idx = name[:-1].rpartition("[")
            if idx.isdigit():
                return head
        return name
