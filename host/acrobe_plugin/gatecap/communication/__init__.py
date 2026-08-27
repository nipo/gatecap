"""The transports a rack can be reached over, one module per interface.

Importing this package registers every mode gatecap ships on
:class:`acrobe_plugin.gatecap.generator.CommunicationRegistry`, which is where
a description's ``communication.mode`` is resolved:

* ``apb`` (:mod:`.apb`) has no adapter at all -- the rack hands its own APB
  completer out as its port pair, and the transport is the instantiating
  design's business;
* ``swd`` (:mod:`.swd`) terminates a two-wire debug link on a Mem-AP whose
  memory side is bridged to APB;
* ``spi`` (:mod:`.spi`) terminates a memory-style SPI slave any master can
  drive, and hands its accesses straight to APB;
* ``axi4_stream`` (:mod:`.stream`), ``jtag`` (:mod:`.jtag`), ``serial_hdlc``
  (:mod:`.serial_hdlc`) and ``usb`` (:mod:`.usb`) all end in the same
  stream-to-APB bridge (:mod:`.bridged`) and differ only in how the command
  bytes reach it -- straight off the entity's stream ports, out of a JTAG data
  register, off a serial line, or out of the bulk endpoint pair of a USB Full
  Speed device the rack is.

A third-party transport ships the same way, from its own acrobe plugin.
"""

# Import order is registration order, which is the order an unknown mode is
# reported against.
from .apb import ApbCommunication
from .swd import SwdCommunication
from .spi import SpiCommunication
from .stream import Axi4StreamCommunication
from .jtag import JtagCommunication
from .serial_hdlc import SerialHdlcCommunication
from .usb import UsbCommunication
from .bridged import BridgedCommunication

__all__ = ["ApbCommunication", "Axi4StreamCommunication",
           "BridgedCommunication", "JtagCommunication",
           "SerialHdlcCommunication", "SpiCommunication", "SwdCommunication",
           "UsbCommunication"]
