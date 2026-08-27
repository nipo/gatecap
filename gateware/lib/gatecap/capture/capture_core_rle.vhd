library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_logic;
use nsl_logic.bool.all;

-- Run-length-encoding capture core, single window with pre-trigger.
--
-- Trades random access for capture time: repeated samples collapse to a
-- count, so idle/stable stretches cost about one buffer line. Each line is
-- `signal_count_c + 1` bits: bit signal_count_c is a tag, 0 = a sample in
-- the low bits, 1 = a repeat count of the last sample. A run's count line
-- is overwritten in place (same address, incrementing) until the count
-- field overflows, then spills to the next line.
--
-- The buffer is split: a pre-region ring [0, pre_lines) and a post-region
-- [pre_lines, depth). While armed the core RLE-encodes into the ring,
-- rolling on changes; a single run's counters are capped so they never
-- evict the run's own sample (at least one sample line always survives, so
-- the ring never becomes undecodable orphan counts) -- once capped the run
-- freezes until the value changes or the trigger fires. On the trigger the
-- trigger sample is written as the first post line, then post-trigger
-- encodes linearly until the buffer fills or an abort.
--
-- Readout pointers: pre_head_o / pre_n_o describe the ring (oldest line and
-- valid line count) and end_ptr_o the post end. The host reads pre_n_o
-- lines from pre_head_o (wrapping in the ring, discarding leading orphan
-- counts) then [pre_lines, end_ptr) linearly, decoding forward; the trigger
-- sits at the pre/post boundary.
entity capture_core_rle is
  generic (
    signal_count_c : natural;
    depth_l2_c : natural;
    -- Upper bound on the run-count field, in bits. A line's payload is
    -- signal_count wide, so a run count could otherwise be that wide -- absurd
    -- for a large probe set. The count uses min(count_bits_c, signal_count-1)
    -- bits and is zero-padded into the payload, so the host reads the whole
    -- word as the count without knowing the bound.
    count_bits_c : natural := 32
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    signals_i : in std_ulogic_vector(signal_count_c-1 downto 0);

    -- Control (from control block)
    arm_i : in std_ulogic;
    abort_i : in std_ulogic;
    -- Trigger event (from the trigger block), sampled while armed.
    trigger_i : in std_ulogic;
    pre_lines_i : in unsigned(depth_l2_c downto 0);
    -- Post-trigger real-cycle cap: capture stops after this many sample
    -- clocks (0 = no cap, run until the buffer fills). Bounds the wall-clock
    -- span of a capture when activity is sparse and runs are long.
    max_cycles_i : in unsigned(31 downto 0) := (others => '0');

    -- Status (to control block)
    state_o : out std_ulogic_vector(1 downto 0);
    triggered_o : out std_ulogic;
    -- High while armed AND the pre-region ring is full: ready to accept a
    -- trigger. Drives the trigger block's enable so a trigger cannot fire
    -- before nominal pre-trigger context exists (with pre_lines = 0 it is high
    -- as soon as armed). Auto-disarms on trigger (state leaves ARMED).
    ready_o : out std_ulogic;
    pre_head_o : out unsigned(depth_l2_c-1 downto 0);  -- ring oldest line
    pre_n_o : out unsigned(depth_l2_c downto 0);        -- ring valid lines
    end_ptr_o : out unsigned(depth_l2_c downto 0);      -- post end line

    -- Trace buffer write side; data is tag & payload.
    write_en_o : out std_ulogic;
    write_addr_o : out unsigned(depth_l2_c-1 downto 0);
    write_data_o : out std_ulogic_vector(signal_count_c downto 0)
    );
end entity;

