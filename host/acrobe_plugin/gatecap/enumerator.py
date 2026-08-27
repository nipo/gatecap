"""Memory-mapped enumerator for a gatecap rack.

``MemoryMappedEnumerator`` reads the descriptor blob at the base the transport
advertises and spawns the tree it describes. The root is a rack
(``rack.RACK_UUID``) and nothing else: any other type is a core this host does
not know how to read, and says so.

The tree is two levels: one node per instrument, bound by the instrument type
UUID in ``instruments``, and under each of them one node per child register
file, bound by the block type UUID in ``db``. A child's register base is
``descriptor-base + segment + offset``.

Names are free-form instance data: an analyzer holding several capture domains
prefixes its blocks with the domain (``rx.control``, ``rx.buffer``, ...), and
references between blocks use those names verbatim. They scope to one
instrument, so a reference resolves inside a single children map and cannot
reach across instruments.

A child's offset may be null, which describes a reference-only block: it has
no registers of its own and only refers to others. Its driver is built with
``base=None``; a driver that does drive registers derives from
``MemoryMappedBlock``, which refuses a missing base.

Once every child of an instrument exists, each driver exposing
``siblings_resolve`` is handed the name -> node map of that instrument -- the
instrument itself included -- so a driver holding references binds them while
the whole set is known.

Drivers ship with the instrument that owns them -- gatecap's own in
:mod:`acrobe_plugin.gatecap.instrument.la` -- and register on the two
databases when their package is imported.
"""

import io

import cbor2

from acrobe.db import Db
from acrobe.node import Node

from .rack import RACK_UUID, DescriptorError, Rack


class MemoryMappedEnumerator(Node):
    # Blocks, keyed by the type UUID of a descriptor child.
    db = Db("gatecap mm driver")
    # Instruments, keyed by the type UUID of a rack envelope.
    instruments = Db("gatecap instrument driver")

    def __init__(self, bridge, base, name):
        super().__init__(name)
        self.bridge = bridge
        self.base = base

    async def start(self):
        root = await self.__descriptor()
        if not (isinstance(root, list) and root and root[0] == RACK_UUID):
            kind = root[0] if isinstance(root, list) and root else root
            raise DescriptorError(
                f"the descriptor at {self.base:#x} is typed {kind}, not a "
                f"gatecap rack ({RACK_UUID}): this host reads no other "
                f"descriptor format")
        await self.__rack(Rack.parse_object(root))

    async def __rack(self, rack):
        for instrument in rack:
            base = self.base + instrument.base
            node = await self.instruments.acall(
                instrument.type_uuid, self.bridge, base, instrument)
            self.parent.child_add(node)
            children = {}
            for child in instrument.children.values():
                child_base = (None if child.offset is None
                              else base + child.offset)
                children[child.name] = await self.db.acall(
                    child.obj[0], self.bridge, child_base, child.name,
                    child.obj)
            for child in children.values():
                node.child_add(child)
            self.__resolve(list(children.values()) + [node], children)

    @staticmethod
    def __resolve(nodes, scope):
        for node in nodes:
            resolve = getattr(node, "siblings_resolve", None)
            if resolve is not None:
                resolve(scope)

    async def __descriptor(self):
        # The descriptor length is not advertised, so grow the read until it
        # decodes. Reading past the ROM aliases harmlessly -- cbor2 stops at
        # the first (complete) item.
        size = 64 * self.bridge.word_bytes
        while True:
            raw = await self.bridge.mem_read(self.base, size)
            try:
                return cbor2.load(io.BytesIO(raw))
            except cbor2.CBORDecodeEOF:
                if size >= 16384:
                    raise
                size *= 2


class MemoryMappedBlock(Node):
    """Base for a block driver that owns registers: holds the bridge and the
    block's register base. A reference-only descriptor entry carries no base,
    which such a driver cannot work from, so it is rejected here instead of
    reaching the transport with an address computed from nothing.

    The bridge is the address space the rack is mapped in -- an
    :class:`acrobe.protocol.memory.Interface`, so a driver reads and writes
    through ``read32`` / ``write32`` / ``mem_read`` / ``mem_write`` -- plus two
    numbers of its own: ``word_bytes``, the width the descriptor's geometry is
    expressed in, and ``max_burst``, the words one transport transaction
    moves."""

    def __init__(self, bridge, base, name):
        super().__init__(name)
        if base is None:
            raise ValueError(
                f"block {name!r} ({type(self).__name__}) drives registers, but "
                f"its descriptor entry carries no register base")
        self.bridge = bridge
        self.base = base


class MemoryMappedInstrument(Node):
    """Base for an instrument driver: holds the bridge, the base of the
    instrument's segment and the envelope it was built from.

    An instrument always owns a segment, so the base is never missing. What
    the envelope carries past the four framework fields is the instrument
    type's own business, and reaches the driver as ``tail``."""

    def __init__(self, bridge, base, envelope):
        super().__init__(envelope.name)
        self.bridge = bridge
        self.base = base
        self.envelope = envelope
        self.tail = envelope.tail

    @property
    def size(self):
        """Bytes of address space the instrument was allocated."""
        return self.envelope.size


class BlockAddress:
    """How a frontend names a node of an enumerated rack.

    An instrument goes by its instance name; a block below it by the
    instrument's name and its own, dot-joined -- two analyzers may hold a
    domain of the same name, and their blocks must still be distinguishable.
    A bare block name stays a legal address as long as one node answers to it,
    which is what a rack holding one instrument always is."""

    SEPARATOR = "."

    @classmethod
    def of(cls, node):
        parent = node.parent
        if isinstance(parent, MemoryMappedInstrument):
            return f"{parent.name}{cls.SEPARATOR}{node.name}"
        return node.name

    @classmethod
    def targets(cls, nodes):
        """``address -> node`` for a set of nodes: every qualified address,
        plus the bare names no two nodes share."""
        nodes = list(nodes)
        addresses = {cls.of(node): node for node in nodes}
        seen = {}
        for node in nodes:
            seen[node.name] = seen.get(node.name, 0) + 1
        for node in nodes:
            if seen[node.name] == 1:
                addresses.setdefault(node.name, node)
        return addresses

    @classmethod
    def canonical(cls, nodes):
        """The addresses worth showing a user: one per node, qualified."""
        return sorted(cls.of(node) for node in nodes)


@MemoryMappedEnumerator.db.register_default
class UnknownComponent(Node):
    """Fallback for a block whose UUID has no registered driver, so it still
    appears in the enumerated tree. A baseless entry is kept as well: with no
    driver behind the UUID there is no register access to miss."""

    def __init__(self, bridge, base, name, obj):
        super().__init__(f"{name} (unknown {obj[0]})")
        self.bridge = bridge
        self.base = base
        self.obj = obj


@MemoryMappedEnumerator.instruments.register_default
class UnknownInstrument(MemoryMappedInstrument):
    """Fallback for an instrument whose UUID has no registered driver. Its
    name, footprint and tail stay readable, and its children are enumerated
    like any other instrument's -- a known block under an unknown instrument
    still gets its driver."""

    def __init__(self, bridge, base, envelope):
        super().__init__(bridge, base, envelope)
        self.name = f"{envelope.name} (unknown {envelope.type_uuid})"
