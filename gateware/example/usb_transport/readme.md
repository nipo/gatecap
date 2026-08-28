# Gatecap over USB Full Speed

A Tang Console board where the gatecap rack *is* the USB device: no probe, no
bridge, no cable in between. The rack holds one control/status panel, and the
whole link is three IOs and a PLL.

## What is on the board

* `clk_i` — the 50 MHz oscillator. A PLL makes 60 MHz out of it, which is the
  USB transceiver's reference clock and the rack's host clock at once. The
  PLL's lock is the rack's reset.
* `usb_dp_io`, `usb_dn_io` — D+ and D-, ordinary LVCMOS33 IOs. The
  transceiver is fabric; there is no USB chip on the board.
* `usb_dp_pull_io` — a third ordinary IO, wired to D+ through a 1.5 kΩ
  resistor. Driving it high is how the board announces a Full Speed device;
  the rack drives it, so the device appears once the design has come up.
* `s_n_i(1)` resets, `s_n_i(2)` is the panel's `s2` status bit,
  `ready_led_o` is the panel's `led` control bit, and `done_led_o` follows the
  rack's `online_o` — lit once the host has configured the device.

The panel runs on the board's 50 MHz clock rather than on the USB one, so it
keeps answering while the USB domain is held in reset.

## The core

The rack is not in the tree: `project.gbs.yaml` declares `description.yaml`
as a repository and depends on `gatecap_generated.demo_package`, so the build
generates it. That needs the gatecap host package installed (`doc/host`).

## Build the bitstream

With Gowin EDA installed and declared in gbs:

    $ gbs project build

## Program the FPGA

    $ /path/to/bin/programmer_cli --cable "USB Debugger A" --device GW5AT-60B \
        --fsFile $PWD/usb11.fs --operation_index 2

## Connecting

Plugging the board's USB socket into the host is the whole procedure. The
device enumerates as gatecap's own vendor and product, with the rack's
descriptor fingerprint as its serial number:

    $ lsusb
    Bus 000 Device 004: ID 1500:deca

    $ acrobe info adapters
      gatecap-035f79e4  1500:deca  interfaces: gatecap

`0403:6010` there is the board's own programmer, on the other socket: the
capture link is a device of its own and shares nothing with it.

The fingerprint changes whenever the rack's self-description does, so a board
reprogrammed with a different rack gets a different name, and a path that
named the old one fails by name instead of addressing a map that has moved.

The rack is the device's one interface:

    $ acrobe info enumerate -r gatecap-035f79e4/gatecap
    Node tree:
      gatecap
        bridge
          enumerator
          panel
            registers

    $ acrobe gatecap -r gatecap-035f79e4/gatecap info
    panel:
      control/status panel
      control led: 1 bit(s)
      status s2: 1 bit(s)

    $ acrobe gatecap -r gatecap-035f79e4/gatecap gui

Driving the panel from a script closes the loop. The fingerprint the rack
reports through its register file is the one the bus already gave as the
serial number, so the device the host matched and the rack it is talking to
are the same thing; and a control write lights `ready_led_o` on the board.

```python
from acrobe.root import root

async def main():
    rack = await root("gatecap-035f79e4/gatecap")
    panel = rack.child_lookup("bridge").child_lookup("panel")
    print("fingerprint: %08x" % await panel.fingerprint())
    print("status:", await panel.status_read("s2"))
    for value in (1, 0):
        await panel.control_write("led", value)
        print("led:", await panel.control_read("led"))
```

    $ acrobe run panel.py
    fingerprint: 035f79e4
    status: 0
    led: 1
    led: 0

Every output quoted under "Connecting" came from one session against this
board, over the USB link itself. The `/path/to` in the programming command is
the one thing left for you to fill in.
