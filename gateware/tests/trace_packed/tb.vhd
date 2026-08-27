library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, gatecap;
use nsl_amba.apb.all;

entity tb is
end entity;

architecture sim of tb is

  constant depth_l2_c : natural := 6;
  constant apb_cfg_c : config_t := config(address_width => 12,
                                          data_bus_width => 32,
                                          err => true);

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  -- 8-bit DUT: 4 samples per 32-bit word.
  signal apb8_m : master_t;
  signal apb8_s : slave_t;
  signal w8_en_s : std_ulogic := '0';
  signal w8_addr_s : unsigned(depth_l2_c-1 downto 0) := (others => '0');
  signal w8_data_s : std_ulogic_vector(7 downto 0) := (others => '0');

  -- 16-bit DUT: 2 samples per 32-bit word.
  signal apb16_m : master_t;
  signal apb16_s : slave_t;
  signal w16_en_s : std_ulogic := '0';
  signal w16_addr_s : unsigned(depth_l2_c-1 downto 0) := (others => '0');
  signal w16_data_s : std_ulogic_vector(15 downto 0) := (others => '0');

begin

  dut8: gatecap.trace.trace_buffer_packed
    generic map(
      apb_config_c => apb_cfg_c,
      sample_width_c => 8,
      depth_l2_c => depth_l2_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb8_m,
      apb_o => apb8_s,
      write_clock_i => clock_s,
      write_en_i => w8_en_s,
      write_addr_i => w8_addr_s,
      write_data_i => w8_data_s
      );

  dut16: gatecap.trace.trace_buffer_packed
    generic map(
      apb_config_c => apb_cfg_c,
      sample_width_c => 16,
      depth_l2_c => depth_l2_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb16_m,
      apb_o => apb16_s,
      write_clock_i => clock_s,
      write_en_i => w16_en_s,
      write_addr_i => w16_addr_s,
      write_data_i => w16_data_s
      );

  clock_gen: process
  begin
    while not done_s loop
      clock_s <= '0';
      wait for 5 ns;
      clock_s <= '1';
      wait for 5 ns;
    end loop;
    wait;
  end process;

  stim: process
    variable v : unsigned(31 downto 0);
    variable e : boolean;
  begin
    apb8_m <= transfer_idle(apb_cfg_c);
    apb16_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);

    -- 8-bit: write 16 samples at sequential sample addresses. They pack 4
    -- to a word at byte lanes selected by the address LSBs.
    for k in 0 to 15 loop
      w8_en_s <= '1';
      w8_addr_s <= to_unsigned(k, depth_l2_c);
      w8_data_s <= std_ulogic_vector(to_unsigned(16#10# + k, 8));
      wait until falling_edge(clock_s);
    end loop;
    w8_en_s <= '0';
    wait until falling_edge(clock_s);

    -- Read the 4 packed words back and check each of the 4 lanes.
    for row in 0 to 3 loop
      apb_read(apb_cfg_c, clock_s, apb8_s, apb8_m,
               addr => to_unsigned(row*4, apb_cfg_c.address_width),
               val => v, err => e);
      assert not e report "8b read error" severity failure;
      for lane in 0 to 3 loop
        assert v((lane+1)*8-1 downto lane*8) = to_unsigned(16#10# + row*4 + lane, 8)
          report "8b wrong lane " & integer'image(lane) & " row " & integer'image(row)
          severity failure;
      end loop;
    end loop;

    -- Byte-enable isolation: fill row 1 (samples 4..7), then rewrite only
    -- lane 2 (sample 6). The other three lanes must be untouched.
    for k in 4 to 7 loop
      w8_en_s <= '1';
      w8_addr_s <= to_unsigned(k, depth_l2_c);
      w8_data_s <= std_ulogic_vector(to_unsigned(16#A0# + (k-4), 8));
      wait until falling_edge(clock_s);
    end loop;
    w8_en_s <= '1';
    w8_addr_s <= to_unsigned(6, depth_l2_c);
    w8_data_s <= x"EE";
    wait until falling_edge(clock_s);
    w8_en_s <= '0';
    wait until falling_edge(clock_s);

    apb_read(apb_cfg_c, clock_s, apb8_s, apb8_m,
             addr => to_unsigned(1*4, apb_cfg_c.address_width),
             val => v, err => e);
    assert v(7 downto 0) = x"A0" report "8b lane0 clobbered" severity failure;
    assert v(15 downto 8) = x"A1" report "8b lane1 clobbered" severity failure;
    assert v(23 downto 16) = x"EE" report "8b lane2 not written" severity failure;
    assert v(31 downto 24) = x"A3" report "8b lane3 clobbered" severity failure;

    -- 16-bit: write 8 samples; they pack 2 to a word.
    for k in 0 to 7 loop
      w16_en_s <= '1';
      w16_addr_s <= to_unsigned(k, depth_l2_c);
      w16_data_s <= std_ulogic_vector(to_unsigned(16#1000# + k, 16));
      wait until falling_edge(clock_s);
    end loop;
    w16_en_s <= '0';
    wait until falling_edge(clock_s);

    for row in 0 to 3 loop
      apb_read(apb_cfg_c, clock_s, apb16_s, apb16_m,
               addr => to_unsigned(row*4, apb_cfg_c.address_width),
               val => v, err => e);
      assert not e report "16b read error" severity failure;
      assert v(15 downto 0) = to_unsigned(16#1000# + row*2, 16)
        report "16b wrong lane0 row " & integer'image(row) severity failure;
      assert v(31 downto 16) = to_unsigned(16#1000# + row*2 + 1, 16)
        report "16b wrong lane1 row " & integer'image(row) severity failure;
    end loop;

    report "trace_buffer_packed testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
