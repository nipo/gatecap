library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_usb, nsl_clocking, nsl_hwdep;
library gatecap_generated;

-- Gatecap over USB Full Speed on a Tang Console: three IOs to a USB socket,
-- and the rack is a device on the bus.
--
-- The board's oscillator is 50 MHz and the phy wants 60, so a PLL sits in
-- between and its lock is the rack's reset. Everything from the line to the
-- rack's registers runs on that 60 MHz clock; the control/status panel runs on
-- the board clock instead, which is what lets it answer while the USB domain
-- is held in reset.
entity main is
  port (
    usb_dp_io, usb_dn_io, usb_dp_pull_io : inout std_logic;
    usb_unused_io : inout std_logic_vector(0 to 5);

    s_n_i: in std_ulogic_vector(1 to 2);
    done_led_o: inout std_logic;
    ready_led_o: inout std_logic;
    clk_i: in std_ulogic
  );
end main;

architecture arch of main is

  constant clock_ext_hz_c : integer := 50e6;
  constant clock_usb_hz_c : integer := 60e6;

  signal reset_ext_n_s, reset_usb_n_s : std_ulogic;
  signal clock_ext_s, clock_usb_s : std_ulogic;

  signal usb_o : nsl_usb.io.usb_io_c;
  signal usb_i : nsl_usb.io.usb_io_s;
  signal online_s : std_ulogic;

begin

  usb_unused_io <= (others => 'Z');

  reset_ext_n_s <= s_n_i(1);

  clock_ext_buffer: nsl_hwdep.clock.clock_buffer
    port map(
      clock_i => clk_i,
      clock_o => clock_ext_s
      );

  pll: nsl_clocking.pll.pll_basic
    generic map(
      input_hz_c => clock_ext_hz_c,
      output_hz_c => clock_usb_hz_c
      )
    port map(
      clock_i => clock_ext_s,
      reset_n_i => reset_ext_n_s,

      clock_o => clock_usb_s,
      locked_o => reset_usb_n_s
      );

  -- The pads: the differential pair, and the IO pulling D+ up through a
  -- 1.5 kohm resistor to say a Full Speed device is there.
  io_driver: nsl_usb.io.io_fs_driver
    port map(
      bus_o => usb_i,
      bus_i => usb_o,
      bus_io.dp => usb_dp_io,
      bus_io.dm => usb_dn_io,
      dp_pullup_control_io => usb_dp_pull_io
      );

  cap: gatecap_generated.demo_package.demo_core
    generic map(
      burst_length_l2_c => 8
      )
    port map(
      clock_i => clock_usb_s,
      reset_n_i => reset_usb_n_s,

      usb_o => usb_o,
      usb_i => usb_i,
      online_o => online_s,

      panel_clock_i => clock_ext_s,
      panel_reset_n_i => reset_ext_n_s,
      panel_led_o => ready_led_o,
      panel_s2_i => "not"(s_n_i(2))
      );

  done_led_o <= online_s;

end arch;
