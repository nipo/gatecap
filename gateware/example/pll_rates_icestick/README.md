# PLL rates on the iCEstick

Hardware bench for an NSL `pll_multi` realization on the iCE40: one
clock measurer watching the PLL's output against the 12 MHz board
oscillator the PLL itself runs from, reached over the FT2232H port-B
SPI.

The request -- 20 MHz with 2% of slack -- is deliberately not
reachable on the iCE40's power-of-two output divider. The solver maps
19.875 MHz (VCO 636 MHz, feedback 53, output divider 32) and that is
the rate the host reads back: the bench proves the realized clock is
the one the solver predicted, not the one asked for.

Reference and PLL input being the same crystal, the crystal's own
error cancels out of the ratio.

## Building and running

    $ gbs project build
    $ acrobe chip -r icestick/ice40 program pll_rates.bin
    $ acrobe gatecap -r 'icestick/spi-io(fmax=1M)/cs0/gatecap' rates
    clock,rate_hz
    out0,19873792

`fmax=1M` is not optional: the MPSSE engine clocks at its 30 MHz
hardware ceiling unless told otherwise, and the rack's SPI slave
oversamples SCK with its own 12 MHz clock. The rate the description
declares (`max_rate`) is what the gateware is built for; the host
cannot learn it before discovery, so it has to be stated on the
resource path.

The measured rate is a multiple of the 1.024 kHz quantum, and the
measurement window is a whole number of reference cycles, which is
worth another 64 ppm -- hence 19873792 rather than 19875000.

The HX1K is small enough that the rack's size matters: `update_hz_l2`
is 10 (rather than the once-per-second default) and `max_rate` 24 MHz,
which is what makes the design fit.

## The green LED

Lit once the PLL reports lock.
