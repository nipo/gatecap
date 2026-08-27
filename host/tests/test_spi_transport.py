"""The SPI transport as an address space: what reaches the wire.

The transport answers the memory model (``mem_read`` / ``mem_write`` /
``read32`` / ``write32``) with SPI-flash shaped transactions. These tests hold
a fake SPI target in place of the master and assert on the transactions it
sees: the discovery blob a connection opens with, one transaction per burst,
the opcode and the big-endian address, the read turnaround byte, whole words,
and the split at ``max_burst``.

Run: python3.13 -m pytest host/tests/test_spi_transport.py
"""

import asyncio

import cbor2
import pytest

from acrobe.protocol.spi import Shift

from acrobe_plugin.gatecap.spi import SpiDiscoveryError, SpiRack

WORD_BYTES = 4
MAX_BURST = 8    # words per chip select, so a split is cheap to write


def blob(addr_bits=24, data_bytes_l2=2, base=0, signature=b"GCAP"):
    """A discovery blob as the adapter composes it."""
    return signature + cbor2.dumps([addr_bits, data_bytes_l2, base])


DISCOVERY_HEAD = (1 + SpiRack.DISCOVERY_ADDRESS_BYTES
                  + SpiRack.DISCOVERY_DUMMY_BYTES)
# What the adapter's payload opens with, filling the address and dummy slots
# of the SFDP layout so the blob lands at the data position.
DISCOVERY_FILLER = bytes(SpiRack.DISCOVERY_ADDRESS_BYTES
                         + SpiRack.DISCOVERY_DUMMY_BYTES)


class FakeTarget:
    """A SPI target answering the way the adapter does: the discovery opcode
    streams the filler and the blob from the byte slot right after it and then
    zeros, a read shifts out the words the burst covers, a write commits them,
    and the address increments per byte from the one the command carried.

    It records the mosi of every transaction, which is what a test asserts
    the wire format on."""

    def __init__(self):
        self.words = {}
        self.transactions = []
        self.discovery = blob()

    def transaction(self, *shifts):
        self.transactions.append(b"".join(bytes(shift.mosi)
                                          for shift in shifts))
        head = shifts[0].mosi
        if head[0] == SpiRack.OPCODE_DISCOVERY:
            assert len(head) == DISCOVERY_HEAD, \
                "discovery carries the SFDP address and dummy phases"
            data = shifts[1]
            # The payload streams from the byte slot right after the opcode,
            # so the filler is spent on the head's four remaining slots and
            # the data shift starts on the blob itself.
            stream = (b"\x00" + DISCOVERY_FILLER + self.discovery).ljust(
                len(head) + data.byte_count, b"\x00")
            data.miso = stream[len(head):len(head) + data.byte_count]
            future = asyncio.get_running_loop().create_future()
            future.set_result(shifts)
            return future
        opcode, addr = head[0], int.from_bytes(head[1:5], "big")
        if opcode == SpiRack.OPCODE_WRITE:
            payload = head[5:]
            for offset in range(0, len(payload), WORD_BYTES):
                self.words[addr + offset] = int.from_bytes(
                    payload[offset:offset + WORD_BYTES], "little")
        else:
            assert opcode == SpiRack.OPCODE_READ, f"opcode {opcode:#x}"
            assert len(head) == 6, "a read carries one turnaround byte"
            data = shifts[1]
            data.miso = b"".join(
                self.words.get(addr + offset, 0).to_bytes(WORD_BYTES, "little")
                for offset in range(0, data.byte_count, WORD_BYTES))
        future = asyncio.get_running_loop().create_future()
        future.set_result(shifts)
        return future

    # -- what a test asserts on --

    def reads(self):
        """(address, bytes asked for) of every read transaction."""
        return [(int.from_bytes(cmd[1:5], "big"), len(cmd) - 6)
                for cmd in self.transactions
                if cmd[0] == SpiRack.OPCODE_READ]

    def writes(self):
        """(address, data) of every write transaction."""
        return [(int.from_bytes(cmd[1:5], "big"), cmd[5:])
                for cmd in self.transactions
                if cmd[0] == SpiRack.OPCODE_WRITE]


def racked(coroutine):
    """Run ``coroutine(rack, target)`` on a transport whose master is the fake
    target, cut down to a burst length a test can write out."""
    async def run():
        target = FakeTarget()
        rack = SpiRack(target)
        rack.option_set("max_burst", MAX_BURST)
        return await coroutine(rack, target)
    return asyncio.run(run())


