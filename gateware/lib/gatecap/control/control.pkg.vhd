library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;

package control is

  -- Cycles from a matched condition to the trigger_control strobe (its
  -- registered output). A capture core back-dates the trigger sample by
  -- this much. A future trigger type with more pipeline stages declares
  -- its own constant.
  constant trigger_control_latency_c : natural := 1;

  -- The edge trigger registers its inputs twice, so its match strobe trails
  -- the new-value cycle by one more (see trigger_control_edge).
  constant trigger_control_edge_latency_c : natural := 2;

  component capture_control is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      capture_len_width_c : natural;
      depth_l2_c : natural;
      window_count_c : natural := 1;
      fingerprint_c : unsigned(31 downto 0) := (others => '0')
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      apb_i : in nsl_amba.apb.master_t;
      apb_o : out nsl_amba.apb.slave_t;

      arm_o : out std_ulogic;
      abort_o : out std_ulogic;
      capture_len_o : out unsigned(capture_len_width_c-1 downto 0);
      pre_trigger_len_o : out unsigned(capture_len_width_c-1 downto 0);
      window_count_o : out unsigned(capture_len_width_c-1 downto 0);
      enable_o : out std_ulogic;

      state_i : in std_ulogic_vector(1 downto 0);
      triggered_i : in std_ulogic;
      ready_i : in std_ulogic;
      head_i : in unsigned(depth_l2_c-1 downto 0);
      head_we_i : in std_ulogic
      );
  end component;

  component capture_control_rle is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      depth_l2_c : natural;
      fingerprint_c : unsigned(31 downto 0) := (others => '0')
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      apb_i : in nsl_amba.apb.master_t;
      apb_o : out nsl_amba.apb.slave_t;

      arm_o : out std_ulogic;
      abort_o : out std_ulogic;
      pre_lines_o : out unsigned(depth_l2_c downto 0);
      max_cycles_o : out unsigned(31 downto 0);
      enable_o : out std_ulogic;

      state_i : in std_ulogic_vector(1 downto 0);
      triggered_i : in std_ulogic;
      ready_i : in std_ulogic;
      end_ptr_i : in unsigned(depth_l2_c downto 0);
      pre_head_i : in unsigned(depth_l2_c-1 downto 0);
      pre_n_i : in unsigned(depth_l2_c downto 0)
      );
  end component;

  component trigger_control is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      signal_count_c : natural;
      async_c : boolean := false
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      apb_i : in nsl_amba.apb.master_t;
      apb_o : out nsl_amba.apb.slave_t;

      capture_clock_i : in std_ulogic := '0';
      capture_reset_n_i : in std_ulogic := '1';

      signals_i : in std_ulogic_vector(signal_count_c-1 downto 0);

      enable_i : in std_ulogic := '1';

      trigger_o : out std_ulogic
      );
  end component;

  component trigger_control_edge is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      signal_count_c : natural;
      async_c : boolean := false
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      apb_i : in nsl_amba.apb.master_t;
      apb_o : out nsl_amba.apb.slave_t;

      capture_clock_i : in std_ulogic := '0';
      capture_reset_n_i : in std_ulogic := '1';

      signals_i : in std_ulogic_vector(signal_count_c-1 downto 0);

      enable_i : in std_ulogic := '1';

      trigger_o : out std_ulogic
      );
  end component;

end package;
