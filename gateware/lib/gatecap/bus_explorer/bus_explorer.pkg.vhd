library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library gatecap, nsl_amba, nsl_data;
use nsl_data.bytestream.all;
use nsl_data.cbor.all;
use nsl_data.uuid.all;

-- Bus-explorer instrument: an APB master driven from the host, for
-- interactive exploration of a register map that lives outside the rack
-- (a transceiver DRP port, a PLL reconfiguration interface, third-party IP).
--
-- There is no pass-through aperture: a target that never answers would stall
-- the backplane and with it the transport. Every target access goes through
-- an indirect command engine that owns a timeout and reports failures as
-- status bits, so a dead target costs a timeout and nothing else.
--
-- Two cooperating functions behind one register file:
--
--   command engine  one operation at a time, staged in config registers and
--                   fired by an action write: read, write, or masked write.
--                   A masked write is a read-modify-write executed on the
--                   target bus, (old and not mask) or (data and mask), as an
--                   indivisible pair with respect to the scanner.
--   scanner         up to slot_count_c target addresses of interest. When
--                   the engine is idle and scanning is enabled, the enabled
--                   slots are swept round-robin, one target read each, into
--                   a result array in the status region -- so the standard
--                   burst status poll carries live values with no extra
--                   transport round trip.
--
-- The instrument is split in two entities because the target bus has its own
-- clock:
--
--   bus_explorer_shell  APB register file, engine FSM, scanner, timeout;
--                       host clock domain
--   bus_explorer_core   minimal APB master executing one command at a time;
--                       target clock domain
--
-- Neither entity contains a clock crossing. The assembler wires shell to
-- core, inserting an interdomain_fifo_slice in each direction when the two
-- clocks differ and plain wires when they are the same, so the same-clock
-- case costs nothing.
--
-- Inner contract, shell port -> core port:
--
--   command_o        -> command_i        \  one interdomain_fifo_slice of
--   command_valid_o  -> command_valid_i   > command_width(...) bits, host
--   command_ready_i  <- command_ready_o  /  clock in, target clock out
--
--   response_i       <- response_o       \  one interdomain_fifo_slice of
--   response_valid_i <- response_valid_o  > response_width(...) bits, target
--   response_ready_o -> response_ready_i /  clock in, host clock out
--
-- Both are plain valid/ready streams: a beat happens on the cycle where
-- valid and ready are both asserted, valid never waits on ready. One
-- transaction is in flight at a time, and no multi-word value ever crosses:
-- scan results are deposited in the shell-side array one at a time.
--
-- The timeout counts in the host domain, which is sound even on a
-- non-permanent host clock: the clock is by construction running while
-- someone is waiting for the answer.
package bus_explorer is

  -- Type of the bus-explorer instrument, as the host driver keys its lookup
  -- by, and of its one register block. The envelope tail below is frozen:
  -- changing it means minting a new UUID, never reusing this one.
  constant BUS_EXPLORER_UUID_C : uuid_t :=
    uuid("5804305e-b62b-400f-94e3-86c905d87b97");
  constant BUS_EXPLORER_ENGINE_UUID_C : uuid_t :=
    uuid("b6da0744-8162-4879-8563-67f506557b89");

  -- Register-file words the instrument decodes. Its address-space footprint
  -- follows from it.
  constant BUS_EXPLORER_REG_COUNT_L2_C : natural := 8;

  -- Bytes of address space the instrument occupies, log2.
  function bus_explorer_size_l2(data_bus_width_l2 : natural) return natural;

  -- Operation encoded in the low bits of a COMMAND write.
  constant OP_READ_C : unsigned(1 downto 0) := "00";
  constant OP_WRITE_C : unsigned(1 downto 0) := "01";
  constant OP_MASKED_WRITE_C : unsigned(1 downto 0) := "10";

  -- Error code of the last completed manual command, in STATUS.
  constant ERROR_OK_C : unsigned(1 downto 0) := "00";
  -- The target did not answer within timeout_c host cycles.
  constant ERROR_TIMEOUT_C : unsigned(1 downto 0) := "01";
  -- The target answered with pslverr.
  constant ERROR_SLVERR_C : unsigned(1 downto 0) := "10";
  -- The COMMAND write held an operation code that is not one of the three
  -- above. No target access was made.
  constant ERROR_COMMAND_C : unsigned(1 downto 0) := "11";

  -- STATUS bit positions.
  constant STATUS_BUSY_C : natural := 0;
  constant STATUS_DONE_C : natural := 1;
  constant STATUS_ERROR_LSB_C : natural := 2;
  constant STATUS_SCAN_ACTIVE_C : natural := 4;

  -- APB configuration of the target port, from the instrument's target
  -- generics. APB data buses come in 8, 16 and 32 bits, so a target data
  -- width that is not one of these rides the next one up: the engine
  -- truncates write data and zero-extends read data to target_data_width_c,
  -- and the unused high bits of the bus are driven low.
  --
  -- The address register is driven verbatim onto paddr, whose width is
  -- target_address_width_c. The instrument does not scale a word address to
  -- a byte address: a target whose registers are not byte-addressed (a DRP,
  -- for one) is bridged outside the instrument, and that bridge owns the
  -- convention.
  function target_apb_config(address_width : natural;
                             data_width : natural)
    return nsl_amba.apb.config_t;

  -- Command stream, shell to core. Bit layout, LSB first:
  --
  --   0                             write (0 = read, 1 = write)
  --   1                             tag
  --   2 +: address_width            target address
  --   2+address_width +: data_width write data (meaningless on a read)
  --
  -- The tag is the sequence bit that makes a late response discardable: the
  -- shell flips it whenever it abandons an access on a timeout, so the
  -- answer that eventually arrives no longer matches what the shell awaits.
  function command_width(address_width : natural;
                         data_width : natural) return natural;
  function command_pack(address_width : natural;
                        data_width : natural;
                        write : std_ulogic;
                        tag : std_ulogic;
                        address : unsigned;
                        wdata : unsigned) return std_ulogic_vector;
  function command_write(command : std_ulogic_vector) return std_ulogic;
  function command_tag(command : std_ulogic_vector) return std_ulogic;
  function command_address(command : std_ulogic_vector;
                           address_width : natural) return unsigned;
  function command_wdata(command : std_ulogic_vector;
                         address_width : natural;
                         data_width : natural) return unsigned;

  -- Response stream, core to shell. Bit layout, LSB first:
  --
  --   0               error (the target answered with pslverr)
  --   1               tag, copied from the command it answers
  --   2 +: data_width read data (meaningless on a write or an error)
  function response_width(data_width : natural) return natural;
  function response_pack(data_width : natural;
                         error : std_ulogic;
                         tag : std_ulogic;
                         rdata : unsigned) return std_ulogic_vector;
  function response_error(response : std_ulogic_vector) return std_ulogic;
  function response_tag(response : std_ulogic_vector) return std_ulogic;
  function response_rdata(response : std_ulogic_vector;
                          data_width : natural) return unsigned;

  -- Typed engine register-block object: [ type ]. The block's register map
  -- follows from the instrument's envelope tail, so the object holds nothing
  -- beyond the type a host driver binds on.
  function bus_explorer_block_desc return byte_string;

  -- Envelope of a bus-explorer instrument:
  --
  --   [ type, size_l2, name, children,
  --     address-width, data-width, slot-count, map ]
  --
  -- The four leading fields are the framework's. The tail is this type's:
  -- the two target widths and the slot count dimension the register map the
  -- host drives, and map is a free-form identifier of the target's register
  -- map (e.g. "xilinx-gtye4-drp") which the host resolves against its own
  -- library of SVD documents. It may be empty. Register-map decode
  -- deliberately does not live in the descriptor: the map describes external
  -- IP and can run to hundreds of registers.
  --
  -- children holds the engine block at offset 0.
  function bus_explorer_envelope(
    size_l2 : natural;
    name : string;
    children : byte_string;
    address_width : natural;
    data_width : natural;
    slot_count : natural;
    map_id : string) return byte_string;

  -- Host-domain half: the APB register file, the engine and the scanner.
  -- Register map (word offsets within each 0x100-stride region, N =
  -- slot_count_c):
  --
  --   0x000 action, write-only, reads as zero
  --     +0        COMMAND: a write fires the staged operation, its low two
  --               bits holding OP_*_C above. Reserved codes complete at once
  --               with ERROR_COMMAND_C and no target access.
  --   0x100 config, read/write, readable back truncated to the target widths
  --     +0        ADDRESS, target address of the manual operation
  --     +1        WDATA, write data
  --     +2        WMASK, mask of a masked write, 1 bits taken from WDATA
  --     +3        SLOT_ENABLE, one bit per scan slot
  --     +4        SCAN_CTRL, bit 0 enables the scanner
  --   0x200 status, read-only
  --     +0        STATUS: busy(0), done(1), error(3 downto 2),
  --               scan-active(4)
  --     +1        FINGERPRINT
  --     +2        RDATA, data of the last manual read, zero-extended. A
  --               masked write leaves the value it read here.
  --     +3        SCAN_VALID, one bit per slot, set once a slot has been
  --               read without error
  --     +4        SCAN_ERROR, one bit per slot, set by a failed slot read
  --     +5 .. +4+N  scan results, zero-extended, one per slot
  --   0x300 arrays, read/write
  --     +0 .. +N-1  slot target addresses
  --
  -- The whole live state is one contiguous read-only run, so a single burst
  -- status poll carries it.
  --
  -- busy is set by the COMMAND write itself and cleared when the operation
  -- completes, done is set on completion and cleared by the next COMMAND
  -- write; both describe manual operations only, the scanner has
  -- scan-active. Writing SLOT_ENABLE clears the valid and error bits of the
  -- slots it disables, and writing a slot address clears that slot's, the
  -- old value no longer describing the programmed address. A scan read in
  -- flight when either is written is discarded on arrival, so a slot the
  -- host has just reprogrammed cannot be marked valid by the answer to the
  -- question it used to ask.
  component bus_explorer_shell is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      target_address_width_c : natural range 1 to 32 := 32;
      target_data_width_c : natural range 1 to 32 := 32;
      slot_count_c : natural range 1 to 32 := 8;
      -- Host cycles an access may take, from the COMMAND write to the
      -- target's answer, before the engine abandons it.
      timeout_c : positive := 65536;
      fingerprint_c : unsigned(31 downto 0) := (others => '0')
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      apb_i : in nsl_amba.apb.master_t;
      apb_o : out nsl_amba.apb.slave_t;

      command_o : out std_ulogic_vector(
        command_width(target_address_width_c, target_data_width_c)-1 downto 0);
      command_valid_o : out std_ulogic;
      command_ready_i : in std_ulogic;

      response_i : in std_ulogic_vector(
        response_width(target_data_width_c)-1 downto 0);
      response_valid_i : in std_ulogic;
      response_ready_o : out std_ulogic
      );
  end component;

  -- Target-domain half: one APB master, one transaction at a time. It holds
  -- no timeout of its own -- an access it started against a target that
  -- never answers stays outstanding until the target does answer, and the
  -- shell has long since given up on it and flipped the tag.
  component bus_explorer_core is
    generic (
      target_address_width_c : natural range 1 to 32 := 32;
      target_data_width_c : natural range 1 to 32 := 32
      );
    port (
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      command_i : in std_ulogic_vector(
        command_width(target_address_width_c, target_data_width_c)-1 downto 0);
      command_valid_i : in std_ulogic;
      command_ready_o : out std_ulogic;

      response_o : out std_ulogic_vector(
        response_width(target_data_width_c)-1 downto 0);
      response_valid_o : out std_ulogic;
      response_ready_i : in std_ulogic;

      -- Target bus, dimensioned by target_apb_config of the generics above.
      apb_o : out nsl_amba.apb.master_t;
      apb_i : in nsl_amba.apb.slave_t
      );
  end component;

