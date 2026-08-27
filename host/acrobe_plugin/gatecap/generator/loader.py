"""YAML front end: load a description, keeping signal-entry tags.

A signal entry is typed by its YAML tag (``!bus``, ``!axi4-stream``, ...); the
untagged mapping is the bare scalar. The loader keeps the tag and the source
position of every tagged node in a :class:`Tagged` wrapper and defers all
judgement to the parser, so a wrong payload is reported as a description error
with a line number instead of a YAML traceback.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from .errors import DescriptionError


@dataclass(frozen=True)
class Tagged:
    """A tagged YAML node: its tag, its payload and where it came from.

    ``payload`` is the constructed mapping, or None when the node was not a
    mapping at all (which the parser rejects with the tag in the message)."""

    tag: str
    payload: dict | None
    line: int | None

    def mapping(self, path):
        if self.payload is None:
            raise DescriptionError(
                f"{self.tag} takes a mapping of keys", path, self.line)
        return self.payload


class DescriptionLoader(yaml.SafeLoader):
    """SafeLoader that wraps every tagged node instead of failing on it."""

    @staticmethod
    def construct_tagged(loader, node):
        line = node.start_mark.line + 1
        if isinstance(node, yaml.MappingNode):
            return Tagged(node.tag, loader.construct_mapping(node, deep=True),
                          line)
        return Tagged(node.tag, None, line)


DescriptionLoader.add_constructor(None, DescriptionLoader.construct_tagged)


class YamlSource:
    """Reads a description file (or string) into plain Python values."""

    @staticmethod
    def load(text, origin=None):
        try:
            document = yaml.load(text, Loader=DescriptionLoader)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            raise DescriptionError(
                f"YAML syntax error: {getattr(e, 'problem', e)}",
                origin, None if mark is None else mark.line + 1) from None
        if document is None:
            raise DescriptionError("description is empty", origin)
        if not isinstance(document, dict):
            raise DescriptionError("description must be a mapping", origin)
        return document

    @classmethod
    def load_file(cls, path):
        try:
            with open(path, "r") as f:
                text = f.read()
        except OSError as e:
            raise DescriptionError(f"cannot read {path}: {e.strerror}") from None
        return cls.load(text, origin=str(path))

    @staticmethod
    def line_of(value):
        return value.line if isinstance(value, Tagged) else None
