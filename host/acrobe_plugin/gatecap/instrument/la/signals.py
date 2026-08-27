"""VCD layout for a flat gatecap probe-name list.

The descriptor names the probes of a capture with one name spec, which
``names.SignalNames`` expands to one name per probe bit. ``VcdLayout`` turns
those flat names back into VCD variables: a dotted name nests into scopes, and
array elements ``name[i]`` regroup into one sized bus, optionally carrying an
enum for value translation.
"""

from __future__ import annotations

import logging


class VcdLayout:
    """VCD variables for a flat probe-name list. Dotted names nest into
    scopes; with ``buses`` (the default) array members ``name[i]`` join into
    one sized bus. Records, per variable, which probe bit feeds each of its
    positions so a sample word can be split across the variables. ``enums``
    (as returned by ``SignalNames.parse``) attaches a value->label table to
    the matching bus.

    Set ``buses=False`` to keep every array element as its own single-bit
    var: some VCD consumers (this project's sigrok build) drop vector vars
    and mis-parse the file when any is present."""

    class Var:
        def __init__(self, scope, name, enum=None):
            self.scope = scope        # tuple of scope components
            self.name = name
            self.size = 0
            self.positions = {}       # bit position within var -> probe bit
            self.handle = None
            self.enum = enum          # {value: label} or None

        def value(self, sample):
            v = 0
            for position, bit in self.positions.items():
                v |= ((sample >> bit) & 1) << position
            return v

        def label(self, value):
            # Symbolic name for a decoded value, or None if unmapped/no enum.
            return None if self.enum is None else self.enum.get(value)

    def __init__(self, names, buses=True, enums=None):
        enums = enums or {}
        self.vars = []
        by_key = {}
        for bit, name in enumerate(names):
            scope, leaf, position = self.__parse(name, buses)
            key = (scope, leaf)
            var = by_key.get(key)
            if var is None:
                full = ".".join(scope + (leaf,))
                var = self.Var(scope, leaf, enum=enums.get(full))
                by_key[key] = var
                self.vars.append(var)
            position = 0 if position is None else position
            var.positions[position] = bit
            var.size = max(var.size, position + 1)
        self.__check_enum_widths()

    def __check_enum_widths(self):
        for var in self.vars:
            if not var.enum:
                continue
            over = [v for v in var.enum if v >> var.size]
            if over:
                logging.getLogger(__name__).warning(
                    "enum for %s maps values %s beyond its %d-bit width",
                    ".".join(var.scope + (var.name,)), sorted(over), var.size)

    @staticmethod
    def __parse(name, buses):
        # buses: "command.data[7]" -> (("command",), "data", 7); the array
        # regroups into one bus. Otherwise the [7] stays in the leaf and each
        # element is its own scalar var. "sck" -> ((), "sck", None) either way.
        position = None
        base = name
        if buses and base.endswith("]") and "[" in base:
            head, index = base[:-1].rsplit("[", 1)
            if index.isdigit():
                base, position = head, int(index)
        *scopes, leaf = base.split(".")
        return tuple(scopes), leaf, position

    def register(self, writer, root="capture"):
        # ``root`` is the scope the variables hang under: one component, or a
        # tuple of them when several layouts share a file and each needs its
        # own path below the common root.
        root = root if isinstance(root, tuple) else (root,)
        for var in self.vars:
            if var.enum:
                # A string var carries the label; the viewer shows it verbatim.
                var.handle = writer.register_var(
                    root + var.scope, var.name, "string")
            else:
                var.handle = writer.register_var(
                    root + var.scope, var.name, "wire", size=var.size)

    def emit(self, writer, timestamp, sample):
        for var in self.vars:
            value = var.value(sample)
            if var.enum:
                writer.change(var.handle, timestamp,
                              var.label(value) or f"0x{value:x}")
            else:
                writer.change(var.handle, timestamp, value)
