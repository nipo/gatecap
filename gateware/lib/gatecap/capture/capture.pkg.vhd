library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package capture is

  component capture_core is
    generic (
      signal_count_c : natural;
      capture_len_width_c : natural;
      depth_l2_c : natural;
      window_count_c : natural := 1;
      trigger_latency_c : natural := 0
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      signals_i : in std_ulogic_vector(signal_count_c-1 downto 0);

      arm_i : in std_ulogic;
      abort_i : in std_ulogic;
      trigger_i : in std_ulogic;
      capture_len_i : in unsigned(capture_len_width_c-1 downto 0);
      pre_trigger_len_i : in unsigned(capture_len_width_c-1 downto 0);
      window_count_i : in unsigned(capture_len_width_c-1 downto 0);

      state_o : out std_ulogic_vector(1 downto 0);
      triggered_o : out std_ulogic;
      ready_o : out std_ulogic;
      head_o : out unsigned(depth_l2_c-1 downto 0);
      head_we_o : out std_ulogic;

      write_en_o : out std_ulogic;
      write_addr_o : out unsigned(depth_l2_c-1 downto 0);
      write_data_o : out std_ulogic_vector(signal_count_c-1 downto 0)
      );
  end component;

  component capture_core_rle is
    generic (
      signal_count_c : natural;
      depth_l2_c : natural;
      count_bits_c : natural := 32
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      signals_i : in std_ulogic_vector(signal_count_c-1 downto 0);

      arm_i : in std_ulogic;
      abort_i : in std_ulogic;
      trigger_i : in std_ulogic;
      pre_lines_i : in unsigned(depth_l2_c downto 0);
      max_cycles_i : in unsigned(31 downto 0) := (others => '0');

      state_o : out std_ulogic_vector(1 downto 0);
      triggered_o : out std_ulogic;
      ready_o : out std_ulogic;
      pre_head_o : out unsigned(depth_l2_c-1 downto 0);
      pre_n_o : out unsigned(depth_l2_c downto 0);
      end_ptr_o : out unsigned(depth_l2_c downto 0);

      write_en_o : out std_ulogic;
      write_addr_o : out unsigned(depth_l2_c-1 downto 0);
      write_data_o : out std_ulogic_vector(signal_count_c downto 0)
      );
  end component;

end package;
