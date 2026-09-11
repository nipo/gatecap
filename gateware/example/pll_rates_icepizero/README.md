# PLL rates on the iCEPi-Zero

Hardware bench for an NSL `pll_multi` realization on the ECP5: one
clock measurer watching every output of the PLL against the 50 MHz
board oscillator the PLL itself runs from, reached over the board's
FT231X UART carrying HDLC frames.

`out1` (100 MHz) and `out2` (50 MHz) are exact by construction.
`out0` asks for 512 x 44.1 kHz with 500 ppm of slack, which no integer
divider reaches exactly: the solver maps 22.580645 MHz (VCO 700 MHz,
divider 31) and that is what the host reads back.

Reference and PLL input being the same crystal, the crystal's own
error cancels out of the ratio, so the rates come back good to the
once-per-second window's 1 Hz quantum.

## Building and running

    $ gbs project build
    $ acrobe chip -r ftdi-<serial>/icepizero/jtag/chain/0 program pll_rates.bit
    $ acrobe gatecap -r 'tty-<dev>/serial(rate=1000000)/hdlc/addr00/gatecap' rates
    clock,rate_hz
    out0,22580645
    out1,100000000
    out2,50000000

The JTAG load leaves the FT231X in bitbang mode; the UART datapath
needs it back in its serial mode, which opening the tty does not do by
itself. Unplugging and replugging the board, or resetting the FTDI
mode, is what separates a JTAG load from the first UART session.

## The LED

Lit once the PLL reports lock.
