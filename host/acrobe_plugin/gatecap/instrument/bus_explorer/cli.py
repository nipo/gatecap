"""The ``bus`` subcommands, on gatecap's own CLI group.

A bus explorer is reached through the same resource path as everything else in
a rack, so the verbs hang off ``acrobe gatecap`` and reuse its ``-r/--root``:

  acrobe gatecap -r udp/127.0.0.1:4253/gatecap bus read 0x04
  acrobe gatecap -r udp/127.0.0.1:4253/gatecap bus write 0x04 0x21 --mask 0xff
  acrobe gatecap -r udp/127.0.0.1:4253/gatecap bus dump 0 8 --output regs.csv

Register maps are user-local configuration and need no target at all:

  acrobe gatecap bus map add gatecap-demo-device device.svd
  acrobe gatecap bus map list

Every verb drives the same driver stack the pane does, so what a command
reports and what the pane shows are shared, not duplicated -- a write made here
lands in the same journal.
"""

import sys

import asyncclick as click

from acrobe_plugin.gatecap.cli import gatecap, require_root
from acrobe_plugin.gatecap.enumerator import BlockAddress

from .driver import BusAccessError, BusExplorer, BusExplorerConsole
from .svd import MapLibrary, SvdError


@gatecap.group(help="Drive a bus explorer's target bus, and manage the SVD "
                    "maps that name its registers")
def bus():
    pass


async def explorer(ctx, name):
    """The named bus explorer, or the only one in the rack."""
    node = await ctx.obj.resolve(require_root(ctx))
    explorers = list(node.children_of_class(BusExplorer))
    if not explorers:
        raise click.ClickException("no bus explorer in this rack")
    if name is None:
        # Naming nothing in a rack with several is an error rather than a
        # guess: the two master different buses.
        if len(explorers) > 1:
            raise click.ClickException(
                "several bus explorers, name one with -i: "
                + ", ".join(BlockAddress.canonical(explorers)))
        return explorers[0]
    found = BlockAddress.targets(explorers).get(name)
    if found is None:
        raise click.ClickException(
            f"no bus explorer {name!r} (available: "
            + ", ".join(BlockAddress.canonical(explorers)) + ")")
    return found


def number(text, what):
    """A value on the command line: hex with 0x, binary with 0b, else
    decimal. Underscores are allowed, as in the descriptions."""
    try:
        return int(str(text).replace("_", ""), 0)
    except ValueError:
        raise click.ClickException(f"{what} {text!r} is not a number") from None


def instrument_option(f):
    return click.option("-i", "--instrument", "instrument_name", default=None,
                        help="Bus explorer to drive, when the rack holds "
                             "several")(f)


def map_option(f):
    return click.option("--map", "map_path", default=None,
                        type=click.Path(exists=True, dir_okay=False),
                        help="SVD file to decode with, instead of the one the "
                             "descriptor's map identifier resolves to")(f)


def with_map(node, map_path):
    if map_path is not None:
        node.map_file(map_path)
    return node


@bus.command(help="Read one target address and print the value, decoded when "
                  "a map names it")
@click.argument("address")
@instrument_option
@map_option
@click.pass_context
async def read(ctx, address, instrument_name, map_path):
    node = with_map(await explorer(ctx, instrument_name), map_path)
    target = number(address, "address")
    try:
        value = await node.read(target)
    except BusAccessError as e:
        raise click.ClickException(str(e)) from None
    name = node.name_at(target)
    click.echo(f"0x{target:x}: 0x{value:x}" + (f"  {name}" if name else ""))
    for field in node.fields_at(target, value):
        label = f" ({field['label']})" if field["label"] else ""
        click.echo(f"  [{field['msb']}:{field['lsb']}] {field['name']} = "
                   f"0x{field['value']:x}{label}")


@bus.command(help="Write one target address. With --mask, the engine does the "
                  "read-modify-write on the target bus")
@click.argument("address")
@click.argument("value")
@click.option("--mask", default=None,
              help="Bits to write; the rest are left as the target holds them")
@instrument_option
@map_option
@click.pass_context
async def write(ctx, address, value, mask, instrument_name, map_path):
    node = with_map(await explorer(ctx, instrument_name), map_path)
    target, word = number(address, "address"), number(value, "value")
    try:
        if mask is None:
            await node.write(target, word)
        else:
            await node.write_masked(target, word, number(mask, "mask"))
    except (BusAccessError, ValueError) as e:
        raise click.ClickException(str(e)) from None
    name = node.name_at(target)
    click.echo(f"wrote 0x{word:x} to 0x{target:x}"
               + (f"  {name}" if name else ""), err=True)


@bus.command(help="Write one field of a mapped register, by name: the mask "
                  "comes out of the map")
@click.argument("register")
@click.argument("field")
@click.argument("value")
@instrument_option
@map_option
@click.pass_context
async def field(ctx, register, field, value, instrument_name, map_path):
    node = with_map(await explorer(ctx, instrument_name), map_path)
    try:
        placed = await node.field_write(register, field, value)
    except (BusAccessError, LookupError, ValueError) as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"wrote {register}.{field}={value} "
               f"(0x{placed['address']:x} mask 0x{placed['mask']:x})",
               err=True)


@bus.command(help="Read a run of target addresses and write CSV")
@click.argument("start")
@click.argument("count", type=int)
@click.option("--step", default=None,
              help="Bytes between addresses (default: the target data bus "
                   "width)")
@click.option("--output", type=click.Path(dir_okay=False), default=None,
              help="File to write (default: print to stdout)")
@instrument_option
@map_option
@click.pass_context
async def dump(ctx, start, count, step, output, instrument_name, map_path):
    node = with_map(await explorer(ctx, instrument_name), map_path)
    entries = await node.sweep(number(start, "start"), count,
                               None if step is None else number(step, "step"))
    text = BusExplorerConsole.csv(entries)
    if output:
        with open(output, "w") as f:
            f.write(text)
        click.echo(f"wrote {output}", err=True)
    else:
        sys.stdout.write(text)


@bus.group(help="Register the SVD documents a descriptor's map identifier "
                "resolves to. User-local, and needs no target")
def map():
    pass


@map.command("add", help="Register FILE under the map identifier ID")
@click.argument("map_id")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def map_add(map_id, path):
    try:
        document = MapLibrary().add(map_id, path)
    except SvdError as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"{map_id}: {len(document)} register(s) in "
               f"{len(document.peripherals)} peripheral(s) from {path}")


@map.command("list", help="Show every registered map")
def map_list():
    maps = MapLibrary().registered()
    if not maps:
        click.echo("no register map is registered "
                   "(acrobe gatecap bus map add <id> <file.svd>)")
        return
    for map_id, path in sorted(maps.items()):
        click.echo(f"{map_id}\t{path}")


@map.command("remove", help="Forget the map registered as ID")
@click.argument("map_id")
def map_remove(map_id):
    try:
        MapLibrary().remove(map_id)
    except KeyError as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"removed {map_id}", err=True)
