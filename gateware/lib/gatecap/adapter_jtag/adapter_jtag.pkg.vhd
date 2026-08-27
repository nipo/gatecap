library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba;

package adapter_jtag is

  -- Communication adapter reaching a rack through the FPGA's own test-access
  -- port: command frames ride a user data register, the APB requester below
  -- carries them into the rack.
  component jtag_adapter is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      burst_length_l2_c : natural;
      descriptor_base_c : natural := 0
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      chip_tck_i : in std_ulogic := '0';
      chip_tms_i : in std_ulogic := '0';
      chip_tdi_i : in std_ulogic := '0';
      chip_tdo_o : out std_ulogic;

      apb_o : out nsl_amba.apb.master_t;
      apb_i : in nsl_amba.apb.slave_t
      );
  end component;

end package;
