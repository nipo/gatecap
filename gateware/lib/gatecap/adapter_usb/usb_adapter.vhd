library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_usb, gatecap;
use gatecap.adapter_usb.all;

-- USB Full Speed front end of a rack: three IOs, and any USB host on the
-- other end.
--
-- The Full Speed phy is fabric, so the whole link needs nothing of the device
-- but two IOs for the differential pair and a third driving the D+ pullup
-- through a resistor -- and a reference clock at the rate the phy recovers
-- bits with. That clock is the rack's host clock too: everything from the
-- line to the APB requester runs in one domain.
--
-- What sits above the phy is usb_utmi_adapter, which is where the device
-- descriptors, the endpoint pair and the bridge are.
entity usb_adapter is
  generic (
    apb_config_c : nsl_amba.apb.config_t;
    burst_length_l2_c : natural;
    fingerprint_c : unsigned(31 downto 0);
    clock_frequency_c : natural := PHY_CLOCK_60M_C;
    descriptor_base_c : natural := 0
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    usb_o : out nsl_usb.io.usb_io_c;
    usb_i : in nsl_usb.io.usb_io_s;

    online_o : out std_ulogic;

    apb_o : out nsl_amba.apb.master_t;
    apb_i : in nsl_amba.apb.slave_t
    );
end entity;

architecture rtl of usb_adapter is

  signal utmi_data_to_phy_s : nsl_usb.utmi.utmi_data8_sie2phy;
  signal utmi_data_from_phy_s : nsl_usb.utmi.utmi_data8_phy2sie;
  signal utmi_system_to_phy_s : nsl_usb.utmi.utmi_system_sie2phy;
  signal utmi_system_from_phy_s : nsl_usb.utmi.utmi_system_phy2sie;

begin

  phy: nsl_usb.fs_phy.fs_utmi8_phy
    generic map(
      ref_clock_mhz_c => clock_frequency_c / 1000000
      )
    port map(
      ref_clock_i => clock_i,
      reset_n_i => reset_n_i,

      bus_o => usb_o,
      bus_i => usb_i,

      utmi_data_i => utmi_data_to_phy_s,
      utmi_data_o => utmi_data_from_phy_s,
      utmi_system_i => utmi_system_to_phy_s,
      utmi_system_o => utmi_system_from_phy_s
      );

  device: gatecap.adapter_usb.usb_utmi_adapter
    generic map(
      apb_config_c => apb_config_c,
      burst_length_l2_c => burst_length_l2_c,
      fingerprint_c => fingerprint_c,
      clock_frequency_c => clock_frequency_c,
      descriptor_base_c => descriptor_base_c
      )
    port map(
      reset_n_i => reset_n_i,

      online_o => online_o,

      utmi_data_o => utmi_data_to_phy_s,
      utmi_data_i => utmi_data_from_phy_s,
      utmi_system_o => utmi_system_to_phy_s,
      utmi_system_i => utmi_system_from_phy_s,

      apb_o => apb_o,
      apb_i => apb_i
      );

end architecture;