end package;

package body bus_explorer is

  function bus_explorer_size_l2(data_bus_width_l2 : natural)
    return natural is
  begin
    return BUS_EXPLORER_REG_COUNT_L2_C + data_bus_width_l2;
  end function;

  function target_apb_config(address_width : natural;
                             data_width : natural)
    return nsl_amba.apb.config_t
  is
    variable bus_width : natural := 32;
  begin
    assert address_width >= 1 and address_width <= 32
      report "bus explorer target address width must be in 1 to 32"
      severity failure;
    assert data_width >= 1 and data_width <= 32
      report "bus explorer target data width must be in 1 to 32"
      severity failure;

    if data_width <= 8 then
      bus_width := 8;
    elsif data_width <= 16 then
      bus_width := 16;
    end if;

    return nsl_amba.apb.config(address_width => address_width,
                               data_bus_width => bus_width,
                               ready => true,
                               err => true);
  end function;

  function command_width(address_width : natural;
                         data_width : natural) return natural is
  begin
    return 2 + address_width + data_width;
  end function;

  function command_pack(address_width : natural;
                        data_width : natural;
                        write : std_ulogic;
                        tag : std_ulogic;
                        address : unsigned;
                        wdata : unsigned) return std_ulogic_vector
  is
    variable ret : std_ulogic_vector(
      command_width(address_width, data_width)-1 downto 0);
  begin
    ret(0) := write;
    ret(1) := tag;
    ret(2+address_width-1 downto 2) :=
      std_ulogic_vector(resize(address, address_width));
    ret(2+address_width+data_width-1 downto 2+address_width) :=
      std_ulogic_vector(resize(wdata, data_width));
    return ret;
  end function;

  function command_write(command : std_ulogic_vector) return std_ulogic is
    alias c : std_ulogic_vector(command'length-1 downto 0) is command;
  begin
    return c(0);
  end function;

  function command_tag(command : std_ulogic_vector) return std_ulogic is
    alias c : std_ulogic_vector(command'length-1 downto 0) is command;
  begin
    return c(1);
  end function;

  function command_address(command : std_ulogic_vector;
                           address_width : natural) return unsigned
  is
    alias c : std_ulogic_vector(command'length-1 downto 0) is command;
  begin
    return unsigned(c(2+address_width-1 downto 2));
  end function;

  function command_wdata(command : std_ulogic_vector;
                         address_width : natural;
                         data_width : natural) return unsigned
  is
    alias c : std_ulogic_vector(command'length-1 downto 0) is command;
  begin
    return unsigned(c(2+address_width+data_width-1 downto 2+address_width));
  end function;

  function response_width(data_width : natural) return natural is
  begin
    return 2 + data_width;
  end function;

  function response_pack(data_width : natural;
                         error : std_ulogic;
                         tag : std_ulogic;
                         rdata : unsigned) return std_ulogic_vector
  is
    variable ret : std_ulogic_vector(response_width(data_width)-1 downto 0);
  begin
    ret(0) := error;
    ret(1) := tag;
    ret(2+data_width-1 downto 2) :=
      std_ulogic_vector(resize(rdata, data_width));
    return ret;
  end function;

  function response_error(response : std_ulogic_vector) return std_ulogic is
    alias r : std_ulogic_vector(response'length-1 downto 0) is response;
  begin
    return r(0);
  end function;

  function response_tag(response : std_ulogic_vector) return std_ulogic is
    alias r : std_ulogic_vector(response'length-1 downto 0) is response;
  begin
    return r(1);
  end function;

  function response_rdata(response : std_ulogic_vector;
                          data_width : natural) return unsigned
  is
    alias r : std_ulogic_vector(response'length-1 downto 0) is response;
  begin
    return unsigned(r(2+data_width-1 downto 2));
  end function;

  function bus_explorer_block_desc return byte_string is
  begin
    -- CBOR tag 37 is the binary UUID tag, as every gatecap type field uses.
    return cbor_array(
      cbor_tagged(37, cbor_bstr(BUS_EXPLORER_ENGINE_UUID_C)));
  end function;

  function bus_explorer_envelope(
    size_l2 : natural;
    name : string;
    children : byte_string;
    address_width : natural;
    data_width : natural;
    slot_count : natural;
    map_id : string) return byte_string is
  begin
    return gatecap.descriptor.instrument_envelope(
      type_uuid => BUS_EXPLORER_UUID_C,
      size_l2 => size_l2,
      name => name,
      children => children,
      t0 => cbor_positive(address_width),
      t1 => cbor_positive(data_width),
      t2 => cbor_positive(slot_count),
      t3 => cbor_tstr(map_id));
  end function;

end package body;
