"""Trace buffer driver: an addressable sample store internal to the
component tree (no UI of its own). A capture control reads it intra-Python
to reconstruct a trace; samples pack ``samples_per_word`` to an APB word.
"""

import uuid

from acrobe_plugin.gatecap.enumerator import (MemoryMappedBlock,
                                             MemoryMappedEnumerator)

# Must match the UUID in the gateware (gatecap.descriptor).
BUFFER_UUID = uuid.UUID("0f9d2ab1-afb1-44f4-b8d1-a35e6244e339")


@MemoryMappedEnumerator.db.register(BUFFER_UUID)
class Buffer(MemoryMappedBlock):
    def __init__(self, bridge, base, name, obj):
        super().__init__(bridge, base, name)
        # [type, sample-stride, total-size-l2]
        _, self.sample_stride, self.total_size_l2 = obj

    @property
    def word_bits(self):
        return 8 * self.bridge.word_bytes

    @property
    def samples_per_word(self):
        # How many samples share one APB word: 1 when a sample fills the word
        # (stride == word bits), >1 for a byte-lane packed buffer. A wide
        # sample spans several words instead (see words_per_sample), so this is
        # clamped to 1 there.
        return max(1, self.word_bits // self.sample_stride)

    @property
    def words_per_sample(self):
        # How many APB words a sample spans: 1 for plain/packed, >1 for a wide
        # sample whose stride advertises a run of words.
        return max(1, self.sample_stride // self.word_bits)

    @property
    def depth(self):
        # Number of samples the buffer holds.
        words = (1 << self.total_size_l2) // self.bridge.word_bytes
        if self.sample_stride > self.word_bits:
            return words // self.words_per_sample
        return words * self.samples_per_word

    def transfer_words(self, start, count):
        """How many APB words ``read_contiguous(start, count)`` moves. Known
        from the geometry alone, so a readback can declare the size of its
        transfer before the first word of it is on the wire."""
        if count <= 0:
            return 0
        if self.sample_stride > self.word_bits:
            return count * self.words_per_sample
        spw = self.samples_per_word
        return (start + count - 1) // spw - start // spw + 1

    def window_words(self, head, count):
        """How many APB words ``read_window(head, count)`` moves."""
        return self.transfer_words((head // count) * count, count)

    async def __transfer(self, addr, nwords, progress):
        # One transport burst at a time -- the address space splits a longer
        # read into the same bursts, so the traffic is unchanged, but the loop
        # is where a readback in flight reports how far it has got.
        wb = self.bridge.word_bytes
        out = bytearray()
        for off in range(0, nwords, self.bridge.max_burst):
            n = min(self.bridge.max_burst, nwords - off)
            out += await self.bridge.mem_read(addr + off * wb, n * wb)
            if progress is not None:
                progress.advance(n)
        return bytes(out)

    async def read_contiguous(self, start, count, signal_count, progress=None):
        # Read `count` samples stored contiguously from sample index `start`.
        # `progress` is the fetch progress of the capture block driving the
        # readback, advanced burst by burst (None for an uninstrumented read).
        wb = self.bridge.word_bytes
        mask = (1 << signal_count) - 1
        if self.sample_stride > self.word_bits:
            # Wide: each sample is `wps` consecutive words, little-endian; the
            # high padding words read as zero, so masking to signal_count drops
            # them.
            wps = self.words_per_sample
            data = await self.__transfer(self.base + start * wps * wb,
                                         count * wps, progress)
            words = [int.from_bytes(data[i * wb:(i + 1) * wb], "little")
                     for i in range(count * wps)]
            out = []
            for j in range(count):
                v = 0
                for k in range(wps):
                    v |= words[j * wps + k] << (k * self.word_bits)
                out.append(v & mask)
            return out
        # Plain / packed: `spw` samples share a word at `sample_stride`-bit lanes.
        spw = self.samples_per_word
        stride = self.sample_stride
        first_word = start // spw
        nwords = (start + count - 1) // spw - first_word + 1
        data = await self.__transfer(self.base + first_word * wb, nwords, progress)
        words = [int.from_bytes(data[i * wb:(i + 1) * wb], "little")
                 for i in range(nwords)]
        return [(words[(start + j) // spw - first_word]
                 >> ((start + j) % spw * stride)) & mask
                for j in range(count)]

    async def read_window(self, head, count, signal_count, progress=None):
        # A window occupies a count-sized slot, rolled so `head` addresses
        # its oldest sample. Read the whole slot, then de-rotate it into
        # time order.
        slot_base = (head // count) * count
        rot = head - slot_base
        slot = await self.read_contiguous(slot_base, count, signal_count, progress)
        return [slot[(rot + j) % count] for j in range(count)]
