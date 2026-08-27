library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_coresight, nsl_synthesis;

-- Serial-wire debug front end of a rack: two wires, and a stock SWD debug
-- probe on the other end.
--
-- The adapter carries a whole debug port: a serial-wire DP, one Mem-AP behind
-- it, and an AXI-to-APB bridge turning the AP's memory accesses into the
-- transfers the rack answers. Nothing above the wire is gatecap-specific -- a
-- debugger walks the DP and the AP as it would any other target -- and the
-- AP's identification register is what tells the host it is looking at a
-- gatecap rack, so no transport-level identify blob is involved: the
-- descriptor sits at the bottom of the AP's memory space and the host reads
-- it straight away.
--
-- Two APB configurations meet here. The Mem-AP addresses a flat 32-bit space
-- and the AXI-to-APB bridge requires the two sides of it to match, so the
-- bridge speaks 32 address bits; the rack's own map is narrower, and its
-- configuration applies from its router inward, where the top address bits
-- are simply not decoded. The data width is the rack's on both sides, since
-- that is the one thing the bridge does carry through.
entity swd_adapter is
  generic (
    apb_config_c : nsl_amba.apb.config_t;
    -- The descriptor's byte address. This link states no geometry of its own
    -- -- the host reads the descriptor at the bottom of the AP's memory space
    -- -- so only the pinned base is representable.
    descriptor_base_c : natural := 0
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    -- Serial-wire debug pins: the clock and the data line's input, output and
    -- direction.
    swd_i : in nsl_coresight.swd.swd_slave_i;
    swd_o : out nsl_coresight.swd.swd_slave_o;

    apb_o : out nsl_amba.apb.master_t;
    apb_i : in nsl_amba.apb.slave_t
    );
end entity;

architecture rtl of swd_adapter is

  constant base_message_c : string :=
    "a serial-wire link carries no descriptor base: it must be pinned at 0";

  -- JEP106 identity of a stock ARM serial-wire DP.
  constant dp_idr_c : unsigned(31 downto 0) := x"0ba00477";
  -- Access-port identification register. Continuation 3, identity 0x6d is a
  -- reserved JEP106 code, so this value can only be a gatecap rack; the host
  -- binds its driver to it.
  constant ap_idr_c : unsigned(31 downto 0) := x"04ed0001";
  -- No CoreSight ROM table below the AP: an all-ones base is the encoding for
  -- "none present", which keeps a debugger from walking into the map.
  constant rom_base_c : unsigned(31 downto 0) := x"ffffffff";
  -- The Mem-AP's address space, and hence the bridge's on both sides.
  constant address_width_c : natural := 32;
  constant data_bus_width_c : natural := 8 * 2**apb_config_c.data_bus_width_l2;

  -- The Mem-AP's memory side. Its address space is flat and 32 bits wide, and
  -- the bridge below it wants the same geometry on both of its sides.
  constant axi_config_c : nsl_amba.axi4_mm.config_t :=
    nsl_amba.axi4_mm.config(address_width => address_width_c,
                            data_bus_width => data_bus_width_c);

  -- The bridge's completer side, matching it. The rack's own map is narrower:
  -- its router decodes the low address bits and the rest go unread.
  constant bridge_apb_config_c : nsl_amba.apb.config_t :=
    nsl_amba.apb.config(address_width => address_width_c,
                        data_bus_width => data_bus_width_c);

  -- The DP's access-port bus, then the one leg of it this rack answers on.
  signal dap_s : nsl_coresight.dapbus.dapbus_bus;
  signal ap_s : nsl_coresight.dapbus.dapbus_bus;
  signal axi_s : nsl_amba.axi4_mm.bus_t;
  signal ctrl_s : std_ulogic_vector(31 downto 0);
  signal stat_s : std_ulogic_vector(31 downto 0);

begin

  assert descriptor_base_c = 0
    report base_message_c
    severity failure;

  descriptor_base_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => base_message_c,
      condition_c => descriptor_base_c = 0
      )
    port map(
      unused_i => '0'
      );

  dp: nsl_coresight.dp.swdp_sync
    generic map(
      idr => dp_idr_c
      )
    port map(
      ref_clock_i => clock_i,
      ref_reset_n_i => reset_n_i,
      swd_i => swd_i,
      swd_o => swd_o,
      dap_o => dap_s.ms,
      dap_i => dap_s.sm,
      ctrl_o => ctrl_s,
      stat_i => stat_s,
      abort_o => open
      );

  -- Power-up and reset requests are granted as they come: nothing here is
  -- powered separately from the rest of the design, so each acknowledge
  -- follows its request.
  handshake: process(ctrl_s)
  begin
    stat_s <= ctrl_s;
    stat_s(27) <= ctrl_s(26);
    stat_s(29) <= ctrl_s(28);
    stat_s(31) <= ctrl_s(30);
  end process;

  -- One access port on the DP, and it is the rack's.
  interconnect: nsl_coresight.dapbus.dapbus_interconnect
    generic map(
      access_port_count => 1
      )
    port map(
      s_i => dap_s.ms,
      s_o => dap_s.sm,
      m_i(0) => ap_s.sm,
      m_o(0) => ap_s.ms
      );

  -- The access port the host recognises the rack by. It is not secured apart
  -- from the design it lives in, so the secure-access input is tied high.
  mem_ap: nsl_coresight.ap.ap_axi4_lite
    generic map(
      rom_base => rom_base_c,
      config_c => axi_config_c,
      idr => ap_idr_c
      )
    port map(
      clk_i => clock_i,
      reset_n_i => reset_n_i,
      dbgen_i => ctrl_s(28),
      spiden_i => '1',
      dap_i => ap_s.ms,
      dap_o => ap_s.sm,
      axi_o => axi_s.m,
      axi_i => axi_s.s
      );

  bridge: nsl_amba.axi_apb.axi4_apb_bridge
    generic map(
      axi_config_c => axi_config_c,
      apb_config_c => bridge_apb_config_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      irq_n_o => open,
      axi_i => axi_s.m,
      axi_o => axi_s.s,
      apb_o => apb_o,
      apb_i => apb_i
      );

end architecture;
