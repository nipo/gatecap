library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, nsl_memory, gatecap;
use nsl_amba.apb.all;
use nsl_data.bytestream.all;
use nsl_data.endian.all;
use gatecap.trace.all;

-- Packed trace buffer: dumb dual-port memory that stores several samples
-- per APB word using the RAM's per-byte write enables. A sample occupies a
-- power-of-two byte lane (1, 2 or 4 bytes); the write address is a sample
-- index whose LSBs pick the lane and whose upper bits pick the RAM row, so
-- the capture core's sample-indexed address stream drives it unchanged --
-- no accumulator and no partial-word flush, since each sample is an
-- independent byte-masked write. The read port is APB, read-only and word
-- wide; the host unpacks the lanes. Writes complete with SLVERR.
entity trace_buffer_packed is
  generic (
    apb_config_c : config_t;
    sample_width_c : natural;
    depth_l2_c : natural
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    apb_i : in master_t;
    apb_o : out slave_t;

    -- Write side clock (the capture domain). Tie to clock_i for a single-clock
    -- instance; drive from the capture clock for an asynchronous capture.
    write_clock_i : in std_ulogic;
    write_en_i : in std_ulogic;
    write_addr_i : in unsigned(depth_l2_c-1 downto 0);
    write_data_i : in std_ulogic_vector(sample_width_c-1 downto 0)
    );
end entity;

architecture rtl of trace_buffer_packed is

  constant word_bytes_c : natural := 2**apb_config_c.data_bus_width_l2;
  constant word_bits_c : natural := 8 * word_bytes_c;
  constant lane_bytes_c : natural := packed_lane_bytes(sample_width_c, word_bytes_c);
  constant lane_bits_c : natural := 8 * lane_bytes_c;
  constant lane_l2_c : natural := packed_lane_l2(sample_width_c, word_bytes_c);
  constant spw_c : natural := packed_samples_per_word(sample_width_c, word_bytes_c);
  constant row_bits_c : natural := depth_l2_c - lane_l2_c;

  signal apb_addr_s : unsigned(apb_config_c.address_width-1 downto apb_config_c.data_bus_width_l2);
  signal apb_read_s, apb_read_done_s : std_ulogic;
  signal apb_rbytes_s : byte_string(0 to word_bytes_c-1);

  signal lane_s : natural range 0 to spw_c-1;
  signal row_s : unsigned(row_bits_c-1 downto 0);
  signal we_s : std_ulogic_vector(word_bytes_c-1 downto 0);
  signal wdata_s : std_ulogic_vector(word_bits_c-1 downto 0);

  signal read_row_s : unsigned(row_bits_c-1 downto 0);
  signal read_word_s : std_ulogic_vector(word_bits_c-1 downto 0);

begin

  assert lane_bytes_c <= word_bytes_c
    report "packed buffer requires a sample that fits one APB word" severity failure;

  slave: nsl_amba.apb.apb_slave
    generic map(
      config_c => apb_config_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,

      apb_i => apb_i,
      apb_o => apb_o,

      address_o => apb_addr_s,

      w_data_o => open,
      w_mask_o => open,
      w_ready_i => '1',
      w_error_i => '1',
      w_valid_o => open,

      r_data_i => apb_rbytes_s,
      r_ready_o => apb_read_s,
      r_valid_i => apb_read_done_s
      );

  ram: nsl_memory.ram.ram_2p_homogeneous
    generic map(
      addr_size_c => row_bits_c,
      word_size_c => 8,
      data_word_count_c => word_bytes_c
      )
    port map(
      a_clock_i => write_clock_i,
      a_enable_i => write_en_i,
      a_write_en_i => we_s,
      a_address_i => row_s,
      a_data_i => wdata_s,
      a_data_o => open,

      b_clock_i => clock_i,
      b_enable_i => '1',
      b_write_en_i => (others => '0'),
      b_address_i => read_row_s,
      b_data_i => (others => '-'),
      b_data_o => read_word_s
      );

  -- The address LSBs are the byte lane, the upper bits the RAM row. Null
  -- slices (lane_l2_c = 0, one sample per word) collapse to lane 0.
  lane_s <= to_integer(write_addr_i(lane_l2_c-1 downto 0));
  row_s <= write_addr_i(depth_l2_c-1 downto lane_l2_c);

  -- The sample is replicated into every lane slot; the byte-enable picks
  -- which lane is actually written, so no per-lane data mux is needed.
  wdata_gen: for i in 0 to spw_c-1 generate
    wdata_s((i+1)*lane_bits_c-1 downto i*lane_bits_c)
      <= std_ulogic_vector(resize(unsigned(write_data_i), lane_bits_c));
  end generate;

  byte_enable: process(lane_s)
    variable v : std_ulogic_vector(word_bytes_c-1 downto 0);
  begin
    v := (others => '0');
    for b in 0 to word_bytes_c-1 loop
      if b / lane_bytes_c = lane_s then
        v(b) := '1';
      end if;
    end loop;
    we_s <= v;
  end process;

  read_row_s <= resize(apb_addr_s, row_bits_c);
  apb_rbytes_s <= to_le(unsigned(read_word_s));

  -- ram_2p_homogeneous has a one-cycle registered read: read_word_s is
  -- valid the cycle after apb_slave asserts r_ready_o.
  read_done: process(clock_i, reset_n_i)
  begin
    if rising_edge(clock_i) then
      if apb_read_s = '1' and apb_read_done_s = '0' then
        apb_read_done_s <= '1';
      elsif apb_read_done_s = '1' then
        apb_read_done_s <= '0';
      end if;
    end if;
    if reset_n_i = '0' then
      apb_read_done_s <= '0';
    end if;
  end process;

end architecture;
