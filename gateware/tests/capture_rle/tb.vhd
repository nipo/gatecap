library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library gatecap;

entity tb is
end entity;

architecture sim of tb is

  constant signal_count_c : natural := 8;
  constant depth_l2_c : natural := 8;

  signal clock_s : std_ulogic := '0';
  signal reset_n_s : std_ulogic := '0';
  signal done_s : boolean := false;

  signal sig_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');
  signal arm_s, abort_s : std_ulogic := '0';
  signal trig_value_s, trig_mask_s : std_ulogic_vector(signal_count_c-1 downto 0) := (others => '0');
  -- The trigger block now lives outside the core; model its value/mask
  -- compare here and feed the core a single trigger line.
  signal trigger_s : std_ulogic;
  signal pre_lines_s : unsigned(depth_l2_c downto 0) := (others => '0');
  signal max_cycles_s : unsigned(31 downto 0) := (others => '0');

  signal state_s : std_ulogic_vector(1 downto 0);
  signal triggered_s : std_ulogic;
  signal pre_head_s : unsigned(depth_l2_c-1 downto 0);
  signal pre_n_s : unsigned(depth_l2_c downto 0);
  signal end_ptr_s : unsigned(depth_l2_c downto 0);
  signal write_en_s : std_ulogic;
  signal write_addr_s : unsigned(depth_l2_c-1 downto 0);
  signal write_data_s : std_ulogic_vector(signal_count_c downto 0);

  type mem_t is array (0 to 2**depth_l2_c-1) of std_ulogic_vector(signal_count_c downto 0);
  signal mem_s : mem_t;