def seed(target, base, values):
    for index, value in enumerate(values):
        target.words[base + index * WORD_BYTES] = value


# -- discovery --------------------------------------------------------------

def test_discovery_is_an_sfdp_read_and_a_bounded_one():
    async def body(rack, target):
        payload = await rack.discovery_blob()
        assert target.transactions == [
            bytes([SpiRack.OPCODE_DISCOVERY])
            + bytes(SpiRack.DISCOVERY_ADDRESS_BYTES)
            + bytes(SpiRack.DISCOVERY_DUMMY_BYTES)
            + bytes(SpiRack.DISCOVERY_BYTES)]
        # The address and dummy phases are the SFDP layout's; the blob begins
        # at the data position, and the read is bounded because the wire
        # states no length.
        assert len(payload) == SpiRack.DISCOVERY_BYTES
        assert payload.startswith(b"GCAP")

    racked(body)


def test_the_blob_places_the_rack_where_the_target_says():
    # A base of zero is what a generated rack publishes, but nothing on the
    # host assumes it: the enumerator is spawned at whatever came off the
    # wire.
    async def body(rack, target):
        target.discovery = blob(addr_bits=28, data_bytes_l2=2, base=0x40000)
        await rack.start()
        assert (rack.base, rack.addr_bits, rack.word_bytes) == \
            (0x40000, 28, 4)
        enumerators = [child for child in rack.children
                       if child.name == "enumerator"]
        assert [child.base for child in enumerators] == [0x40000]

    racked(body)


def test_the_zeros_padding_the_bounded_read_are_not_part_of_the_blob():
    # The blob is shorter than the read, and the target pads with zeros --
    # which are valid CBOR items. Decoding stops at the end of the first one.
    async def body(rack, target):
        target.discovery = blob(base=0x100)
        assert len(target.discovery) < SpiRack.DISCOVERY_BYTES
        await rack.discover()
        assert rack.base == 0x100

    racked(body)


def test_a_target_that_does_not_answer_the_signature_is_refused():
    async def body(rack, target):
        for answer in (b"", b"\x00\x00\x00\x00", b"SFDP" + b"\x83\x18\x18",
                       blob(signature=b"GCAp")):
            target.discovery = answer
            with pytest.raises(SpiDiscoveryError,
                               match="did not answer gatecap discovery"):
                await rack.discover()
        # And nothing was assumed in its place.
        assert rack.base is None

    racked(body)


def test_a_blob_whose_cbor_does_not_decode_is_refused():
    # An item running past the bounded read is the truncation that matters:
    # the target pads with zeros, so a short definite item is simply
    # completed by them, while an item that never ends is not.
    async def body(rack, target):
        for answer in (b"GCAP" + b"\x9f",               # array with no break
                       b"GCAP" + b"\x5b" + (1 << 32).to_bytes(8, "big"),
                       b"GCAP" + cbor2.dumps([24, 2]),  # two fields
                       b"GCAP" + cbor2.dumps([24, 2, 0, 7]),
                       b"GCAP" + cbor2.dumps("rack"),
                       b"GCAP" + cbor2.dumps([24, 2, -1])):
            target.discovery = answer
            with pytest.raises(SpiDiscoveryError,
                               match="did not answer gatecap discovery"):
                await rack.discover()
        assert rack.base is None

    racked(body)


def test_an_address_space_wider_than_the_wire_is_refused():
    # The wire address is four bytes: a rack claiming more could not be
    # addressed through it at all.
    async def body(rack, target):
        target.discovery = blob(addr_bits=40)
        with pytest.raises(SpiDiscoveryError, match="40-bit address space"):
            await rack.discover()

    racked(body)


# -- the wire format --------------------------------------------------------

def test_a_read_is_an_opcode_a_big_endian_address_and_a_turnaround():
    async def body(rack, target):
        seed(target, 0x123450, [0xDEADBEEF])
        value = await rack.read32(0x123450)
        assert target.transactions[0][:6] == bytes(
            [SpiRack.OPCODE_READ, 0x00, 0x12, 0x34, 0x50, 0x00])
        return value

    assert racked(body) == 0xDEADBEEF


