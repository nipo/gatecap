library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_spi;

package adapter_spi is

  -- Communication adapter for a plain SPI link: a memory-style slave any SPI
  -- master can drive, and the APB requester below carrying its accesses into
  -- the rack.
  --
  -- One transaction per chip-select assertion, SPI-flash shaped:
  --
  --   read      [0x0b, address(address_bytes_c, big-endian), dummy] -> data...
  --   write     [0x02, address(address_bytes_c, big-endian), data...]
  --   discovery [0x5a, address(3, big-endian), dummy]
  --                 -> "GCAP", CBOR geometry, then zeros
  --
  -- The opcode is the flash fast-read, page-program and SFDP byte
  -- respectively; 0x5a answers the discovery blob, 0x02 writes and any other
  -- opcode reads. The blob is what a host reads first: it holds the address
  -- of the descriptor, so nothing in the address space has to be assumed to
  -- find the rack. Discovery carries the SFDP address and dummy phases so a
  -- master needs no layout of its own for it; the address is ignored, the
  -- payload always starting at its first byte, and a master that keeps
  -- clocking past its end reads zeros. The read and write address is a byte
  -- address of the rack's APB space, and it increments per data byte, so a
  -- burst is as long as the master keeps the chip select asserted. The read
  -- carries one dummy byte between address and data: that turnaround is what
  -- covers the APB read latency, since the link has no backpressure.
  --
  -- Data bytes are little-endian within an APB word -- the byte at a
  -- word-aligned address carries bits 7 downto 0 of that word -- which is the
  -- byte order every other gatecap link uses.
  --
  -- Accesses are word-aligned and a whole number of words. A burst cut in the
  -- middle of a word discards the incomplete word: a write of it never
  -- happens, and a read of it is simply data the master did not clock out. A
  -- write burst that does not start on a word boundary drops bytes until the
  -- next one. Reads are prefetched one word ahead, so a burst reads one word
  -- past its last: every completer in a rack answers reads without side
  -- effects, which is what makes that safe.
  --
  -- The whole adapter runs on clock_i and samples the SPI pins there. The
  -- shift register underneath takes an SCK edge after two consecutive
  -- samples of it, shifts the bit out a cycle later, and needs one cycle
  -- more at a byte boundary; the edge lands anywhere inside a clock cycle on
  -- top of that. So an SCK half period must span five clock cycles: clock_i
  -- runs at least ten times the highest SCK rate the master will use, which
  -- is what the two rate generics state and the elaboration assert checks.
  component spi_adapter is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      -- Rate of clock_i in Hz.
      clock_frequency_c : natural;
      -- Highest SCK rate in Hz the master is allowed to use. Together with
      -- the clock rate it pins the oversampling ratio, and that ratio is what
      -- bounds the APB latency this link can absorb.
      sck_max_rate_c : natural;
      -- Address bytes on the wire. Four by default: the wire format then
      -- states no rack geometry, which it cannot anyway -- the geometry is in
      -- the descriptor, and the descriptor is read through this very address.
      address_bytes_c : natural range 1 to 4 := 4;
      -- SPI mode, (cpol, cpha) as the usual 0 to 3 encoding.
      spi_mode_c : natural range 0 to 3 := 0;
      -- Byte address of the descriptor, as the discovery blob states it.
      descriptor_base_c : natural := 0
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      -- SPI slave pins: chip select, clock and the two data lines, the
      -- outgoing one with its output enable.
      spi_i : in nsl_spi.spi.spi_slave_i;
      spi_o : out nsl_spi.spi.spi_slave_o;

      apb_o : out nsl_amba.apb.master_t;
      apb_i : in nsl_amba.apb.slave_t
      );
  end component;

end package;
