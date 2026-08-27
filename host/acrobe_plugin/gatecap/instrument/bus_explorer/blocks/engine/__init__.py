"""The engine: the whole register file of a bus explorer behind one APB
completer.

Its descriptor object carries nothing but its type. The map is the register-map
convention applied to the instrument's slot count, which the envelope tail
already states, and a second copy of it in the descriptor could only ever
drift -- so the offsets live here and the instrument driver above decides what
to do with them.

The block moves words and stages commands; it decides nothing. What an
operation means, what its error code is called and what gets written down about
it is the instrument's business.

It offers no UI of its own: the pane an explorer shows is this block, and a
second surface over the same registers would only be a way to disagree with it.
"""

import asyncio
import uuid

from acrobe_plugin.gatecap.enumerator import (MemoryMappedBlock,
                                              MemoryMappedEnumerator)

# Must match BUS_EXPLORER_ENGINE_UUID_C in the gateware (gatecap.bus_explorer).
BUS_EXPLORER_ENGINE_UUID = uuid.UUID("b6da0744-8162-4879-8563-67f506557b89")


@MemoryMappedEnumerator.db.register(BUS_EXPLORER_ENGINE_UUID)
class BusExplorerEngine(MemoryMappedBlock):
    """Byte offsets of the four regions, and the words inside them."""

    # 0x000 action, write-only.
    COMMAND = 0x000
    # 0x100 config, read/write.
    ADDRESS = 0x100
    WDATA = 0x104
    WMASK = 0x108
    SLOT_ENABLE = 0x10C
    SCAN_CTRL = 0x110
    # 0x200 status, read-only, one contiguous run through the scan results.
    STATUS = 0x200
    FINGERPRINT = 0x204
    RDATA = 0x208
    SCAN_VALID = 0x20C
    SCAN_ERROR = 0x210
    SCAN_RESULT = 0x214
    # 0x300 arrays, read/write.
    SLOT_ADDRESS = 0x300

    # Words of the status region before the per-slot results.
    STATUS_HEAD = 5

    def __init__(self, bridge, base, name, obj):
        super().__init__(bridge, base, name)
        self.obj = obj

    def word_bytes(self):
        return self.bridge.word_bytes

    async def words(self, offset, count):
        """``count`` consecutive words from ``offset``, in one burst."""
        size = self.word_bytes()
        raw = await self.bridge.mem_read(self.base + offset, count * size)
        return [int.from_bytes(raw[i * size:(i + 1) * size], "little")
                for i in range(count)]

    async def word(self, offset):
        return await self.bridge.read32(self.base + offset)

    async def write(self, offset, value):
        await self.bridge.write32(self.base + offset, value)

    async def write_words(self, offset, values):
        """Consecutive words from ``offset``, in one burst write."""
        size = self.word_bytes()
        raw = b"".join(value.to_bytes(size, "little") for value in values)
        await self.bridge.mem_write(self.base + offset, raw)

    async def fire(self, address, wdata, wmask, op):
        """Stage an operation and fire it: the three config words in one burst
        write, then the COMMAND write that starts it.

        Both go out in one batch, so the staging and the firing cost the
        transport a single round trip -- and they stay in order, which is the
        whole of the contract (COMMAND consumes what the config region holds
        at the moment it is written)."""
        await asyncio.gather(
            self.write_words(self.ADDRESS, [address, wdata, wmask]),
            self.write(self.COMMAND, op))

    async def status(self, slot_count):
        """The whole read-only run in one burst: STATUS, FINGERPRINT, RDATA,
        the two scan flag words and one result per slot."""
        return await self.words(self.STATUS, self.STATUS_HEAD + slot_count)

    async def slot_addresses(self, slot_count):
        return await self.words(self.SLOT_ADDRESS, slot_count)

    async def slot_addresses_write(self, addresses):
        await self.write_words(self.SLOT_ADDRESS, list(addresses))
