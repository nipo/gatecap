# Vivado IP packaging

This project packages a gatecap rack as a Vivado block-design IP. It
holds no HDL of its own: the rack and the topcell it is packaged
behind are both generated from `simple.yaml`.

The IP carries one AXI4-Stream slave and one master (the link the host
talks over), one clock and one active-low reset, and the probe of the
capture domain. Dropping it into a block design and wiring the two
stream interfaces to whatever carries them — a DMA, a UART bridge, an
Ethernet stack — is the whole of the integration.

## How it is asked for

Two things in `project.gbs.yaml` differ from an ordinary gatecap
project:

    root_library_name: gatecap_generated

    root:
      name: ila_vivado_ip
      deps:
        - gatecap_generated.ila_vivado_ip

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

`gatecap_axi_ila.zip` is the packaged IP, ready for a Vivado IP
repository.
