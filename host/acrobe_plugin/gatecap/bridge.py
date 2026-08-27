"""gatecap frame node and stream-to-APB bridge as acrobe nodes.

``GatecapFramed`` is the generic self-enumeratable frame node, registered
on ``Datagram.db`` under "gatecap": resolving ``udp/host:port/gatecap``
spawns it as a child of the datagram. It answers the ff00 identify frame
and, on start, spawns the child matching the reported type UUID from its
own ``db`` (a bridge here, a router later).

``Bridge`` is such a child: it is the rack's address space in the
:mod:`acrobe.protocol.memory` sense, and on start it spawns a
memory-mapped enumerator that walks the descriptor through it.
"""

import asyncio
import io
from dataclasses import dataclass

import cbor2

from uuid import UUID
from acrobe.engine import Batcher
from acrobe.db import Db
from acrobe.node import Node
from acrobe.protocol import memory
from acrobe.protocol.datagram import Datagram
from acrobe.component.arm.ap import Ap
from acrobe.component.arm.mem_ap import MemAp

from .enumerator import MemoryMappedEnumerator


@dataclass(frozen=True, slots=True)
class Identify:
    pass


class FrameError(Exception):
    """A reply frame no payload can be taken from: empty, or carrying a
    non-zero status byte."""


@Datagram.db.register("gatecap")
class GatecapFramed(Batcher, Node):
    """Self-enumeratable frame node: answers the ff00 identify frame and
    spawns the child matching the reported type UUID from ``db`` (a bridge
    here, a router later)."""

    db = Db("gatecap framed node")

    def __init__(self, transport, name = "gatecap", params = None):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.transport = transport

    async def flush_ops(self, batch):
        for op, future in batch:
            try:
                cmd, decode = self.encode(op)
            except Exception as exc:  # noqa: BLE001 -- surface to caller
                if future is not None and not future.done():
                    future.set_exception(exc)
                continue
            self.issue(cmd, future, decode)

    def issue(self, cmd, future, decode):
        """Put one command frame on the wire and resolve `future` with
        `decode` applied to its reply. One command per frame, one reply frame
        per command, replies in command order: several frames may be in flight,
        and each is matched to the receive posted with it."""
        self.transport.send(cmd)
        self.wire(self.transport.recv(), future, decode)

    def wire(self, recv_future, future, decode):
        def _done(f):
            if future is None or future.done():
                # Nobody to hand a failure to; take it out of the future so it
                # is not reported as never retrieved.
                if not f.cancelled():
                    f.exception()
                return
            try:
                future.set_result(decode(f.result()))
            except Exception as exc:  # noqa: BLE001
                future.set_exception(exc)
        recv_future.add_done_callback(_done)

    def encode(self, op):
        if isinstance(op, Identify):
            return bytes([0xFF, 0x00]), self.__decode_identify
        raise TypeError(f"unsupported op {type(op).__name__}")

    @staticmethod
    def payload(res):
        data = bytes(res[0])
        if not data:
            raise FrameError("empty response")
        if data[-1] & 1:
            raise FrameError(f"bridge error, status 0x{data[-1]:02x}")
        return data[:-1]

    def __decode_identify(self, res):
        return cbor2.load(io.BytesIO(self.payload(res)))

    # -- public operation shortcuts --

    def identify(self):
        return self.post(Identify())

    # -- discovery --

    async def start(self):
        # Identify is a typed array: [type-uuid, fields...].
        id_data = await self.identify()
        type_uuid = id_data[0]
        node = await self.db.acall(type_uuid, self.transport, params=id_data[1:])
        self.child_add(node)

