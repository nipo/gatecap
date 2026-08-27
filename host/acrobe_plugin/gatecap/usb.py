"""A rack that is a USB device of its own, as an acrobe adapter.

A gatecap rack reached over USB is not behind a probe: it *is* the device on
the bus. It enumerates as vendor 0x1500, product 0xdeca with one
vendor-defined interface, and its serial-number string is the descriptor
fingerprint, so the bus already says what the thing is and which rack it
holds. Plugging one in is the whole configuration::

    $ acrobe info adapters
      gatecap-a1b2c3d4  1500:deca  interfaces: gatecap

and the rack is at ``gatecap-<fingerprint>/gatecap``, a path derived from the
device rather than written by hand.

The interface holds one bulk endpoint pair and nothing else. Frames are
delimited the way USB delimits them: a transfer ends on a short packet, and a
frame whose length is a whole number of packets is followed by a zero-length
one. :class:`UsbFramed` is that framing and no more -- one datagram in, one
datagram out -- which is exactly what the frame bridge in :mod:`.bridge`
speaks, so everything above this module is the transport-independent stack.
"""

import asyncio
from collections import deque

from acrobe.adapter.model import Adapter, AdapterInfo, adapter_db
from acrobe.db import NoMatch
from acrobe.lifecycle import cancel_shutdown, on_shutdown
from acrobe.protocol.datagram import Datagram, Recv, Send

from .bridge import GatecapFramed


class UsbFramed(Datagram):
    """Datagram channel over one bulk endpoint pair.

    Sends and receives each run in a background task: the batcher's
    ``flush_ops`` may not await IO, so it only queues, and the loops below own
    the endpoints. That also keeps the two directions independent, which the
    request/response pattern above needs -- the receive of a reply is posted
    with the command that asks for it, and the IN transfer may well reach the
    device before the OUT one does. USB makes that harmless: the device NAKs
    an IN it has nothing for, and the host controller retries until the
    transfer's own deadline."""

    # Bytes asked for in one IN transfer. A whole number of packets, so a
    # transfer returning less than this has ended on a short packet -- which
    # is the only way a frame boundary is conveyed.
    READ_PACKETS = 64
    # Deadline of one transfer, milliseconds. A rack answers a command in
    # microseconds; what this really bounds is how long a device that has
    # stopped answering is waited for.
    TIMEOUT_MS = 5000
    # Deadline of the drain at open, milliseconds: long enough for a packet
    # already queued to arrive, short enough not to be felt.
    DRAIN_MS = 20

    def __init__(self, ep_out, ep_in, mps, name="usb"):
        super().__init__(name)
        self.ep_out = ep_out
        self.ep_in = ep_in
        self.mps = mps
        self.read_size = self.READ_PACKETS * mps
        self.__sends = deque()
        self.__recvs = deque()
        self.__writer = None
        self.__reader = None

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Send):
                self.__sends.append((op.data, future))
            elif isinstance(op, Recv):
                self.__recvs.append(future)
            else:
                raise TypeError(
                    f"{type(self).__name__} carries datagrams, not "
                    f"{type(op).__name__}")
        if self.__sends:
            self.__writer = self.__ensure(self.__writer, self.__writer_loop)
        if self.__recvs:
            self.__reader = self.__ensure(self.__reader, self.__reader_loop)

    @staticmethod
    def __ensure(task, body):
        if task is None or task.done():
            return asyncio.ensure_future(body())
        return task

    async def __writer_loop(self):
        while self.__sends:
            data, future = self.__sends.popleft()
            try:
                await self.__write_frame(data)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- forward to caller
                self.__fail(future, exc)
                continue
            if future is not None and not future.done():
                future.set_result(None)

    async def __reader_loop(self):
        while self.__recvs:
            future = self.__recvs.popleft()
            try:
                frame = await self.__read_frame()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- forward to caller
                self.__fail(future, exc)
                continue
            if future is not None and not future.done():
                future.set_result((frame, None))

    @staticmethod
    def __fail(future, exc):
        if future is not None and not future.done():
            future.set_exception(exc)

    async def __write_frame(self, data):
        """One datagram out. A frame that is a whole number of packets ends on
        a zero-length one: without it the device is still waiting for the rest
        of the frame. An empty frame is that zero-length packet and nothing
        else -- a second one would be a second empty frame."""
        await self.ep_out.write(bytes(data), timeout=self.TIMEOUT_MS)
        if data and len(data) % self.mps == 0:
            await self.ep_out.write(b"", timeout=self.TIMEOUT_MS)

    async def __read_frame(self):
        """One datagram in, gathered until the short packet that ends it."""
        frame = bytearray()
        while True:
            chunk = await self.ep_in.read(self.read_size,
                                          timeout=self.TIMEOUT_MS)
            frame += chunk
            if len(chunk) < self.read_size:
                return bytes(frame)

    def drain(self):
        """Discard whatever the endpoint still holds from a previous session.

        A host that died between a command and its reply leaves the reply in
        the device; read it now and the frames would be one apart for the rest
        of the connection."""
        from ausb.exception import TransferTimeout
        try:
            while True:
                if not self.ep_in.read_sync(self.read_size,
                                            timeout=self.DRAIN_MS):
                    return
        except TransferTimeout:
            return

    async def stop(self):
        for task in (self.__writer, self.__reader):
            if task is not None:
                task.cancel()
        self.__writer = None
        self.__reader = None


