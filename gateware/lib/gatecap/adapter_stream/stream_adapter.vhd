library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_data, gatecap;
use nsl_data.bytestream.all;
use gatecap.descriptor.all;

-- AXI4-Stream front end of a rack: one byte per beat in each direction,
-- command frames in, response frames out.
--
-- The stream-to-APB bridge is the whole adapter: it serves the identify
-- reply describing the map the host is about to walk, and turns read and
-- write commands into APB transfers. What carries the two streams -- a
-- socket in simulation, a USB or network endpoint on hardware -- is the
-- instantiating design's business.
entity stream_adapter is
  generic (
    -- Requester geometry towards the rack.
    apb_config_c : nsl_amba.apb.config_t;
    -- Geometry of both link streams. One byte per beat, with last.
    stream_config_c : nsl_amba.axi4_stream.config_t;
    -- Words the host may read in one command, log2. It is the host's read
    -- budget, not a property of the wire.
    burst_length_l2_c : natural;
    -- Byte address the descriptor sits at, as the identify reply states it.
    descriptor_base_c : natural := 0
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    rx_i : in nsl_amba.axi4_stream.master_t;
    rx_o : out nsl_amba.axi4_stream.slave_t;
    tx_o : out nsl_amba.axi4_stream.master_t;
    tx_i : in nsl_amba.axi4_stream.slave_t;

    apb_o : out nsl_amba.apb.master_t;
    apb_i : in nsl_amba.apb.slave_t
    );
end entity;

architecture rtl of stream_adapter is

  -- Transport-level self-description: the map geometry the host reads
  -- before anything else.
  constant identify_c : byte_string := bridge_identify(
    addr_bits => apb_config_c.address_width,
    data_bytes_l2 => apb_config_c.data_bus_width_l2,
    burst_length_l2 => burst_length_l2_c,
    descriptor_base => descriptor_base_c);

begin

  bridge: nsl_amba.stream_apb.apb_stream_bridge
    generic map(
      apb_config_c => apb_config_c,
      stream_config_c => stream_config_c,
      burst_length_l2_c => burst_length_l2_c,
      identify_c => identify_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      rx_i => rx_i,
      rx_o => rx_o,
      tx_o => tx_o,
      tx_i => tx_i,
      apb_o => apb_o,
      apb_i => apb_i
      );

end architecture;
