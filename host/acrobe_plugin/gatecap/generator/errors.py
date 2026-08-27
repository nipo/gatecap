"""Errors raised while reading a capture-core description."""

from __future__ import annotations


class DescriptionError(Exception):
    """A description is malformed or inconsistent.

    Carries the dotted path of the offending node and, when the YAML parser
    knew it, the source line. Every check decidable from the description alone
    raises this before any VHDL is produced.
    """

    def __init__(self, message, path=None, line=None):
        self.message = message
        self.path = path
        self.line = line
        super().__init__(self.format())

    def format(self):
        where = []
        if self.path:
            where.append(self.path)
        if self.line is not None:
            where.append(f"line {self.line}")
        if where:
            return f"{', '.join(where)}: {self.message}"
        return self.message
