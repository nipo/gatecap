library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library gatecap, nsl_amba, nsl_data;
use nsl_data.bytestream.all;
use nsl_data.cbor.all;
use nsl_data.uuid.all;

-- Clock-rate measurement instrument for a gatecap rack.
--
-- One reference clock carries the time base; every other clock the block
-- watches is counted against it and published as a rate in Hz. The reference
-- is the only clock whose rate the design states, and it states it as a
-- generic: the measurement is a ratio, so an error on that number scales
-- every result.
--
-- It is a gatecap instrument: one entity behind one APB port, described by an
-- envelope named by a UUID. It holds one register file and nothing else, so
-- its envelope carries no child and everything it publishes sits in the
-- envelope's tail.
package clock_measurer is

  -- Type of the clock-rate measurement instrument, as the host driver keys
  -- its lookup by. The envelope tail below is frozen: changing it means
  -- minting a new UUID, never reusing this one.
  constant CLOCK_MEASURER_UUID_C : uuid_t :=
    uuid("ba9af9d4-8767-4567-8e56-01bb12307fb7");

  -- Register-file words the instrument decodes. Its address-space footprint
  -- follows from it, and the block checks the footprint it is given against
  -- it: the envelope and the entity are dimensioned from this one constant.
  constant CLOCK_MEASURER_REG_COUNT_L2_C : natural := 8;

  -- Bytes of address space the instrument occupies, log2.
  function clock_measurer_size_l2(data_bus_width_l2 : natural) return natural;

  -- Instrument envelope of a clock measurer:
  --
  --   [ type, size_l2, name, {},
  --     reference-name, reference-hz, update-hz-l2, [ measured-names ] ]
  --
  -- The four leading fields are the framework's. The tail is this type's:
  -- reference-name and reference-hz identify the time base; update-hz-l2 is
  -- the update rate as a log2 of updates per second, which is also the log2
  -- of the quantum every published rate is rounded down to; measured-names
  -- lists the observed clocks in register order, so the host pairs the rate
  -- array with names without a second lookup.
  --
  -- measured_names is one comma-separated text; names are taken verbatim
  -- between commas, and an empty text yields an empty list.
  function clock_measurer_envelope(
    name : string;
    size_l2 : natural;
    reference_name : string;
    reference_hz : natural;
    update_hz_l2 : natural;
    measured_names : string) return byte_string;

  -- Rate array of a clock-measurement block, on the gatecap map convention:
  --
  --   0x200 + 0    STATUS (R)   reserved, reads zero
  --   0x200 + 4    FINGERPRINT (R)  descriptor UID of the instance
  --   0x300 + 4*i  RATE[i] (R)  measured rate of clock i, in Hz
  --
  -- The rates are read-only, one word per observed clock, contiguous, so a
  -- host reads the whole set in one burst. There is no action group and no
  -- configuration group: the measurement is free-running from reset, with
  -- nothing to arm and nothing to program.
  component clock_rate_block is
    generic (
      apb_config_c : nsl_amba.apb.config_t;
      -- Address-space footprint, log2, as the envelope declares it. The block
      -- checks it against the decoding it really does.
      size_l2_c : natural;
      -- Observed clocks, hence rate registers.
      measured_count_c : natural;
      -- Nominal rate of reference_clock_i, in Hz.
      reference_hz_c : natural;
      -- Bits of an unsigned rate value: ceil(log2(max-expected-rate + 1)).
      rate_width_c : natural;
      -- Rates refresh 2**update_hz_l2_c times per second, so the measurement
      -- window is 2**-update_hz_l2_c reference seconds and a published rate
      -- is a multiple of 2**update_hz_l2_c Hz.
      update_hz_l2_c : natural := 0;
      -- Descriptor fingerprint, exposed read-only so the host polls this
      -- instrument for change detection like any other.
      fingerprint_c : unsigned(31 downto 0) := (others => '0')
      );
    port (
      -- Host domain: the APB register file.
      clock_i : in std_ulogic;
      reset_n_i : in std_ulogic;

      apb_i : in nsl_amba.apb.master_t;
      apb_o : out nsl_amba.apb.slave_t;

      -- Time base every measurement is taken against.
      reference_clock_i : in std_ulogic;

      -- Observed clocks, one rate register each, in index order.
      measured_clock_i : in std_ulogic_vector(measured_count_c-1 downto 0)
      );
  end component;

end package;

package body clock_measurer is

  function clock_measurer_size_l2(data_bus_width_l2 : natural)
    return natural is
  begin
    return CLOCK_MEASURER_REG_COUNT_L2_C + data_bus_width_l2;
  end function;

  function clock_measurer_envelope(
    name : string;
    size_l2 : natural;
    reference_name : string;
    reference_hz : natural;
    update_hz_l2 : natural;
    measured_names : string) return byte_string is

    function name_count(names : string) return natural is
      variable count : natural := 1;
    begin
      if names'length = 0 then
        return 0;
      end if;
      for i in names'range loop
        if names(i) = ',' then
          count := count + 1;
        end if;
      end loop;
      return count;
    end function;

    function names_encoded(names : string) return byte_string is
    begin
      if names'length = 0 then
        return null_byte_string;
      end if;
      for i in names'range loop
        if names(i) = ',' then
          return cbor_tstr(names(names'left to i-1))
            & names_encoded(names(i+1 to names'right));
        end if;
      end loop;
      return cbor_tstr(names);
    end function;

  begin
    return gatecap.descriptor.instrument_envelope(
      type_uuid => CLOCK_MEASURER_UUID_C,
      size_l2 => size_l2,
      name => name,
      t0 => cbor_tstr(reference_name),
      t1 => cbor_positive(reference_hz),
      t2 => cbor_positive(update_hz_l2),
      t3 => cbor_array_hdr(length => name_count(measured_names))
        & names_encoded(measured_names));
  end function;

end package body;
