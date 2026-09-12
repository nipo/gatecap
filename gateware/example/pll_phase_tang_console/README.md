# PLL phase shift on the Tang Console 60k

Hardware bench for the phase shift of the GW5A PLLA, measured in the
gateware rather than taken on trust.

One `pll_multi` produces four clocks of 10 MHz, three of them shifted
by an eighth, three eighths and five eighths of a cycle, and a fifth
clock at twenty-four times that rate to measure them with. An eighth
of a cycle is three cycles of the measuring clock, so the counts are
expected to be 3, 9 and 15.

`phase_meter` counts measuring-clock cycles from the unshifted clock's
rising edge to each shifted one's. Every clock comes from the same
PLL, so sampling one with another is deterministic rather than a race;
the measuring clock carries half a cycle of shift of its own so that
the unshifted clock's edges fall between its own rather than onto
them.

## Building and running

    $ gbs project build
    $ programmer_cli --location <n> --device GW5AT-60B \
        --operation_index 2 -f pll_phase.fs --frequency 15MHz

The two board LEDs answer without a host: `ready` states the meter has
seen every clock rise, and `done` is lit when the counts are 3, 9, 15
-- a shift that delays -- or blinks when they are 21, 15, 9, which is
what a shift the other way would give.

For the counts themselves, over the USB2 data port:

    $ acrobe gatecap -r tty-<dev>/serial/chunked/gatecap info
    phase:
      control/status panel
      status offset1: 8 bit(s)
      ...

and reading `offset1`..`offset3` off the panel gives 3, 9, 15.

## What it settled

The datasheet states `CLKOUTx_PE_COARSE` and `CLKOUTx_PE_FINE` without
saying which way they move an output. Measured on a GW5AT-60B, the
counts rise with the requested phase and match to the cycle, so the
shift is a delay, and `CLKOUTx_PE_COARSE` counts whole VCO cycles with
`CLKOUTx_PE_FINE` counting eighths.
