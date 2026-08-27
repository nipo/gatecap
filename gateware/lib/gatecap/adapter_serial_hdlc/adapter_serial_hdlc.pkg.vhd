library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;

package adapter_serial_hdlc is

  -- Communication adapter for a two-wire serial link: an 8n1 UART carrying
  -- HDLC frames, and the APB requester below carrying them into the rack.
  component serial_hdlc_adapter is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      clock_frequency_c : natural;
      baud_rate_c : natural;
      burst_length_l2_c : natural;
      descriptor_base_c : natural := 0
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      uart_rx_i : in std_ulogic;
      uart_tx_o : out std_ulogic;

      apb_o : out nsl_amba.apb.master_t;
      apb_i : in nsl_amba.apb.slave_t
      );
  end component;

end package;
