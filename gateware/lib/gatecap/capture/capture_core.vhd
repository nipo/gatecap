library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library nsl_logic;
use nsl_logic.bool.all;

-- Logic-analyzer capture core, segmented multi-window.
--
-- Owns the trace-buffer write pointer: the buffer is dumb addressable
-- memory, the core supplies the write address. A capture takes
-- `window_count` windows; each window fills a fixed slot of `capture_len`
-- samples at slot base window_index*capture_len, so the slots sit
-- back-to-back and window_count*capture_len must fit the buffer.
--
-- Within a window the write pointer rolls inside the slot: while armed it
-- overwrites the slot so the last `pre_trigger_len` samples before the
-- trigger are kept, then writes the trigger sample and
-- `capture_len - pre_trigger_len - 1` more, filling the slot exactly once.
-- The window's samples occupy the whole slot rotated by head_offset; the
-- core emits `head_o` (the slot address of the oldest sample) with a
-- one-cycle `head_we_o` strobe as each window completes, in order. The
-- host reads `capture_len` samples from head_o, wrapping within the slot,
-- to recover the window in time order (trigger at index pre_trigger_len).
-- After the last window the core returns to idle.
--
-- Boundary is CDC-ready: arm_i/abort_i are single-cycle pulses, the
-- configuration inputs are held stable while armed. Here control and
-- capture share one clock.
entity capture_core is
  generic (
    signal_count_c : natural;
    capture_len_width_c : natural;
    depth_l2_c : natural;
    window_count_c : natural := 1;
    -- Trigger-block latency in cycles: the trigger strobe arrives this many
    -- cycles after its condition held, so the trigger sample is the one
    -- written that many cycles ago. The core back-dates to it (the samples
    -- are already in the slot); no data-path delay is needed.
    trigger_latency_c : natural := 0
    );
  port (
    clock_i : in std_ulogic;
    reset_n_i : in std_ulogic;

    signals_i : in std_ulogic_vector(signal_count_c-1 downto 0);

    -- Control (from control block)
    arm_i : in std_ulogic;
    abort_i : in std_ulogic;
    -- Trigger event (from the trigger block); sampled while armed once the
    -- pre-trigger window is filled.
    trigger_i : in std_ulogic;
    capture_len_i : in unsigned(capture_len_width_c-1 downto 0);
    pre_trigger_len_i : in unsigned(capture_len_width_c-1 downto 0);
    window_count_i : in unsigned(capture_len_width_c-1 downto 0);

    -- Status (to control block)
    state_o : out std_ulogic_vector(1 downto 0);
    triggered_o : out std_ulogic;
    -- High while armed AND the pre-trigger window is filled: the core is ready
    -- to accept a trigger. It drives the trigger block's enable so a trigger
    -- cannot fire before nominal pre-trigger context exists. It drops on
    -- trigger-accept (state leaves ARMED) and stays low through the window,
    -- re-rises once the next window's ring has refilled, and stays low once
    -- the capture is done -- so the trigger auto-disarms and re-arms per window.
    ready_o : out std_ulogic;
    -- Per-window head, pulsed as each window completes (windows in order).
    head_o : out unsigned(depth_l2_c-1 downto 0);
    head_we_o : out std_ulogic;

    -- Trace buffer write side (address supplied by this core)
    write_en_o : out std_ulogic;
    write_addr_o : out unsigned(depth_l2_c-1 downto 0);
    write_data_o : out std_ulogic_vector(signal_count_c-1 downto 0)
    );
end entity;

architecture rtl of capture_core is

  constant STATE_IDLE_C : std_ulogic_vector(1 downto 0) := "00";
  constant STATE_ARMED_C : std_ulogic_vector(1 downto 0) := "01";
  constant STATE_CAPTURING_C : std_ulogic_vector(1 downto 0) := "10";

  type state_t is (ST_IDLE, ST_ARMED, ST_CAPTURING);

  type regs_t is
  record
    state : state_t;
    triggered : std_ulogic;
    capture_len : unsigned(capture_len_width_c-1 downto 0);
    pre_trigger_len : unsigned(capture_len_width_c-1 downto 0);
    window_count : unsigned(capture_len_width_c-1 downto 0);
    window_idx : unsigned(capture_len_width_c-1 downto 0);
    pre_count : unsigned(capture_len_width_c-1 downto 0);
    samples_rem : unsigned(capture_len_width_c-1 downto 0);
    offset : unsigned(capture_len_width_c-1 downto 0);
    slot_base : unsigned(depth_l2_c-1 downto 0);
    head_offset : unsigned(capture_len_width_c-1 downto 0);
  end record;

  signal r, rin : regs_t;

  -- Probed signals may come from opendrain/tristated drivers carrying weak
  -- or high-Z levels; normalise to X01 so the stored samples are clean
  -- logic values.
  signal signals_s : std_ulogic_vector(signal_count_c-1 downto 0);

