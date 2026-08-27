library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, nsl_memory, gatecap;
use nsl_amba.apb.all;
use nsl_data.bytestream.all;
use nsl_data.endian.all;
use gatecap.trace.all;

-- Trace buffer: dumb dual-port memory, one sample per line. The write port
-- takes an explicit line address (the capture core owns the pointer). The RAM
-- is sample_width_c bits wide, so storage costs only what a sample needs.
--
-- A sample that fits one APB word reads back zero-extended (one word per line).
-- A wider sample spans several words: a line occupies a power-of-two run of
-- words, the address LSBs select the in-line word, and the read side muxes out
-- the 32-bit slice (padding words beyond the sample read as zero). Only the
-- sample's bits cost BRAM; the padding is address space, not storage. The read
-- port is APB, read-only; writes complete with SLVERR.
entity trace_buffer is
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

architecture rtl of trace_buffer is

  constant word_bits_c : natural := 8 * 2**apb_config_c.data_bus_width_l2;
  -- Words per line (1 when a sample fits one word) and the address LSBs that
  -- index the word within a line.
  constant line_words_c : natural := line_word_count(sample_width_c, word_bits_c);
  constant word_l2_c : natural := line_word_l2(sample_width_c, word_bits_c);
  constant addr_bits_c : natural :=
    apb_config_c.address_width - apb_config_c.data_bus_width_l2;

  signal apb_addr_s : unsigned(apb_config_c.address_width-1 downto apb_config_c.data_bus_width_l2);
  signal apb_read_s, apb_read_done_s : std_ulogic;
  signal apb_rbytes_s : byte_string(0 to 2**apb_config_c.data_bus_width_l2-1);

  -- Word address split into a line (RAM address) and the in-line word index.
  signal word_index_s : unsigned(addr_bits_c-1 downto 0);
  signal sub_word_s : natural range 0 to line_words_c-1;
  signal read_addr_s : unsigned(depth_l2_c-1 downto 0);
  signal read_data_s : std_ulogic_vector(sample_width_c-1 downto 0);
  -- The sample zero-extended to a whole (power-of-two) run of words.
  signal read_wide_s : std_ulogic_vector(line_words_c*word_bits_c-1 downto 0);

begin

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

  -- The RAM is only as wide as a sample: the APB word width is a read-side
  -- transport detail, not a storage cost. Samples are zero-extended to the
  -- APB word on the way out.
  -- Two clocks: write port on write_clock_i (capture domain), read port on
  -- clock_i (host/APB domain). A single-clock instance ties the two together.
  ram: nsl_memory.ram.ram_2p_r_w
    generic map(
      addr_size_c => depth_l2_c,
      data_size_c => sample_width_c,
      clock_count_c => 2
      )
    port map(
      clock_i(0) => write_clock_i,
      clock_i(1) => clock_i,

      write_address_i => write_addr_i,
      write_en_i => write_en_i,
      write_data_i => write_data_i,

      read_address_i => read_addr_s,
      read_en_i => '1',
      read_data_o => read_data_s
      );

  -- line = word_index / line_words (a shift); sub = word_index mod line_words
  -- (the low bits). For a one-word sample line_words = 1, so sub = 0 and the
  -- read is the plain zero-extended sample.
  word_index_s <= resize(apb_addr_s, addr_bits_c);
  read_addr_s <= resize(shift_right(word_index_s, word_l2_c), depth_l2_c);
  sub_word_s <= to_integer(word_index_s and to_unsigned(line_words_c-1, addr_bits_c));
  read_wide_s <= std_ulogic_vector(resize(unsigned(read_data_s), line_words_c*word_bits_c));
  apb_rbytes_s <= to_le(unsigned(
    read_wide_s(sub_word_s*word_bits_c + word_bits_c-1 downto sub_word_s*word_bits_c)));

  -- ram_2p_r_w has a one-cycle registered read: r_data_i is valid the
  -- cycle after apb_slave asserts r_ready_o.
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
