"""The framed bridge as an address space: what reaches the wire.

The bridge answers the memory model (``mem_read`` / ``mem_write`` /
``read32`` / ``write32``) with command frames of its own encoding. These tests
hold a fake link in place of the transport and assert on the frames it sees:
one per burst, whole words, addresses and counts as the command encoding
states them.

Run: python3.13 -m pytest host/tests/test_bridge.py
"""

import asyncio
from collections import deque

import pytest

from acrobe_plugin.gatecap.bridge import Bridge, FrameError

ADDR_BITS = 24
DATA_BYTES_L2 = 2
BURST_LENGTH_L2 = 3      # eight words per burst, so a split is cheap to write
DESCRIPTOR_BASE = 0

WORD_BYTES = 2 ** DATA_BYTES_L2
MAX_BURST = 2 ** BURST_LENGTH_L2


class FakeLink:
    """A datagram transport answering command frames the way the
    stream-to-APB bridge does: a read returns the words it was asked for, a
    write commits them, and both end on a status byte."""

    def __init__(self):
        self.words = {}
        self.commands = []
        self.status = 0
        self.__replies = deque()

    # -- the Datagram surface the bridge uses --

    def send(self, data):
        self.commands.append(bytes(data))
        self.__replies.append(self.__answer(bytes(data)))

    def recv(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result((self.__replies.popleft(), None))
        return future

    # -- the wire --

    def __answer(self, cmd):
        opcode, cmd = cmd[0], cmd[1:]
        addr = int.from_bytes(cmd[:ADDR_BITS // 8], "little")
        cmd = cmd[ADDR_BITS // 8:]
        if opcode == 0x80:
            words = int.from_bytes(cmd, "little") + 1
            return (b"".join(self.words.get(addr + i * WORD_BYTES, 0)
                             .to_bytes(WORD_BYTES, "little")
                             for i in range(words))
                    + bytes([self.status]))
        assert opcode == 0x00, f"unknown opcode {opcode:#x}"
        for offset in range(0, len(cmd), WORD_BYTES):
            self.words[addr + offset] = int.from_bytes(
                cmd[offset:offset + WORD_BYTES], "little")
        return bytes([self.status])

    # -- what a test asserts on --

    def reads(self):
        """(address, words) of every read command sent."""
        return [(int.from_bytes(cmd[1:1 + ADDR_BITS // 8], "little"),
                 int.from_bytes(cmd[1 + ADDR_BITS // 8:], "little") + 1)
                for cmd in self.commands if cmd[0] == 0x80]

    def writes(self):
        """(address, data) of every write command sent."""
        return [(int.from_bytes(cmd[1:1 + ADDR_BITS // 8], "little"),
                 cmd[1 + ADDR_BITS // 8:])
                for cmd in self.commands if cmd[0] == 0x00]


def bridged(coroutine):
    """Run `coroutine(bridge, link)` on a bridge parameterised the way an
    identify reply parameterises it."""
    async def run():
        link = FakeLink()
        bridge = Bridge(link, params=[ADDR_BITS, DATA_BYTES_L2,
                                      BURST_LENGTH_L2, DESCRIPTOR_BASE])
        return await coroutine(bridge, link)
    return asyncio.run(run())


def seed(link, base, values):
    for index, value in enumerate(values):
        link.words[base + index * WORD_BYTES] = value


# -- reads ------------------------------------------------------------------

def test_a_word_run_is_one_burst_frame():
    # The status poll: two contiguous words, one transaction.
    async def body(bridge, link):
        seed(link, 0x200, [0x11223344, 0xC0FFEE01])
        data = await bridge.mem_read(0x200, 2 * WORD_BYTES)
        assert link.reads() == [(0x200, 2)]
        return data

    assert bridged(body) == (0x11223344).to_bytes(4, "little") \
        + (0xC0FFEE01).to_bytes(4, "little")


def test_a_long_read_is_split_at_the_burst_length():
    async def body(bridge, link):
        seed(link, 0x1000, range(1, 20))
        data = await bridge.mem_read(0x1000, 19 * WORD_BYTES)
        assert link.reads() == [(0x1000, 8), (0x1020, 8), (0x1040, 3)]
        return data

    assert bridged(body) == b"".join(
        value.to_bytes(4, "little") for value in range(1, 20))


def test_a_byte_range_is_widened_to_the_words_covering_it():
    # The wire has no sub-word read, so the range is covered by whole words
    # and trimmed on reassembly.
    async def body(bridge, link):
        seed(link, 0x40, [0x03020100, 0x07060504])
        data = await bridge.mem_read(0x41, 6)
        assert link.reads() == [(0x40, 2)]
        return data

    assert bridged(body) == bytes([1, 2, 3, 4, 5, 6])


def test_a_register_read_is_one_word():
    async def body(bridge, link):
        seed(link, 0x204, [0xDEADBEEF])
        value = await bridge.read32(0x204)
        assert link.reads() == [(0x204, 1)]
        return value

    assert bridged(body) == 0xDEADBEEF


def test_an_empty_read_reaches_no_wire():
    async def body(bridge, link):
        data = await bridge.mem_read(0x100, 0)
        assert link.commands == []
        return data

    assert bridged(body) == b""


# -- writes -----------------------------------------------------------------

def test_a_word_run_is_written_in_one_frame():
    async def body(bridge, link):
        payload = b"".join(value.to_bytes(4, "little") for value in (7, 8, 9))
        await bridge.mem_write(0x100, payload)
        assert link.writes() == [(0x100, payload)]
        return [link.words[0x100 + i * WORD_BYTES] for i in range(3)]

    assert bridged(body) == [7, 8, 9]


def test_a_long_write_is_split_at_the_burst_length():
    async def body(bridge, link):
        payload = b"".join(value.to_bytes(4, "little") for value in range(10))
        await bridge.mem_write(0x2000, payload)
        assert [addr for addr, _ in link.writes()] == [0x2000, 0x2020]
        assert [len(data) for _, data in link.writes()] == [32, 8]
        return [link.words[0x2000 + i * WORD_BYTES] for i in range(10)]

    assert bridged(body) == list(range(10))


def test_a_register_write_is_one_word():
    async def body(bridge, link):
        await bridge.write32(0x004, 0x5A5A5A5A)
        assert link.writes() == [(0x004, (0x5A5A5A5A).to_bytes(4, "little"))]
        return link.words[0x004]

    assert bridged(body) == 0x5A5A5A5A


def test_a_partial_word_write_is_refused():
    # No byte enables on the wire, and read-modify-write over registers with
    # side effects is not the bridge's call to make.
    async def body(bridge, link):
        with pytest.raises(ValueError):
            await bridge.mem_write(0x100, b"abc")
        with pytest.raises(ValueError):
            await bridge.mem_write(0x102, b"abcd")
        assert link.commands == []

    bridged(body)


# -- faults -----------------------------------------------------------------

def test_a_status_byte_fails_the_access():
    async def body(bridge, link):
        link.status = 1
        with pytest.raises(FrameError):
            await bridge.mem_read(0x300, 4 * WORD_BYTES)
        with pytest.raises(FrameError):
            await bridge.write32(0x300, 0)

    bridged(body)


def test_a_fault_fails_every_burst_of_the_read():
    # One failing burst is one failed blob: the caller gets the exception, not
    # a short or padded buffer.
    async def body(bridge, link):
        link.status = 1
        with pytest.raises(FrameError):
            await bridge.mem_read(0, (MAX_BURST + 1) * WORD_BYTES)
        assert len(link.reads()) == 2

    bridged(body)
