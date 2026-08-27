"""The ``rates`` subcommand, on gatecap's own CLI group.

A clock measurer is reached through the same resource path as everything else
in a rack, so the command hangs off ``acrobe gatecap`` and reuses its
``-r/--root``:

  acrobe gatecap -r udp/127.0.0.1:4252/gatecap rates
  acrobe gatecap -r udp/127.0.0.1:4252/gatecap rates --output rates.csv

It reads through the instrument's console adaptor -- the same driver stack the
GUI polls, so what the command dumps and what the pane draws are shared, not
duplicated.
"""

import sys

import asyncclick as click

from acrobe_plugin.gatecap.cli import gatecap, require_root
from acrobe_plugin.gatecap.enumerator import BlockAddress

from .driver import ClockMeasurer


@gatecap.command(help="Read every measured clock rate once and write CSV. "
                      "INSTRUMENT names the clock measurer, and may be "
                      "omitted when the rack holds only one")
@click.argument("instrument_name", metavar="INSTRUMENT", required=False)
@click.option("--output", type=click.Path(dir_okay=False), default=None,
              help="File to write (default: print to stdout)")
@click.pass_context
async def rates(ctx, instrument_name, output):
    node = await ctx.obj.resolve(require_root(ctx))
    measurers = list(node.children_of_class(ClockMeasurer))
    if not measurers:
        raise click.ClickException("no clock measurer in this rack")
    if instrument_name is None:
        # Naming nothing in a rack with several is an error rather than a
        # guess: the two would report different clocks.
        if len(measurers) > 1:
            raise click.ClickException(
                "several clock measurers, name one: "
                + ", ".join(BlockAddress.canonical(measurers)))
        measurer = measurers[0]
    else:
        measurer = BlockAddress.targets(measurers).get(instrument_name)
        if measurer is None:
            raise click.ClickException(
                f"no clock measurer {instrument_name!r} (available: "
                + ", ".join(BlockAddress.canonical(measurers)) + ")")
    text = await measurer.ui_adaptor("console").csv()
    if output:
        with open(output, "w") as f:
            f.write(text)
        click.echo(f"wrote {output}", err=True)
    else:
        sys.stdout.write(text)