begin

  dut: gatecap.capture.capture_core_rle
    generic map(
      signal_count_c => signal_count_c,
      depth_l2_c => depth_l2_c
      )
    port map(
      clock_i => clock_s,
      reset_n_i => reset_n_s,
      signals_i => sig_s,
      arm_i => arm_s,
      abort_i => abort_s,
      trigger_i => trigger_s,
      pre_lines_i => pre_lines_s,
      max_cycles_i => max_cycles_s,
      state_o => state_s,
      triggered_o => triggered_s,
      ready_o => open,
      pre_head_o => pre_head_s,
      pre_n_o => pre_n_s,
      end_ptr_o => end_ptr_s,
      write_en_o => write_en_s,
      write_addr_o => write_addr_s,
      write_data_o => write_data_s
      );

  trigger_s <= '1' when (sig_s and trig_mask_s) = (trig_value_s and trig_mask_s)
               else '0';

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

  mem_writer: process(clock_s)
  begin
    if rising_edge(clock_s) then
      if write_en_s = '1' then
        mem_s(to_integer(write_addr_s)) <= write_data_s;
      end if;
    end if;
  end process;

  stim: process
    type sample_array is array (0 to 511) of unsigned(signal_count_c-1 downto 0);
    variable pref, postf, dec : sample_array;
    variable pref_len, postf_len, dec_len : natural;

    procedure feed(sig : natural; times : natural; record_to : natural) is
      -- record_to: 0 = none, 1 = pref, 2 = postf
    begin
      for i in 1 to times loop
        sig_s <= std_ulogic_vector(to_unsigned(sig, signal_count_c));
        if record_to = 1 then
          pref(pref_len) := to_unsigned(sig, signal_count_c); pref_len := pref_len + 1;
        elsif record_to = 2 then
          postf(postf_len) := to_unsigned(sig, signal_count_c); postf_len := postf_len + 1;
        end if;
        wait until falling_edge(clock_s);
      end loop;
    end procedure;

    -- Decode `n` lines from `base` (wrapping in [0, wrap) when wrap /= 0),
    -- appending samples to dec; discard leading orphan counts.
    procedure decode(base, n, wrap : natural; allow_orphan : boolean) is
      variable a : natural;
      variable line : std_ulogic_vector(signal_count_c downto 0);
      variable last : unsigned(signal_count_c-1 downto 0) := (others => '0');
      variable seen_sample : boolean := not allow_orphan;
    begin
      for i in 0 to n-1 loop
        if wrap = 0 then
          a := base + i;
        else
          a := (base + i) mod wrap;
        end if;
        line := mem_s(a);
        if line(signal_count_c) = '0' then
          last := unsigned(line(signal_count_c-1 downto 0));
          dec(dec_len) := last; dec_len := dec_len + 1;
          seen_sample := true;
        elsif seen_sample then
          for j in 1 to to_integer(unsigned(line(signal_count_c-1 downto 0))) loop
            dec(dec_len) := last; dec_len := dec_len + 1;
          end loop;
        end if;
      end loop;
    end procedure;

    -- Compare the run-collapsed value sequence of dec[0:a] and pref.
    function collapsed_ok return boolean is
      variable p, d : natural := 0;
      variable pv, dv : sample_array;
      variable pn, dn : natural := 0;
    begin
      for i in 0 to pref_len-1 loop
        if pn = 0 or pv(pn-1) /= pref(i) then pv(pn) := pref(i); pn := pn + 1; end if;
      end loop;
      for i in 0 to dec_len-1 loop
        if dn = 0 or dv(dn-1) /= dec(i) then dv(dn) := dec(i); dn := dn + 1; end if;
      end loop;
      if pn /= dn then return false; end if;
      for i in 0 to pn-1 loop
        if pv(i) /= dv(i) then return false; end if;
      end loop;
      return true;
    end function;
  begin
    reset_n_s <= '0';
    wait for 23 ns;
    wait until falling_edge(clock_s);
    reset_n_s <= '1';
    wait until falling_edge(clock_s);

    -- Test 1: post-only (pre_lines = 0), a run that overflows the count.
    pref_len := 0; postf_len := 0; dec_len := 0;
    pre_lines_s <= (others => '0');
    trig_value_s <= x"AA"; trig_mask_s <= x"FF";
    feed(16#00#, 4, 0);
    arm_s <= '1'; wait until falling_edge(clock_s); arm_s <= '0';
    feed(16#00#, 3, 0);
    feed(16#AA#, 1, 2);   -- trigger sample
    feed(16#AA#, 4, 2);
    feed(16#BB#, 1, 2);
    feed(16#11#, 260, 2); -- overflow run
    feed(16#22#, 2, 2);
    abort_s <= '1'; wait until falling_edge(clock_s); abort_s <= '0';
    wait until falling_edge(clock_s);
    decode(0, to_integer(end_ptr_s), 0, false);
    assert dec_len = postf_len
      report "T1: decoded " & integer'image(dec_len) & " expected "
           & integer'image(postf_len) severity failure;
    for i in 0 to postf_len-1 loop
      assert dec(i) = postf(i) report "T1: mismatch at " & integer'image(i) severity failure;
    end loop;
    assert to_integer(end_ptr_s) < postf_len report "T1: no compression" severity failure;

    -- Test 2: pre-trigger, a pattern that fits the ring, then post.
    pref_len := 0; postf_len := 0; dec_len := 0;
    pre_lines_s <= to_unsigned(32, depth_l2_c+1);
    trig_value_s <= x"20"; trig_mask_s <= x"FF";
    feed(16#77#, 2, 0);          -- settle
    arm_s <= '1'; wait until falling_edge(clock_s); arm_s <= '0';
    feed(16#10#, 5, 1);          -- pre pattern (recorded)
    feed(16#11#, 2, 1);
    feed(16#12#, 1, 1);
    feed(16#13#, 4, 1);
    feed(16#14#, 1, 1);
    feed(16#20#, 1, 2);          -- trigger sample
    feed(16#20#, 2, 2);          -- post
    feed(16#21#, 1, 2);
    feed(16#22#, 3, 2);
    abort_s <= '1'; wait until falling_edge(clock_s); abort_s <= '0';
    wait until falling_edge(clock_s);

    -- Decode post [pre_lines, end_ptr): must match exactly.
    dec_len := 0;
    decode(32, to_integer(end_ptr_s) - 32, 0, false);
    assert dec_len = postf_len
      report "T2 post: decoded " & integer'image(dec_len) & " expected "
           & integer'image(postf_len) severity failure;
    for i in 0 to postf_len-1 loop
      assert dec(i) = postf(i) report "T2 post: mismatch at " & integer'image(i) severity failure;
    end loop;

    -- Decode pre ring: value sequence must match, at least one sample.
    dec_len := 0;
    decode(to_integer(pre_head_s), to_integer(pre_n_s), 32, true);
    assert dec_len >= 1 report "T2 pre: no pre-trigger sample" severity failure;
    assert collapsed_ok report "T2 pre: value sequence mismatch" severity failure;

    -- Test 3: post-trigger real-cycle cap. A long idle run RLE-compresses to a
    -- couple of lines but must stop on its own after max_cycles real cycles
    -- (no abort), so a sparse signal cannot make a capture last forever.
    pre_lines_s <= (others => '0');
    max_cycles_s <= to_unsigned(10, 32);
    trig_value_s <= x"AA"; trig_mask_s <= x"FF";
    feed(16#00#, 3, 0);
    arm_s <= '1'; wait until falling_edge(clock_s); arm_s <= '0';
    feed(16#00#, 2, 0);            -- armed, waiting for the trigger value
    feed(16#AA#, 30, 0);           -- trigger + a long run; the cap stops it
    wait until falling_edge(clock_s);
    assert state_s = "00" report "T3: cap did not return to idle" severity failure;
    assert triggered_s = '1' report "T3: not triggered" severity failure;
    dec_len := 0;
    decode(0, to_integer(end_ptr_s), 0, false);
    assert dec_len = 10
      report "T3: capped length " & integer'image(dec_len) & " expected 10"
      severity failure;
    for i in 0 to dec_len-1 loop
      assert dec(i) = to_unsigned(16#AA#, signal_count_c)
        report "T3: value mismatch at " & integer'image(i) severity failure;
    end loop;
    assert to_integer(end_ptr_s) < 10 report "T3: no compression" severity failure;
    max_cycles_s <= (others => '0');

    report "capture_core_rle testbench PASSED (post + pre-trigger + cap)" severity note;
    done_s <= true;
    wait;
  end process;

end architecture;
