library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, nsl_logic, nsl_spi, nsl_synthesis, gatecap;
use nsl_amba.apb.all;
use nsl_data.bytestream.all;
use nsl_logic.bool.all;
use gatecap.descriptor.all;

-- SPI front end of a rack: four wires, and any SPI master on the other end.
--
-- The link is memory style, one transaction per chip-select assertion: an
-- opcode byte, a big-endian byte address, one dummy byte on reads, then data
-- until the master releases the chip select. The framing is a SPI flash's,
-- the SFDP opcode included: 0x5a streams a discovery blob -- the signature
-- "GCAP" and a CBOR array of the map geometry -- instead of reading memory,
-- which is how the host learns where the descriptor sits before it can read
-- anything meaningful.
--
-- The link has no backpressure: the master clocks read data out at line rate
-- and the slave cannot stall it. Two things make that safe. The slave is
-- built by oversampling SCK in the local clock domain, so a data byte spans
-- at least eighty local cycles; and the read dummy byte, plus
-- the word prefetched behind the one being shifted out, put whole byte times
-- between an APB read being issued and its data being needed. Every completer
-- a rack exposes answers in a handful of cycles, well inside that.
--
-- The byte assembly is the wrapper's work: the controller carries one byte
-- per address, and this entity gathers them into APB words, least significant
-- byte at the lowest address.
entity spi_adapter is
  generic (
    apb_config_c : config_t;
    clock_frequency_c : natural;
    sck_max_rate_c : natural;
    address_bytes_c : natural range 1 to 4 := 4;
    spi_mode_c : natural range 0 to 3 := 0;
    -- The descriptor's byte address, as the discovery blob states it. A
    -- generated rack pins it at 0 -- the backplane maps the ROM there -- but
    -- the value reaching the wire is this generic, whatever it is.
    descriptor_base_c : natural := 0
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    spi_i : in nsl_spi.spi.spi_slave_i;
    spi_o : out nsl_spi.spi.spi_slave_o;

    apb_o : out master_t;
    apb_i : in slave_t
    );
end entity;

architecture rtl of spi_adapter is

  -- What the shift register below costs, from the SCK edge that shifts a bit
  -- out to that bit reaching MISO: two clock cycles for the edge to pass the
  -- debouncer, one to shift, and one more at a byte boundary, where the next
  -- byte is loaded the cycle after the shift. The edge itself lands anywhere
  -- inside a clock cycle, so five cycles is the worst of it, and the master
  -- samples MISO half an SCK period after that edge. Hence: an SCK half
  -- period must span five clock cycles, and a full period ten.
  constant sck_ratio_min_c : natural := 10;
  constant rate_message_c : string :=
    "the SPI clock must leave at least ten clock cycles per SCK period";
  constant address_message_c : string :=
    "the wire address must be wide enough for the rack's address space";

  -- Flash opcodes: page program writes, fast read -- and anything else --
  -- reads.
  constant write_opcode_c : byte := x"02";
  -- The read turnaround, in bytes. One is what a fast read carries, and what
  -- the APB read of the first word of a burst is given.
  constant dummy_bytes_c : natural := 1;

  -- The blob the discovery opcode answers with, the SFDP opcode's address and
  -- dummy slots included as filler (SPI_DISCOVERY_* in gatecap.descriptor).
  -- Composed here rather than read out of the rack: it states where the
  -- rack's own descriptor is, so nothing in the address space can be needed
  -- to reach it. The opcode is pinned like the write and read ones -- the
  -- host speaks it before it knows anything about this target, so it cannot
  -- be told -- and identification, 0x9f, is left to read memory like any
  -- opcode the controller does not reserve.
  constant discovery_data_c : byte_string := spi_discovery(
    addr_bits => apb_config_c.address_width,
    data_bytes_l2 => apb_config_c.data_bus_width_l2,
    descriptor_base => descriptor_base_c);

  constant cpol_c : std_ulogic := to_logic(spi_mode_c >= 2);
  constant cpha_c : std_ulogic := to_logic((spi_mode_c mod 2) = 1);

  constant word_bytes_c : natural := 2**apb_config_c.data_bus_width_l2;
  constant lane_bits_c : natural := apb_config_c.data_bus_width_l2;
  constant addr_width_c : natural := apb_config_c.address_width;
  constant ones_strb_c : std_ulogic_vector(0 to word_bytes_c-1) := (others => '1');

  -- Byte lane of a wire address inside its APB word. The address is
  -- don't-care while no transaction is running, which must not reach
  -- to_integer.
  function lane_of(addr : unsigned) return natural
  is
    variable lane_v : unsigned(lane_bits_c-1 downto 0);
  begin
    if lane_bits_c = 0 then
      return 0;
    end if;
    lane_v := addr(lane_bits_c-1 downto 0);
    if is_x(std_ulogic_vector(lane_v)) then
      return 0;
    end if;
    return to_integer(lane_v);
  end function;

  -- Address of the APB word a wire address falls in. Address bits above the
  -- rack's space are not decoded, as on every other link.
  function word_base_of(addr : unsigned) return unsigned
  is
    variable base_v : unsigned(addr_width_c-1 downto 0);
  begin
    base_v := addr(addr_width_c-1 downto 0);
    base_v(lane_bits_c-1 downto 0) := (others => '0');
    return base_v;
  end function;

  type apb_state_t is (
    APB_IDLE,
    APB_SETUP,
    APB_ACCESS
    );

  type regs_t is
  record
    -- The word the master is shifting out, and the one fetched behind it.
    cur : byte_string(0 to word_bytes_c-1);
    cur_addr : unsigned(addr_width_c-1 downto 0);
    cur_valid : boolean;
    nxt : byte_string(0 to word_bytes_c-1);
    nxt_valid : boolean;

    -- The word being gathered from the write data, and the byte lane awaited
    -- next. A transaction that ends before the top lane leaves wbuf_full
    -- clear, and the partial word is dropped.
    wbuf : byte_string(0 to word_bytes_c-1);
    wbuf_addr : unsigned(addr_width_c-1 downto 0);
    wbuf_lane : natural range 0 to word_bytes_c-1;
    wbuf_full : boolean;

    apb_state : apb_state_t;
    apb_addr : unsigned(addr_width_c-1 downto 0);
    apb_write : boolean;
    -- Where a read in flight lands, and whether its data is to be dropped
    -- because the chip select was released under it.
    apb_to_nxt : boolean;
    apb_stale : boolean;
  end record;

  signal r, rin : regs_t;

  signal selected_s : std_ulogic;
  signal wire_addr_s : unsigned(address_bytes_c*8-1 downto 0);
  signal rdata_s, wdata_s : byte_string(0 to 0);
  signal rready_s, rvalid_s, wvalid_s, wready_s : std_ulogic;

