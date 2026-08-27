Connecting over the different transports
========================================

How the core is reached is decided in the gateware — a frame interface fed by
a transport you wire yourself, or one of the links the generated core brings
with it (:doc:`../usage/description`). On the host, that choice shows up in
one place only: the resource path. This page gives the path for each of them,
and what the segments in it mean.

Anatomy of a resource path
--------------------------

A resource path is a ``/``-separated walk down acrobe's device tree. Each
segment names a node spawned on the one before it, and the walk falls into
three parts::

   udp/127.0.0.1:4249/nsl_jtag/chain/0/bnoc_continuous_transport/gatecap
   \________________/ \________________________________________/ \_____/
        adapter                    link and framing              handler

**The adapter** is where the path starts: a debug probe by name, a serial
port, a network endpoint (``udp/<host>:<port>``, ``tcp/<host>:<port>``).
``acrobe info adapters`` lists what is currently attached.

**The link and framing** segments are protocol layers, each stacked on the
one before: a JTAG chain and the data register selected in it, an SWD debug
port and its access port, an HDLC framer over a byte pipe. This is the part
that differs between backends, and the only part.

**The handler** is ``gatecap``: it says "a capture core answers here", and
resolving it starts discovery. The plugin registers it three times — on
acrobe's datagram layer, for every link that carries frames; as a chip type on
a SPI chip select, for a rack that is addressed as a memory (`SPI`_ below); and
as the one interface of a rack that is itself a USB device (`USB`_ below). One
backend has no such segment at all — see `SWD`_.

Any segment takes options in parentheses, ``name(key=value)``:
``nsl_swd(fin=100M)`` states the wire clock of a simulated SWD transactor,
``dp(ap_probe=0)`` restricts the access-port scan to port 0.

``acrobe info enumerate`` resolves a path and prints the tree it reached,
which is the quickest way to tell a wrong segment from a silent target::

   $ acrobe info enumerate -r udp/127.0.0.1:4249/nsl_jtag/chain/0/bnoc_continuous_transport/gatecap
   Node tree:
     gatecap
       bridge
         enumerator
         la
           spi.buffer
           spi.control
           spi.trigger

``la`` there is the instrument the core holds — a logic analyzer, named by the
description — and the blocks under it are what :doc:`cli` and :doc:`gui` then
drive.

.. _host-transport-stream:

Stream transports
-----------------

A core with the frame interface — a rack generated with
``mode: axi4_stream`` — is carried by whatever transport you put in front of
it, and the path ends in ``gatecap`` as soon as that transport hands acrobe
framed messages::

   udp/127.0.0.1:4242/gatecap

That is the simulation bench shape (:doc:`../developer/simulation`). Nothing
above the transport is specific to the link: the same path with a different
head names the same core over a different wire.

.. _host-transport-jtag:

JTAG
----

The core sits on a user data register of the FPGA's own test-access port, so
the path walks the chain to reach it::

   udp/127.0.0.1:4249/nsl_jtag/chain/0/bnoc_continuous_transport/gatecap

Reading it from the left:

``chain``
   Scans the JTAG chain and attaches one node per device found. Each device is
   named by its position, so ``0`` is the first — and, on a board with a lone
   FPGA, the only one::

      $ acrobe info enumerate -r udp/127.0.0.1:4249/nsl_jtag/chain
      Node tree:
        chain
          LFE5UM-25

   The IDCODE is matched against acrobe's part database, which is what tells
   the host the instruction-register length and which instruction selects a
   user data register. An unrecognised part therefore stops the walk here.

``bnoc_continuous_transport``
   The application riding that user data register: the framed FIFO the capture
   core's transport claims. It is what turns shifted bits into the frames the
   handler expects.

``gatecap``
   The capture core itself, as on any other framed link.

Over a real probe only the adapter changes; the three segments after it are
the same::

   <probe>/jtag/chain/0/bnoc_continuous_transport/gatecap

Probes that expose a ``jtag`` interface to acrobe include FTDI MPSSE-based
cables, J-Link, ST-Link and XDS110; ``acrobe info adapters`` names the ones
plugged in. The capture then shares the programming cable, and costs no pin.

.. _host-transport-swd:

SWD
---

A core generated with ``mode: swd`` carries a whole debug port of its own, so
it is reached the way any debug target is — through the wire, its debug port
and an access port::

   udp/127.0.0.1:4249/nsl_swd(fin=100M)/dp(ap_probe=0)

Two things set this apart from the other backends.

**There is no** ``gatecap`` **segment.** The core's Mem-AP answers with an
identification register of ``0x04ed0001``, and the host plugin registers a
handler against that value. The access port *is* the capture core: the walk
recognises it by its own identity and attaches the blocks under it::

   $ acrobe info enumerate -r "udp/127.0.0.1:4249/nsl_swd(fin=100M)/dp(ap_probe=0)"
   Node tree:
     dp
       gatecap
         enumerator
         la
           spi.buffer
           spi.control
           spi.trigger

**The walk finds it on its own.** Naming the wire is enough — the debug port
and the access ports behind it are discovered, so the tail of the path is
optional and only narrows the search. Both of these name the same core::

   acrobe gatecap -r "udp/127.0.0.1:4249/nsl_swd(fin=100M)/dp(ap_probe=0)" info
   acrobe gatecap -r "udp/127.0.0.1:4249/nsl_swd(fin=100M)" info

On hardware that means two pins on the FPGA and a stock debug probe on the
other end — CMSIS-DAP/DAPLink, ST-Link and J-Link probes all expose an ``swd``
interface — with the path reduced to ``<probe>/swd``.

