"""acrobe CLI subcommands for gatecap.

Adds a ``gatecap`` group to the acrobe CLI. It resolves a rack and drives it
through the blocks' console adaptors -- the same driver stack the GUI uses, so
what a command reports and what a panel shows are shared, not duplicated:

  acrobe gatecap -r udp/127.0.0.1:4242/gatecap info
  acrobe gatecap gui                     # graphical UI, start disconnected
  acrobe gatecap -r udp/127.0.0.1:4242/gatecap gui   # ... autoconnected

The group knows no instrument type. An instrument adds its own command to it
-- ``capture`` comes from the logic analyzer, ``rates`` from a third-party
clock measurer -- by importing ``gatecap`` here and decorating with
``@gatecap.command``; ``require_root`` gives such a command the same
``-r/--root`` handling as the ones below.
"""

import asyncclick as click

from acrobe.cli import base


@base.cli.group(help="Inspect and drive a gatecap rack")
@click.option("-r", "--root", "root_path", default=None,
              help="Resource path, e.g. udp/127.0.0.1:4242/gatecap "
                   "(required for info/capture; an optional autoconnect "
                   "target for gui)")
@click.pass_context
async def gatecap(ctx, root_path):
    ctx.obj.gatecap_root = root_path


def require_root(ctx):
    """The resource path a command was given, or a usage error."""
    root = ctx.obj.gatecap_root
    if not root:
        raise click.ClickException("a resource path is required (-r/--root)")
    return root


def _console_adaptors(node):
    """The console adaptor of every block that provides one (the trace buffer
    has none), in enumeration order."""
    for child in node.children_find(lambda x: True):
        factory = getattr(child, "ui_adaptor", None)
        adaptor = factory("console") if factory else None
        if adaptor is not None:
            yield adaptor


@gatecap.command(help="Launch the graphical UI (optionally autoconnecting to -r)")
@click.pass_context
def gui(ctx):
    # Lazy import: the GUI's deps (pywebview, Surfer assets) are not needed by
    # info/capture. A --root, if given, is passed through as an autoconnect
    # target; otherwise the window starts disconnected.
    from .gui.app import main
    main(ctx.obj.gatecap_root)


@gatecap.command(help="Serve the graphical UI over HTTP for a browser "
                      "(headless; no window, no pywebview)")
@click.option("--bind", default="127.0.0.1:8000", show_default=True,
              metavar="HOST:PORT",
              help="Address to listen on. The API is unauthenticated and "
                   "drives hardware: keep the loopback default and tunnel "
                   "(ssh -L) unless the network is trusted.")
@click.pass_context
async def serve(ctx, bind):
    from .gui.app import serve as run
    await run(bind, ctx.obj.gatecap_root)


@gatecap.command(help="Show the blocks and their capabilities")
@click.pass_context
async def info(ctx):
    node = await ctx.obj.resolve(require_root(ctx))
    adaptors = list(_console_adaptors(node))
    if not adaptors:
        click.echo("No gatecap blocks found.")
        return
    for adaptor in adaptors:
        for line in adaptor.info():
            click.echo(line)


@gatecap.command(help="Generate a capture core from a YAML description")
@click.argument("description", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", required=True,
              type=click.Path(file_okay=False),
              help="Directory to write the generated core into")
def generate(description, output):
    # No target involved: this reads a file and writes VHDL, so it takes no
    # resource path and resolves no capture tree.
    from .generator import DescriptionParser, DescriptionError, Generator

    try:
        parsed = DescriptionParser.load_file(description)
        core = Generator.of(parsed)
    except DescriptionError as e:
        raise click.ClickException(str(e))
    click.echo(f"{description}: rack {parsed.name.dotted()}, "
               f"{len(parsed.instruments)} instrument(s) over "
               f"{parsed.communication.mode}", err=True)
    for path in core.write(output):
        click.echo(f"wrote {path}", err=True)
