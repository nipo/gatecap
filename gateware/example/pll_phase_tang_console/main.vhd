library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_usb, nsl_clocking, nsl_hwdep, nsl_bnoc, nsl_io, nsl_indication, nsl_amba;
use nsl_clocking.pll.all;
library gatecap_generated;

-- Phase-shift bench on the Sipeed Tang Console 60k, reached over the USB2
-- transport of the tang_console example.
--
-- One PLL produces four clocks of the same rate, three of them shifted by an
-- eighth, three eighths and five eighths of a cycle, plus a clock at
-- twenty-four times that rate to measure them with. An eighth of a cycle is
-- three cycles of the measuring clock, so the counts the meter reports are
-- expected to be 3, 9 and 15 -- and 21, 15 and 9 would say the block shifts
-- the other way, which is the one thing the datasheet does not settle.
--
-- The measuring clock carries half a cycle of shift of its own, so that the
-- unshifted clock's edges fall between its own rather than onto them.
entity main is
  port (
    usb_dxp_io : inout std_logic;
    usb_dxn_io : inout std_logic;
    usb_rxdp_i : in std_logic;
    usb_rxdn_i : in std_logic;
    usb_pullup_en_o : out std_logic;
    usb_term_dp_io : inout std_logic;
    usb_term_dn_io : inout std_logic;

    s_n_i: in std_ulogic_vector(1 to 2);
    done_led_o: inout std_logic;
    ready_led_o: inout std_logic;
    clk_i: in std_ulogic
  );
end main;

architecture arch of main is

  constant clock_ext_s_hz_c : integer := 50e6;
  constant clock_usb_s_hz_c : integer := 60e6;
  constant max_txn_length_l2_c : integer := 5;

  constant test_hz_c : natural := 10_000_000;
  constant meter_ratio_c : natural := 24;

  constant cfg_c : pll_config_t := pll_config(
    input_hz => clock_ext_s_hz_c,
    o0 => pll_output(test_hz_c),
    o1 => pll_output(test_hz_c, phase => (num => 1, den => 8)),
    o2 => pll_output(test_hz_c, phase => (num => 3, den => 8)),
    o3 => pll_output(test_hz_c, phase => (num => 5, den => 8)),
    o4 => pll_output(test_hz_c * meter_ratio_c, phase => (num => 1, den => 2)));

  constant count_width_c : natural := 8;

  signal pll_locked_s : std_ulogic;
  signal utmi_s : nsl_usb.utmi.utmi8_bus;

  signal app_reset_n_s, reset_merged_n_s, reset_n_s,
    gatecap_reset_n_s: std_ulogic;
  signal online : std_ulogic;

  signal clock_usb_s, clock_ext_s : std_ulogic;

  signal pll_clock_s : std_ulogic_vector(0 to cfg_c.output_count-1);
  signal meter_reset_n_s, settled_s : std_ulogic;
  signal lag_match_s, lead_match_s : std_ulogic;
  signal blink_s : unsigned(24 downto 0);
  signal offset_s : unsigned(4*count_width_c-1 downto 0);

  type pipe_io is
  record
    cmd, rsp : nsl_bnoc.pipe.pipe_bus_t;
  end record;

  signal gatecap_pipe_s: pipe_io;

