library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_amba, gatecap;
use nsl_amba.apb.all;

entity tb is
end entity;

architecture sim of tb is

  constant sample_width_c : natural := 8;
  constant depth_l2_c : natural := 6;
  constant apb_cfg_c : config_t := config(address_width => 12,
                                          data_bus_width => 32,
                                          err => true);
  constant sample_count_c : natural := 8;

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal apb_m : master_t;
  signal apb_s : slave_t;

  signal write_en_s : std_ulogic := '0';
  signal write_addr_s : unsigned(depth_l2_c-1 downto 0) := (others => '0');
  signal write_data_s : std_ulogic_vector(sample_width_c-1 downto 0) := (others => '0');

begin

  dut: gatecap.trace.trace_buffer
    generic map(
      apb_config_c => apb_cfg_c,
      sample_width_c => sample_width_c,
      depth_l2_c => depth_l2_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      apb_i => apb_m,
      apb_o => apb_s,
      write_clock_i => clock_s,
      write_en_i => write_en_s,
      write_addr_i => write_addr_s,
      write_data_i => write_data_s
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
    apb_m <= transfer_idle(apb_cfg_c);
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);

    -- Write a run of samples at explicit, non-sequential addresses: the
    -- buffer is dumb and honours whatever write_addr it is given.
    for k in 0 to sample_count_c-1 loop
      write_en_s <= '1';
      write_addr_s <= to_unsigned((k * 7) mod 2**depth_l2_c, depth_l2_c);
      write_data_s <= std_ulogic_vector(to_unsigned(16#10# + k, sample_width_c));
      wait until falling_edge(clock_s);
    end loop;
    write_en_s <= '0';
    -- A disabled cycle must not overwrite: address 0 already holds k=0.
    write_addr_s <= to_unsigned(0, depth_l2_c);
    write_data_s <= std_ulogic_vector(to_unsigned(16#ff#, sample_width_c));
    wait until falling_edge(clock_s);

    -- Read the samples back over APB from the addresses they were written to.
    for k in 0 to sample_count_c-1 loop
      apb_read(apb_cfg_c, clock_s, apb_s, apb_m,
               addr => to_unsigned(((k * 7) mod 2**depth_l2_c) * 4, apb_cfg_c.address_width),
               val => v, err => e);
      assert not e
        report "read error at word " & integer'image(k) severity failure;
      assert v = to_unsigned(16#10# + k, 32)
        report "wrong value at word " & integer'image(k) severity failure;
    end loop;

    report "trace_buffer testbench PASSED" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