def test_a_write_is_an_opcode_an_address_and_the_data_with_no_turnaround():
    async def body(rack, target):
        await rack.write32(0x001020, 0x5A5A5A5A)
        assert target.transactions == [
            bytes([SpiRack.OPCODE_WRITE, 0x00, 0x00, 0x10, 0x20])
            + (0x5A5A5A5A).to_bytes(4, "little")]
        return target.words[0x001020]

    assert racked(body) == 0x5A5A5A5A


# -- reads ------------------------------------------------------------------

def test_a_word_run_is_one_chip_select_assertion():
    # The status poll: two contiguous words, one transaction.
    async def body(rack, target):
        seed(target, 0x200, [0x11223344, 0xC0FFEE01])
        data = await rack.mem_read(0x200, 2 * WORD_BYTES)
        assert target.reads() == [(0x200, 8)]
        return data

    assert racked(body) == (0x11223344).to_bytes(4, "little") \
        + (0xC0FFEE01).to_bytes(4, "little")


def test_a_long_read_is_split_at_max_burst():
    async def body(rack, target):
        seed(target, 0x1000, range(1, 20))
        data = await rack.mem_read(0x1000, 19 * WORD_BYTES)
        assert target.reads() == [(0x1000, 32), (0x1020, 32), (0x1040, 12)]
        return data

    assert racked(body) == b"".join(
        value.to_bytes(4, "little") for value in range(1, 20))


def test_a_byte_range_is_widened_to_the_words_covering_it():
    # The wire has no sub-word read, so the range is covered by whole words
    # and trimmed on reassembly.
    async def body(rack, target):
        seed(target, 0x40, [0x03020100, 0x07060504])
        data = await rack.mem_read(0x41, 6)
        assert target.reads() == [(0x40, 8)]
        return data

    assert racked(body) == bytes([1, 2, 3, 4, 5, 6])


def test_an_empty_read_reaches_no_wire():
    async def body(rack, target):
        data = await rack.mem_read(0x100, 0)
        assert target.transactions == []
        return data

    assert racked(body) == b""


# -- writes -----------------------------------------------------------------

def test_a_long_write_is_split_at_max_burst():
    async def body(rack, target):
        payload = b"".join(value.to_bytes(4, "little") for value in range(10))
        await rack.mem_write(0x2000, payload)
        assert [addr for addr, _ in target.writes()] == [0x2000, 0x2020]
        assert [len(data) for _, data in target.writes()] == [32, 8]
        return [target.words[0x2000 + i * WORD_BYTES] for i in range(10)]

    assert racked(body) == list(range(10))


def test_a_partial_word_write_is_refused():
    # No byte enables on the wire, and read-modify-write over registers with
    # side effects is not the transport's call to make.
    async def body(rack, target):
        with pytest.raises(ValueError):
            await rack.mem_write(0x100, b"abc")
        with pytest.raises(ValueError):
            await rack.mem_write(0x102, b"abcd")
        assert target.transactions == []

    racked(body)


# -- faults -----------------------------------------------------------------

def test_a_short_answer_from_the_master_fails_the_access():
    # The transport cannot pad what the master did not clock out: a caller
    # gets the failure, never a short or zero-filled buffer.
    async def body(rack, target):
        def short(*shifts):
            for shift in shifts:
                if shift.read_miso:
                    shift.miso = b"\x00"
            future = asyncio.get_running_loop().create_future()
            future.set_result(shifts)
            return future

        target.transaction = short
        with pytest.raises(IOError):
            await rack.mem_read(0x300, 4 * WORD_BYTES)

    racked(body)


def test_the_burst_length_option_is_a_word_count():
    async def body(rack, target):
        rack.option_set("max_burst", 1)
        seed(target, 0, range(4))
        await rack.mem_read(0, 4 * WORD_BYTES)
        assert [addr for addr, _ in target.reads()] == [0, 4, 8, 12]
        with pytest.raises(ValueError):
            rack.option_set("max_burst", 0)
        # An unknown option is not this node's, and leaves it alone.
        rack.option_set("baud", 9600)
        assert rack.max_burst == 1

    racked(body)


def test_the_read_shift_asks_for_exactly_the_burst():
    async def body(rack, target):
        await rack.mem_read(0x10, 3 * WORD_BYTES)
        shift = Shift(12, read_miso=True)
        assert target.reads() == [(0x10, shift.byte_count)]

    racked(body)
