"""The ``capture`` subcommand, on gatecap's own CLI group.

A logic analyzer is reached through the same resource path as everything else
in a rack, so the command hangs off ``acrobe gatecap`` and reuses its
``-r/--root``:

  acrobe gatecap -r udp/127.0.0.1:4242/gatecap capture la.control \\
      --trigger 0xa0/0xf0 --count 64 --output trace.vcd

It drives the capture through the blocks' console adaptors -- the same driver
stack the GUI uses, so a capture and its trace rendering are shared, not
duplicated.
"""

import asyncio
import sys
import time

import asyncclick as click

from acrobe_plugin.gatecap.cli import gatecap, require_root
from acrobe_plugin.gatecap.enumerator import BlockAddress

from .blocks.control import Control, RleControl
from .driver import LogicAnalyzer
from .plan import Duration


async def _await_done(control, timeout):
    """Poll until the capture completes (core returns to idle) and return its
    triggered flag. Waits for the trigger indefinitely when timeout is 0; on
    timeout it aborts and returns False (the trigger never fired). Shows the
    driver's progress string live on a terminal (same string as the GUI)."""
    deadline = time.monotonic() + timeout if timeout else None
    tty = sys.stderr.isatty()
    last, width = "", 0
    try:
        while True:
            s = await control.status()
            if s[0] == control.STATE_IDLE:
                return s[1]
            if tty:
                report = await control.progress()
                if report and report != last:
                    width = max(width, len(report))
                    click.echo("\r  " + report.ljust(width), err=True, nl=False)
                    last = report
            if deadline is not None and time.monotonic() >= deadline:
                await control.abort()
                return False
            await asyncio.sleep(0.05)
    finally:
        if last:
            click.echo("", err=True)   # end the in-place progress line


def _format_for(output, fmt, default="csv"):
    if fmt:
        return fmt
    if output and output.lower().endswith(".vcd"):
        return "vcd"
    if output and output.lower().endswith(".csv"):
        return "csv"
    return default


@gatecap.command(help="Run a one-shot capture and dump the trace. TARGET names "
                      "a capture control (\"la.control\", or just \"control\" "
                      "where it is unambiguous), captured in its own samples "
                      "(--count/--pretrigger, or --pre-lines/--max-time for an "
                      "RLE core), or a logic analyzer holding several capture "
                      "domains -- naming the analyzer arms its whole "
                      "correlated group over a window given in real time "
                      "(--span/--pre) and dumps one composed absolute-time "
                      "VCD")
@click.argument("control_name", metavar="TARGET")
@click.option("--trigger", "triggers", multiple=True, metavar="TERM",
              help="Trigger term, repeatable. Per-signal NAME=VALUE (a scalar "
                   "0/1, a bus hex like command.data=0x66, or a bus "
                   "VALUE/MASK); or a single whole-vector value/mask like "
                   "0xa0/0xf0. Unlisted signals stay don't-care; none = "
                   "trigger on any sample")
@click.option("--count", type=int, default=None,
              help="Number of samples (raw controls; required for them)")
@click.option("--pretrigger", type=int, default=0,
              help="Raw: samples to keep from before the trigger (0 = "
                   "post-trigger only); the trigger sample is at relative 0")
@click.option("--pre-lines", "pre_lines", type=int, default=0,
              help="RLE (also for a group's RLE members): pre-trigger ring "
                   "size in lines (0 = post-trigger only)")
@click.option("--max-time", "max_time", type=float, default=0.0,
              help="RLE: post-trigger time cap in seconds (0 = until the "
                   "buffer fills)")
@click.option("--span", metavar="DURATION", default=None,
              help="Group: the capture window in real time -- 10us, 1.5ms, "
                   "800ns, or plain seconds. Members sample at different "
                   "rates, so each converts this span with its own capture "
                   "clock (an RLE member takes the post-trigger part of it as "
                   "its time cap)")
@click.option("--pre", metavar="DURATION", default=None,
              help="Group: how much of --span precedes the trigger (default "
                   "0); same syntax as --span")
@click.option("--timeout", type=float, default=0.0,
              help="Seconds to wait for the trigger before giving up "
                   "(0 = wait indefinitely; Ctrl-C also stops)")
@click.option("--output", type=click.Path(dir_okay=False), default=None,
              help="File to write (default: print to stdout)")
@click.option("--format", "fmt", type=click.Choice(["csv", "vcd"]), default=None,
              help="Output format (default: from --output extension, else csv)")
