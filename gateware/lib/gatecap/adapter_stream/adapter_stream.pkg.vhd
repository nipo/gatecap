library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;

package adapter_stream is

  -- Communication adapter for a byte-oriented AXI4-Stream link: the host's
  -- command frames arrive on rx, the responses leave on tx, and the APB
  -- requester below carries them into the rack.
  component stream_adapter is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      stream_config_c : nsl_amba.axi4_stream.config_t;
      burst_length_l2_c : natural;
      descriptor_base_c : natural := 0
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      rx_i : in nsl_amba.axi4_stream.master_t;
      rx_o : out nsl_amba.axi4_stream.slave_t;
      tx_o : out nsl_amba.axi4_stream.master_t;
      tx_i : in nsl_amba.axi4_stream.slave_t;

      apb_o : out nsl_amba.apb.master_t;
      apb_i : in nsl_amba.apb.slave_t
      );
  end component;

end package;
