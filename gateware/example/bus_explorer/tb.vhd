library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library gatecap, nsl_amba, nsl_simulation;
-- The APB package is the one used unqualified here: the target bus is what
-- this bench models. The transport's stream types are named in full, the two
-- packages having a config_t, a master_t and a slave_t apiece.
use nsl_amba.apb.all;
library gatecap_generated;
use gatecap_generated.busx_pkg.all;

-- UDP harness for an instrument-only rack whose one instrument is a bus
-- explorer: a UDP socket gateway in front of the rack, and a stub device
-- behind the explorer's target port. Driven externally by the acrobe host;
-- runs until killed.
--
-- The stub is the device a bring-up session explores, small enough to assert
-- against and awkward enough to be worth exploring:
--
--   0x000 ID       read-only, a constant; a write is accepted and ignored
--   0x004 CTRL     read/write, three fields (ENABLE, MODE, GAIN)
--   0x008 STATUS   read-only, computed from CTRL -- writing a field of one
--                  register and watching another react is the whole shape of
--                  the work this instrument exists for
--   0x00c SCRATCH  read/write, a plain word
--   0x010 MIRROR   read-only, SCRATCH inverted: a second feedback path, and
--                  one no field decode is needed to check
--   0x020 FAULT    always answers pslverr, as does every unmapped address
--   0x024 SLOW     withholds pready far longer than the engine's timeout, so
--                  the host sees a timeout and the late answer is drained
--
-- The register map above is what host/tests/data/demo_device.svd describes,
-- and what the description's `map: gatecap-demo-device` names.
--
-- The target bus runs on its own clock, unrelated to the host clock, so every
-- access crosses the instrument's domain boundary in both directions.
entity tb is
end entity;

architecture sim of tb is

  constant stream_cfg_c : nsl_amba.axi4_stream.config_t :=
    nsl_amba.axi4_stream.config(1, last => true);

  constant address_width_c : natural := 12;
  constant data_width_c : natural := 32;
  constant target_cfg_c : config_t :=
    gatecap.bus_explorer.target_apb_config(address_width_c, data_width_c);

  -- Target map, byte addresses on a 32-bit bus.
  constant REG_ID_C : natural := 16#000#;
  constant REG_CTRL_C : natural := 16#004#;
  constant REG_STATUS_C : natural := 16#008#;
  constant REG_SCRATCH_C : natural := 16#00c#;
  constant REG_MIRROR_C : natural := 16#010#;
  constant REG_FAULT_C : natural := 16#020#;
  constant REG_SLOW_C : natural := 16#024#;

  constant ID_VALUE_C : unsigned(31 downto 0) := x"5ca1ab1e";
  constant SLOW_VALUE_C : unsigned(31 downto 0) := x"5104da7a";

  -- Target cycles SLOW makes an access wait: long enough that the engine
  -- always gives up first, short enough that it never eats the next command.
  --
  -- The engine abandons an access 1024 host cycles (6.14 us) after the
  -- COMMAND write; this waits 2048 target cycles (7.17 us) from psel, so the
  -- timeout fires with a microsecond to spare and the late answer -- the one
  -- the engine's drain step exists to discard -- lands about a microsecond
  -- after the host has been told the access failed. The core holds the
  -- access open until then, so a stall much longer than the timeout would
  -- still be running when the host fired its next command, and would time
  -- that one out too.
  constant SLOW_CYCLES_C : natural := 2048;

  signal clock_s : std_ulogic;
  signal bus_clock_s : std_ulogic;
  signal reset_n_s : std_ulogic;

  signal rx_cmd_s : nsl_amba.axi4_stream.master_t;
  signal rx_rdy_s : nsl_amba.axi4_stream.slave_t;
  signal tx_rsp_s : nsl_amba.axi4_stream.master_t;
  signal tx_rdy_s : nsl_amba.axi4_stream.slave_t;

  signal target_m : master_t;
  signal target_s : slave_t;

  signal ctrl_s : unsigned(31 downto 0) := (others => '0');
  signal scratch_s : unsigned(31 downto 0) := (others => '0');
  signal slow_count_s : natural range 0 to SLOW_CYCLES_C := 0;

  -- STATUS, computed from CTRL: the enable and mode echoed, plus a RUNNING
  -- bit that is only set in one of the three modes.
  function status_of(ctrl : unsigned) return unsigned is
    variable ret : unsigned(31 downto 0) := (others => '0');
  begin
    ret(0) := ctrl(0);
    ret(2 downto 1) := ctrl(2 downto 1);
    if ctrl(0) = '1' and ctrl(2 downto 1) = "01" then
      ret(3) := '1';
    end if;
    ret(15 downto 8) := ctrl(11 downto 4);
    return ret;
  end function;

