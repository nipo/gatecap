"""SVD decode for a bus explorer's target, host-side and host-side only.

The descriptor carries a free-text map identifier, never a map: the register
map of a transceiver DRP or a third-party IP block describes external hardware
and runs to hundreds of registers, which belongs in neither a ROM nor a
descriptor. The host resolves that identifier against documents the user
registered (:class:`MapLibrary`) and falls back to raw hex when it has none --
every decode surface below answers None rather than failing, so a target with
no map is explored exactly as one with a map, minus the names.

SVD is the one map format. The subset parsed here is the practical one:

* ``device`` -> ``peripherals`` -> ``peripheral`` -> ``registers`` ->
  ``register`` -> ``fields`` -> ``field`` -> ``enumeratedValues``;
* ``baseAddress`` plus ``addressOffset`` as the absolute address of a
  register, which is the address the engine drives onto the target verbatim;
* a field's bit range in each of the three spellings SVD allows --
  ``bitOffset``/``bitWidth``, ``bitRange`` as ``[msb:lsb]``, and
  ``msb``/``lsb``;
* ``access`` on a register or a field, so a read-only register is not offered
  for writing;
* ``dim``/``dimIncrement``/``dimIndex`` register arrays, expanded into one
  register per index;
* ``derivedFrom`` between peripherals.

Anything past that subset raises :class:`SvdError` naming the element rather
than being skipped: a map that silently lost half its registers would decode
addresses to the wrong names, which is worse than no map at all.

Only ``xml.etree`` is used -- decode must not cost the host a dependency.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path


class SvdError(ValueError):
    """A document is not SVD, or uses a construct this subset does not
    implement."""


class Scalar:
    """The number spellings SVD writes an integer in."""

    @classmethod
    def integer(cls, text, what):
        """``0x1f``, ``#0001``, ``31``, and the ``k``/``m`` suffixes, as an
        int."""
        if text is None:
            return None
        text = text.strip()
        if not text:
            raise SvdError(f"{what} is empty")
        negative = text.startswith("-")
        if negative:
            text = text[1:]
        scale = 1
        if text[-1:] in ("k", "K"):
            scale, text = 1024, text[:-1]
        elif text[-1:] in ("m", "M"):
            scale, text = 1024 * 1024, text[:-1]
        try:
            if text.startswith("#"):
                # A binary literal, whose x/X digits are don't-care bits. A
                # don't-care in a number the host has to compute with has no
                # meaning, so they read as zero.
                value = int(text[1:].replace("x", "0").replace("X", "0"), 2)
            elif text[:2].lower() == "0x":
                value = int(text[2:], 16)
            elif text[:2].lower() == "0b":
                value = int(text[2:], 2)
            else:
                value = int(text, 10)
        except ValueError:
            raise SvdError(f"{what} is not a number: {text!r}") from None
        return -value * scale if negative else value * scale

    @classmethod
    def required(cls, element, tag, what):
        value = cls.integer(Element.text(element, tag), f"{what} {tag}")
        if value is None:
            raise SvdError(f"{what} has no {tag}")
        return value


class Element:
    """Reading an SVD element without carrying a namespace: SVD documents are
    published both with and without one, and the tag names are the same."""

    @staticmethod
    def tag(element):
        tag = element.tag
        return tag.split("}", 1)[1] if "}" in tag else tag

    @classmethod
    def children(cls, element, tag):
        if element is None:
            return []
        return [child for child in element if cls.tag(child) == tag]

    @classmethod
    def child(cls, element, tag):
        found = cls.children(element, tag)
        return found[0] if found else None

    @classmethod
    def text(cls, element, tag, default=None):
        child = cls.child(element, tag)
        if child is None or child.text is None:
            return default
        return child.text.strip()


@dataclass(frozen=True)
class SvdEnum:
    """One ``enumeratedValue``: a name bound to a field value."""

    name: str
    value: int | None
    description: str | None
    is_default: bool

    @classmethod
    def parse(cls, element, what):
        name = Element.text(element, "name")
        if name is None:
            raise SvdError(f"{what} has an enumeratedValue with no name")
        is_default = Element.text(element, "isDefault", "false") == "true"
        value = Scalar.integer(Element.text(element, "value"),
                               f"{what}.{name} value")
        if value is None and not is_default:
            raise SvdError(f"{what}.{name} has neither a value nor isDefault")
        return cls(name=name, value=value,
                   description=Element.text(element, "description"),
                   is_default=is_default)


@dataclass(frozen=True)
class SvdField:
    """One field of a register: where its bits are, and what they mean."""

    name: str
    description: str | None
    lsb: int
    msb: int
    access: str | None
    enums: tuple = ()

    @property
    def width(self):
        return self.msb - self.lsb + 1

    @property
    def mask(self):
        """The field's bits, in register-word position."""
        return ((1 << self.width) - 1) << self.lsb

    def extract(self, word):
        """This field's value out of a whole register word."""
        return (word >> self.lsb) & ((1 << self.width) - 1)

    def place(self, value):
        """A field value in register-word position, refusing one that does not
        fit: a value wider than the field would silently reach into its
        neighbours, and the mask says the neighbours are not being written."""
        if value < 0 or value > (1 << self.width) - 1:
            raise ValueError(
                f"{value} does not fit field {self.name} "
                f"({self.width} bit(s), max {(1 << self.width) - 1})")
        return value << self.lsb

    def label(self, value):
        """The enumerated name a field value carries, or None."""
        default = None
        for entry in self.enums:
            if entry.is_default:
                default = entry.name
            elif entry.value == value:
                return entry.name
        return default

    def encode(self, value):
        """A field value from either an integer or one of its enumerated
        names."""
        if isinstance(value, str):
            for entry in self.enums:
                if entry.name == value:
                    if entry.value is None:
                        raise ValueError(
                            f"{self.name}={value!r} is the default label, "
                            f"which binds no value to write")
                    return entry.value
            try:
                return Scalar.integer(value, f"field {self.name}")
            except SvdError:
                raise ValueError(
                    f"field {self.name} has no value named {value!r}"
                    + (" (" + ", ".join(e.name for e in self.enums) + ")"
                       if self.enums else "")) from None
        return int(value)

    def writable(self):
        return self.access is None or "write" in self.access

    @classmethod
    def parse(cls, element, what):
        name = Element.text(element, "name")
        if name is None:
            raise SvdError(f"{what} has a field with no name")
        if element.get("derivedFrom") is not None:
            raise SvdError(
                f"{what}.{name} is derivedFrom another field, which this "
                f"decoder does not resolve")
        lsb, msb = cls.__range(element, f"{what}.{name}")
        return cls(name=name, description=Element.text(element, "description"),
                   lsb=lsb, msb=msb, access=Element.text(element, "access"),
                   enums=cls.__enums(element, f"{what}.{name}"))

    @staticmethod
    def __range(element, what):
        """The three spellings of a bit range, in the order SVD lists them."""
        offset = Scalar.integer(Element.text(element, "bitOffset"),
                                f"{what} bitOffset")
        if offset is not None:
            width = Scalar.integer(Element.text(element, "bitWidth"),
                                   f"{what} bitWidth")
            return offset, offset + (1 if width is None else width) - 1
        text = Element.text(element, "bitRange")
        if text is not None:
            body = text.strip().strip("[]")
            if ":" not in body:
                raise SvdError(f"{what} bitRange {text!r} is not [msb:lsb]")
            msb_text, lsb_text = body.split(":", 1)
            return (Scalar.integer(lsb_text, f"{what} bitRange lsb"),
                    Scalar.integer(msb_text, f"{what} bitRange msb"))
        lsb = Scalar.integer(Element.text(element, "lsb"), f"{what} lsb")
        msb = Scalar.integer(Element.text(element, "msb"), f"{what} msb")
        if lsb is None or msb is None:
            raise SvdError(f"{what} states no bit range")
        return lsb, msb

    @staticmethod
    def __enums(element, what):
        entries = []
        for group in Element.children(element, "enumeratedValues"):
            if group.get("derivedFrom") is not None:
                raise SvdError(
                    f"{what} has enumeratedValues derivedFrom another field, "
                    f"which this decoder does not resolve")
            for value in Element.children(group, "enumeratedValue"):
                entries.append(SvdEnum.parse(value, what))
        return tuple(entries)


