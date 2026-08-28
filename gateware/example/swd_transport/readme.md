# Gatecap SWD Transport demo

This demo platform features:

* A USB2 stack running in the FPGA that emulates a CDC-ACM serial port,

* A SPI master transactor connected behind that serial port,

* A Gatecap capture system that answers on two pins that implement SWD target.

## The core

The rack is not in the tree: `project.gbs.yaml` declares `description.yaml`
as a repository and depends on `gatecap_generated.spi_probe`, so the build
generates it. That needs the gatecap host package installed (`doc/host`).

## Build the bitstream

Let's suppose you have Gowin EDA correctly installed and declared in gbs:

    $ gbs project build

## Program the FPGA

We'll use programmer in CLI:

    $ /path/to/bin/programmer_cli --cable "USB Debugger A" --device GW5AT-60B --fsFile $PWD/usb2.fs --operation_index 2

## Design running

Design is now running. On usb, we have a device:

    $ lsusb
    Bus 001 Device 004: ID dead:beef

and it is seen by acrobe:

    $ acrobe info adapters
      tty-cu.usbmodemlol1  /dev/cu.usbmodemlol1  interfaces: serial

and it allows to enumerate the on-board flash chip:

    $ acrobe -vvvv info enumerate -r 'tty-cu.usbmodemlol1/serial/chunked/nsl_spi(fin=10M)/cs0/flash'
    flash: JEDEC ID: 0xef4017 (Winbond)
    flash: SFDP v1.0, 1 parameter header(s)
    flash: Size: 8MiB, page: 256B, addr: 3B
    flash: Erase: 4kiB (cmd 0x20)
    flash: Erase: 32kiB (cmd 0x52)
    flash: Erase: 64kiB (cmd 0xd8)
    Node tree:
      flash

## Gatecap access

At last, by connecting any supported SWD adapter, we can also connect to gatecap core and work with it,
first, we can ensure gatecap component tree autodiscovers correctly:

    $ acrobe -vvv info enumerate -r itap/swd
    HwRoot.itap: iTap itap (board='itap_probe_d', build='74B4ED03-513E-401B-9500-F8AB9348ED3A', date='May 14 2026'): boot mode UART_NONE
    HwRoot.itap.swd: DPIDR 0x0ba00477
    HwRoot.itap.swd.dp: DPIDR 0x0ba00477 — DPv0 (ADIv5)
    HwRoot.itap.swd.dp: TARGETID: not available (DPv0, requires DPv2+)
    HwRoot.itap.swd.dp: DP powered up (CTRL/STAT 0xf0000000)
    HwRoot.itap.swd.dp: AP0 discovered: idr=0x04770002 class=0x8 type=0x2
    HwRoot.itap.swd.dp.gatecap: CFG 0x04ed0000 (LA=0, LD=0)
    HwRoot.itap.swd.dp.gatecap: BASE 0xFFFFFFFF: no debug components (sentinel)
    HwRoot.itap.swd.dp: Chip ID: unidentified
    Node tree:
      swd
        dp
          gatecap
            enumerator
            spi.buffer
            spi.control
            spi.trigger

and then retrieve the core configuration information:

    $ acrobe gatecap -r itap/swd info
    spi.control:
      probes (26): sck, cs_n, mosi, miso, command.data[0], command.data[1], command.data[2], command.data[3], command.data[4], command.data[5], command.data[6], command.data[7], command.valid, command.last, command.ready, response.data[0], response.data[1], response.data[2], response.data[3], response.data[4], response.data[5], response.data[6], response.data[7], response.valid, response.last, response.ready
      trigger: value-mask match, up to 8191 samples, up to 1 window(s), pre-trigger capable
      sample clock: 60 MHz
      sink spi.buffer: 32-bit samples, depth 4096 samples
    spi.trigger:
      signals (4): cs_n, command.valid, command.last, command.ready
      edge/transition match (level, rising, falling)