begin

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
                      capture_len_i, pre_trigger_len_i, window_count_i)
    variable post_total : unsigned(capture_len_width_c-1 downto 0);
    variable head_off : unsigned(capture_len_width_c-1 downto 0);
    -- Distance from the current sample back to the oldest kept sample: the
    -- pre-trigger window plus the trigger-block latency (the trigger sample
    -- itself sits trigger_latency_c behind the current sample).
    variable back : unsigned(capture_len_width_c-1 downto 0);
    variable complete : boolean;
  begin
    rin <= r;
    head_we_o <= '0';
    head_o <= (others => '0');
    complete := false;
    head_off := r.head_offset;

    case r.state is
      when ST_IDLE =>
        if arm_i = '1' then
          rin.state <= ST_ARMED;
          rin.triggered <= '0';
          rin.capture_len <= capture_len_i;
          rin.pre_trigger_len <= pre_trigger_len_i;
          rin.window_count <= window_count_i;
          rin.window_idx <= (others => '0');
          rin.slot_base <= (others => '0');
          rin.offset <= (others => '0');
          rin.pre_count <= (others => '0');
        end if;

      when ST_ARMED =>
        -- A sample is written this cycle (see mealy); roll the offset
        -- inside the slot.
        if r.offset = r.capture_len - 1 then
          rin.offset <= (others => '0');
        else
          rin.offset <= r.offset + 1;
        end if;
        -- Accumulate enough history to back-date the trigger sample and
        -- still keep its pre-trigger window.
        back := r.pre_trigger_len + trigger_latency_c;
        if r.pre_count < back then
          rin.pre_count <= r.pre_count + 1;
        end if;

        post_total := r.capture_len - r.pre_trigger_len;
        -- Oldest kept sample sits `back` behind the current sample.
        if r.offset >= back then
          head_off := r.offset - back;
        else
          head_off := r.offset - back + r.capture_len;
        end if;

        if abort_i = '1' then
          rin.state <= ST_IDLE;
        elsif trigger_i = '1'
          and r.pre_count >= back then
          rin.triggered <= '1';
          rin.head_offset <= head_off;
          -- trigger_latency_c post samples are already in the slot; capture
          -- the rest (or none, if the window is already full).
          if post_total <= trigger_latency_c + 1 then
            complete := true;
          else
            rin.samples_rem <= post_total - 1 - trigger_latency_c;
            rin.state <= ST_CAPTURING;
          end if;
        end if;

      when ST_CAPTURING =>
        if r.offset = r.capture_len - 1 then
          rin.offset <= (others => '0');
        else
          rin.offset <= r.offset + 1;
        end if;
        if abort_i = '1' then
          rin.state <= ST_IDLE;
        elsif r.samples_rem = 1 then
          complete := true;
        else
          rin.samples_rem <= r.samples_rem - 1;
        end if;
    end case;

    if complete then
      head_we_o <= '1';
      head_o <= r.slot_base + resize(head_off, depth_l2_c);
      if r.window_idx + 1 < r.window_count then
        rin.window_idx <= r.window_idx + 1;
        rin.slot_base <= r.slot_base + resize(r.capture_len, depth_l2_c);
        rin.offset <= (others => '0');
        rin.pre_count <= (others => '0');
        rin.state <= ST_ARMED;
      else
        rin.state <= ST_IDLE;
      end if;
    end if;
  end process;

  mealy: process(r, signals_s)
  begin
    case r.state is
      when ST_IDLE => state_o <= STATE_IDLE_C;
      when ST_ARMED => state_o <= STATE_ARMED_C;
      when ST_CAPTURING => state_o <= STATE_CAPTURING_C;
    end case;

    triggered_o <= r.triggered;
    -- Ready once the pre-trigger window is filled. The trigger's own latency is
    -- NOT added here: enable rises at pre_trigger_len, the trigger's pipeline
    -- takes trigger_latency_c cycles to emit the tick, and pre_count climbs to
    -- `back` (pre_trigger_len + trigger_latency_c) in exactly that time -- so
    -- acceptance still sees the full `back` history with no extra trigger delay.
    ready_o <= to_logic(r.state = ST_ARMED and r.pre_count >= r.pre_trigger_len);

    write_en_o <= to_logic(r.state = ST_ARMED or r.state = ST_CAPTURING);
    write_addr_o <= r.slot_base + resize(r.offset, depth_l2_c);
    write_data_o <= signals_s;
  end process;

end architecture;