@dataclass(frozen=True)
class SvdRegister:
    """One register at one absolute address."""

    name: str
    display_name: str | None
    description: str | None
    address: int
    size: int
    access: str | None
    reset_value: int | None
    peripheral: str
    fields: tuple = ()

    @property
    def qualified(self):
        return f"{self.peripheral}.{self.name}"

    def writable(self):
        return self.access is None or "write" in self.access

    def field(self, name):
        for entry in self.fields:
            if entry.name == name:
                return entry
        raise KeyError(
            f"register {self.qualified} has no field {name!r}"
            + (" (" + ", ".join(f.name for f in self.fields) + ")"
               if self.fields else ""))

    def decode(self, word):
        """``[{name, lsb, msb, width, value, label, access}]`` over the fields,
        low bits first. A register with no fields decodes to nothing, and the
        caller shows the raw word."""
        return [{"name": f.name, "lsb": f.lsb, "msb": f.msb, "width": f.width,
                 "value": f.extract(word), "label": f.label(f.extract(word)),
                 "access": f.access or self.access,
                 "description": f.description,
                 "enum": {str(e.value): e.name for e in f.enums
                          if e.value is not None}}
                for f in sorted(self.fields, key=lambda f: f.lsb)]

    @classmethod
    def parse(cls, element, peripheral, base, what):
        name = Element.text(element, "name")
        if name is None:
            raise SvdError(f"{what} has a register with no name")
        if element.get("derivedFrom") is not None:
            raise SvdError(
                f"{what}.{name} is derivedFrom another register, which this "
                f"decoder does not resolve")
        offset = Scalar.required(element, "addressOffset", f"{what}.{name}")
        size = Scalar.integer(Element.text(element, "size"),
                              f"{what}.{name} size") or 32
        common = dict(
            display_name=Element.text(element, "displayName"),
            description=Element.text(element, "description"),
            size=size, access=Element.text(element, "access"),
            reset_value=Scalar.integer(Element.text(element, "resetValue"),
                                       f"{what}.{name} resetValue"),
            peripheral=peripheral,
            fields=tuple(SvdField.parse(f, f"{what}.{name}")
                         for group in Element.children(element, "fields")
                         for f in Element.children(group, "field")))
        dim = Scalar.integer(Element.text(element, "dim"), f"{what}.{name} dim")
        if dim is None:
            return [cls(name=name, address=base + offset, **common)]
        return cls.__array(element, name, base + offset, dim, common,
                           f"{what}.{name}")

    @classmethod
    def __array(cls, element, name, address, dim, common, what):
        """A ``dim`` register array, one register per index. The index
        substitutes for ``%s`` in the name; a name without one is suffixed, so
        two members never collide."""
        increment = Scalar.required(element, "dimIncrement", what)
        indices = Element.text(element, "dimIndex")
        if indices is None:
            labels = [str(i) for i in range(dim)]
        elif "-" in indices and "," not in indices:
            first, last = indices.split("-", 1)
            labels = [str(i) for i in range(int(first), int(last) + 1)]
        else:
            labels = [part.strip() for part in indices.split(",")]
        if len(labels) != dim:
            raise SvdError(
                f"{what} declares dim {dim} but a dimIndex of {len(labels)}")
        return [cls(name=(name.replace("%s", label) if "%s" in name
                          else f"{name}{label}"),
                    address=address + i * increment, **common)
                for i, label in enumerate(labels)]


