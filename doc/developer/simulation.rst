Simulation as a target
======================

This chapter is for developers: it is how the repository's own benches are
driven, and how a capture setup is brought up before a bitstream exists. Skip
it if you only ever talk to real hardware.

Because the core reaches the host through frames and nothing else, a
simulator makes a perfectly good target. NSL provides a gateway that carries
the core's stream over a UDP socket, so a testbench instantiating a rack
behind it is reachable at::

   udp/127.0.0.1:4242/gatecap

— the same path shape, the same driver, the same commands as a board. The
host cannot tell the difference, which is the point.

This is useful to:

* bring up a capture setup — probe selection, names, trigger, buffer
  sizing — before any bitstream exists;
* develop and test host-side automation with a deterministic target;
* reproduce a host bug without hardware.

The repository's own benches work this way: ``gateware/example/socket*`` are
testbenches wrapping one configuration each (plain, packed, wide, RLE, edge
trigger, enumerated fields) behind a UDP gateway, and the host test suite
drives them exactly as it would drive a board. ``gateware/example/spi_example``
goes further, simulating a full SPI transactor and memory alongside the
capture core, which is where the screenshot in :doc:`../host/gui` comes from.

The generated cores of :doc:`../usage/description` have benches of their own,
one per communication mode. Each simulates the link as well as the core, so
the resource path is the one a board would take:

``socket_generated``
   A two-domain core whose single trigger correlates both captures, behind
   the same UDP gateway::

      udp/127.0.0.1:4248/gatecap

``clock_rates``
   A rack of instruments only — one clock measurer, no capture at all —
   watching three clocks more than a decade apart against a 100 MHz
   reference::

      udp/127.0.0.1:4252/gatecap

``bus_explorer``
   A rack of one bus explorer, with a stub device on its target port: a
   handful of registers, one address that always refuses and one that answers
   far too late, so error handling and the timeout are exercised as well as
   the ordinary reads and writes::

      udp/127.0.0.1:4253/gatecap

   ``host/tests/data/demo_device.svd`` is the register map of that stub;
   register it with ``acrobe gatecap bus map add gatecap-demo-device
   host/tests/data/demo_device.svd`` and the names it declares show up in the
   pane and on the command line.

``socket_spi``
   A rack of a logic analyzer and a control/status panel reached over plain
   SPI. The bench simulates the master as well: the UDP gateway feeds an NSL
   SPI transactor, whose four wires are the rack's::

      udp/127.0.0.1:4254/nsl_spi(fin=100M,fmax=10M)/cs0/gatecap

   There is no framing anywhere on that path — the host asks the SFDP opcode
   ``0x5a`` where the self-description is, and reads it there.

``spi_example_jtag``
   The SPI platform again, this time reached through a simulated test-access
   port with one device in its chain::

      udp/127.0.0.1:4249/nsl_jtag/chain/0/bnoc_continuous_transport/gatecap

``spi_example_swd``
   The same platform over a simulated serial-wire link, where the core
   carries its own debug port and the access port *is* the capture core —
   hence no ``gatecap`` segment::

      udp/127.0.0.1:4249/nsl_swd(fin=100M)/dp(ap_probe=0)

``spi_example_uart``
   The same platform over an 8n1 UART carrying HDLC frames, its line brought
   out on a TCP socket instead of a UDP one::

      tcp/127.0.0.1:4249/hdlc/addr00/gatecap

The three SPI platforms drive the probed traffic through a second connection,
``udp/127.0.0.1:4250/nsl_spi/cs0``, which is the SPI transactor producing the
transactions being captured. :doc:`../host/transports` explains the segments of
each of these paths.

Building and running them uses `gbs <https://github.com/nipo/gbs/>`_ and
GHDL.
