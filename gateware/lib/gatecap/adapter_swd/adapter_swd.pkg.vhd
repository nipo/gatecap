library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_coresight;

package adapter_swd is

  -- Communication adapter for a two-wire serial-wire debug link: a whole
  -- debug port whose one access port is the rack, and the APB requester
  -- below carrying its memory accesses into it.
  component swd_adapter is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      descriptor_base_c : natural := 0
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      swd_i : in nsl_coresight.swd.swd_slave_i;
      swd_o : out nsl_coresight.swd.swd_slave_o;

      apb_o : out nsl_amba.apb.master_t;
      apb_i : in nsl_amba.apb.slave_t
      );
  end component;

end package;