@dataclass
class SvdPeripheral:
    """One peripheral: a base address and the registers above it."""

    name: str
    description: str | None
    base_address: int
    registers: list = dataclass_field(default_factory=list)


class SvdDocument:
    """A parsed SVD document, indexed the way a bus explorer asks of it: by
    absolute address, and by register name."""

    def __init__(self, name, peripherals, source=None):
        self.name = name
        self.peripherals = list(peripherals)
        self.source = source
        self.registers = [register for peripheral in self.peripherals
                          for register in peripheral.registers]
        self.by_address = {}
        for register in self.registers:
            # Two peripherals may legitimately overlay one address (an
            # alternate view); the first one named wins, deterministically.
            self.by_address.setdefault(register.address, register)
        self.by_name = {}
        for register in self.registers:
            self.by_name.setdefault(register.qualified, register)
            self.by_name.setdefault(register.name, register)

    def __len__(self):
        return len(self.registers)

    def register_at(self, address):
        """The register at a target address, or None."""
        return self.by_address.get(address)

    def register(self, name):
        """A register by ``PERIPHERAL.NAME`` or by bare name when only one
        peripheral has it."""
        register = self.by_name.get(name)
        if register is None:
            raise KeyError(f"no register named {name!r} in map {self.name!r}")
        return register

    @classmethod
    def parse_file(cls, path):
        path = Path(path)
        try:
            tree = ET.parse(path)
        except ET.ParseError as e:
            raise SvdError(f"{path}: not well-formed XML: {e}") from None
        return cls.parse(tree.getroot(), source=str(path))

    @classmethod
    def parse_text(cls, text, source=None):
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise SvdError(f"not well-formed XML: {e}") from None
        return cls.parse(root, source=source)

    @classmethod
    def parse(cls, root, source=None):
        if Element.tag(root) != "device":
            raise SvdError(
                f"root element is <{Element.tag(root)}>, not <device>: "
                f"this is not an SVD document")
        name = Element.text(root, "name", "device")
        groups = Element.children(root, "peripherals")
        if not groups:
            raise SvdError(f"SVD device {name!r} holds no <peripherals>")
        raw = [element for group in groups
               for element in Element.children(group, "peripheral")]
        parsed = {}
        order = []
        for element in raw:
            peripheral = cls.__peripheral(element, parsed, name)
            parsed[peripheral.name] = peripheral
            order.append(peripheral)
        return cls(name, order, source=source)

    @classmethod
    def __peripheral(cls, element, parsed, device):
        name = Element.text(element, "name")
        if name is None:
            raise SvdError(f"SVD device {device!r} has a peripheral with no "
                           f"name")
        base = Scalar.required(element, "baseAddress", f"peripheral {name}")
        groups = Element.children(element, "registers")
        for group in groups:
            if Element.children(group, "cluster"):
                raise SvdError(
                    f"peripheral {name} holds a <cluster>, which this decoder "
                    f"does not flatten")
        registers = [register
                     for group in groups
                     for element_r in Element.children(group, "register")
                     for register in SvdRegister.parse(element_r, name, base,
                                                       f"peripheral {name}")]
        derived = element.get("derivedFrom")
        if derived is not None:
            if derived not in parsed:
                raise SvdError(
                    f"peripheral {name} is derivedFrom {derived!r}, which is "
                    f"not a peripheral declared before it")
            if registers:
                raise SvdError(
                    f"peripheral {name} is derivedFrom {derived!r} and also "
                    f"declares registers, which this decoder does not merge")
            registers = [SvdRegister(**{**source.__dict__,
                                        "address": base + (source.address
                                                           - parsed[derived]
                                                           .base_address),
                                        "peripheral": name})
                         for source in parsed[derived].registers]
        return SvdPeripheral(name=name,
                             description=Element.text(element, "description"),
                             base_address=base, registers=registers)


