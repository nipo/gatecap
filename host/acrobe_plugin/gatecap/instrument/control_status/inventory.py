"""What a control/status panel holds, and where its registers are.

The envelope tail carries the whole inventory of a panel: the control and
status registers as one name spec each, the tick names one text per packed
word, and the counter width. Nothing else describes the panel -- the register
block's own descriptor object is type-only -- so the map below is derived from
that inventory and from the register-map convention, and a host that can read
the tail can drive the hardware.

Widths come out of the name spec: an item naming one bit is a one-bit
register, ``dac_level[0:11]`` a twelve-bit one, and element 0 is the low bit.
A ``<...>`` suffix binds the enumeration table a widget decodes with.
"""

from __future__ import annotations

from acrobe_plugin.gatecap.names import SignalNames


class PanelField:
    """One control or status register: a name, the width the description
    declared, and the table its value decodes with."""

    def __init__(self, name, index, enum=None):
        self.name = name
        self.index = index          # register number within its kind
        self.width = 1
        self.enum = enum or None

    def __repr__(self):
        return (f"<{type(self).__name__} {self.name}[{self.width}]"
                f"{' enum' if self.enum else ''}>")

    @property
    def mask(self):
        return (1 << self.width) - 1

    def decode(self, value):
        """The label bound to a value, or None when nothing binds it."""
        return None if self.enum is None else self.enum.get(value)

    def encode(self, value):
        """A user-given value as the integer to write: an ``int``, or a label
        of the field's own enumeration table."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            if value < 0 or value > self.mask:
                raise ValueError(
                    f"{value} does not fit the {self.width}-bit field "
                    f"{self.name!r}")
            return value
        if isinstance(value, str):
            if self.enum is None:
                raise ValueError(
                    f"field {self.name!r} carries no enumeration, so "
                    f"{value!r} is not a value it takes")
            for number, label in self.enum.items():
                if label == value:
                    return number
            raise ValueError(
                f"{value!r} is not a label of {self.name!r} (labels: "
                + ", ".join(sorted(self.enum.values())) + ")")
        raise TypeError(
            f"field {self.name!r} takes an integer or an enumeration label, "
            f"not {value!r}")


class PanelTick:
    """One tick, in or out: the word it is packed in, its bit in that word,
    and its word-major number -- which is the number of its counter when it is
    a tick input."""

    def __init__(self, name, word, bit, index):
        self.name = name
        self.word = word
        self.bit = bit
        self.index = index

    def __repr__(self):
        return f"<PanelTick {self.name} word {self.word} bit {self.bit}>"


class PanelInventory:
    """The four kinds of a panel, in descriptor order, plus the counter
    width."""

    # Fields the tail carries, in order. Every panel carries all five: an
    # absent kind is an empty text or an empty array.
    TAIL_FIELDS = ("control names", "status names", "tick-out names",
                   "tick-in names", "counter width")
    WIDTH_MAX = 32

    def __init__(self, controls, statuses, tick_out, tick_in, counter_width):
        self.controls = tuple(controls)
        self.statuses = tuple(statuses)
        self.tick_out = tuple(tuple(word) for word in tick_out)
        self.tick_in = tuple(tuple(word) for word in tick_in)
        self.counter_width = counter_width

    @classmethod
    def parse(cls, tail, owner):
        """Build an inventory from an envelope tail. ``owner`` names the
        instrument in every message, so a rack of several panels points at the
        offending one."""
        if not isinstance(tail, list) or len(tail) != len(cls.TAIL_FIELDS):
            raise ValueError(
                f"control/status panel {owner!r} carries {len(tail)} tail "
                f"field(s), not the {len(cls.TAIL_FIELDS)} every panel does "
                f"({', '.join(cls.TAIL_FIELDS)})")
        control_names, status_names, tick_out, tick_in, counter_width = tail
        if not isinstance(counter_width, int) or not (
                1 <= counter_width <= cls.WIDTH_MAX):
            raise ValueError(
                f"control/status panel {owner!r} declares a counter width of "
                f"{counter_width!r}, which is not 1 to {cls.WIDTH_MAX}")
        return cls(controls=cls.fields(control_names, "control", owner),
                   statuses=cls.fields(status_names, "status", owner),
                   tick_out=cls.ticks(tick_out, "tick-out", owner),
                   tick_in=cls.ticks(tick_in, "tick-in", owner),
                   counter_width=counter_width)

    @classmethod
    def fields(cls, spec, kind, owner):
        """The registers one name spec names: one item per register, the
        item's width the register's declared width."""
        if not isinstance(spec, str):
            raise TypeError(
                f"control/status panel {owner!r} names its {kind}s with "
                f"{spec!r}, which is not a name spec")
        names, enums = SignalNames.parse(spec)
        fields = []
        for name in names:
            base, element = cls.__element(name)
            if fields and fields[-1].name == base and element == fields[-1].width:
                fields[-1].width += 1
                continue
            if element not in (None, 0):
                raise ValueError(
                    f"{kind} {base!r} of panel {owner!r} names element "
                    f"{element} where element 0 was expected: a panel names "
                    f"each register with one ascending item")
            fields.append(PanelField(base, len(fields), enums.get(base)))
        for field in fields:
            if field.width > cls.WIDTH_MAX:
                raise ValueError(
                    f"{kind} {field.name!r} of panel {owner!r} is "
                    f"{field.width} bits wide, over the {cls.WIDTH_MAX} a "
                    f"register holds")
        return fields

    @staticmethod
    def __element(name):
        """``("dac_level", 3)`` for ``dac_level[3]``, ``("led", None)`` for a
        name with no element."""
        if name.endswith("]") and "[" in name:
            head, _, index = name[:-1].rpartition("[")
            if index.isdigit():
                return head, int(index)
        return name, None

    @classmethod
    def ticks(cls, words, kind, owner):
        """The packed tick words: one text per word, one name per bit. Word
        boundaries are the simultaneity groups, and tick inputs are numbered
        word-major -- which is the order of their sticky bits and counters."""
        if not isinstance(words, list):
            raise TypeError(
                f"control/status panel {owner!r} names its {kind}s with "
                f"{words!r}, which is not an array of texts")
        packed = []
        index = 0
        for word, text in enumerate(words):
            if not isinstance(text, str) or not text:
                raise ValueError(
                    f"{kind} word {word} of panel {owner!r} is {text!r}, not a "
                    f"comma list of tick names")
            names = SignalNames.expand(text)
            if len(names) > cls.WIDTH_MAX:
                raise ValueError(
                    f"{kind} word {word} of panel {owner!r} packs "
                    f"{len(names)} ticks, over the {cls.WIDTH_MAX} a word "
                    f"holds")
            packed.append([PanelTick(name, word, bit, index + bit)
                           for bit, name in enumerate(names)])
            index += len(names)
        return packed

    def tick_out_ticks(self):
        return [tick for word in self.tick_out for tick in word]

    def tick_in_ticks(self):
        return [tick for word in self.tick_in for tick in word]

    def counter_count(self):
        return len(self.tick_in_ticks())

    def named(self, sequence, name, kind):
        for item in sequence:
            if item.name == name:
                return item
        raise KeyError(
            f"no {kind} named {name!r} (there is "
            + (", ".join(item.name for item in sequence) or "none") + ")")

    def control(self, name):
        return self.named(self.controls, name, "control")

    def status(self, name):
        return self.named(self.statuses, name, "status")

    def tick_output(self, name):
        return self.named(self.tick_out_ticks(), name, "tick output")

    def tick_input(self, name):
        return self.named(self.tick_in_ticks(), name, "tick input")

    def names(self):
        """Every signal name of the panel, whatever its kind."""
        return ([field.name for field in self.controls]
                + [field.name for field in self.statuses]
                + [tick.name for tick in self.tick_out_ticks()]
                + [tick.name for tick in self.tick_in_ticks()])


class PanelMap:
    """Where a panel's registers sit, as a function of its inventory and of
    the register-map convention: the action region holds the tick-out strobe
    words then the sticky-clear and counter-clear words, the status region the
    fixed pair then the sticky, status and counter words as one contiguous run,
    and the array region the control words."""

    WORD = 4
    ACTION = 0x000
    CONFIG = 0x100
    STATUS = 0x200
    ARRAY = 0x300

    def __init__(self, inventory):
        self.inventory = inventory
        self.tick_out_words = len(inventory.tick_out)
        self.tick_in_words = len(inventory.tick_in)
        self.status_words = len(inventory.statuses)
        self.control_words = len(inventory.controls)
        self.counter_words = inventory.counter_count()
        # Word ranks inside the status run, which is read in one burst.
        self.sticky_at = 2
        self.status_at = self.sticky_at + self.tick_in_words
        self.counter_at = self.status_at + self.status_words
        self.status_run = self.counter_at + self.counter_words

    def tick_out(self, word):
        return self.ACTION + self.WORD * word

    def sticky_clear(self, word):
        return self.ACTION + self.WORD * (self.tick_out_words + word)

    def counter_clear(self, word):
        return self.ACTION + self.WORD * (self.tick_out_words
                                          + self.tick_in_words + word)

    def fingerprint(self):
        return self.STATUS + self.WORD

    def sticky(self, word):
        return self.STATUS + self.WORD * (self.sticky_at + word)

    def status(self, index):
        return self.STATUS + self.WORD * (self.status_at + index)

    def counter(self, index):
        return self.STATUS + self.WORD * (self.counter_at + index)

    def control(self, index):
        return self.ARRAY + self.WORD * index
