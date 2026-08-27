library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_bnoc, nsl_data, nsl_synthesis, nsl_usb, gatecap;
use nsl_data.text.all;
use gatecap.adapter_usb.all;

-- USB front end of a rack, from the UTMI+ boundary inwards: the device stack
-- and the stream-to-APB bridge behind it.
--
-- The device exposes one vendor-defined interface holding one bulk endpoint
-- pair, and the endpoint pair is a framed one: a datagram ends on a short
-- packet, and a datagram whose length is a whole number of packets ends on a
-- zero-length one. Frames are what the bridge speaks, so the pair is a
-- byte-stream link with the boundaries already drawn -- no framing of the
-- rack's own rides above it.
--
-- The descriptors are the rack's identity on the bus. Vendor and product are
-- gatecap's own and fixed: what a device with them exposes is this interface,
-- and a host may say so without opening it. The serial-number string is the
-- rack's descriptor fingerprint in hex, so two boards running different racks
-- are told apart on the bus, and one board reprogrammed with a different rack
-- is noticed.
--
-- The stack's application reset -- USB bus reset, and the host's own
-- deconfiguring of the device -- resets the bridge and the frame adapters,
-- and nothing above them. A command cut short by a bus reset leaves no half
-- state behind, and the instruments in the rack are not disturbed by a host
-- re-enumerating.
entity usb_utmi_adapter is
  generic (
    apb_config_c : nsl_amba.apb.config_t;
    burst_length_l2_c : natural;
    fingerprint_c : unsigned(31 downto 0);
    clock_frequency_c : natural := PHY_CLOCK_60M_C;
    descriptor_base_c : natural := 0
    );
  port (
    reset_n_i : in std_ulogic;

    online_o : out std_ulogic;

    utmi_data_o : out nsl_usb.utmi.utmi_data8_sie2phy;
    utmi_data_i : in nsl_usb.utmi.utmi_data8_phy2sie;
    utmi_system_o : out nsl_usb.utmi.utmi_system_sie2phy;
    utmi_system_i : in nsl_usb.utmi.utmi_system_phy2sie;

    apb_o : out nsl_amba.apb.master_t;
    apb_i : in nsl_amba.apb.slave_t
    );
end entity;

architecture rtl of usb_utmi_adapter is

  constant clock_message_c : string :=
    "the USB phy reference clock must run at 48 or 60 MHz";
  constant clock_ok_c : boolean :=
    clock_frequency_c = PHY_CLOCK_48M_C or clock_frequency_c = PHY_CLOCK_60M_C;

  -- The serial-number string: the fingerprint, eight lowercase hex digits.
  -- It is an elaboration constant like the fingerprint itself, so it is a
  -- plain string descriptor and nothing has to be sampled at run time.
  constant serial_c : string(1 to 8) :=
    to_hex_string(std_ulogic_vector(fingerprint_c));

  -- Bulk endpoint size, log2. Full Speed bulk carries 64 bytes at most, and
  -- a shorter packet only costs transactions.
  constant mps_l2_c : natural := 6;

  signal app_reset_n_s : std_ulogic;
  signal clock_s : std_ulogic;

  signal rx_framed_s, tx_framed_s : nsl_bnoc.framed.framed_bus_t;
  signal rx_s, tx_s : nsl_amba.axi4_stream.bus_t;

begin

  assert clock_ok_c
    report clock_message_c
    severity failure;

  clock_rate_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => clock_message_c,
      condition_c => clock_ok_c
      )
    port map(
      unused_i => '0'
      );

  clock_s <= utmi_system_i.clock;

  func: nsl_usb.func.vendor_framed_pair
    generic map(
      vendor_id_c => VENDOR_ID_C,
      product_id_c => PRODUCT_ID_C,
      device_version_c => x"0100",
      manufacturer_c => "Gatecap",
      product_c => "Gatecap rack",
      serial_c => serial_c,
      hs_supported_c => false,
      self_powered_c => false,
      phy_clock_rate_c => clock_frequency_c,
      framed_fs_mps_l2_c => mps_l2_c
      )
    port map(
      reset_n_i => reset_n_i,
      app_reset_n_o => app_reset_n_s,
      online_o => online_o,

      phy_system_o => utmi_system_o,
      phy_system_i => utmi_system_i,
      phy_data_o => utmi_data_o,
      phy_data_i => utmi_data_i,

      out_o => rx_framed_s.req,
      out_i => rx_framed_s.ack,
      in_i => tx_framed_s.req,
      in_o => tx_framed_s.ack
      );

  rx_adapter: nsl_bnoc.axi_adapter.framed_to_axi4_stream
    port map(
      clock_i => clock_s,
      reset_n_i => app_reset_n_s,
      framed_i => rx_framed_s.req,
      framed_o => rx_framed_s.ack,
      axi_o => rx_s.m,
      axi_i => rx_s.s
      );

  tx_adapter: nsl_bnoc.axi_adapter.axi4_stream_to_framed
    port map(
      clock_i => clock_s,
      reset_n_i => app_reset_n_s,
      axi_i => tx_s.m,
      axi_o => tx_s.s,
      framed_o => tx_framed_s.req,
      framed_i => tx_framed_s.ack
      );

  stream: gatecap.adapter_stream.stream_adapter
    generic map(
      apb_config_c => apb_config_c,
      stream_config_c => nsl_bnoc.axi_adapter.axi4_stream_framed_config_c,
      burst_length_l2_c => burst_length_l2_c,
      descriptor_base_c => descriptor_base_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => app_reset_n_s,
      rx_i => rx_s.m,
      rx_o => rx_s.s,
      tx_o => tx_s.m,
      tx_i => tx_s.s,
      apb_o => apb_o,
      apb_i => apb_i
      );

end architecture;
