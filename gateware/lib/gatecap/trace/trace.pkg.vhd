library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;

package trace is

  -- Byte-lane packing (trace_buffer_packed): a sample occupies the
  -- smallest power-of-two run of bytes that holds it (1, 2 or 4 for an
  -- 8-, 16- or 32-bit APB word), so an integer, power-of-two number of
  -- samples share one word and the address LSBs pick the byte lane.
  --   packed_lane_bytes:      bytes per sample after rounding
  --   packed_samples_per_word: samples that share one APB word
  --   packed_lane_l2:         address LSBs used as the byte-lane index
  function packed_lane_bytes(sample_width, word_bytes : natural) return natural;
  function packed_samples_per_word(sample_width, word_bytes : natural) return natural;
  function packed_lane_l2(sample_width, word_bytes : natural) return natural;

  -- Wide samples: a sample wider than one APB word spans several words. A line
  -- occupies a power-of-two run of words (so the address LSBs pick the word),
  -- and only the sample's bits cost BRAM (the padding words read as zero).
  --   line_word_count: words per line (1 for a sample that fits one word)
  --   line_word_l2:    address LSBs used as the in-line word index
  function line_word_count(sample_width, word_bits : natural) return natural;
  function line_word_l2(sample_width, word_bits : natural) return natural;

  -- log2 of the trace buffer size in bytes: what an instrument's region size
  -- has to cover, hence the footprint it declares and the address width the
  -- rack allocates for it. The generated core derives it here, at elaboration,
  -- because a buffer holding a probe of generic width has no size until then.
  function buffer_size_l2(buffer_depth_l2, signal_count, data_bus_width_l2 : natural;
                          packed, rle : boolean) return natural;

  -- Dumb trace memory, one sample per address. The RAM is only as wide as
  -- a sample; the read port zero-extends to the APB word.
  component trace_buffer is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      sample_width_c : natural;
      depth_l2_c : natural
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      apb_i : in nsl_amba.apb.master_t;
      apb_o : out nsl_amba.apb.slave_t;

      write_clock_i : in std_ulogic;
      write_en_i : in std_ulogic;
      write_addr_i : in unsigned(depth_l2_c-1 downto 0);
      write_data_i : in std_ulogic_vector(sample_width_c-1 downto 0)
      );
  end component;

  -- Same interface as trace_buffer, but packs several samples per APB word
  -- using the RAM's per-byte write enables. The address LSBs select the
  -- byte lane, so the capture core's sample-indexed address stream drives
  -- it unchanged. Reads return whole APB words; the host unpacks lanes.
  component trace_buffer_packed is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      sample_width_c : natural;
      depth_l2_c : natural
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      apb_i : in nsl_amba.apb.master_t;
      apb_o : out nsl_amba.apb.slave_t;

      write_clock_i : in std_ulogic;
      write_en_i : in std_ulogic;
      write_addr_i : in unsigned(depth_l2_c-1 downto 0);
      write_data_i : in std_ulogic_vector(sample_width_c-1 downto 0)
      );
  end component;

end package;

package body trace is

  -- Rounds up to a power-of-two byte count. The caller decides whether the
  -- result fits a word (trace_buffer_packed asserts it); this must not, so the
  -- subsystem can evaluate it for a wide (non-packed) sample without failing.
  function packed_lane_bytes(sample_width, word_bytes : natural) return natural is
    variable need : natural := (sample_width + 7) / 8;
    variable lb : natural := 1;
  begin
    while lb < need loop
      lb := lb * 2;
    end loop;
    return lb;
  end function;

  function packed_samples_per_word(sample_width, word_bytes : natural) return natural is
  begin
    return word_bytes / packed_lane_bytes(sample_width, word_bytes);
  end function;

  function packed_lane_l2(sample_width, word_bytes : natural) return natural is
    variable spw : natural := packed_samples_per_word(sample_width, word_bytes);
    variable l2 : natural := 0;
  begin
    while 2 ** l2 < spw loop
      l2 := l2 + 1;
    end loop;
    return l2;
  end function;

  function line_word_count(sample_width, word_bits : natural) return natural is
    variable need : natural := (sample_width + word_bits - 1) / word_bits;
    variable w : natural := 1;
  begin
    while w < need loop
      w := w * 2;
    end loop;
    return w;
  end function;

  function line_word_l2(sample_width, word_bits : natural) return natural is
    variable w : natural := line_word_count(sample_width, word_bits);
    variable l2 : natural := 0;
  begin
    while 2 ** l2 < w loop
      l2 := l2 + 1;
    end loop;
    return l2;
  end function;

  function buffer_size_l2(buffer_depth_l2, signal_count, data_bus_width_l2 : natural;
                          packed, rle : boolean) return natural is
    constant word_bytes : natural := 2 ** data_bus_width_l2;
    constant word_bits : natural := 8 * word_bytes;
    variable sample_width : natural := signal_count;
    variable words_l2 : natural := buffer_depth_l2;
  begin
    -- The RLE tag rides one extra bit per line.
    if rle then
      sample_width := sample_width + 1;
    end if;
    -- packed (sub-word, several samples per word) and wide (supra-word, a run
    -- of words per sample) are mutually exclusive: at most one term is
    -- non-zero.
    if packed then
      words_l2 := words_l2 - packed_lane_l2(signal_count, word_bytes);
    end if;
    words_l2 := words_l2 + line_word_l2(sample_width, word_bits);
    return words_l2 + data_bus_width_l2;
  end function;

end package body;