begin

  simdrv: nsl_simulation.driver.simulation_driver
    generic map(
      clock_count => 2,
      reset_count => 1,
      done_count => 1
      )
    port map(
      clock_period(0) => 6 ns,        -- host / APB clock
      clock_period(1) => 3500 ps,     -- target bus clock, unrelated
      reset_duration => (others => 44 ns),
      clock_o(0) => clock_s,
      clock_o(1) => bus_clock_s,
      reset_n_o(0) => reset_n_s,
      done_i => "0"
      );

  net: nsl_amba.stream_to_udp.axi4_stream_udp_gateway
    generic map(
      config_c => stream_cfg_c,
      bind_port_c => 4253
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      tx_i => tx_rsp_s,
      tx_o => tx_rdy_s,
      rx_o => rx_cmd_s,
      rx_i => rx_rdy_s
      );

  dut: busx_core
    generic map(
      stream_config_c => stream_cfg_c,
      burst_length_l2_c => 8
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      rx_i => rx_cmd_s,
      rx_o => rx_rdy_s,
      tx_o => tx_rsp_s,
      tx_i => tx_rdy_s,
      dut_busclk_i => bus_clock_s,
      dut_reset_n_i => reset_n_s,
      dut_target_o => target_m,
      dut_target_i => target_s
      );

  -- Stub device: combinational decode, registered storage. Every address the
  -- map does not hold answers pslverr, which is what a host exploring an
  -- unknown map has to be able to survive.
  device_decode: process(target_m, ctrl_s, scratch_s, slow_count_s)
    variable addr_v : unsigned(address_width_c-1 downto 0);
    variable error_v : boolean;
    variable ready_v : boolean;
    variable value_v : unsigned(31 downto 0);
  begin
    target_s <= response_idle(target_cfg_c);

    if is_selected(target_cfg_c, target_m) then
      addr_v := address(target_cfg_c, target_m);
      error_v := false;
      ready_v := true;
      value_v := (others => '0');

      case to_integer(addr_v) is
        when REG_ID_C => value_v := ID_VALUE_C;
        when REG_CTRL_C => value_v := ctrl_s;
        when REG_STATUS_C => value_v := status_of(ctrl_s);
        when REG_SCRATCH_C => value_v := scratch_s;
        when REG_MIRROR_C => value_v := not scratch_s;
        when REG_SLOW_C =>
          value_v := SLOW_VALUE_C;
          ready_v := slow_count_s = SLOW_CYCLES_C;
        when others => error_v := true;
      end case;

      if is_write(target_cfg_c, target_m) then
        target_s <= write_response(target_cfg_c, error => error_v,
                                   ready => ready_v);
      else
        target_s <= read_response(target_cfg_c, value => value_v,
                                  error => error_v, ready => ready_v);
      end if;
    end if;
  end process;

  device_store: process(bus_clock_s, reset_n_s)
    variable addr_v : unsigned(address_width_c-1 downto 0);
  begin
    if rising_edge(bus_clock_s) then
      -- The stall counter runs while SLOW is selected and is dropped the
      -- moment the access ends, so the next access to it stalls again.
      if is_selected(target_cfg_c, target_m)
        and address(target_cfg_c, target_m)
            = to_unsigned(REG_SLOW_C, address_width_c) then
        if slow_count_s /= SLOW_CYCLES_C then
          slow_count_s <= slow_count_s + 1;
        end if;
      else
        slow_count_s <= 0;
      end if;

      if is_access(target_cfg_c, target_m)
        and is_write(target_cfg_c, target_m) then
        addr_v := address(target_cfg_c, target_m);
        -- The read-only registers accept a write and keep their value: a
        -- register file that answered pslverr to every write would not tell
        -- read-only apart from absent.
        case to_integer(addr_v) is
          when REG_CTRL_C =>
            ctrl_s <= resize(value(target_cfg_c, target_m), 32);
          when REG_SCRATCH_C =>
            scratch_s <= resize(value(target_cfg_c, target_m), 32);
          when others => null;
        end case;
      end if;
    end if;

    if reset_n_s = '0' then
      ctrl_s <= (others => '0');
      scratch_s <= (others => '0');
      slow_count_s <= 0;
    end if;
  end process;

end architecture;
