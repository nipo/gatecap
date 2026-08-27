library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_usb;

package adapter_usb is

  -- Communication adapter for a USB Full Speed link: the device stack, a
  -- vendor-defined interface carrying one bulk endpoint pair, and the APB
  -- requester below carrying the host's accesses into the rack.
  --
  -- The device is a gatecap rack and says so on the bus: vendor 0x1500,
  -- product 0xdeca, one interface of class 0xff, and the rack's own
  -- descriptor fingerprint as the serial-number string. A host therefore
  -- knows what it has found, and which rack it is, before it opens anything.
  --
  -- Framing is the endpoint's own: a datagram ends on a short packet, and a
  -- datagram that is a whole number of packets ends on a zero-length one. One
  -- datagram is exactly one bridge command or one bridge response, so above
  -- the endpoint the link is the byte-stream one every other transport ends
  -- in.
  --
  -- The interface is the only one the device exposes, and the endpoint pair is
  -- the only one behind it: nothing multiplexes, so the host never has to be
  -- told which endpoint a rack sits on.
  --
  -- USB bus reset is not the rack's reset. It restarts the device stack and
  -- the bridge behind it -- a command interrupted by a reset is abandoned, not
  -- half-applied -- while the instruments keep running on the rack's own
  -- reset. Re-enumerating a bus does not throw a capture away.
  constant VENDOR_ID_C : unsigned(15 downto 0) := x"1500";
  constant PRODUCT_ID_C : unsigned(15 downto 0) := x"deca";

  -- Reference clock rates the Full Speed phy has a recovery loop for.
  constant PHY_CLOCK_48M_C : natural := 48_000_000;
  constant PHY_CLOCK_60M_C : natural := 60_000_000;

  -- The whole front end: the Full Speed phy, the device stack and the bridge.
  --
  -- clock_i is the phy's reference clock and the rack's host clock alike, so
  -- the rack runs at the rate the phy needs -- 60 MHz, or 48 MHz where a
  -- board's PLL reaches that and not the other.
  --
  -- The link side is a pair of records, not pads: dp, dm and the two output
  -- enables (the differential driver's, and the D+ pullup's, which is how the
  -- device announces itself). What turns them into pads is the instantiating
  -- design's business -- nsl_usb.io.io_fs_driver does it for plain IOs.
  component usb_adapter is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      -- Words the host may read in one command, log2. It is the host's read
      -- budget, not a property of the wire: a datagram is as long as it needs
      -- to be.
      burst_length_l2_c : natural;
      -- Per-instance UID of the rack's descriptor. It is the device's serial
      -- number string, eight lowercase hex digits, so a host picks a rack out
      -- of several on the bus without opening any of them.
      fingerprint_c : unsigned(31 downto 0);
      -- Rate of clock_i in Hz. Only the two rates above are phy reference
      -- clocks; anything else is refused at elaboration.
      clock_frequency_c : natural := PHY_CLOCK_60M_C;
      -- Byte address the descriptor sits at, as the identify reply states it.
      descriptor_base_c : natural := 0
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      -- Line drive and line state. dp_pullup_en is the device-presence
      -- signal: it is asserted once the stack is ready to be enumerated.
      usb_o : out nsl_usb.io.usb_io_c;
      usb_i : in nsl_usb.io.usb_io_s;

      -- The host has configured the device and the link is usable. A LED's
      -- worth of status, and nothing the rack itself depends on.
      online_o : out std_ulogic;

      apb_o : out nsl_amba.apb.master_t;
      apb_i : in nsl_amba.apb.slave_t
      );
  end component;

  -- The same front end from the UTMI+ boundary inwards: the device stack and
  -- the bridge, with the phy left to the instantiating design. This is what a
  -- board with a phy of its own -- a ULPI transceiver, a hard macro -- binds
  -- instead of the entity above.
  --
  -- The clock is the phy's, taken from utmi_system_i, as UTMI+ has it.
  component usb_utmi_adapter is
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
  end component;

end package;