begin

  reset_merged_n_s <= s_n_i(1);

  clock_ext_buffer: nsl_hwdep.clock.clock_buffer
    port map(
      clock_i => clk_i,
      clock_o => clock_ext_s
      );

  usb_pll: nsl_clocking.pll.pll_basic
    generic map(
      input_hz_c => clock_ext_s_hz_c,
      output_hz_c => clock_usb_s_hz_c
      )
    port map(
      clock_i => clock_ext_s,
      reset_n_i => reset_merged_n_s,

      clock_o => clock_usb_s,
      locked_o => reset_n_s
      );

  -- The meter runs on a PLL output, so it is held in reset until the PLL
  -- says it has locked.
  meter_reset_n_s <= reset_merged_n_s and pll_locked_s;

  test_pll: nsl_clocking.pll.pll_multi
    generic map(
      config_c => cfg_c
      )
    port map(
      clock_i => clock_ext_s,
      reset_n_i => reset_merged_n_s,
      clock_o => pll_clock_s,
      locked_o => pll_locked_s
      );

  hs_phy: work.softphy.gw_usb2_phy
    port map(
      clock_i => clock_usb_s,
      reset_n_i => reset_n_s,

      usb_dxp_io => usb_dxp_io,
      usb_dxn_io => usb_dxn_io,
      usb_rxdp_i => usb_rxdp_i,
      usb_rxdn_i => usb_rxdn_i,
      usb_pullup_en_o => usb_pullup_en_o,
      usb_term_dp_io => usb_term_dp_io,
      usb_term_dn_io => usb_term_dn_io,

      utmi_i => utmi_s.sie2phy,
      utmi_o => utmi_s.phy2sie
      );

  func: nsl_usb.func.serial_port
    generic map(
      vendor_id_c => x"dead",
      product_id_c => x"beef",
      device_version_c => x"0100",
      manufacturer_c => "Nipo",
      product_c => "Gatecap example",
      serial_c => "lol",
      hs_supported_c => true,
      phy_clock_rate_c => clock_usb_s_hz_c,
      self_powered_c => false
      )
    port map(
      phy_system_o => utmi_s.sie2phy.system,
      phy_system_i => utmi_s.phy2sie.system,
      phy_data_o => utmi_s.sie2phy.data,
      phy_data_i => utmi_s.phy2sie.data,

      reset_n_i => reset_n_s,

      app_reset_n_o => app_reset_n_s,
      online_o => online,

      rx_o => gatecap_pipe_s.cmd.req,
      rx_i => gatecap_pipe_s.cmd.ack,
      tx_i => gatecap_pipe_s.rsp.req,
      tx_o => gatecap_pipe_s.rsp.ack
      );

  -- What the meter is expected to report, if a phase shift delays an
  -- output: an eighth of a cycle is three cycles of the measuring clock.
  -- A block shifting the other way would report the complements instead,
  -- and each LED stands for one of the two readings.
  lag_match_s <= '1' when settled_s = '1'
                 and offset_s(2*count_width_c-1 downto count_width_c) = 3
                 and offset_s(3*count_width_c-1 downto 2*count_width_c) = 9
                 and offset_s(4*count_width_c-1 downto 3*count_width_c) = 15
                 else '0';

  lead_match_s <= '1' when settled_s = '1'
                  and offset_s(2*count_width_c-1 downto count_width_c) = 21
                  and offset_s(3*count_width_c-1 downto 2*count_width_c) = 15
                  and offset_s(4*count_width_c-1 downto 3*count_width_c) = 9
                  else '0';

  -- Two LEDs, three answers: ready states the meter has seen every clock
  -- rise, so its counts stand for something, and done tells the two
  -- readings apart -- lit for a shift that delays, blinking for one that
  -- advances, dark for neither.
  blinker: process(clock_ext_s, reset_merged_n_s) is
  begin
    if reset_merged_n_s = '0' then
      blink_s <= (others => '0');
    elsif rising_edge(clock_ext_s) then
      blink_s <= blink_s + 1;
    end if;
  end process;

  ready_led_o <= settled_s;
  done_led_o <= '1' when lag_match_s = '1'
                else blink_s(blink_s'left) when lead_match_s = '1'
                else '0';

  cap: block is
    use nsl_amba.axi4_stream.all;
    signal cap_cmd_s, cap_rsp_s: bus_t;

    type framed_io is
    record
      cmd, rsp : nsl_bnoc.framed.framed_bus_t;
    end record;
    signal gatecap_framed_s: framed_io;
  begin

    gatecap_unchunker: nsl_bnoc.chunked_link.framed_unchunker
      port map(
        reset_n_i => app_reset_n_s,
        clock_i => clock_usb_s,

        in_i => gatecap_pipe_s.cmd.req,
        in_o => gatecap_pipe_s.cmd.ack,

        reset_n_o => gatecap_reset_n_s,

        out_o => gatecap_framed_s.cmd.req,
        out_i => gatecap_framed_s.cmd.ack
        );

    gatecap_chunker: nsl_bnoc.chunked_link.framed_chunker
      generic map(
        max_txn_length_l2_c => max_txn_length_l2_c
        )
      port map(
        reset_n_i => gatecap_reset_n_s,
        clock_i => clock_usb_s,

        in_i => gatecap_framed_s.rsp.req,
        in_o => gatecap_framed_s.rsp.ack,

        out_o => gatecap_pipe_s.rsp.req,
        out_i => gatecap_pipe_s.rsp.ack
        );

    rsp_adapter: nsl_bnoc.axi_adapter.framed_to_axi4_stream
      port map(
        clock_i => clock_usb_s,
        reset_n_i => gatecap_reset_n_s,
        framed_i => gatecap_framed_s.cmd.req,
        framed_o => gatecap_framed_s.cmd.ack,
        axi_o => cap_cmd_s.m,
        axi_i => cap_cmd_s.s
        );

    cmd_adapter: nsl_bnoc.axi_adapter.axi4_stream_to_framed
      port map(
        clock_i => clock_usb_s,
        reset_n_i => gatecap_reset_n_s,
        axi_i => cap_rsp_s.m,
        axi_o => cap_rsp_s.s,
        framed_o => gatecap_framed_s.rsp.req,
        framed_i => gatecap_framed_s.rsp.ack
        );

    meter_inst: entity work.phase_meter
      generic map(
        clock_count_c => 4,
        count_width_c => count_width_c
        )
      port map(
        clock_i => pll_clock_s(0 to 3),
        meter_clock_i => pll_clock_s(4),
        meter_reset_n_i => meter_reset_n_s,
        offset_o => offset_s,
        settled_o => settled_s
        );

    rack: gatecap_generated.pll_phase_hw.pll_phase_core
      generic map(
        stream_config_c => nsl_bnoc.axi_adapter.axi4_stream_framed_config_c,
        burst_length_l2_c => 10
        )
      port map(
        clock_i => clock_usb_s,
        reset_n_i => gatecap_reset_n_s,

        rx_i => cap_cmd_s.m,
        rx_o => cap_cmd_s.s,
        tx_o => cap_rsp_s.m,
        tx_i => cap_rsp_s.s,

        phase_meter_i => pll_clock_s(4),
        phase_reset_n_i => meter_reset_n_s,
        phase_offset1_i => offset_s(2*count_width_c-1 downto count_width_c),
        phase_offset2_i => offset_s(3*count_width_c-1 downto 2*count_width_c),
        phase_offset3_i => offset_s(4*count_width_c-1 downto 3*count_width_c),
        phase_settled_i => settled_s
        );
  end block;

end arch;
