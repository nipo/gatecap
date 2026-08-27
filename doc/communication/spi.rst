``SPI``
=======

Four wires and any SPI master, from a USB adapter to custom
implementation in a FPGA or a MCU. The rack answers as a SPI flash
would — ``0x02`` writes, ``0x0b`` reads, four big-endian address
bytes, one turnaround byte before read data, data until chip select
rises — and the host reads its self-description from address zero, so
nothing on the wire is gatecap-specific. It adds ``spi_i`` and
``spi_o``, the pin records of ``nsl_spi.spi``, and no generic you have
to state.

This is the one mode with keys of its own:

``max_rate``
   **Required**: the highest SCK rate, in Hz, the master will ever use.

``spi_mode``
   0 to 3, the usual CPOL/CPHA numbering, defaulting to 0.

.. code-block:: yaml

   communication:
     mode: spi
     clock: la.sample        # a 100 MHz domain…
     max_rate: 10_000_000    # …so 10 MHz is the fastest legal SCK
     spi_mode: 0             # CPOL/CPHA; the default

The slave is oversampled — SCK is an ordinary input, sampled on the host clock,
with no clock-domain crossing anywhere — so the host clock must run at **at
least ten times** ``max_rate``. That is checked at elaboration, in simulation
and in synthesis alike, and a rack whose host clock rate the description does
not state (the domain it rides declares no ``frequency``) adds a
``clock_frequency_c`` generic for you to state it in Hz.

Declaring a rate the master never reaches costs nothing but the check;
declaring one it exceeds is a bug the gateware cannot detect at run time, which
is why the key has no default. Host side:
:ref:`SPI paths <host-transport-spi>`.

Instantiation
-------------

The four wires are the whole interface, gathered in the two pin records the
adapter takes. There is nothing to state at instantiation: the rates and the
SPI mode were settled by the description, so they are already in the generated
entity's generic map.

.. code-block:: vhdl

   spi_i_s.sck <= sck_s;
   spi_i_s.cs_n <= cs_n_s;
   spi_i_s.mosi <= mosi_s;
   miso_s <= nsl_io.io.to_logic(spi_o_s.miso);

   dut: spi_capture
     port map(
       reset_n_i => reset_n_s,
       spi_i => spi_i_s,
       spi_o => spi_o_s,
       la_main_clock_i => clock_s,
       la_main_reset_n_i => reset_n_s,
       la_main_state_i => state_s,
       la_main_count_i => count_s
       );

``spi_i`` gathers SCK, CS and MOSI as plain inputs; ``spi_o.miso`` is an
``nsl_io.io.tristated`` record, so the rack can share MISO with the other chip
selects of the same bus — resolve it onto a pad, or onto a multiplexer, the
way your design handles the rest of that bus. The host bursts a whole
chip-select assertion at a time and needs no generic to bound it: burst length
is the master's choice, not the core's
(:ref:`SPI paths <host-transport-spi>`).
