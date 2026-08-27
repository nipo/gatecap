"""The rack's address space over a plain SPI link, as an acrobe node.

``SpiRack`` is registered on ``acrobe.protocol.spi.Target.child_db`` under
"gatecap", so any SPI master acrobe exposes reaches a rack the same way::

    udp/127.0.0.1:4254/nsl_spi(fin=100M,fmax=25M)/cs0/gatecap   (a simulator)
    ftdi/.../spi/cs0/gatecap                                    (a USB bridge)

The master is the backend, and the only thing asked of it is
``Target.transaction(Shift, ...)``: hold the chip select, shift the bytes,
return what came back on MISO. Nothing here depends on which master that is --
per-transfer size caps and CS quirks are the master's business -- so a new
backend is a new SPI interface in acrobe, not a change here.

The wire is SPI-flash shaped, one transaction per chip-select assertion::

    discovery [0x5a, address(3, big-endian), dummy]
                  -> "GCAP", CBOR geometry, then zeros
    read      [0x0b, address(4, big-endian), dummy] -> data...
    write     [0x02, address(4, big-endian), data...]

The read and write address is a byte address of the rack's APB space and
increments per data byte, so a burst is as long as the master keeps the chip
select asserted; the dummy byte is the turnaround that covers the rack's APB
read latency. Bytes are little-endian within a 32-bit word.

Discovery is the first transaction of a connection, and it is a flash's SFDP
read: the opcode, three address bytes and one dummy byte, then the four ASCII
bytes ``GCAP`` and a CBOR array ``[addr-bits, data-bytes-l2,
descriptor-base]``, which is where the descriptor is to be read and how wide
the words are. The address is written as zero and ignored -- the rack answers
its payload from the first byte whatever it carries -- so the phase exists
only to make the transaction an SFDP read on the wire. Nothing is assumed --
without that answer the link is not a gatecap rack, and the connection fails
by name rather than reading address 0 and hoping.

The bulk family is therefore the native one -- one chip-select assertion per
burst -- and the register family rides on it through
:class:`~acrobe.protocol.memory.RegisterFromBulk`, exactly as on the framed
bridge. A read may ask for any byte range: it is widened to the words covering
it and trimmed on reassembly, and the adapter's own prefetch reads one word
past the end of every burst, which is safe because no completer in a rack has
read side effects. A write may not be partial -- the wire carries no byte
enables, and a read-modify-write over registers with side effects is not
something a transport may do behind its caller's back.
"""

import io

import cbor2

from acrobe.engine import Batcher, BackgroundLowering
from acrobe.node import Node
from acrobe.protocol import memory
from acrobe.protocol.spi import Shift, Target

from .enumerator import MemoryMappedEnumerator


class SpiDiscoveryError(IOError):
    """What the discovery opcode answered is not a gatecap rack's blob."""