``gateware/example/swd_transport`` is such a board demo: a Gowin part running
a USB device stack, a SPI transactor behind it, and a generated capture core
answering on ``swclk``/``swdio`` while probing the SPI wires and the
transactor's framed buses. Its readme walks the whole session, from
programming the bitstream through the probe's discovery of the access port to
``acrobe gatecap -r <probe>/swd info``.

.. _host-transport-serial:

Serial (HDLC)
-------------

A core generated with ``mode: serial_hdlc`` speaks HDLC-delimited frames over
an 8n1 UART, and acrobe brings the matching framing: ``hdlc`` stacks frame
delimiting, byte stuffing and the frame check sequence on a byte pipe, and
``addr<NN>`` adds the two-byte address/control header the gateware's framer
inserts — address ``00`` for the core::

   tcp/127.0.0.1:4249/hdlc/addr00/gatecap

That is the path over the simulation bench, whose TCP socket stands in for the
serial line (:doc:`../developer/simulation`). Over a real port the two framing
segments are unchanged and stack on the port's byte pipe instead —
``tty-<port>/serial/…`` — with one caveat: the line rate is not expressible in
the path. acrobe configures a serial port through its serial-port interface,
so the port must already run at the core's ``baud_rate_c``.

.. _host-transport-spi:

SPI
---

A core generated with ``mode: spi`` is a chip on a SPI bus, and is named like
one: the path walks the master, then the chip select it hangs on, and ends in
``gatecap``::

   udp/127.0.0.1:4254/nsl_spi(fin=100M,fmax=10M)/cs0/gatecap

Reading it from the left:

``nsl_spi``
   The master. Here it is a transactor inside a simulation
   (:doc:`../developer/simulation`), whose options state the clock it is fed
   (``fin``) and the fastest SCK it may generate (``fmax``). What has to stay
   under the ``max_rate`` the rack was built with is the SCK that comes out:
   the divider only ever rounds down, so asking for the rack's declared rate is
   asking for the fastest legal link — 10 MHz out of a 100 MHz input here,
   exactly the 10 MHz the bench's rack declares. Over a USB bridge this segment
   is the bridge's own SPI interface instead::

      <probe>/spi/cs0/gatecap

``cs0``
   The chip select the rack answers on — the same node any other chip on that
   bus would be reached through.

``gatecap``
   The rack. It is registered as a *chip type*, so it sits exactly where a
   flash chip would sit in the tree, and takes one option::

      udp/127.0.0.1:4254/nsl_spi(fin=100M,fmax=10M)/cs0/gatecap(max_burst=16)

   ``max_burst`` is the largest number of 32-bit words the host puts in one
   chip-select assertion, 64 by default. Lower it for a master that caps its
   transfers; nothing on the wire depends on it.

There is no framing and no handshake on this path: the rack is a memory, and a
read or write burst is one chip-select assertion — opcode, address, a
turnaround byte on reads, then data. A status poll of two adjacent registers
therefore costs a single assertion, as it does over a frame.

Discovery is the first assertion of a connection, and it is a flash chip's
SFDP read: the host clocks opcode ``0x5a``, three address bytes and a dummy
byte, then reads back the four ASCII bytes ``GCAP`` followed by a short CBOR
array holding the rack's address width, its word size and the address its
self-description sits at. The address bytes are written as zero and the rack
ignores them; they are there so the transaction is the standard one.
Everything after that is read at the address the rack named. A chip select
that answers something else is not a gatecap rack, and the connection fails
saying so rather than reading address zero on the off chance.

The FTDI form above is structurally the same walk as the simulated one and is
built from the same node types, but it has not yet been exercised against
hardware; the simulated master is what the test suite drives.

.. _host-transport-usb:

USB
---

A core generated with ``mode: usb`` is not behind an adapter — it *is* the
adapter. It enumerates as vendor ``1500``, product ``deca``, the plugin
recognises those ids, and the device shows up in the adapter list like a probe
would::

   $ acrobe info adapters
     gatecap-a1b2c3d4  1500:deca  interfaces: gatecap

The name after the dash is the device's serial number, which the rack sets to
its own descriptor fingerprint. Two boards on one bus are therefore distinct
by name, and a board reprogrammed with a different rack changes name — so a
path that used to work fails by name instead of silently addressing a map that
has moved.

The path is the adapter and its one interface, and there is nothing to look
up to write it::

   gatecap-a1b2c3d4/gatecap

``gatecap-<fingerprint>``
   The device. Nothing is opened until the interface below it is summoned; a
   bus scan only reads the serial.

``gatecap``
   The rack. The device exposes one vendor-defined interface holding one bulk
   endpoint pair, and that pair carries the same command frames every other
   framed link carries, so everything from here inwards — the bridge, the
   descriptor walk, the instruments — is what the other transports reach too::

      $ acrobe info enumerate -r gatecap-a1b2c3d4/gatecap
      Node tree:
        gatecap
          bridge
            enumerator
            panel
              registers

Frame boundaries are USB's own: a datagram ends on a short packet, and one
whose length is a whole number of packets ends on a zero-length one. There is
no framing of gatecap's above the endpoint, and no option on the path — the
host's read budget is the ``burst_length_l2_c`` the rack was built with, and
it is announced by the core.

.. _host-transport-apb:

APB
---

``mode: apb`` is not a host transport. The core drops its front end and
becomes an APB completer on your design's own bus, to be driven by whatever
requester already lives there — a CPU, a testbench, another bridge. There is
no resource path for it, and reaching it from the host means giving that bus a
link of its own. See :doc:`../usage/description`.
