"""The register file of a control/status panel: the whole map of the
instrument behind one APB completer.

Its descriptor object carries nothing but its type. The map is a pure function
of the panel's inventory and of the register-map convention (see
:mod:`..inventory`), and a copy of it in the descriptor could only ever drift,
so the addresses are computed by the instrument driver and this block only
moves words.

It offers no UI of its own: the panel the instrument shows is this block, and a
second surface over the same registers would only be a way to disagree with it.
"""

import uuid

from acrobe_plugin.gatecap.enumerator import (MemoryMappedBlock,
                                              MemoryMappedEnumerator)

# Must match CONTROL_STATUS_BLOCK_UUID_C in the gateware (gatecap.descriptor).
PANEL_REGISTERS_UUID = uuid.UUID("2ee04b40-1620-438f-a783-0989ef7e19d3")


@MemoryMappedEnumerator.db.register(PANEL_REGISTERS_UUID)
class PanelRegisters(MemoryMappedBlock):
    def __init__(self, bridge, base, name, obj):
        super().__init__(bridge, base, name)
        self.obj = obj

    async def words(self, offset, count):
        """``count`` consecutive words from ``offset``, in one burst."""
        size = self.bridge.word_bytes
        raw = await self.bridge.mem_read(self.base + offset, count * size)
        return [int.from_bytes(raw[i * size:(i + 1) * size], "little")
                for i in range(count)]

    async def word(self, offset):
        return await self.bridge.read32(self.base + offset)

    async def write(self, offset, value):
        await self.bridge.write32(self.base + offset, value)