@adapter_db.register(AdapterInfo("gatecap", vid=0x1500, pid=0xdeca))
class GatecapUsbAdapter(Adapter):
    """A gatecap rack on the USB bus.

    The adapter is the device; its one interface is the rack. The handle is
    opened when that interface is first summoned, as every other USB adapter
    does it, so a bus scan costs nothing but the transient open the enumerator
    makes to read the serial."""

    # The interface's name, and the child the rack is reached as. The device
    # exposes one thing and it is a rack, so the name states that rather than
    # the wire it rides.
    RACK = "gatecap"
    VENDOR_CLASS = 0xFF
    BULK_ATTRIBUTE = 2

    def __init__(self, name, info=None, descriptor=None):
        super().__init__(name, info, descriptor)
        self.device = None
        self.datagram = None
        self.__interface = None

    def child_hints(self):
        return [self.RACK]

    async def child_spawn(self, name):
        if name != self.RACK:
            raise NoMatch("interface", name)
        await self.__ensure_open()
        return GatecapFramed(self.datagram, name=self.RACK)

    async def __ensure_open(self):
        if self.datagram is not None:
            return
        from ausb.handle import BulkInEndpoint, BulkOutEndpoint
        device = self.descriptor.open()
        interface, out_address, in_address, mps = self.__find_interface(device)
        self.__release_kernel(device, interface)
        device.handle.claimInterface(interface)
        datagram = UsbFramed(BulkOutEndpoint(device, out_address, mps),
                             BulkInEndpoint(device, in_address, mps), mps)
        datagram.drain()
        self.device = device
        self.__interface = interface
        self.datagram = datagram
        on_shutdown(self.close)

    @staticmethod
    def __release_kernel(device, interface):
        import usb1
        try:
            device.handle.detachKernelDriver(interface)
        except (usb1.USBErrorNotFound, usb1.USBErrorNotSupported,
                usb1.USBErrorAccess):
            pass

    @classmethod
    def __find_interface(cls, device):
        """The vendor-defined interface and its bulk pair, as
        ``(interface, out address, in address, packet size)``. The device
        exposes exactly one, but it is looked up rather than assumed: the
        interface number is not part of what the vendor and product ids
        promise."""
        configuration = device.descriptor[device.configuration]
        for index, interface in enumerate(configuration):
            setting = interface[0]
            if setting.classes[0] != cls.VENDOR_CLASS:
                continue
            found = cls.__bulk_pair(setting)
            if found is not None:
                return (index,) + found
        raise IOError(
            "no vendor-defined interface with a bulk endpoint pair: this "
            "device answers to gatecap's vendor and product ids without "
            "being a rack")

    @classmethod
    def __bulk_pair(cls, setting):
        out_address = in_address = None
        mps = 0
        for endpoint in setting:
            if (endpoint.attributes & 0x3) != cls.BULK_ATTRIBUTE:
                continue
            if endpoint.address & 0x80:
                if in_address is None:
                    in_address = endpoint.address
                    mps = max(mps, endpoint.max_packet_size)
            elif out_address is None:
                out_address = endpoint.address
                mps = max(mps, endpoint.max_packet_size)
        if out_address is None or in_address is None:
            return None
        return out_address, in_address, mps

    async def close(self):
        if self.datagram is None:
            return
        cancel_shutdown(self.close)
        await self.datagram.stop()
        try:
            self.device.handle.releaseInterface(self.__interface)
        finally:
            self.device.handle.close()
        self.datagram = None
        self.device = None
        self.__interface = None
