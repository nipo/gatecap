"""Typed access to the keys of a YAML mapping, with precise errors.

Every description-level check that can be phrased as "this key must be a
mapping / a string / an integer in range" goes through here, so a malformed
description is reported with its dotted path instead of a Python traceback.
Instrument plugins validate their own body with the same helpers.
"""

from __future__ import annotations

from .errors import DescriptionError
from .loader import Tagged
from .vhdl import Identifier


class Field:
    """Typed access to one key of a YAML mapping, with precise errors."""

    @staticmethod
    def mapping(container, key, path, required=False):
        value = container.get(key)
        if value is None:
            if required:
                raise DescriptionError(f"{key} is required", path)
            return {}
        if isinstance(value, Tagged):
            raise DescriptionError(
                f"{key} must be an untagged mapping, got {value.tag}",
                f"{path}.{key}", value.line)
        if not isinstance(value, dict):
            raise DescriptionError(
                f"{key} must be a mapping, got {type(value).__name__}",
                f"{path}.{key}")
        return value

    @staticmethod
    def string(container, key, path, default=None, required=False):
        value = container.get(key)
        if value is None:
            if required:
                raise DescriptionError(f"{key} is required", path)
            return default
        if not isinstance(value, str):
            raise DescriptionError(
                f"{key} must be a string, got {value!r}", f"{path}.{key}")
        return value

    @staticmethod
    def integer(container, key, path, default=None, minimum=None, maximum=None):
        value = container.get(key)
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool):
            raise DescriptionError(
                f"{key} must be an integer, got {value!r}", f"{path}.{key}")
        if minimum is not None and value < minimum:
            raise DescriptionError(
                f"{key} must be at least {minimum}, got {value}",
                f"{path}.{key}")
        if maximum is not None and value > maximum:
            raise DescriptionError(
                f"{key} must be at most {maximum}, got {value}",
                f"{path}.{key}")
        return value

    @staticmethod
    def boolean(container, key, path, default=None):
        value = container.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise DescriptionError(
                f"{key} must be a boolean, got {value!r}", f"{path}.{key}")
        return value

    @staticmethod
    def known_keys(container, allowed, path):
        for key in container:
            if key not in allowed:
                raise DescriptionError(
                    f"unknown key {key!r} (known: {', '.join(sorted(allowed))})",
                    path)

    @staticmethod
    def identifier(name, path, what):
        reason = Identifier.rejection(name)
        if reason:
            raise DescriptionError(f"{what} {name!r} {reason}", path)
        return name