@Target.child_db.register("gatecap")
class SpiRack(memory.RegisterFromBulk, BackgroundLowering, Batcher,
              Node):
    """A gatecap rack behind an SPI master."""

    ops = memory.Interface.BULK_OPS

    OPCODE_READ = 0x0B
    OPCODE_WRITE = 0x02
    OPCODE_DISCOVERY = 0x5A
    # Address bytes on the wire, and the read turnaround. Both are the wire
    # format's, not the rack's: the rack's geometry is in the discovery blob
    # and in the descriptor, neither of which can be read without a wire
    # format to read them through.
    ADDRESS_BYTES = 4
    DUMMY_BYTES = 1
    # The discovery opcode is SFDP, whose layout is three address bytes and
    # eight dummy clocks before the first data byte. The rack ignores the
    # address, but the phases are clocked all the same: the transaction is an
    # SFDP read, and the payload's data position is where these two put it.
    DISCOVERY_ADDRESS_BYTES = 3
    DISCOVERY_DUMMY_BYTES = 1

    SIGNATURE = b"GCAP"
    # Bytes clocked out of the discovery opcode. The blob carries no length,
    # and a read past its end returns zeros, so the master reads a bound
    # instead: the signature (4), a CBOR array header (1) and three unsigned
    # integers at their widest CBOR encoding (9 each) come to 32 bytes. A
    # field wider than 64 bits, or a fourth field, is a new blob shape.
    DISCOVERY_BYTES = 32

    # The rack is mapped in 32-bit words, like every other gatecap transport.
    # The discovery blob states the width, and start() replaces this with what
    # the target answered.
    word_bytes = 4
    # Words moved by one chip-select assertion. The protocol sets no limit --
    # the burst ends when the master releases the chip select -- so this is
    # purely how much of a transfer the host asks a master to hold in one go,
    # and a master with a tighter cap is told through the ``max_burst``
    # option: ``.../cs0/gatecap(max_burst=16)``.
    MAX_BURST = 64

    def __init__(self, target, name="gatecap"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.target = target
        self.max_burst = self.MAX_BURST
        # Filled by discovery, which runs before anything is addressed.
        self.base = None
        self.addr_bits = None

    def option_set(self, key, value):
        if key == "max_burst":
            burst = int(value)
            if burst < 1:
                raise ValueError(f"{self.name}: max_burst must be at least "
                                 f"one word, got {burst}")
            self.max_burst = burst

    # -- address-space lowering --

    async def flush_ops(self, batch):
        self.dispatch(batch)

    async def run_ops(self, batch):
        for op, future in batch:
            try:
                if isinstance(op, memory.ReadBlob):
                    result = await self.read_blob(op.addr, op.size)
                elif isinstance(op, memory.WriteBlob):
                    await self.write_blob(op.addr, op.data)
                    result = None
                else:
                    raise TypeError(
                        f"{type(self).__name__} cannot lower "
                        f"{type(op).__name__}")
            except Exception as exc:  # noqa: BLE001 -- surface to the caller
                if future is not None and not future.done():
                    future.set_exception(exc)
                continue
            if future is not None:
                future.set_result(result)

    async def read_blob(self, addr, size):
        """Any byte range, covered by whole-word bursts and clipped back to
        what was asked for."""
        if size <= 0:
            return b""
        first = addr - addr % self.word_bytes
        end = addr + size
        raw = bytearray()
        cursor = first
        while cursor < end:
            words = min(self.max_burst,
                        (end - cursor + self.word_bytes - 1) // self.word_bytes)
            raw += await self.__read_transfer(cursor, words * self.word_bytes)
            cursor += words * self.word_bytes
        head = addr - first
        return bytes(raw[head:head + size])

    async def write_blob(self, addr, data):
        """Whole words at a word-aligned address, in bursts of at most
        ``max_burst`` words."""
        self.__write_check(addr, len(data))
        chunk = self.max_burst * self.word_bytes
        for offset in range(0, len(data), chunk):
            await self.__write_transfer(addr + offset,
                                        data[offset:offset + chunk])

    def __write_check(self, addr, size):
        if addr % self.word_bytes or size % self.word_bytes:
            raise ValueError(
                f"{self.name}: the wire writes whole {self.word_bytes}-byte "
                f"words, so [0x{addr:x}, 0x{addr + size:x}) cannot be written")

    async def __read_transfer(self, addr, size):
        """One chip-select assertion: opcode, address, turnaround, then
        ``size`` bytes clocked out of the rack."""
        head = Shift(self.__command(self.OPCODE_READ, addr)
                     + bytes(self.DUMMY_BYTES), read_miso=False)
        data = Shift(size, read_miso=True)
        await self.target.transaction(head, data)
        if data.miso is None or len(data.miso) != size:
            raise IOError(
                f"{self.name}: the master returned "
                f"{0 if data.miso is None else len(data.miso)} of {size} "
                f"bytes read at 0x{addr:x}")
        return bytes(data.miso)

    async def __write_transfer(self, addr, data):
        """One chip-select assertion: opcode, address, then the data. No
        turnaround -- a written word lands once it has fully arrived."""
        await self.target.transaction(
            Shift(self.__command(self.OPCODE_WRITE, addr) + bytes(data),
                  read_miso=False))

    def __command(self, opcode, addr):
        return (bytes([opcode])
                + addr.to_bytes(self.ADDRESS_BYTES, "big"))

    # -- discovery --

    async def start(self):
        await self.discover()
        enumerator = MemoryMappedEnumerator(self, base=self.base,
                                            name="enumerator")
        self.child_add(enumerator)

    async def discovery_blob(self):
        """One chip-select assertion on the discovery opcode: the SFDP address
        and turnaround phases, then ``DISCOVERY_BYTES`` clocked out of the
        target. The head is shifted without reading MISO, so what the target
        streams during those phases is never part of what is parsed."""
        head = Shift(bytes([self.OPCODE_DISCOVERY])
                     + bytes(self.DISCOVERY_ADDRESS_BYTES)
                     + bytes(self.DISCOVERY_DUMMY_BYTES), read_miso=False)
        data = Shift(self.DISCOVERY_BYTES, read_miso=True)
        await self.target.transaction(head, data)
        if data.miso is None or len(data.miso) != self.DISCOVERY_BYTES:
            raise self.__unanswered(
                f"the master returned "
                f"{0 if data.miso is None else len(data.miso)} of "
                f"{self.DISCOVERY_BYTES} bytes")
        return bytes(data.miso)

    async def discover(self):
        """Read the blob and take the map geometry from it. Everything this
        node addresses afterwards is placed by what the target answered
        here."""
        blob = await self.discovery_blob()
        if not blob.startswith(self.SIGNATURE):
            raise self.__unanswered(
                f"expected the signature {self.SIGNATURE.decode()!r}, got "
                f"{blob[:len(self.SIGNATURE)]!r}")
        # The blob carries no length: the CBOR item ends where it ends, and
        # the zeros padding the bounded read are past it.
        try:
            fields = cbor2.load(io.BytesIO(blob[len(self.SIGNATURE):]))
        except Exception as exc:
            raise self.__unanswered(
                f"the signature is followed by no decodable CBOR item "
                f"({exc})") from exc
        if not isinstance(fields, list) or len(fields) != 3 \
           or not all(isinstance(field, int) and field >= 0
                      for field in fields):
            raise self.__unanswered(
                f"the payload is not [addr-bits, data-bytes-l2, "
                f"descriptor-base], but {fields!r}")
        addr_bits, data_bytes_l2, base = fields
        if addr_bits > self.ADDRESS_BYTES * 8:
            raise self.__unanswered(
                f"the rack states a {addr_bits}-bit address space, wider "
                f"than the {self.ADDRESS_BYTES * 8}-bit address the wire "
                f"carries")
        self.addr_bits = addr_bits
        self.word_bytes = 1 << data_bytes_l2
        self.base = base

    def __unanswered(self, detail):
        return SpiDiscoveryError(
            f"{self.name}: the target did not answer gatecap discovery on "
            f"0x{self.OPCODE_DISCOVERY:02x}: {detail}")