architecture rtl of capture_core_rle is

  constant STATE_IDLE_C : std_ulogic_vector(1 downto 0) := "00";
  constant STATE_ARMED_C : std_ulogic_vector(1 downto 0) := "01";
  constant STATE_CAPTURING_C : std_ulogic_vector(1 downto 0) := "10";
  constant DEPTH_C : natural := 2**depth_l2_c;
  -- Run-count width: bounded by count_bits_c but never wider than the payload
  -- can hold alongside the tag. (RLE needs at least one count bit.)
  constant count_width_c : natural :=
    if_else(count_bits_c < signal_count_c - 1, count_bits_c, signal_count_c - 1);

  type state_t is (ST_IDLE, ST_ARMED, ST_CAPTURING);

  type regs_t is
  record
    state : state_t;
    triggered : std_ulogic;
    pre_lines : unsigned(depth_l2_c downto 0);
    prev : std_ulogic_vector(signal_count_c-1 downto 0);
    counting : boolean;
    count : unsigned(count_width_c-1 downto 0);
    wp : unsigned(depth_l2_c downto 0);           -- address of the newest line
    head : unsigned(depth_l2_c-1 downto 0);       -- ring oldest
    nfilled : unsigned(depth_l2_c downto 0);      -- ring valid line count
    run_sample : unsigned(depth_l2_c-1 downto 0); -- current run's sample addr
    cycles : unsigned(31 downto 0);               -- post-trigger real cycles
  end record;

  signal r, rin : regs_t;
  signal signals_s : std_ulogic_vector(signal_count_c-1 downto 0);

