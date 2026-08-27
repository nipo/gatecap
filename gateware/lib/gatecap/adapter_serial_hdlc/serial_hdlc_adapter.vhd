library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_bnoc, nsl_line_coding, nsl_math, nsl_synthesis, nsl_uart,
  gatecap;

-- Serial front end of a rack: two wires, an 8n1 UART carrying HDLC frames.
--
-- HDLC delimits the frames and checks them; XON/XOFF in both directions keeps
-- either end from overrunning the other, since neither modem line is on the
-- rack. A frame is exactly one command or one response, so the stream adapter
-- below sees whole frames on the byte-wide AXI4-Stream the bridge speaks.
entity serial_hdlc_adapter is
  generic (
    apb_config_c : nsl_amba.apb.config_t;
    -- Rate of clock_i in Hz. The UART needs its bit period in clock cycles,
    -- which is this divided by the baud rate.
    clock_frequency_c : natural;
    -- Bits per second on the line, 8n1. The one thing the two ends must
    -- agree on.
    baud_rate_c : natural;
    burst_length_l2_c : natural;
    descriptor_base_c : natural := 0
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    uart_rx_i : in std_ulogic;
    uart_tx_o : out std_ulogic;

    apb_o : out nsl_amba.apb.master_t;
    apb_i : in nsl_amba.apb.slave_t
    );
end entity;

architecture rtl of serial_hdlc_adapter is

  -- A bit period of fewer than this many clock cycles leaves the receiver
  -- nothing to sample the line with.
  constant cycles_per_bit_min_c : natural := 4;
  constant rate_message_c : string :=
    "the UART baud rate must leave at least 4 clock cycles per bit";

  -- Bit period in clock cycles, less one.
  constant divisor_c : unsigned :=
    nsl_math.arith.to_unsigned_auto(clock_frequency_c / baud_rate_c - 1);

  -- A frame is held until its check sequence clears, so the buffer must hold
  -- the longest command the host can send: a full-burst write, its opcode and
  -- its address.
  constant frame_max_size_c : natural :=
    2**burst_length_l2_c * 2**apb_config_c.data_bus_width_l2 + 8;

  signal uart_rx_s : nsl_bnoc.pipe.pipe_bus_t;
  signal uart_tx_s : nsl_bnoc.pipe.pipe_bus_t;
  signal hdlc_rx_s : nsl_bnoc.pipe.pipe_bus_t;
  signal hdlc_tx_s : nsl_bnoc.pipe.pipe_bus_t;

  -- XON/XOFF state, each direction telling the other whether bytes may flow.
  signal peer_ready_s : std_ulogic;
  signal rx_ready_s : std_ulogic;

  signal rx_framed_s : nsl_bnoc.framed.framed_bus_t;
  signal tx_framed_s : nsl_bnoc.framed.framed_bus_t;
  signal rx_s : nsl_amba.axi4_stream.bus_t;
  signal tx_s : nsl_amba.axi4_stream.bus_t;

begin

  assert clock_frequency_c >= cycles_per_bit_min_c * baud_rate_c
    report rate_message_c
    severity failure;

  uart_rate_check: nsl_synthesis.assertion.synth_assert
    generic map(
      message_c => rate_message_c,
      condition_c => clock_frequency_c >= cycles_per_bit_min_c * baud_rate_c
      )
    port map(
      unused_i => '0'
      );

  -- 8n1, no modem lines: flow control is in band.
  uart: nsl_uart.transactor.uart8
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      divisor_i => divisor_c,
      rx_i => uart_rx_i,
      tx_o => uart_tx_o,
      rx_data_o => uart_rx_s.req,
      rx_data_i => uart_rx_s.ack,
      tx_data_i => uart_tx_s.req,
      tx_data_o => uart_tx_s.ack
      );

  flow_rx: nsl_uart.flow_control.xonxoff_rx
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      peer_ready_o => peer_ready_s,
      rx_ready_o => rx_ready_s,
      serdes_i => uart_rx_s.req,
      serdes_o => uart_rx_s.ack,
      rx_o => hdlc_rx_s.req,
      rx_i => hdlc_rx_s.ack
      );

  flow_tx: nsl_uart.flow_control.xonxoff_tx
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      can_transmit_i => peer_ready_s,
      can_receive_i => rx_ready_s,
      tx_i => hdlc_tx_s.req,
      tx_o => hdlc_tx_s.ack,
      serdes_o => uart_tx_s.req,
      serdes_i => uart_tx_s.ack
      );

  unframer: nsl_line_coding.hdlc.hdlc_framed_unframer
    generic map(
      frame_max_size_c => frame_max_size_c
      )
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      hdlc_i => hdlc_rx_s.req,
      hdlc_o => hdlc_rx_s.ack,
      framed_o => rx_framed_s.req,
      framed_i => rx_framed_s.ack
      );

  framer: nsl_line_coding.hdlc.hdlc_framed_framer
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      framed_i => tx_framed_s.req,
      framed_o => tx_framed_s.ack,
      hdlc_o => hdlc_tx_s.req,
      hdlc_i => hdlc_tx_s.ack
      );

  rx_adapter: nsl_bnoc.axi_adapter.framed_to_axi4_stream
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      framed_i => rx_framed_s.req,
      framed_o => rx_framed_s.ack,
      axi_o => rx_s.m,
      axi_i => rx_s.s
      );

  tx_adapter: nsl_bnoc.axi_adapter.axi4_stream_to_framed
    port map(
      clock_i => clock_i,
      reset_n_i => reset_n_i,
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
      clock_i => clock_i,
      reset_n_i => reset_n_i,
      rx_i => rx_s.m,
      rx_o => rx_s.s,
      tx_o => tx_s.m,
      tx_i => tx_s.s,
      apb_o => apb_o,
      apb_i => apb_i
      );

end architecture;
