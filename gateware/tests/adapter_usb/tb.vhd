library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, nsl_clocking, nsl_data, nsl_simulation, nsl_usb, gatecap;
use nsl_data.bytestream.all;
use nsl_data.crc.all;
use nsl_data.text.all;
use nsl_usb.usb.all;
use nsl_usb.testing.all;
use gatecap.testing.all;

-- usb_utmi_adapter against the conformance sequence, over a USB host model.
--
-- The bench is a host: it resets the bus, addresses and configures the device,
-- reads the two descriptors the rack's identity is in, and then runs the
-- conformance sequence through the bulk endpoint pair -- one command datagram
-- out, one response datagram in. The phy is not in the picture; the model
-- drives UTMI+ directly, which is where the clock comes from too.
--
-- Every packet the device sends is compared as it came off the wire, PID byte
-- and CRC included, and a mismatch is fatal.
entity tb is
end entity;

architecture sim of tb is

  -- An arbitrary fingerprint: what matters is that this exact value comes
  -- back as the serial-number string.
  constant fingerprint_c : unsigned(31 downto 0) := x"a1b2c3d4";
  constant serial_c : string := to_hex_string(std_ulogic_vector(fingerprint_c));

  constant dev_addr_c : device_address_t := "0100100";
  constant data_ep_c : endpoint_no_t := x"1";
  constant control_ep_c : endpoint_no_t := x"0";

  -- The device descriptor the adapter is expected to publish: Full Speed
  -- only, 64-byte control endpoint, gatecap's vendor and product, and the
  -- three string indices manufacturer / product / serial.
  constant device_descriptor_c : byte_string :=
    from_hex("12011001000000400015cade000101020301");
  constant serial_descriptor_c : byte_string :=
    nsl_usb.descriptor.string_from_ascii(serial_c);

  signal p2s : utmi8_p2s;
  signal s2p : utmi8_s2p;

  signal reset_n_s, dut_reset_n_s, online_s : std_ulogic;
  signal apb_s : nsl_amba.apb.bus_t;

  -- A packet as the wire carries it: PID byte, payload, and the data CRC
  -- where the packet has one.
  function packet_of(pid : pid_t;
                     data : byte_string := null_byte_string)
    return byte_string
  is
    variable head_v : byte_string(0 to 0);
  begin
    head_v(0) := pid_byte(pid);
    return head_v & data;
  end function;

  function with_crc(data : byte_string) return byte_string
  is
    constant state_c : crc_state_t :=
      crc_update(data_crc_params_c, crc_init(data_crc_params_c), data);
  begin
    return data & crc_spill(data_crc_params_c, state_c);
  end function;

  function data_pid_of(toggle : std_ulogic) return pid_t
  is
  begin
    if toggle = '0' then
      return PID_DATA0;
    end if;
    return PID_DATA1;
  end function;

  procedure packet_expect(signal s2p : in utmi8_s2p;
                          signal p2s : out utmi8_p2s;
                          what : string;
                          expected : byte_string)
  is
    variable rx_v : byte_stream;
  begin
    utmi_packet_receive(s2p, p2s, rx_v);
    assert rx_v.all'length = expected'length and rx_v.all = expected
      report what & ": expected " & to_hex_string(expected)
      & ", the device sent " & to_hex_string(rx_v.all)
      severity failure;
    deallocate(rx_v);
  end procedure;

  -- One datagram out: a whole frame in one packet, which every command of
  -- the conformance sequence fits in.
  procedure datagram_out(signal s2p : in utmi8_s2p;
                         signal p2s : out utmi8_p2s;
                         toggle : std_ulogic;
                         data : byte_string)
  is
  begin
    utmi_packet_send(s2p, p2s, PID_OUT, token_data(dev_addr_c, data_ep_c));
    utmi_packet_send(s2p, p2s, data_pid_of(toggle), with_crc(data));
    packet_expect(s2p, p2s, "command datagram", packet_of(PID_ACK));
  end procedure;

  procedure datagram_in(signal s2p : in utmi8_s2p;
                        signal p2s : out utmi8_p2s;
                        toggle : std_ulogic;
                        data : byte_string)
  is
  begin
    utmi_packet_send(s2p, p2s, PID_IN, token_data(dev_addr_c, data_ep_c));
    packet_expect(s2p, p2s, "response datagram",
                  packet_of(data_pid_of(toggle), with_crc(data)));
    utmi_packet_send(s2p, p2s, PID_ACK);
  end procedure;

  -- A standard device-to-host control transfer whose whole answer fits one
  -- packet, which both descriptors read here do.
  procedure descriptor_expect(signal s2p : in utmi8_s2p;
                              signal p2s : out utmi8_p2s;
                              what : string;
                              value : unsigned(15 downto 0);
                              expected : byte_string)
  is
    variable setup_v : setup_t;
  begin
    setup_v.direction := DEVICE_TO_HOST;
    setup_v.rtype := SETUP_TYPE_STANDARD;
    setup_v.recipient := SETUP_RECIPIENT_DEVICE;
    setup_v.request := REQUEST_GET_DESCRIPTOR;
    setup_v.value := value;
    setup_v.index := (others => '0');
    setup_v.length := to_unsigned(expected'length, 16);

    utmi_packet_send(s2p, p2s, PID_SETUP, token_data(dev_addr_c, control_ep_c));
    utmi_packet_send(s2p, p2s, PID_DATA0, with_crc(setup_pack(setup_v)));
    packet_expect(s2p, p2s, what & " setup", packet_of(PID_ACK));

    utmi_packet_send(s2p, p2s, PID_IN, token_data(dev_addr_c, control_ep_c));
    packet_expect(s2p, p2s, what, packet_of(PID_DATA1, with_crc(expected)));
    utmi_packet_send(s2p, p2s, PID_ACK);

    utmi_packet_send(s2p, p2s, PID_OUT, token_data(dev_addr_c, control_ep_c));
    utmi_packet_send(s2p, p2s, PID_DATA1, with_crc(null_byte_string));
    packet_expect(s2p, p2s, what & " status", packet_of(PID_ACK));
  end procedure;

begin

  dut: gatecap.adapter_usb.usb_utmi_adapter
    generic map(
      apb_config_c => adapter_apb_config_c,
      burst_length_l2_c => adapter_burst_length_l2_c,
      fingerprint_c => fingerprint_c,
      descriptor_base_c => adapter_descriptor_base_c
      )
    port map(
      reset_n_i => dut_reset_n_s,

      online_o => online_s,

      utmi_data_o => s2p.data,
      utmi_data_i => p2s.data,
      utmi_system_o => s2p.system,
      utmi_system_i => p2s.system,

      apb_o => apb_s.m,
      apb_i => apb_s.s
      );

  completer: nsl_amba.ram.apb_ram
    generic map(
      config_c => adapter_apb_config_c,
      byte_size_l2_c => adapter_completer_size_l2_c
      )
    port map(
      clock_i => p2s.system.clock,
      reset_n_i => dut_reset_n_s,
      apb_i => apb_s.m,
      apb_o => apb_s.s
      );

  -- The stimulus drives the phy clock, so the reset it releases has to be
  -- brought into that domain rather than timed against it.
  reset_sync: nsl_clocking.async.async_edge
    port map(
      clock_i => p2s.system.clock,
      data_i => reset_n_s,
      data_o => dut_reset_n_s
      );

  stim: process
    -- Data toggle of the endpoint pair. Both endpoints start at DATA0 and
    -- carry one transfer per step, so one variable tracks both.
    variable toggle_v : std_ulogic := '0';
  begin
    reset_n_s <= '0';
    utmi_init(s2p, p2s);
    utmi_wait(s2p, p2s, 1 us);
    reset_n_s <= '1';
    utmi_reset(s2p, p2s);
    utmi_wait(s2p, p2s, 10 us);

    utmi_control_write(s2p, p2s,
                       dev_addr => x"00",
                       request => REQUEST_SET_ADDRESS,
                       value => dev_addr_c);

    -- The rack's identity on the bus: vendor and product are gatecap's own,
    -- and the serial-number string is the descriptor fingerprint.
    descriptor_expect(s2p, p2s, "device descriptor",
                      unsigned(DESCRIPTOR_TYPE_DEVICE) & x"00",
                      device_descriptor_c);
    descriptor_expect(s2p, p2s, "serial descriptor",
                      unsigned(DESCRIPTOR_TYPE_STRING) & x"03",
                      serial_descriptor_c);

    utmi_control_write(s2p, p2s,
                       dev_addr => dev_addr_c,
                       request => REQUEST_SET_CONFIGURATION,
                       value => x"1");

    -- One command datagram, one response datagram, the toggle alternating on
    -- each endpoint of its own.
    for step in 0 to conformance_step_count_c - 1 loop
      datagram_out(s2p, p2s, toggle_v, conformance_command(step));
      utmi_wait(s2p, p2s, 5 us);
      datagram_in(s2p, p2s, toggle_v, conformance_response(step));
      utmi_wait(s2p, p2s, 2 us);
      toggle_v := not toggle_v;
    end loop;

    assert online_s = '1'
      report "the device is configured, so it must report itself online"
      severity failure;

    report "adapter_usb testbench PASSED" severity note;
    wait;
  end process;

end architecture;