begin

  assert signal_count_c >= 2
    report "RLE capture needs at least 2 signals (one count bit)" severity failure;

  signals_s <= to_x01(signals_i);

  regs: process(clock_i, reset_n_i)
  begin
    if rising_edge(clock_i) then
      r <= rin;
    end if;
    if reset_n_i = '0' then
      r.state <= ST_IDLE;
      r.triggered <= '0';
    end if;
  end process;

  transition: process(r, arm_i, abort_i, trigger_i,
                      pre_lines_i, max_cycles_i, signals_s)
    constant count_max_c : unsigned(count_width_c-1 downto 0) := (others => '1');
    variable cur_addr : unsigned(depth_l2_c downto 0);
    variable na : unsigned(depth_l2_c downto 0);  -- ring-next of wp
    variable tag : std_ulogic;
    variable payload : unsigned(signal_count_c-1 downto 0);
    variable do_write : boolean;

    -- Append a ring entry: advance the cursor and grow/evict the window.
    procedure ring_advance is
    begin
      rin.wp <= na;
      if r.nfilled < r.pre_lines then
        rin.nfilled <= r.nfilled + 1;
      elsif r.head = r.pre_lines - 1 then
        rin.head <= (others => '0');
      else
        rin.head <= r.head + 1;
      end if;
    end procedure;
  begin
    rin <= r;
    do_write := false;
    cur_addr := (others => '0');
    tag := '0';
    payload := (others => '0');
    -- ring-next(wp) within [0, pre_lines)
    if r.wp = r.pre_lines - 1 then
      na := (others => '0');
    else
      na := r.wp + 1;
    end if;

    case r.state is
      when ST_IDLE =>
        if arm_i = '1' then
          rin.state <= ST_ARMED;
          rin.triggered <= '0';
          rin.pre_lines <= pre_lines_i;
          rin.counting <= false;
          rin.wp <= (others => '0');
          rin.head <= (others => '0');
          rin.nfilled <= (others => '0');
        end if;

      when ST_ARMED =>
        if abort_i = '1' then
          rin.state <= ST_IDLE;
        elsif trigger_i = '1' then
          -- Trigger sample is the first post line, at address pre_lines.
          cur_addr := r.pre_lines;
          tag := '0';
          payload := unsigned(signals_s);
          do_write := true;
          rin.prev <= signals_s;
          rin.counting <= false;
          rin.count <= (others => '0');
          rin.wp <= r.pre_lines;
          rin.triggered <= '1';
          rin.cycles <= to_unsigned(1, 32);  -- trigger sample is cycle 1
          rin.state <= ST_CAPTURING;
        elsif r.pre_lines = 0 then
          null;  -- no pre-region; wait for the trigger
        elsif r.nfilled = 0 then
          -- First pre-region sample.
          cur_addr := (others => '0');
          tag := '0';
          payload := unsigned(signals_s);
          do_write := true;
          rin.prev <= signals_s;
          rin.counting <= false;
          rin.wp <= (others => '0');
          rin.head <= (others => '0');
          rin.nfilled <= to_unsigned(1, depth_l2_c+1);
          rin.run_sample <= (others => '0');
        elsif signals_s = r.prev then
          tag := '1';
          if not r.counting then
            if na(depth_l2_c-1 downto 0) = r.run_sample then
              do_write := false;  -- no room for a count (pre_lines = 1)
            else
              cur_addr := na;
              payload := to_unsigned(1, signal_count_c);
              do_write := true;
              rin.counting <= true;
              rin.count <= to_unsigned(1, count_width_c);
              ring_advance;
            end if;
          elsif r.count /= count_max_c then
            cur_addr := r.wp;  -- extend in place
            payload := resize(r.count + 1, signal_count_c);   -- zero-padded left
            do_write := true;
            rin.count <= r.count + 1;
          elsif na(depth_l2_c-1 downto 0) = r.run_sample then
            do_write := false;  -- cap: spilling would evict the sample
          else
            cur_addr := na;
            payload := to_unsigned(1, signal_count_c);
            do_write := true;
            rin.count <= to_unsigned(1, count_width_c);
            ring_advance;
          end if;
        else
          -- Distinct sample: a new run.
          cur_addr := na;
          tag := '0';
          payload := unsigned(signals_s);
          do_write := true;
          rin.prev <= signals_s;
          rin.counting <= false;
          rin.run_sample <= na(depth_l2_c-1 downto 0);
          ring_advance;
        end if;

      when ST_CAPTURING =>
        -- Linear RLE into the post region [pre_lines, depth).
        if signals_s = r.prev then
          tag := '1';
          if not r.counting then
            cur_addr := r.wp + 1;
            payload := to_unsigned(1, signal_count_c);
          elsif r.count /= count_max_c then
            cur_addr := r.wp;
            payload := resize(r.count + 1, signal_count_c);   -- zero-padded left
          else
            cur_addr := r.wp + 1;
            payload := to_unsigned(1, signal_count_c);
          end if;
        else
          tag := '0';
          payload := unsigned(signals_s);
          cur_addr := r.wp + 1;
        end if;

        if abort_i = '1' then
          rin.state <= ST_IDLE;
        elsif max_cycles_i /= 0 and r.cycles >= max_cycles_i then
          rin.state <= ST_IDLE;  -- reached the post-trigger cycle cap
        elsif cur_addr >= DEPTH_C then
          rin.state <= ST_IDLE;  -- buffer full
        else
          do_write := true;
          rin.cycles <= r.cycles + 1;
          if tag = '1' then
            rin.counting <= true;
            rin.count <= resize(payload, count_width_c);
            rin.wp <= cur_addr;
          else
            rin.prev <= signals_s;
            rin.counting <= false;
            rin.count <= (others => '0');
            rin.wp <= cur_addr;
          end if;
        end if;
    end case;

    write_en_o <= to_logic(do_write);
    write_addr_o <= cur_addr(depth_l2_c-1 downto 0);
    write_data_o <= tag & std_ulogic_vector(payload);
  end process;

  mealy: process(r)
  begin
    case r.state is
      when ST_IDLE => state_o <= STATE_IDLE_C;
      when ST_ARMED => state_o <= STATE_ARMED_C;
      when ST_CAPTURING => state_o <= STATE_CAPTURING_C;
    end case;
    triggered_o <= r.triggered;
    ready_o <= to_logic(r.state = ST_ARMED and r.nfilled >= r.pre_lines);
    pre_head_o <= r.head;
    pre_n_o <= r.nfilled;
    end_ptr_o <= r.wp + 1;  -- one past the newest post line
  end process;

end architecture;
