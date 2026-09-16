# Vivado IP packaging of probed AXI4-Streams

This project packages a gatecap rack as a Vivado block-design IP. It
holds no HDL of its own: the rack and the topcell it is packaged
behind are both generated from `simple.yaml`.

The capture domain probes two AXI4-Stream buses, `rx` and `tx`, so the
IP carries four stream interfaces rather than two: the link the host
talks over — one slave (`s_axis`) and one master (`m_axis`) — and the
two buses under observation, `la_main_rx` and `la_main_tx`. Those two
are *monitor* interfaces: the core drives nothing on a bus it watches,
so every one of their pins, `TREADY` included, is an input. A block
design connects them the way it connects any AXI4-Stream, and what
passes is captured without being disturbed.

## The geometry of an observed bus

The rack takes the configuration of each probed stream as a
`config_t` generic with no default, which a block design has nowhere
to write. The IP therefore states the same geometry in scalars, eight
per probe, which the configuration panel carries:

    la_main_rx_data_bytes   la_main_rx_has_keep
    la_main_rx_id_width     la_main_rx_has_strobe
    la_main_rx_dest_width   la_main_rx_has_ready
    la_main_rx_user_width   la_main_rx_has_last

The pins are sized from them and the `config_t` behind them is rebuilt
from them, so the boundary and the capture cannot disagree. Every pin
exists whatever the panel says: a field turned off is a null range,
and an optional pin carries a default, so a bus without `TUSER` simply
leaves that pin open.

`has_ready` and `has_last` default true, unlike the library's own
factory: an observed stream almost always has both, and this
description's `trigger: vlr` selects over them.

## How it is asked for

Two things in `project.gbs.yaml` differ from an ordinary gatecap
project:

    root_library_name: gatecap_generated

    root:
      name: stream_stream_vivado_ip
      deps:
        - gatecap_generated.stream_stream_vivado_ip

`gatecap_generated.<rack entity>_vivado_ip` is the partition holding
the topcell, and naming it is what makes the generator emit one. It is
named after the topcell, so the dependency and the `topcell` above are
the same word. The topcell is a unit of the generated library, so that
library is the root's, and the root itself has no source to declare.

The IP's identity — vendor, library, name, version, taxonomy,
supported families — stays in the output group's `backend_config`,
beside the part the whole thing is elaborated against.

## Building

    $ gbs project build

`gatecap_stream_stream.zip` is the packaged IP, ready for a Vivado IP
repository.
