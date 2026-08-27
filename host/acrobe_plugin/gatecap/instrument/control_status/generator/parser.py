"""Parsing and validation of a ``!control-status`` instrument body.

Everything the description alone settles is checked here, before any VHDL
exists: identifier legality, declared widths, how many ticks a word packs, how
many registers each region of the map holds, and whether an enumeration fits
the field it labels. Nothing about a panel depends on an entity generic, so
this is the whole of its validation -- the emitted entity carries only the
footprint check every instrument carries.

Every path reported is rooted at the instrument the body belongs to, so a rack
holding several panels points at the offending one.
"""

from __future__ import annotations

from acrobe_plugin.gatecap.generator import (DescriptionError, EnumSpec, Field,
                                             Tagged)
from .schema import PanelBody, PanelLevel, TickWord


class BodyParser:
    """The ``!control-status`` mapping -> a :class:`PanelBody`."""

    KEYS = ("clock", "tick-counter-width", "control", "status", "tick-out",
            "tick-in")
    LEVEL_KEYS = ("width", "enum")
    LEVEL_KINDS = ("control", "status")
    TICK_KINDS = ("tick-out", "tick-in")

    @classmethod
    def parse(cls, payload, path):
        clock = Field.string(payload, "clock", path)
        if clock is not None:
            cls.__clock(clock, f"{path}.clock")
        counter_width = Field.integer(
            payload, "tick-counter-width", path,
            default=PanelBody.COUNTER_WIDTH_DEFAULT, minimum=1,
            maximum=PanelBody.WIDTH_MAX)

        body = PanelBody(
            clock=clock,
            counter_width=counter_width,
            controls=cls.__levels(payload, "control", path),
            statuses=cls.__levels(payload, "status", path),
            tick_out=cls.__ticks(payload, "tick-out", path),
            tick_in=cls.__ticks(payload, "tick-in", path))

        cls.__check_names(body, path)
        cls.__check_regions(body, path)
        return {"panel": body}

    @staticmethod
    def __clock(clock, path):
        """The panel's own clock, named the way a capture domain names its
        own: one identifier, which becomes a boundary port of the instance and
        the clock the rack may be told to ride as ``<instance>.<clock>``."""
        if "." in clock:
            raise DescriptionError(
                f"clock {clock!r} must be a plain name: a panel declares a "
                "clock of its own, which the instance exports as "
                "<instance>.<clock>", path)
        Field.identifier(clock, path, "clock name")

    @classmethod
    def __levels(cls, payload, key, root):
        section = Field.mapping(payload, key, root)
        return tuple(cls.__level(name, entry, key, f"{root}.{key}")
                     for name, entry in section.items())

    @classmethod
    def __level(cls, name, entry, kind, root):
        path = f"{root}.{name}"
        Field.identifier(name, root, f"{kind} name")
        if isinstance(entry, Tagged):
            raise DescriptionError(
                f"a {kind} is a width or an untagged mapping, got {entry.tag}",
                path, entry.line)
        if isinstance(entry, dict):
            Field.known_keys(entry, cls.LEVEL_KEYS, path)
            width = Field.integer(entry, "width", path, minimum=1,
                                  maximum=PanelBody.WIDTH_MAX)
            if width is None:
                raise DescriptionError(f"{kind} {name!r} needs a width", path)
            enum = None
            if "enum" in entry:
                enum = EnumSpec.parse(entry["enum"], f"{path}.enum")
                if enum.widest() >> width:
                    raise DescriptionError(
                        f"enum maps value {enum.widest()} beyond the "
                        f"{width}-bit {kind}", f"{path}.enum")
            return PanelLevel(name=name, width=width, enum=enum)
        if isinstance(entry, int) and not isinstance(entry, bool):
            if entry < 1 or entry > PanelBody.WIDTH_MAX:
                raise DescriptionError(
                    f"{kind} width must be in 1 to {PanelBody.WIDTH_MAX}, got "
                    f"{entry}", path)
            return PanelLevel(name=name, width=entry, enum=None)
        raise DescriptionError(
            f"a {kind} is a width or a mapping of width and enum, got "
            f"{entry!r}", path)

    @classmethod
    def __ticks(cls, payload, key, root):
        path = f"{root}.{key}"
        section = payload.get(key)
        if section is None:
            return ()
        if not isinstance(section, list):
            raise DescriptionError(
                f"{key} is a list of words, each word a list of the tick "
                f"names that strobe together, got {section!r}", path)
        return tuple(cls.__tick_word(index, entry, key, path)
                     for index, entry in enumerate(section))

    @classmethod
    def __tick_word(cls, index, entry, kind, root):
        path = f"{root}[{index}]"
        if not isinstance(entry, list) or not entry:
            raise DescriptionError(
                f"a {kind} word is a non-empty list of tick names, got "
                f"{entry!r}", path)
        if len(entry) > PanelBody.WIDTH_MAX:
            raise DescriptionError(
                f"a {kind} word packs at most {PanelBody.WIDTH_MAX} ticks, "
                f"got {len(entry)}", path)
        for name in entry:
            Field.identifier(name, path, f"{kind} name")
        return TickWord(names=tuple(entry))

    @staticmethod
    def __check_names(body, path):
        if body.empty():
            raise DescriptionError(
                "a control/status panel needs at least one control, status or "
                "tick", path)
        seen = set()
        for name in body.names():
            if name in seen:
                raise DescriptionError(
                    f"signal {name!r} is declared twice: one panel names each "
                    "of its signals once, whatever their kinds", path)
            seen.add(name)

    @staticmethod
    def __check_regions(body, path):
        """The three occupied register regions, each 64 words of the map."""
        room = PanelBody.REGION_WORDS
        action = len(body.tick_out) + 2 * len(body.tick_in)
        if action > room:
            raise DescriptionError(
                f"the action region holds {room} words, this panel needs "
                f"{action}: one per tick-out word, two per tick-in word "
                "(sticky clear and counter clear)", path)
        status = 2 + len(body.tick_in) + len(body.statuses) \
            + body.counter_count()
        if status > room:
            raise DescriptionError(
                f"the status region holds {room} words, this panel needs "
                f"{status}: STATUS, FINGERPRINT, one per tick-in word, one "
                "per status and one per tick input", path)
        if len(body.controls) > room:
            raise DescriptionError(
                f"the array region holds {room} words, this panel needs "
                f"{len(body.controls)}: one per control, whatever its width",
                path)
