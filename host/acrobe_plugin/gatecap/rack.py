"""Rack descriptor decoding.

A rack descriptor is the blob a core serves at its descriptor base. It maps
allocated segment bases to instrument envelopes::

    [ rack-type-uuid,
      next-offset,
      { base: [ type-uuid, size_l2, name, children, ... ] } ]

The four leading envelope fields are framework-owned and frozen by the rack
type UUID: the instrument's type, its address-space footprint as a power of
two, its instance name and its children map. Whatever follows belongs to the
instrument type and is kept here as an opaque tail, for the driver bound to
that UUID to read.

Children are the register files inside one instrument, keyed by name. A child
offset is relative to the instrument's segment, and null for a reference-only
child that owns no register. Names scope to the instrument, so the references
one child holds to another resolve within a single children map.

Nothing here touches a bridge: it turns bytes into structure, and the caller
decides what to enumerate.
"""

import io
import uuid
from dataclasses import dataclass

import cbor2

# Must match RACK_UUID_C in the gateware (gatecap.descriptor).
RACK_UUID = uuid.UUID("aff98e3f-ce7f-483b-acc6-738464439eec")


class DescriptorError(ValueError):
    """A blob is not a rack descriptor, or one of the fields the format
    freezes does not have the shape it froze."""


@dataclass
class Child:
    """One entry of an instrument's children map: a typed object and, unless
    the child is reference-only, its offset within the instrument's segment."""

    name: str
    offset: int | None
    obj: list

    @property
    def type_uuid(self) -> uuid.UUID:
        return self.obj[0]

    @classmethod
    def parse(cls, name, entry) -> "Child":
        if not isinstance(entry, list) or len(entry) != 2:
            raise DescriptorError(
                f"child {name!r} is not an [offset, object] pair")
        offset, obj = entry
        if offset is not None and not isinstance(offset, int):
            raise DescriptorError(
                f"child {name!r} has a non-integer offset {offset!r}")
        if not isinstance(obj, list) or not obj:
            raise DescriptorError(
                f"child {name!r} does not hold a typed object")
        if not isinstance(obj[0], uuid.UUID):
            raise DescriptorError(
                f"child {name!r} object is not typed by a UUID")
        return cls(name=name, offset=offset, obj=obj)


@dataclass
class Instrument:
    """One instrument envelope, with the segment base it is keyed by."""

    name: str
    type_uuid: uuid.UUID
    base: int
    size_l2: int
    children: dict[str, Child]
    tail: list

    @property
    def size(self) -> int:
        return 1 << self.size_l2

    def child_address(self, name, descriptor_base=0) -> int | None:
        """Where a child's registers live, or None for a reference-only
        child. Offsets stack: the descriptor base the bridge advertises, the
        instrument's segment, then the child's own offset."""
        child = self.children[name]
        if child.offset is None:
            return None
        return descriptor_base + self.base + child.offset

    @classmethod
    def parse(cls, base, envelope) -> "Instrument":
        if not isinstance(base, int):
            raise DescriptorError(f"segment base {base!r} is not an integer")
        if not isinstance(envelope, list) or len(envelope) < 4:
            raise DescriptorError(
                f"envelope at base {base:#x} is shorter than the four fields "
                f"every instrument carries")
        type_uuid, size_l2, name, children = envelope[:4]
        if not isinstance(type_uuid, uuid.UUID):
            raise DescriptorError(
                f"envelope at base {base:#x} is not typed by a UUID")
        if not isinstance(size_l2, int):
            raise DescriptorError(
                f"envelope at base {base:#x} has a non-integer size_l2")
        if not isinstance(name, str):
            raise DescriptorError(
                f"envelope at base {base:#x} has a non-text name")
        if not isinstance(children, dict):
            raise DescriptorError(
                f"instrument {name!r} does not hold a children map")
        return cls(
            name=name,
            type_uuid=type_uuid,
            base=base,
            size_l2=size_l2,
            children={child_name: Child.parse(child_name, entry)
                      for child_name, entry in children.items()},
            tail=list(envelope[4:]))


@dataclass
class Rack:
    """A decoded rack descriptor: its instruments, in the order the root map
    lists them, and the chaining offset to a further blob (0 for none)."""

    next_offset: int
    instruments: list[Instrument]

    def __len__(self):
        return len(self.instruments)

    def __iter__(self):
        return iter(self.instruments)

    @classmethod
    def parse(cls, raw) -> "Rack":
        """Decodes the descriptor at the start of `raw`. Trailing bytes are
        ignored: a read of the ROM comes back padded to the burst length."""
        try:
            root = cbor2.load(io.BytesIO(raw))
        except cbor2.CBORDecodeError as error:
            raise DescriptorError(
                f"descriptor does not decode: {error}") from error
        return cls.parse_object(root)

    @classmethod
    def parse_object(cls, root) -> "Rack":
        if not isinstance(root, list) or len(root) != 3:
            raise DescriptorError(
                "descriptor root is not a [type, next-offset, segments] array")
        type_uuid, next_offset, segments = root
        if type_uuid != RACK_UUID:
            raise DescriptorError(
                f"descriptor root type {type_uuid} is not a rack ({RACK_UUID})")
        if not isinstance(next_offset, int):
            raise DescriptorError(
                f"next-offset {next_offset!r} is not an integer")
        if not isinstance(segments, dict):
            raise DescriptorError(
                "descriptor root does not hold a base-keyed segment map")
        return cls(next_offset=next_offset,
                   instruments=[Instrument.parse(base, envelope)
                                for base, envelope in segments.items()])
