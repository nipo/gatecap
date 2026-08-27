library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, nsl_spi, gatecap;
use nsl_data.bytestream.all;
use nsl_data.endian.all;
use nsl_data.text.all;
use gatecap.testing.all;

-- spi_adapter driven by a bit-banged SPI master over a RAM completer.
--
-- This link carries memory accesses of its own, so there is no command frame
-- to build: every transaction is an opcode, a big-endian address, a dummy
-- byte on reads, and data until the chip select is released. The bench drives
-- the wire itself and checks what comes back on MISO, which is what a host
-- SPI master will do. It takes the adapter harness' bus configuration and
-- completer from gatecap.testing, but states its own sequence: burst length
-- is chip-select framing here, so the interesting cases -- the turnaround
-- byte, a burst cut in the middle of a word, the slowest legal clock ratio at
-- every phase -- are ones no other link has.
entity tb is
end entity;

architecture sim of tb is

  constant clock_half_period_c : time := 10 ns;
  -- What the adapter is told about its clock, and the highest SCK rate the
  -- bench uses. The ratio between them is exactly the minimum the adapter
  -- accepts, so the fast transactions below run with no margin at all.
  constant clock_frequency_c : natural := 50000000;
  constant sck_max_rate_c : natural := 5000000;
  -- Half an SCK period, comfortably slow and at the minimum ratio, where it
  -- is exactly five clock periods.
  constant slow_half_c : time := 130 ns;
  constant fast_half_c : time := 100 ns;

  constant read_opcode_c : byte := x"0b";
  constant write_opcode_c : byte := x"02";
  -- The SFDP opcode and its layout: three address bytes and one dummy byte
  -- between opcode and data. Identification, 0x9f, is no longer reserved and
  -- must read memory like any other opcode.
  constant discovery_opcode_c : byte := x"5a";
  constant discovery_filler_c : natural := 3 + 1;
  constant identify_opcode_c : byte := x"9f";
  -- What discovery must answer at the data position, byte for byte, for the
  -- generics this bench elaborates the adapter with: "GCAP", then a
  -- three-element CBOR array (0x83) holding the address width (12), the word
  -- size log2 (2) and the descriptor base (0).
  constant discovery_c : byte_string :=
    from_hex("47434150" & "83" & "0c" & "02" & "00");
  -- Opcode, address, and the read turnaround.
  constant address_bytes_c : natural := 4;
  constant read_head_c : natural := 1 + address_bytes_c + 1;
  constant word_bytes_c : natural := 2**adapter_data_bus_width_l2_c;

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal spi_i_s : nsl_spi.spi.spi_slave_i;
  signal spi_o_s : nsl_spi.spi.spi_slave_o;
  signal apb_s : nsl_amba.apb.bus_t;

  -- One chip-select assertion, mode 0, MSB first: tx goes out on MOSI, what
  -- MISO carried during the same bits comes back in rx.
  procedure spi_xfer(signal m : out nsl_spi.spi.spi_slave_i;
                     signal s : in nsl_spi.spi.spi_slave_o;
                     tx : in byte_string;
                     rx : out byte_string;
                     half_cycle : in time)
  is
    alias txs : byte_string(0 to tx'length-1) is tx;
    variable rxs : byte_string(0 to tx'length-1);
    variable shreg : byte;
  begin
    assert tx'length = rx'length
      report "spi_xfer vectors differ in length"
      severity failure;

    m.mosi <= '-';
    m.sck <= '0';
    wait for half_cycle * 2;
    m.cs_n <= '0';
    wait for half_cycle;

    for off in txs'range
    loop
      shreg := txs(off);

      for b in shreg'range
      loop
        m.mosi <= shreg(shreg'left);
        wait for half_cycle;
        m.sck <= '1';
        shreg := shreg(shreg'left-1 downto 0) & s.miso.v;
        wait for half_cycle;
        m.sck <= '0';
      end loop;

      rxs(off) := shreg;
    end loop;

    wait for half_cycle;
    m.cs_n <= '1';
    m.mosi <= '-';
    wait for half_cycle;

    rx := rxs;
  end procedure;

  procedure spi_write(signal m : out nsl_spi.spi.spi_slave_i;
                      signal s : in nsl_spi.spi.spi_slave_o;
                      address : in natural;
                      data : in byte_string;
                      half_cycle : in time)
  is
    constant tx_c : byte_string :=
      write_opcode_c & to_be(to_unsigned(address, address_bytes_c * 8)) & data;
    variable rx_v : byte_string(0 to tx_c'length-1);
  begin
    spi_xfer(m, s, tx_c, rx_v, half_cycle);
  end procedure;

  procedure spi_read(signal m : out nsl_spi.spi.spi_slave_i;
                     signal s : in nsl_spi.spi.spi_slave_o;
                     address : in natural;
                     data : out byte_string;
                     half_cycle : in time;
                     opcode : in byte := read_opcode_c)
  is
    constant head_c : byte_string :=
      opcode & to_be(to_unsigned(address, address_bytes_c * 8))
      & byte'(x"00");
    variable tx_v : byte_string(0 to head_c'length + data'length - 1)
      := (others => x"00");
    variable rx_v : byte_string(0 to head_c'length + data'length - 1);
  begin
    tx_v(0 to head_c'length-1) := head_c;
    spi_xfer(m, s, tx_v, rx_v, half_cycle);
    data := rx_v(head_c'length to rx_v'right);
  end procedure;

  -- The discovery opcode carries the SFDP layout: three address bytes and one
  -- dummy byte between opcode and data. The payload streams from the byte
  -- slot right after the opcode, so those four slots carry its filler prefix
  -- and the blob proper starts at the data position.
  procedure spi_discovery_read(signal m : out nsl_spi.spi.spi_slave_i;
                               signal s : in nsl_spi.spi.spi_slave_o;
                               filler : out byte_string;
                               data : out byte_string;
                               half_cycle : in time)
  is
    variable tx_v : byte_string(0 to discovery_filler_c + data'length)
      := (others => x"00");
    variable rx_v : byte_string(0 to discovery_filler_c + data'length);
  begin
    assert filler'length = discovery_filler_c
      report "the discovery filler is " & to_string(discovery_filler_c)
      & " bytes"
      severity failure;

    tx_v(0) := discovery_opcode_c;
    spi_xfer(m, s, tx_v, rx_v, half_cycle);
    filler := rx_v(1 to discovery_filler_c);
    data := rx_v(discovery_filler_c + 1 to rx_v'right);
  end procedure;

  procedure check(what : in string;
                  got : in byte_string;
                  expected : in byte_string)
  is
  begin
    assert got = expected
      report what & ": got " & to_hex_string(got)
      & ", expected " & to_hex_string(expected)
      severity failure;
  end procedure;

  -- A payload no address of the completer holds by chance.
  function pattern(seed : natural; count : natural) return byte_string
  is
    variable data_v : byte_string(0 to count-1);
  begin
    for i in data_v'range
    loop
      data_v(i) := std_ulogic_vector(to_unsigned((seed + 37 * i) mod 256, 8));
    end loop;
    return data_v;
  end function;

begin

  dut: gatecap.adapter_spi.spi_adapter
    generic map(
      apb_config_c => adapter_apb_config_c,
      clock_frequency_c => clock_frequency_c,
      sck_max_rate_c => sck_max_rate_c,
      address_bytes_c => address_bytes_c,
      descriptor_base_c => adapter_descriptor_base_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      spi_i => spi_i_s,
      spi_o => spi_o_s,
      apb_o => apb_s.m,
      apb_i => apb_s.s
      );

  completer: nsl_amba.ram.apb_ram
    generic map(
      config_c => adapter_apb_config_c,
      byte_size_l2_c => adapter_completer_size_l2_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_s.m,
      apb_o => apb_s.s
      );

  clock_gen: process
  begin
    while not done_s
    loop
      clock_s <= '0';
      wait for clock_half_period_c;
      clock_s <= '1';
      wait for clock_half_period_c;
    end loop;
    wait;
  end process;

  stim: process
    constant one_word_c : byte_string := pattern(16#11#, word_bytes_c);
    constant burst_c : byte_string := pattern(16#40#, 8 * word_bytes_c);
    constant guard_c : byte_string := pattern(16#7b#, 2 * word_bytes_c);
    constant partial_c : byte_string := pattern(16#c5#, word_bytes_c + 2);

    variable fast_v : byte_string(0 to 8*word_bytes_c-1);
    variable word_v : byte_string(0 to word_bytes_c-1);
    variable pair_v : byte_string(0 to 2*word_bytes_c-1);
    variable burst_v : byte_string(0 to 8*word_bytes_c-1);
    variable nodummy_tx_v : byte_string(0 to address_bytes_c + word_bytes_c);
    variable nodummy_rx_v : byte_string(0 to address_bytes_c + word_bytes_c);
    variable cut_tx_v : byte_string(0 to read_head_c + word_bytes_c + 1);
    variable cut_rx_v : byte_string(0 to read_head_c + word_bytes_c + 1);
    variable discovery_v : byte_string(0 to discovery_c'length-1);
    variable overrun_v : byte_string(0 to discovery_c'length + 7);
    variable filler_v : byte_string(0 to discovery_filler_c-1);
  begin
    spi_i_s.cs_n <= '1';
    spi_i_s.sck <= '0';
    spi_i_s.mosi <= '-';
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);

    -- The discovery blob, before anything has been said about the address
    -- space: it is what tells a host where to look. The blob sits at the SFDP
    -- data position, the address and dummy slots ahead of it carrying the
    -- filler the payload opens with.
    spi_discovery_read(spi_i_s, spi_o_s, filler_v, discovery_v, slow_half_c);
    check("discovery filler", filler_v,
          byte_string'(0 to discovery_filler_c-1 => x"00"));
    check("discovery blob", discovery_v, discovery_c);

    -- Clocked past the payload's end, the wire carries zeros -- so a host may
    -- read a fixed bound without knowing the length.
    spi_discovery_read(spi_i_s, spi_o_s, filler_v, overrun_v, slow_half_c);
    check("discovery blob, over-read", overrun_v(discovery_c'range),
          discovery_c);
    check("padding past the discovery blob",
          overrun_v(discovery_c'length to overrun_v'right),
          byte_string'(0 to 7 => x"00"));

    -- One word out and back. A write carries no turnaround byte: were one
    -- consumed, the payload would land one byte off and this read would not
    -- find it.
    spi_write(spi_i_s, spi_o_s, conformance_address_c, one_word_c, slow_half_c);
    spi_read(spi_i_s, spi_o_s, conformance_address_c, word_v, slow_half_c);
    check("single word", word_v, one_word_c);

    -- A burst of eight words, written and read back in one chip select each:
    -- the address increments across words on both paths, and the read side
    -- keeps a word prefetched behind the one leaving.
    spi_write(spi_i_s, spi_o_s, 16#100#, burst_c, slow_half_c);
    spi_read(spi_i_s, spi_o_s, 16#100#, burst_v, slow_half_c);
    check("burst", burst_v, burst_c);

    -- One word from the middle of what the burst wrote.
    spi_read(spi_i_s, spi_o_s, 16#100# + 4 * word_bytes_c, word_v, slow_half_c);
    check("word from the middle", word_v,
          burst_c(4 * word_bytes_c to 5 * word_bytes_c - 1));

    -- The turnaround byte is a byte of the read framing: a read that does not
    -- clock it delivers no data at all, since the adapter is still counting
    -- the dummy out while the master believes it is reading.
    nodummy_tx_v := (others => x"00");
    nodummy_tx_v(0 to address_bytes_c) :=
      read_opcode_c & to_be(to_unsigned(conformance_address_c,
                                        address_bytes_c * 8));
    spi_xfer(spi_i_s, spi_o_s, nodummy_tx_v, nodummy_rx_v, slow_half_c);
    assert nodummy_rx_v(address_bytes_c + 1 to nodummy_rx_v'right) /= one_word_c
      report "a read without its turnaround byte returned the data anyway"
      severity failure;

    -- Chip selects back to back, one word each.
    for word in 0 to 3
    loop
      spi_read(spi_i_s, spi_o_s, 16#100# + word * word_bytes_c, word_v,
               slow_half_c);
      check("back to back read " & to_string(word), word_v,
            burst_c(word * word_bytes_c to (word + 1) * word_bytes_c - 1));
    end loop;

    -- A write cut in the middle of a word: the whole word that arrived lands,
    -- the incomplete one is dropped and leaves the completer as it was.
    spi_write(spi_i_s, spi_o_s, 16#300#, guard_c, slow_half_c);
    spi_write(spi_i_s, spi_o_s, 16#300#, partial_c, slow_half_c);
    spi_read(spi_i_s, spi_o_s, 16#300#, pair_v, slow_half_c);
    check("word before the cut", pair_v(0 to word_bytes_c-1),
          partial_c(0 to word_bytes_c-1));
    check("word after the cut", pair_v(word_bytes_c to 2*word_bytes_c-1),
          guard_c(word_bytes_c to 2*word_bytes_c-1));

    -- A read cut in the middle of a word, then the same read whole: the
    -- adapter carries nothing from one chip select to the next.
    cut_tx_v := (others => x"00");
    cut_tx_v(0 to read_head_c-1) :=
      read_opcode_c & to_be(to_unsigned(16#300#, address_bytes_c * 8))
      & byte'(x"00");
    spi_xfer(spi_i_s, spi_o_s, cut_tx_v, cut_rx_v, slow_half_c);
    check("word before the read cut",
          cut_rx_v(read_head_c to read_head_c + word_bytes_c - 1),
          partial_c(0 to word_bytes_c-1));
    spi_read(spi_i_s, spi_o_s, 16#300#, pair_v, slow_half_c);
    check("read after a cut one", pair_v(0 to word_bytes_c-1),
          partial_c(0 to word_bytes_c-1));

    -- Discovery in the middle of memory traffic: the opcode reaches no
    -- address space, and the transaction on either side of it is unaffected.
    spi_discovery_read(spi_i_s, spi_o_s, filler_v, discovery_v, slow_half_c);
    check("discovery filler after memory traffic", filler_v,
          byte_string'(0 to discovery_filler_c-1 => x"00"));
    check("discovery blob after memory traffic", discovery_v, discovery_c);
    spi_read(spi_i_s, spi_o_s, 16#100#, burst_v, slow_half_c);
    check("burst read after a discovery", burst_v, burst_c);

    -- Identification is no longer reserved: 0x9f reads memory, like every
    -- opcode but the write and the discovery ones.
    spi_read(spi_i_s, spi_o_s, 16#100#, burst_v, slow_half_c,
             identify_opcode_c);
    check("burst read on the identification opcode", burst_v, burst_c);

    -- The same traffic at the fastest SCK the rate generics allow, where a
    -- byte spans exactly eighty clock cycles. The wire is asynchronous to the
    -- clock, so every alignment of the two has to work: each pass shifts the
    -- phase by a nanosecond, and carries its own payload so that no pass can
    -- pass on what the one before it left.
    for phase in 0 to 19
    loop
      wait for 1 ns;
      fast_v := pattern(16#a3# + phase, 8 * word_bytes_c);
      spi_write(spi_i_s, spi_o_s, 16#200#, fast_v, fast_half_c);
      spi_read(spi_i_s, spi_o_s, 16#200#, burst_v, fast_half_c);
      check("burst at the minimum ratio, clock phase " & to_string(phase),
            burst_v, fast_v);
      spi_read(spi_i_s, spi_o_s, 16#200# + 6 * word_bytes_c, word_v,
               fast_half_c);
      check("single word at the minimum ratio, clock phase "
            & to_string(phase), word_v,
            fast_v(6 * word_bytes_c to 7 * word_bytes_c - 1));
    end loop;

    report "adapter_spi testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