begin

  assert clock_frequency_c >= sck_ratio_min_c * sck_max_rate_c
    report rate_message_c
    severity failure;

  sck_rate_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => rate_message_c,
      condition_c => clock_frequency_c >= sck_ratio_min_c * sck_max_rate_c
      )
    port map(
      unused_i => '0'
      );

  assert address_bytes_c * 8 >= addr_width_c
    report address_message_c
    severity failure;

  address_width_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => address_message_c,
      condition_c => address_bytes_c * 8 >= addr_width_c
      )
    port map(
      unused_i => '0'
      );

  regs: process(clock_i, reset_n_i)
  begin
    if rising_edge(clock_i) then
      r <= rin;
    end if;

    if reset_n_i = '0' then
      r.cur_valid <= false;
      r.nxt_valid <= false;
      r.wbuf_lane <= 0;
      r.wbuf_full <= false;
      r.apb_state <= APB_IDLE;
      r.apb_stale <= false;
    end if;
  end process;

  transition: process(r, apb_i, selected_s, wire_addr_s,
                      rready_s, wvalid_s, wready_s, wdata_s) is
    variable lane_v : natural range 0 to word_bytes_c-1;
    variable base_v : unsigned(addr_width_c-1 downto 0);
    variable hit_v, roll_v : boolean;
    -- The read pipeline as it stands after the byte leaving this cycle: the
    -- word to fetch behind follows from these, not from the registers.
    variable cur_addr_v : unsigned(addr_width_c-1 downto 0);
    variable cur_valid_v, nxt_valid_v : boolean;
  begin
    rin <= r;

    lane_v := lane_of(wire_addr_s);
    base_v := word_base_of(wire_addr_s);
    hit_v := r.cur_valid and r.cur_addr = base_v;
    roll_v := rready_s = '1' and hit_v and lane_v = word_bytes_c - 1;

    if roll_v then
      cur_addr_v := r.cur_addr + word_bytes_c;
      cur_valid_v := r.nxt_valid;
      nxt_valid_v := false;
    else
      cur_addr_v := r.cur_addr;
      cur_valid_v := r.cur_valid;
      nxt_valid_v := r.nxt_valid;
    end if;

    -- The last byte of a word left: the prefetched word takes its place.
    if roll_v then
      rin.cur <= r.nxt;
      rin.cur_valid <= cur_valid_v;
      rin.cur_addr <= cur_addr_v;
      rin.nxt_valid <= nxt_valid_v;
    end if;

    -- Write bytes gather into a word, and only a whole word in lane order
    -- reaches the bus.
    if wvalid_s = '1' and wready_s = '1' then
      if lane_v = r.wbuf_lane then
        rin.wbuf(lane_v) <= wdata_s(0);
        if lane_v = 0 then
          rin.wbuf_addr <= base_v;
        end if;
        if lane_v = word_bytes_c - 1 then
          rin.wbuf_lane <= 0;
          rin.wbuf_full <= true;
        else
          rin.wbuf_lane <= lane_v + 1;
        end if;
      else
        rin.wbuf_lane <= 0;
      end if;
    end if;

    case r.apb_state is
      when APB_IDLE =>
        if r.wbuf_full then
          rin.apb_state <= APB_SETUP;
          rin.apb_write <= true;
          rin.apb_addr <= r.wbuf_addr;
        elsif rready_s = '1' and not hit_v then
          -- The first word of a read burst: the dummy byte is the time this
          -- fetch is given.
          rin.apb_state <= APB_SETUP;
          rin.apb_write <= false;
          rin.apb_to_nxt <= false;
          rin.apb_stale <= false;
          rin.apb_addr <= base_v;
          rin.cur_addr <= base_v;
          rin.cur_valid <= false;
          rin.nxt_valid <= false;
        elsif cur_valid_v and not nxt_valid_v then
          -- Prefetch: the word behind the one being shifted out.
          rin.apb_state <= APB_SETUP;
          rin.apb_write <= false;
          rin.apb_to_nxt <= true;
          rin.apb_stale <= false;
          rin.apb_addr <= cur_addr_v + word_bytes_c;
        end if;

      when APB_SETUP =>
        rin.apb_state <= APB_ACCESS;

      when APB_ACCESS =>
        if is_ready(apb_config_c, apb_i) then
          rin.apb_state <= APB_IDLE;
          if r.apb_write then
            rin.wbuf_full <= false;
          elsif not r.apb_stale then
            if r.apb_to_nxt and roll_v then
              -- The word landing is the one the roll just made current.
              rin.cur <= bytes(apb_config_c, apb_i);
              rin.cur_valid <= true;
            elsif r.apb_to_nxt then
              rin.nxt <= bytes(apb_config_c, apb_i);
              rin.nxt_valid <= true;
            else
              rin.cur <= bytes(apb_config_c, apb_i);
              rin.cur_valid <= true;
            end if;
          end if;
        end if;
    end case;

    -- Chip select released: nothing gathered is carried into the next
    -- transaction, and a read in flight has lost its reader.
    if selected_s = '0' then
      rin.cur_valid <= false;
      rin.nxt_valid <= false;
      rin.wbuf_lane <= 0;
      if r.apb_state /= APB_IDLE and not r.apb_write then
        rin.apb_stale <= true;
      end if;
    end if;
  end process;

  mealy: process(r) is
  begin
    apb_o <= transfer_idle(apb_config_c);

    case r.apb_state is
      when APB_IDLE =>
        null;

      when APB_SETUP =>
        if r.apb_write then
          apb_o <= write_transfer(apb_config_c, addr => r.apb_addr,
                                  bytes => r.wbuf, strb => ones_strb_c,
                                  phase => PHASE_SETUP);
        else
          apb_o <= read_transfer(apb_config_c, addr => r.apb_addr,
                                 phase => PHASE_SETUP);
        end if;

      when APB_ACCESS =>
        if r.apb_write then
          apb_o <= write_transfer(apb_config_c, addr => r.apb_addr,
                                  bytes => r.wbuf, strb => ones_strb_c,
                                  phase => PHASE_ACCESS);
        else
          apb_o <= read_transfer(apb_config_c, addr => r.apb_addr,
                                 phase => PHASE_ACCESS);
        end if;
    end case;
  end process;

  rdata_s <= (0 => r.cur(lane_of(wire_addr_s)));
  rvalid_s <= to_logic(r.cur_valid and r.cur_addr = word_base_of(wire_addr_s));
  wready_s <= to_logic(not r.wbuf_full);

  controller: nsl_spi.slave.spi_memory_controller
    generic map(
      addr_bytes_c => address_bytes_c,
      data_bytes_c => 1,
      write_opcode_c => write_opcode_c,
      dummy_bytes_c => dummy_bytes_c,
      discovery_command_c => SPI_DISCOVERY_COMMAND_C,
      discovery_data_c => discovery_data_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,

      spi_i => spi_i,
      spi_o => spi_o,

      cpol_i => cpol_c,
      cpha_i => cpha_c,

      selected_o => selected_s,

      addr_o => wire_addr_s,

      rdata_i => rdata_s,
      rready_o => rready_s,
      rvalid_i => rvalid_s,

      wdata_o => wdata_s,
      wvalid_o => wvalid_s,
      wready_i => wready_s
      );

end architecture;