class MapLibrary:
    """The user's library of SVD documents, keyed by map identifier.

    A descriptor's map identifier (``xilinx-gtye4-drp``) means nothing to the
    host until someone says which file it is; the binding is user-local
    configuration, stored beside the rest of it (``acrobe gatecap bus map
    add``). Resolution is by identifier first, then by a path given directly --
    a file the user picked in the pane or named on the command line -- and
    ``None`` when neither answers, which is the raw-hex case.
    """

    SECTION = "bus-explorer-maps"

    def __init__(self, config_path=None):
        # A library reads the store afresh on every call rather than caching
        # it: `bus map add` in one process and a pane in another are the normal
        # case, and a stale binding would send the user looking for a bug in
        # the decode.
        self.config_path = config_path

    def config(self):
        # Imported here: the store is the user's config file, which the GUI
        # owns, and a driver must not drag a frontend in at import time.
        from ...gui.config import Config
        return Config(self.config_path)

    def registered(self):
        """``{map identifier: file path}``, as the user registered them."""
        return dict(self.config().data.get(self.SECTION, {}))

    def add(self, map_id, path):
        """Register a document under an identifier. The file is parsed before
        it is stored, so a bad path or a document this decoder cannot read is
        refused where the user can still see why."""
        path = str(Path(path).expanduser().resolve())
        document = SvdDocument.parse_file(path)
        config = self.config()
        config.data.setdefault(self.SECTION, {})[map_id] = path
        config.save()
        return document

    def remove(self, map_id):
        config = self.config()
        maps = config.data.get(self.SECTION, {})
        if map_id not in maps:
            raise KeyError(f"no register map registered as {map_id!r}")
        del maps[map_id]
        config.save()

    def path(self, map_id):
        return self.registered().get(map_id)

    def resolve(self, map_id):
        """The document a map identifier names, or None when the identifier is
        empty or nothing is registered under it. A registered path that no
        longer parses raises: the user asked for that map by name."""
        if not map_id:
            return None
        path = self.path(map_id)
        if path is None:
            return None
        return SvdDocument.parse_file(path)