@GatecapFramed.db.register(UUID("51b5af74-0733-4ddb-9899-158ad7bde322"))
class Bridge(memory.RegisterFromBulk, GatecapFramed):
    """The rack's address space over the frame transport.

    The wire moves whole words: a read command asks for a run of them, a write
    command hands over a run of them with every byte lane enabled. The bulk
    family is therefore the native one -- one command frame per burst -- and
    the register family rides on it through
    :class:`~acrobe.protocol.memory.RegisterFromBulk`. That is the mirror of
    the MEM-AP, which owns a register window and serves blobs by splitting
    them into register-width accesses.

    A read may ask for any byte range: it is widened to the words covering it
    and trimmed on reassembly. A write may not -- the wire carries no byte
    enables, and turning a partial write into a read-modify-write over
    registers with side effects is not something a bridge may do behind its
    caller's back.
    """

    ops = memory.Interface.BULK_OPS

    OPCODE_WRITE = 0x00
    OPCODE_READ = 0x80

    def __init__(self, transport, base=0, name="bridge", params = None):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.transport = transport
        self.base = base
        self.addr_bytes = None
        self.word_bytes = None
        self.count_bytes = None
        self.max_burst = None

        if params:
            # [addr-bits, data-bytes-l2, burst-length-l2, descriptor-base]
            addr_bits, data_bytes_l2, burst_length_l2, descriptor_base = params
            self.addr_bytes = (addr_bits + 7) // 8
            self.word_bytes = 2 ** data_bytes_l2
            self.count_bytes = (burst_length_l2 + 7) // 8
            self.max_burst = 2 ** burst_length_l2
            self.base = descriptor_base

    # -- address-space lowering (identify is handled by the base) --

    async def flush_ops(self, batch):
        loop = asyncio.get_running_loop()
        for op, future in batch:
            try:
                if isinstance(op, memory.ReadBlob):
                    self.__lower_read(op, future, loop)
                elif isinstance(op, memory.WriteBlob):
                    self.__lower_write(op, future, loop)
                else:
                    cmd, decode = self.encode(op)
                    self.issue(cmd, future, decode)
            except Exception as exc:  # noqa: BLE001 -- surface to caller
                if future is None:
                    raise
                if not future.done():
                    future.set_exception(exc)

    def __lower_read(self, op, future, loop):
        pending = None
        if future is not None:
            if op.size <= 0:
                future.set_result(b"")
                return
            pending = memory.PendingBlob(future, op.size, is_read=True)
        for offset, addr, words in self.__read_bursts(op.addr, op.size):
            sub = None if pending is None else loop.create_future()
            self.issue(self.__read_command(addr, words), sub, self.payload)
            if pending is not None:
                pending.attach(offset, words * self.word_bytes, sub)

    def __lower_write(self, op, future, loop):
        self.__write_check(op.addr, len(op.data))
        pending = None
        if future is not None:
            if not op.data:
                future.set_result(None)
                return
            pending = memory.PendingBlob(future, len(op.data), is_read=False)
        chunk = self.max_burst * self.word_bytes
        for offset in range(0, len(op.data), chunk):
            piece = op.data[offset:offset + chunk]
            sub = None if pending is None else loop.create_future()
            self.issue(self.__write_command(op.addr + offset, piece), sub,
                       self.payload)
            if pending is not None:
                pending.attach(offset, len(piece), sub)

    def __read_bursts(self, addr, size):
        """Cover ``[addr, addr + size)`` with bursts of at most ``max_burst``
        whole words, aligned on the word the range starts in.

        Returns ``(blob_offset, burst_addr, words)`` triples. The first offset
        is negative when the range starts mid-word and the last burst may run
        past its end; :class:`~acrobe.protocol.memory.PendingBlob` clips both
        on reassembly."""
        word_bytes = self.word_bytes
        end = addr + size
        cursor = addr - addr % word_bytes
        out = []
        while cursor < end:
            words = min(self.max_burst,
                        (end - cursor + word_bytes - 1) // word_bytes)
            out.append((cursor - addr, cursor, words))
            cursor += words * word_bytes
        return out

    def __write_check(self, addr, size):
        word_bytes = self.word_bytes
        if addr % word_bytes or size % word_bytes:
            raise ValueError(
                f"{self.name}: the wire writes whole {word_bytes}-byte words, "
                f"so [0x{addr:x}, 0x{addr + size:x}) cannot be written")

    def __read_command(self, addr, words):
        return (bytes([self.OPCODE_READ])
                + addr.to_bytes(self.addr_bytes, "little")
                + (words - 1).to_bytes(self.count_bytes, "little"))

    def __write_command(self, addr, data):
        return (bytes([self.OPCODE_WRITE])
                + addr.to_bytes(self.addr_bytes, "little")
                + data)

    # -- discovery --

    async def start(self):
        enumerator = MemoryMappedEnumerator(self, base = self.base,
                                            name = "enumerator")
        self.child_add(enumerator)

@Ap.db.register(0x04ed0001)
class MemApBridge(MemAp):
    """The rack behind a MEM-AP: the access port is the address space the
    descriptor and the registers are read through, so the enumerator walks it
    directly."""

    # An access port moves data through a 32-bit data register, so a rack
    # reached this way is mapped in 32-bit words like any other.
    word_bytes = 4
    # Words per chunk of a long read. The access port splits a blob access on
    # its own, so this only sets how often a readback in flight reports
    # progress; one auto-increment window's worth is a natural step.
    max_burst = 256

    def __init__(self, dp, base = 0, idr = 0, name = "gatecap"):
        super().__init__(dp, name=name, base = 0, idr = 0x04770002)
        self.__base = base

    async def _discover_base_component(self) -> None:
        enumerator = MemoryMappedEnumerator(self, base = self.__base,
                                            name = "enumerator")
        self.child_add(enumerator)