@click.pass_context
async def capture(ctx, control_name, triggers, count, pretrigger, pre_lines,
                  max_time, span, pre, timeout, output, fmt):
    node = await ctx.obj.resolve(require_root(ctx))
    # Capture targets: every control core (raw + RLE), and every logic
    # analyzer holding more than one of them -- naming the analyzer drives the
    # whole correlated group as one.
    nodes = list(node.children_of_class(Control))
    nodes += [a for a in node.children_of_class(LogicAnalyzer) if a.grouped()]
    targets = BlockAddress.targets(nodes)
    control = targets.get(control_name)
    if control is None:
        raise click.ClickException(
            f"no capture target {control_name!r} (available: "
            f"{', '.join(BlockAddress.canonical(nodes)) or 'none'})")

    cc = control.ui_adaptor("console")
    tc = control.trigger_node_get().ui_adaptor("console")

    group = isinstance(control, LogicAnalyzer)
    rle = isinstance(control, RleControl)
    # Reject options meant for another target kind instead of silently
    # ignoring them (which leaves the capture on defaults, e.g. pre_lines=0).
    if group and (count is not None or pretrigger or max_time):
        raise click.ClickException(
            f"{control_name!r} is a logic analyzer over several domains: its "
            "members "
            "sample at different rates, so the same sample count is a "
            "different span on each of them. Give the window in real time "
            "with --span/--pre (e.g. --span 10us --pre 2us), not "
            "--count/--pretrigger/--max-time")
    if not group and (span is not None or pre is not None):
        raise click.ClickException(
            f"{control_name!r} is a single capture control, captured in its "
            "own samples -- use "
            + ("--pre-lines/--max-time" if rle else "--count/--pretrigger")
            + ", not --span/--pre")
    if rle and (count is not None or pretrigger):
        raise click.ClickException(
            f"{control_name!r} is an RLE control -- use --pre-lines/--max-time, "
            "not --count/--pretrigger")
    if not rle and not group and (pre_lines or max_time):
        raise click.ClickException(
            f"{control_name!r} is a raw control -- use --count/--pretrigger, "
            "not --pre-lines/--max-time")
    try:
        span_seconds, pre_seconds = Duration.parse(span), Duration.parse(pre)
    except ValueError as e:
        raise click.ClickException(str(e))

    # The trigger block owns parsing + programming its own compare (value or
    # edge), so this stays trigger-type-agnostic. A value trigger fires whenever
    # the masked signals currently equal the value (mask 0 = any sample).
    try:
        summary = await tc.apply(triggers)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(f"trigger over {tc.driver.signal_count} signals: {summary}",
               err=True)

    # Arm, then wait for the capture to complete -- the trigger to fire and the
    # core to fill/cap. Unlike the self-test one-shot this waits for the
    # trigger, not a fixed poll budget.
    try:
        if group:
            # Every member's own parameters are derived from the window; the
            # plan reports what each of them will actually capture, including
            # any span a member's buffer could not hold.
            plan = await control.configure_and_arm(seconds=span_seconds,
                                                   pre_seconds=pre_seconds,
                                                   pre_lines=pre_lines)
            for line in plan.lines():
                click.echo("  " + line, err=True)
        elif rle:
            await control.configure_and_arm(pre_lines=pre_lines,
                                            max_seconds=max_time)
        else:
            await control.configure_and_arm(count=count, pretrigger=pretrigger)
    except ValueError as e:
        raise click.ClickException(str(e))

    click.echo("armed; waiting " + (f"up to {timeout:g}s " if timeout else "")
               + "for the trigger (Ctrl-C to stop) ...", err=True)
    try:
        triggered = await _await_done(control, timeout)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await control.abort()
        triggered = False
        click.echo("stopped", err=True)

    if rle or group:
        # Both reuse what the arm resolved (the group's per-member plan, the
        # RLE core's own end pointers), so nothing is restated here.
        result = await control.read_trace()
    else:
        result = await control.read_trace(count=count, pretrigger=pretrigger)
    result["triggered"] = triggered
    if not triggered:
        click.echo("warning: no trigger fired; wrote the pre-trigger buffer",
                   err=True)

    # A group has one timebase per member, which only VCD carries, so that is
    # what it defaults to; the console adaptor refuses anything else outright.
    fmt = _format_for(output, fmt, default="vcd" if group else "csv")
    try:
        data = cc.render(result, fmt)
    except ValueError as e:
        raise click.ClickException(str(e))
    if output:
        with open(output, "wb") as f:
            f.write(data)
    else:
        sys.stdout.buffer.write(data)
    click.echo(f"captured ({fmt})" + (f" to {output}" if output else ""),
               err=True)
