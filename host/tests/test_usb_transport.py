"""The USB transport: the framing, and how a rack is found on the device.

``UsbFramed`` turns the batcher's datagram ops into bulk transfers, and the
frame boundaries are USB's own -- a short packet ends a datagram, a
zero-length one ends a datagram that is a whole number of packets. These tests
hold fake endpoints in place of the pair and assert on the transfers they see.
The adapter's side is the descriptor walk: one vendor-defined interface, one
bulk pair, and what happens to a device carrying gatecap's ids without being a
rack.

Run: python3.13 -m pytest host/tests/test_usb_transport.py
"""

import asyncio

import pytest

from acrobe_plugin.gatecap.usb import GatecapUsbAdapter, UsbFramed

MPS = 64


class FakeOut:
    """A bulk OUT endpoint recording every transfer it is handed."""

    def __init__(self, mps=MPS):
        self.mps = mps
        self.transfers = []

    async def write(self, data, timeout=None):
        self.transfers.append(bytes(data))
        return len(data)


class FakeIn:
    """A bulk IN endpoint answering from a queue of packets, the way the
    device would: the host asks for a whole number of packets and the transfer
    ends on the first short one."""

    def __init__(self, packets=(), mps=MPS):
        self.mps = mps
        self.packets = list(packets)
        self.requests = []

    async def read(self, size=0, timeout=None):
        self.requests.append(size)
        out = bytearray()
        while self.packets:
            packet = self.packets.pop(0)
            out += packet
            if len(packet) < self.mps or len(out) >= size:
                break
        return bytes(out)

    def read_sync(self, size=0, timeout=None):
        return b""


def packets_of(data):
    """``data`` cut into the packets the device would send for it, the
    zero-length terminator included where the length calls for one."""
    chunks = [data[at:at + MPS] for at in range(0, len(data), MPS)]
    if not chunks or len(chunks[-1]) == MPS:
        chunks.append(b"")
    return chunks


def linked(coroutine, packets=()):
    """Run ``coroutine(link)`` on a channel over fake endpoints."""
    async def run():
        link = UsbFramed(FakeOut(), FakeIn(packets), MPS)
        try:
            return await coroutine(link)
        finally:
            await link.stop()
    return asyncio.run(run())


# -- what a frame looks like on the wire ------------------------------------

def test_a_frame_shorter_than_a_packet_is_one_transfer():
    async def body(link):
        await link.send(b"\xff\x00")
        assert link.ep_out.transfers == [b"\xff\x00"]

    linked(body)


def test_a_packet_multiple_frame_is_terminated_by_a_zero_length_packet():
    # Without it the device is still waiting for the rest of the frame, and
    # libusb adds none of its own.
    async def body(link):
        await link.send(b"\xa5" * MPS)
        assert link.ep_out.transfers == [b"\xa5" * MPS, b""]

    linked(body)


def test_an_empty_frame_is_one_zero_length_packet_and_no_more():
    # It is its own terminator; a second one would be a second empty frame.
    async def body(link):
        await link.send(b"")
        assert link.ep_out.transfers == [b""]

    linked(body)


def test_a_frame_is_gathered_until_the_short_packet_that_ends_it():
    payload = bytes(range(256)) * 2 + b"tail"

    async def body(link):
        data, context = await link.recv()
        assert data == payload
        assert context is None

    linked(body, packets_of(payload))


def test_a_frame_ending_on_a_packet_boundary_reads_its_terminator():
    payload = b"\x5a" * (2 * MPS)

    async def body(link):
        assert (await link.recv())[0] == payload

    linked(body, packets_of(payload))


def test_transfers_are_a_whole_number_of_packets():
    # A shorter one could end on a full packet, and a full packet is exactly
    # what does not end a frame.
    async def body(link):
        await link.recv()
        assert link.ep_in.requests == [link.read_size]
        assert link.read_size % MPS == 0

    linked(body, packets_of(b"\x01"))


def test_frames_are_delivered_in_the_order_they_were_asked_for():
    async def body(link):
        first = link.recv()
        second = link.recv()
        assert (await first)[0] == b"first"
        assert (await second)[0] == b"second"

    linked(body, packets_of(b"first") + packets_of(b"second"))


def test_a_failing_transfer_reaches_the_caller_and_the_reader_survives_it():
    async def body(link):
        async def refuse(size=0, timeout=None):
            raise IOError("the device went away")

        link.ep_in.read = refuse
        with pytest.raises(IOError):
            await link.recv()
        link.ep_in.read = FakeIn(packets_of(b"back")).read
        assert (await link.recv())[0] == b"back"

    linked(body)


# -- finding the rack on the device -----------------------------------------

class FakeEndpoint:
    def __init__(self, address, attributes=2, max_packet_size=MPS):
        self.address = address
        self.attributes = attributes
        self.max_packet_size = max_packet_size


class FakeSetting:
    def __init__(self, classes, endpoints):
        self.classes = classes
        self.endpoints = endpoints

    def __iter__(self):
        return iter(self.endpoints)


class FakeDevice:
    """Just enough of an opened device for the interface walk: a
    configuration of alternate-setting lists, indexed by configuration
    value."""

    def __init__(self, interfaces):
        self.configuration = 1
        self.descriptor = {1: interfaces}


def found(interfaces):
    walk = getattr(GatecapUsbAdapter, "_GatecapUsbAdapter__find_interface")
    return walk(FakeDevice(interfaces))


def test_the_vendor_interface_and_its_bulk_pair_are_looked_up():
    audio = [FakeSetting((0x01, 0x02), [FakeEndpoint(0x83, attributes=1)])]
    vendor = [FakeSetting((0xFF, 0xFF),
                          [FakeEndpoint(0x82), FakeEndpoint(0x02)])]
    assert found([audio, vendor]) == (1, 0x02, 0x82, MPS)


def test_a_device_with_the_ids_and_no_rack_behind_them_is_refused():
    # Nothing stops a third party from shipping 1500:deca; what makes a rack
    # is the interface, so its absence is an error and not a wrong guess.
    control_only = [FakeSetting((0xFF, 0xFF),
                                [FakeEndpoint(0x81, attributes=3)])]
    with pytest.raises(IOError):
        found([control_only])
