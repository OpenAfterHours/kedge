"""The ``kedge`` command line.

``kedge open <workbook>`` is the shortest path when you already know which workbook you want, and
``kedge hub`` is the one for when you do not: it starts the same server with nothing open and puts
a browser on the landing page, where every workbook kedge has seen is listed with its status and a
new one can be added by browsing or dropping. Neither replaces the other and they share every line
of the sequence that follows.

The rest of the commands exist because the milestones underneath are independently useful.
``inspect`` is an Excel archaeology tool that needs no model at all, ``plan`` is the whole review
gate on the command line — propose, read, acknowledge the drops, approve — for people who would
rather not do that in a chat window, ``reconcile`` is a diff between a notebook's output and the
workbook's cached values, ``watch`` is the unattended hand-in path with no browser anywhere near
it, and ``config`` and ``doctor`` are the two things anyone debugging a local tool with this many
moving parts asks for first.

This is the only module in kedge permitted to print. Everything else logs.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import ssl
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from kedge import __version__, tls
from kedge.config import (
    KEYRING_SERVICE,
    Config,
    ConfigError,
    LoadedConfig,
    api_key_status,
    keyring_set_command,
    load_config,
    user_dir,
)
from kedge.errors import KedgeError
from kedge.lifecycle import (
    MARIMO_PIN,
    health_check,
    installed_marimo_version,
)
from kedge.observability import configure_logging
from kedge.workspace import Workspace, iter_markers

if TYPE_CHECKING:
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.fill import FillReport
    from kedge.plan import PlanRun, PlanStore, ProcessPlan

logger = logging.getLogger(__name__)

__all__ = ["app", "main"]


def _console_args() -> list[str]:
    """The command line as it was typed, with ``~`` expanded and wildcards left alone.

    click expands glob patterns in ``sys.argv`` for itself on Windows -- its
    ``windows_expand_args`` -- because neither ``cmd.exe`` nor PowerShell does it for a native
    program. That is a kindness for a *path* argument and a silent corruption of a *pattern*
    one: ``kedge watch book.xlsx --glob "*.xlsx"``, run from the folder the workbook is in,
    which is the normal place to run it from, reaches the command as ``--glob book.xlsx``,
    sweeps the inbox for a file of that name, receives nothing, and exits 0. A flag that is
    accepted and quietly rewritten is worse than no flag at all.

    So the globbing is off (see :class:`_Cli`) and this puts back the one part of the expansion
    no Windows shell does for us. Environment variables are deliberately not expanded: both
    shells substitute their own before the process starts, so a second pass can only mangle a
    filename that happens to contain a ``$``. Only an argument that is a home-relative path is
    touched at all, so ``--glob "~$*.xlsx"`` -- the Excel lock files -- survives intact.
    """
    return [
        str(Path(arg).expanduser()) if arg == "~" or arg.startswith(("~/", "~\\")) else arg
        for arg in sys.argv[1:]
    ]


class _Cli(typer.Typer):
    """The kedge app, reading its own argv so click does not glob it first.

    The console script is ``kedge.cli:app``, so this is where the decision has to be made rather
    than in a wrapper the entry point does not go through: anything calling the app with no
    arguments -- the installed ``kedge``, the ``__main__`` block below -- gets
    :func:`_console_args` instead of click's expansion of ``sys.argv``. Tests go through
    ``CliRunner``, which always passes its arguments explicitly and so reaches neither; that is
    exactly why the wildcard bug this prevents was invisible to a suite of fifty CLI tests, and
    why the tests that cover it drive ``sys.argv`` instead.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not args:
            kwargs.setdefault("args", _console_args())
        kwargs.setdefault("windows_expand_args", False)
        return super().__call__(*args, **kwargs)


app = _Cli(
    name="kedge",
    help="Turn manual Excel processes into reviewable, reproducible marimo notebooks.",
    no_args_is_help=True,
    add_completion=False,
)

contract_app = typer.Typer(
    name="contract",
    help="Work with hand-in contracts.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(contract_app)

plan_app = typer.Typer(
    name="plan",
    help="Propose, review and approve a workbook's process plan.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(plan_app)


def _console(*, stderr: bool = False) -> Console:
    return Console(stderr=stderr, highlight=False)


def _plain(value: object) -> str:
    """Text rich must render verbatim: a range, a path, a filename, a name somebody typed.

    ``Console.print`` reads square brackets as markup, so anything Excel or a user authored has to
    be escaped on the way in or part of it disappears. Excel's external references are exactly the
    shape rich's tag regex accepts — ``[budget.xlsx]Sheet1!A1`` renders as ``Sheet1!A1``, and a
    message telling somebody to name a range they can no longer see is worse than no message.
    """
    return escape(str(value))


def _fail(message: str) -> typer.Exit:
    _console(stderr=True).print(f"[bold red]error[/bold red] {_plain(message)}")
    return typer.Exit(code=1)


# ── milestone shims ──────────────────────────────────────────────────────────────────────────
#
# Commands whose work belongs to a module another milestone owns resolve that module lazily.
# Until it lands, the command fails loudly and names the module responsible. Nothing here
# silently no-ops, and nothing here is a stub in somebody else's package.


def _resolve(module: str, attribute: str, milestone: str) -> Any:
    try:
        imported = __import__(module, fromlist=[attribute])
        return getattr(imported, attribute)
    except (ImportError, AttributeError) as exc:
        msg = (
            f"{module}.{attribute} is not implemented yet. It belongs to {milestone}, which is "
            f"owned by src/{module.replace('.', '/')}.py. The command surface is wired; the "
            f"implementation is not."
        )
        raise NotImplementedError(msg) from exc


# ── top level ────────────────────────────────────────────────────────────────────────────────


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kedge {__version__} (marimo pin {MARIMO_PIN})")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at DEBUG level.")] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Log warnings and errors only.")
    ] = False,
) -> None:
    """Turn manual Excel processes into reviewable, reproducible marimo notebooks."""
    del version
    configure_logging(verbose=verbose, quiet=quiet)


# ── open ─────────────────────────────────────────────────────────────────────────────────────


@app.command()
def open(  # the verb is the interface; shadowing the builtin is local and harmless
    workbook: Annotated[Path, typer.Argument(help="The Excel workbook to convert.")],
    port: Annotated[
        int | None, typer.Option("--port", help="Fix marimo's port instead of picking a free one.")
    ] = None,
    server_port: Annotated[
        int, typer.Option("--server-port", help="Port for the kedge server on loopback.")
    ] = 8000,
    plan: Annotated[
        Path | None,
        typer.Option(
            "--plan", help="Scaffold from this plan file instead of the latest approved one."
        ),
    ] = None,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open a browser.")
    ] = False,
    skip_analysis: Annotated[
        bool,
        typer.Option(
            "--skip-analysis", help="Reuse the existing analysis.json rather than re-analysing."
        ),
    ] = False,
) -> None:
    """Analyse a workbook, scaffold its notebook, and open the kedge workspace.

    The whole lifecycle from PLAN 2.9: clean up after any crashed previous run, analyse the
    workbook offline, scaffold the notebook from an approved plan, spawn a marimo server that
    kedge owns, assert a kernel session onto it, start the kedge server on loopback, and open a
    browser at it.

    ``--plan`` *adopts* a plan for this workbook — one kept under version control, say, rather than
    the one the plan store last approved. It is recorded in the store as a new version, because
    every other part of kedge decides what is in force by reading the store: scaffolding from a
    file it has never heard of would leave the notebook holding cells while the chat insisted no
    plan existed. If another version was approved, the step says which one this supersedes.

    Naming a file is not approving it, and because adopting one *persists* it, a file that cannot
    be adopted at all is refused before anything is spawned: one that will not load, one written
    for a different workbook, and one claiming an approval the review gate would have refused. The
    single case that still opens is a plan that is simply unapproved — that is a decision waiting
    on the user rather than a mistake in what they typed, so the workbook opens with an empty
    notebook, nothing is recorded, and the step says what is blocking.
    """
    console = _console()
    if not workbook.is_file():
        raise _fail(f"no such workbook: {workbook}")
    if plan is not None and not plan.is_file():
        raise _fail(f"no such plan: {plan}")
    if plan is not None:
        _require_usable_plan(plan, workbook)
    _require_bridge()
    if port is not None:
        console.print(f"[dim]pinning marimo to port {port} instead of picking a free one[/dim]")

    create_hub_app = _resolve("kedge.server.app", "create_hub_app", "M3 (the server and UI)")
    run_server = _resolve("kedge.server.app", "run_server", "M3 (the server and UI)")
    open_workbook = _resolve("kedge.server.hub", "open_workbook", "M3 (the server and UI)")

    # Built here rather than inside the open sequence so `--port` reaches marimo. A flag that is
    # accepted and only printed is worse than no flag at all.
    workspace = _workspace_for(workbook, marimo_port=port)

    # The open sequence lives in one place, and this is not it. Ordering here is not cosmetic --
    # scaffolding needs a live driver, so it cannot precede the marimo launch -- and a second copy
    # of the sequence in the CLI is a second copy to get wrong. Both entry points run the same
    # code; the only difference is where the progress is drawn.
    server_app = create_hub_app(version=__version__)
    state = server_app.state.kedge

    job = asyncio.run(
        open_workbook(
            state,
            workbook,
            reattach=True,
            workspace=workspace,
            plan_path=plan,
            on_event=_print_open_event(console),
        )
    )
    if not any(getattr(frame, "type", "") == "open_ready" for frame in job.frames):
        raise _fail(f"could not open {workbook.name}; see the steps above")

    workspace = state.require_workspace()
    console.print(f"[bold]workspace[/bold] {workspace.project_dir}")
    console.print(f"[green]marimo[/green] {workspace.require_marimo().base_url}")

    url = f"http://127.0.0.1:{server_port}"
    if not no_browser:
        webbrowser.open(url)
    console.print(f"[green]kedge[/green] {url}")
    run_server(server_app, port=server_port)


def _require_usable_plan(path: Path, workbook: Path) -> None:
    """Refuse a ``--plan`` file that cannot be adopted, before anything at all is spawned.

    The line this draws: a mistake in what the user typed is refused here, where they are standing
    and can fix it; a decision they have yet to take is not. The hub degrades and continues on all
    of it, because it has a page to keep answering and no file to name — that behaviour is
    untouched, and nothing here is reachable from it.

    Three refusals, all of them pure checks over the file:

    * **It will not load.** A path typed wrong already failed at the ``is_file`` check above; a
      YAML typo used to run on for five steps, spawn a marimo, open a workbook around an empty
      notebook, point a browser at it and exit 0, with the one FAIL line scrolled off above the
      banner.
    * **It was written for a different workbook.** ``--plan`` now *records* what it adopts, so
      this would file another workbook's decomposition in this one's plan history, approved and in
      force, with every consumer reading it. :func:`_explicit_analysis` refuses the same case for
      the same reason — nothing downstream catches it, and ``workbook_sha256`` is read nowhere in
      ``src/``. The message names the way through, and it has to be the *edit*: the commonest
      legitimate case is last month's plan against ``..._v15.xlsx``, where there is no plan for the
      new name — that being why ``--plan`` was reached for — and opening the old file opens last
      month's workbook. There is deliberately no override flag; a flag would be typed by reflex,
      and what this refuses is durable. A matching filename with a different digest is *not* this
      case: that is the same workbook, changed, which is the normal state of a monthly process and
      is warned about in the open sequence rather than refused. :func:`_require_usable_plan` is not
      the only place this is enforced — see :func:`kedge.server.hub._foreign_workbook`, which
      guards the write itself.
    * **It claims an approval the review gate would have refused.** Anything that went through
      :func:`kedge.plan.review.approve` is approvable by construction, so this catches only a
      hand-written file — typically one proposing a drop nobody acknowledged, which is precisely
      the silent removal the acknowledge verb exists to prevent.

    An *unapproved* file is deliberately not refused: that is the review decision the user has yet
    to take, and the open sequence's account of it — the workbook opens, the notebook stays empty,
    the step says what is blocking — is the right one.

    Raises:
        typer.Exit: naming what is wrong with the file, in the store's own words where it is the
            store that knows.
    """
    from kedge.plan.store import PlanStoreError, plan_from_yaml, read_plan_text

    try:
        plan = plan_from_yaml(read_plan_text(path))
    except PlanStoreError as exc:
        raise _fail(f"the plan at {path} cannot be used: {exc}") from exc

    # The same reading of a plan's `workbook:` the open sequence uses, so the pre-flight and the
    # step cannot come to different conclusions about what the file claims.
    from kedge.server.hub import plan_workbook_name

    named = plan_workbook_name(plan)
    if named and named.casefold() != workbook.name.casefold():
        raise _fail(
            f"the plan at {path} was written for {plan.workbook}, not {workbook.name}. Adopting it "
            f"would file that decomposition in {workbook.name}'s own plan history as the approved "
            f"plan in force, and every stage in it names the other workbook's ranges. If this is "
            f"the same process under a new filename, change the plan's `workbook:` line to "
            f"{workbook.name} and run this again — the file is a review artifact and is meant to "
            f"be edited — and kedge will adopt it, noting as it does that the workbook has changed "
            f"since the plan was written for it."
        )

    blockers = plan.approval_blockers()
    if plan.approval.approved and blockers:
        _console(stderr=True).print(
            f"[bold red]error[/bold red] the plan at {_plain(path)} says it is approved, but the "
            f"review gate would have refused it, so kedge will not adopt it"
        )
        for blocker in blockers:
            typer.echo(f"  - {blocker}", err=True)
        typer.echo(
            "  clear these in the plan file itself, or approve it through `kedge plan approve`, "
            "which cannot record an approval while they stand.",
            err=True,
        )
        raise typer.Exit(code=1)


def _print_open_event(console: Console) -> Callable[[Any], None]:
    """Draw the hub's open-progress frames onto the terminal.

    The hub streams these into a browser; on the command line the same frames become one line per
    step, so `kedge open` says what it is doing for the several seconds it takes rather than
    sitting silent (PLAN M3).
    """
    marks = {"running": "[dim]···[/dim]", "ok": "[green]ok [/green]", "skipped": "[dim]-- [/dim]"}

    def draw(event: Any) -> None:
        # Nothing an event carries is interpolated into markup: these frames name paths, and a
        # step whose whole job is to say which file it could not read must not mangle the name.
        # `_plain` is not enough on its own here. rich only escapes brackets that look like a tag,
        # so `Docs [old]\agreed.yaml` survives it -- but a folder named `[2026]` does not, because
        # the renderer eats the backslash in front of the bracket either way and prints
        # `p[2026]\a.yaml`, a different path, silently. Appending to a `Text` parses no markup at
        # all, which is the only form that is right for both.
        if getattr(event, "type", "") != "open_progress":
            if getattr(event, "type", "") == "error":
                console.print(Text.from_markup("[bold red]error[/bold red] ").append(event.message))
            return
        if event.state == "running":
            return  # The terminal has no spinner to update; report outcomes only.
        mark = marks.get(event.state, "[bold red]FAIL[/bold red]")
        line = Text.from_markup(f"  {mark} ").append(event.step)
        if event.detail:
            line.append(" ").append(event.detail, style="dim")
        console.print(line)

    return draw


# ── hub ──────────────────────────────────────────────────────────────────────────────────────


@app.command()
def hub(
    port: Annotated[
        int, typer.Option("--port", help="Port for the kedge server on loopback.")
    ] = 8000,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open a browser.")
    ] = False,
) -> None:
    """Open the kedge hub: every workbook kedge has seen, and a way to add another.

    Starts the server with no workbook attached. The browser lands on the list rather than on a
    chat window with nothing in it, and opening a workbook from there runs exactly the sequence
    ``kedge open`` runs — clean up, analyse, scaffold, spawn marimo, bootstrap the session — with
    the progress streamed into the page instead of onto the terminal.
    """
    console = _console()
    _require_bridge()
    create_hub_app = _resolve("kedge.server.app", "create_hub_app", "M3 (the server and UI)")
    run_server = _resolve("kedge.server.app", "run_server", "M3 (the server and UI)")

    registry = _resolve("kedge.registry", "WorkbookRegistry", "the workbook registry")
    known = registry.for_user().entries()
    console.print(
        f"[dim]{len(known)} workbook(s) in {user_dir() / 'registry.json'}[/dim]"
        if known
        else "[dim]no workbooks registered yet; add one from the hub[/dim]"
    )

    server_app = create_hub_app(version=__version__)
    url = f"http://127.0.0.1:{port}"
    if not no_browser:
        webbrowser.open(url)
    console.print(f"[green]kedge hub[/green] {url}")
    run_server(server_app, port=port)


# ── inspect ──────────────────────────────────────────────────────────────────────────────────


@app.command()
def inspect(
    workbook: Annotated[Path, typer.Argument(help="The Excel workbook to analyse.")],
    out: Annotated[Path | None, typer.Option("--out", help="Write the analysis JSON here.")] = None,
    report: Annotated[
        Path | None, typer.Option("--report", help="Write an HTML report here.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the analysis to stdout as JSON.")
    ] = False,
) -> None:
    """Analyse a workbook and report what is in it. No model, no network (PLAN M1)."""
    if not workbook.is_file():
        raise _fail(f"no such workbook: {workbook}")

    workspace = _workspace_for(workbook)
    analyse = _resolve("kedge.analysis.analyse", "analyse", "M1 (the analyser)")
    analysis = analyse(workspace.workbook_path)

    destination = out or workspace.analysis_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")

    if report is not None:
        write_report = _resolve("kedge.report", "write_report", "M1 (the analyser)")
        write_report(analysis, report)

    if as_json:
        typer.echo(analysis.model_dump_json(indent=2))
    else:
        _console().print(f"[green]analysis[/green] {destination}")


# ── plan ─────────────────────────────────────────────────────────────────────────────────────
#
# Seven verbs rather than one, because the approval gate is the whole point (PLAN 2.2).
# ``propose`` writes a **draft** and stops there; approving is a separate, deliberate act; and a
# plan that proposes dropping a range cannot be approved until somebody has said something about
# each drop, which is why ``acknowledge`` is a command rather than a hand edit of the YAML.
# There is deliberately no ``--approve`` flag on ``propose``: proposing and approving in one
# breath is exactly the gate this project exists to keep shut.
#
# Only ``propose`` needs a model endpoint. Every review verb is offline — PLAN M2 makes that a
# property of the design ("steps 1, 3 and 4 need no LLM"), and it is what lets somebody approve a
# plan on a train. Nothing below reaches :func:`kedge.plan.propose.completer_from_config`, which
# is the one function that wants a base URL and a key from the keyring.
#
# Exit codes are part of the surface, because ``plan propose`` has two different non-zero
# answers. ``2`` means triage refused: this workbook should not be converted, which
# :class:`~kedge.plan.PlanRun` models as a legitimate result rather than a failure. ``1`` is every
# ordinary failure — no such workbook, no API key, an analysis that will not load. A script that
# cannot tell the two apart either treats "do not convert this" as a crash or treats a crash as an
# editorial opinion.
#
# Imported directly rather than through :func:`_resolve`. The shim exists for a module a
# milestone has not landed yet, and it converts any ImportError or AttributeError into "not
# implemented yet" — for a module that *is* implemented that turns a real breakage, or a typo in
# an attribute name, into a confident lie. Not hypothetical: ``contract infer`` resolved
# ``infer_contract``, a name that has never existed, and every invocation died as "not implemented
# yet" until a test caught it. The imports sit inside the commands so ``kedge --help`` and
# ``kedge config`` do not pay for the plan models, exactly as :func:`_require_bridge` already
# does for the notebook package.


TRIAGE_REFUSED = 2
"""The exit code for "this workbook should not be converted", as distinct from a failure."""


@plan_app.command("propose")
def plan_propose(
    workbook: Annotated[Path, typer.Argument(help="The Excel workbook to plan.")],
    analysis: Annotated[
        Path | None,
        typer.Option("--analysis", help="Plan from this analysis.json instead of the saved one."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan without saving anything.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Propose even when triage says stop.")
    ] = False,
    reseed: Annotated[
        bool,
        typer.Option(
            "--reseed/--no-reseed",
            help="Offer the most recent saved plan to the model as a worked example.",
        ),
    ] = True,
    max_attempts: Annotated[
        int, typer.Option("--max-attempts", help="Attempts, including validation repairs.")
    ] = 3,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the triage, the warnings and the plan as JSON.")
    ] = False,
) -> None:
    """Triage a workbook and propose a process plan for it. The only verb that needs a model.

    What lands on disk is a **draft**. Approving it is a separate act — `kedge plan approve` —
    because the value of a plan is that a human read the decomposition while correcting it was
    still cheap (PLAN 2.2).

    ``--dry-run`` writes nothing at all, which is what makes judging `propose` across a corpus of
    dissimilar workbooks cheap (PLAN 7 step 4): run it over five of them and read the plans.

    Proposing over a plan that is already approved is allowed — this is the batch route, and the
    way to amend a plan by hand is to edit it, not to be refused — but the diff against the
    approved one is printed, so no decomposition ever displaces another one unseen.

    Exit codes: 0 when a plan was proposed, 2 when triage refused — a legitimate answer rather
    than a failure — and 1 for an ordinary failure such as no such workbook or no API key.
    """
    workspace = _plan_workspace(workbook)
    facts = _explicit_analysis(workspace, workbook, analysis) if analysis is not None else None
    in_force = _plan_in_force(_plan_store(workspace))

    from kedge.plan import run_plan

    try:
        run = run_plan(
            workspace.workbook_path,
            analysis=facts,
            analysis_path=analysis,
            workspace=workspace,
            dry_run=dry_run,
            force=force,
            reseed=reseed,
            max_attempts=max_attempts,
        )
    except KedgeError as exc:
        raise _fail(str(exc)) from exc

    # Nothing was written on a dry run, so nothing is being replaced and there is no diff to draw.
    replaced = in_force if run.saved_to is not None else None
    if as_json:
        typer.echo(json.dumps(_plan_run_payload(run, replaced=replaced), indent=2, default=str))
    else:
        _print_plan_run(run, replaced=replaced)
    if run.stopped:
        raise typer.Exit(code=TRIAGE_REFUSED)


def _print_plan_run(run: PlanRun, *, replaced: ProcessPlan | None) -> None:
    """Draw a proposal: the triage and the plan, then the warnings, then where it went."""
    console = _console()
    typer.echo(run.render())
    if run.stopped:
        console.print("\n[dim]triage refused; --force proposes anyway[/dim]")
        return
    if run.warnings:
        console.print(f"\n[yellow]{len(run.warnings)} review warning(s)[/yellow]")
        for warning in run.warnings:
            typer.echo(f"  - {warning}")
    if run.saved_to is None:
        console.print("\n[dim]--dry-run: nothing was written[/dim]")
        return
    console.print(f"\n[green]plan[/green] {_plain(run.saved_to)}")
    console.print(
        "[dim]this is a draft; nothing reaches the notebook until "
        "`kedge plan approve` is given[/dim]"
    )
    if replaced is not None and run.plan is not None:
        console.print(
            f"\n[yellow]plan v{replaced.version} is approved and in force[/yellow] "
            f"[dim](by {_plain(replaced.approval.by or 'nobody named')})[/dim]"
        )
        console.print(
            f"[dim]approving v{run.plan.version} would put this decomposition in its place; "
            f"what differs:[/dim]"
        )
        typer.echo(_replacement_diff(replaced, run.plan))


def _replacement_diff(approved: ProcessPlan, draft: ProcessPlan) -> str:
    """What a new proposal would change about the plan currently in force.

    The chat tool refuses this outright and points at ``amend_plan``; the command line has no
    amend verb to redirect to, so refusing here would leave hand-editing YAML as the only way
    forward, and would break the batch route PLAN 7 step 4 judges the corpus with. Showing the
    diff is the answer instead — it is what the user needed either way, and
    :func:`~kedge.plan.review.render_diff` already has a case for two versions that differ only in
    approval state, which is exactly what a re-proposal of an unchanged process produces.
    """
    from kedge.plan.review import diff_plans, render_diff

    return render_diff(diff_plans(approved, draft))


def _plan_run_payload(run: PlanRun, *, replaced: ProcessPlan | None) -> dict[str, Any]:
    """The machine-readable form of a proposal: what triage decided, and the plan itself."""
    return {
        "workbook": str(run.workbook),
        "stopped": run.stopped,
        "triage": {
            "verdict": run.triage.verdict.value,
            "convertible": run.triage.convertible,
            "complexity": run.triage.complexity,
            "blockers": run.triage.blocker_lines(),
        },
        "saved_to": str(run.saved_to) if run.saved_to is not None else None,
        "warnings": list(run.warnings),
        "plan": run.plan.model_dump(mode="json") if run.plan is not None else None,
        # A script reading JSON gets the same warning the terminal does: this draft, if approved,
        # displaces a decomposition somebody already signed off.
        "replaces_approved_version": replaced.version if replaced is not None else None,
        "diff_from_approved": (
            _replacement_diff(replaced, run.plan)
            if replaced is not None and run.plan is not None
            else None
        ),
    }


@plan_app.command("show")
def plan_show(
    workbook: Annotated[Path, typer.Argument(help="The workbook whose plan to render.")],
    version: Annotated[
        int | None, typer.Option("--version", help="Render this version instead of the latest.")
    ] = None,
) -> None:
    """Render a saved plan, its review warnings, and whatever stands between it and approval.

    Offline. A plan already on disk is read, rendered and assessed with no model call at all.
    """
    workspace = _plan_workspace(workbook)
    store = _plan_store(workspace)
    plan = _require_plan(store, workbook, version=version)

    from kedge.plan import render_plan

    typer.echo(render_plan(plan, analysis=_saved_analysis(workspace)))
    _console().print(f"\n[dim]{_plain(store.path_for(plan.version))}[/dim]")


@plan_app.command("approve")
def plan_approve(
    workbook: Annotated[Path, typer.Argument(help="The workbook whose latest plan to approve.")],
    by: Annotated[
        str | None, typer.Option("--by", help="Who is approving. Defaults to the OS user.")
    ] = None,
    note: Annotated[
        str | None, typer.Option("--note", help="What was checked, or why this is right.")
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Do not ask before replacing an approved plan."),
    ] = False,
) -> None:
    """Approve the latest plan, unlocking the scaffolder. Nothing is scaffolded before this.

    Goes through :func:`kedge.plan.review.approve`, so every approval blocker runs: a plan
    proposing a drop nobody has acknowledged is refused here, with the list and what would clear
    each one, rather than approved quietly.

    When another version is already approved, the diff against it is printed and confirmed before
    anything is recorded — this is the moment the user is standing there, and it is the last one
    at which correcting a replaced decomposition is cheap. ``--yes`` is for scripts.

    The plan's own version is not bumped, because an approval is a decision about a version rather
    than a new one. The *record* of the decision is a new file all the same, so an earlier
    decision on the same version is still there to read.

    Approving the plan already in force is a no-op that succeeds. Nothing has changed, so there is
    no decision to record, and writing a version whose only difference from the one before it is a
    timestamp fills the history with entries that say nothing. To record a *different* reviewer
    against the same decomposition, withdraw the approval first — that is a real change of mind
    and the history should show one.
    """
    workspace = _plan_workspace(workbook)
    store = _plan_store(workspace)
    plan = _require_plan(store, workbook)
    _warn_if_the_workbook_moved_on(plan, workbook)
    if plan.approval.approved:
        _console().print(
            f"[green]already approved[/green] plan v{plan.version} for {_plain(workbook.name)}, "
            f"by {_plain(plan.approval.by or 'someone unrecorded')}"
            + (f" at {_stamp(plan.approval.at)} UTC" if plan.approval.at else "")
            + " [dim]nothing recorded[/dim]"
        )
        return
    if plan.is_approvable:
        _confirm_replacing_the_plan_in_force(store, plan, assume_yes=yes)

    from kedge.plan.review import PlanNotApprovableError, approve

    try:
        approved = approve(plan, by=_reviewer(by), note=note)
    except PlanNotApprovableError as exc:
        _report_blockers(plan, workbook)
        raise typer.Exit(code=1) from exc

    stored, path = _record_plan(store, approved)
    _console().print(
        f"[green]approved[/green] plan v{plan.version} for {_plain(workbook.name)}, "
        f"by {_plain(stored.approval.by)} [dim]recorded as v{stored.version} {_plain(path)}[/dim]"
    )


@plan_app.command("acknowledge")
def plan_acknowledge(
    workbook: Annotated[Path, typer.Argument(help="The workbook whose plan proposes the drops.")],
    range_: Annotated[
        str | None,
        typer.Option("--range", help="One dropped range, exactly as the plan writes it."),
    ] = None,
    all_drops: Annotated[
        bool, typer.Option("--all", help="Confirm every outstanding drop at once.")
    ] = False,
    reject_drop: Annotated[
        bool,
        typer.Option("--reject", help="Refuse the drop instead: the range must be kept."),
    ] = False,
    note: Annotated[
        str | None, typer.Option("--note", help="Your reason. Recorded either way.")
    ] = None,
) -> None:
    """Confirm or refuse the ranges a plan proposes to drop, so approval can proceed.

    An unacknowledged drop blocks approval, because silent removal is indistinguishable from a
    bug. Refusing one is not the same as ignoring it: ``--reject`` keeps the range and raises an
    open question asking which stage consumes it, which keeps approval blocked until somebody
    answers (PLAN 2.2). Both outcomes are recorded against the drop, with the note.
    """
    if all_drops == (range_ is not None):
        raise _fail("pass exactly one of --range and --all")
    if all_drops and reject_drop:
        raise _fail("--reject refuses one named drop; pass --range with it")

    workspace = _plan_workspace(workbook)
    store = _plan_store(workspace)
    plan = _require_plan(store, workbook)
    if all_drops and not plan.unacknowledged_drops:
        raise _fail(f"plan v{plan.version} for {workbook.name} has no unacknowledged drops")

    from kedge.plan.review import acknowledge_all_drops, acknowledge_drop

    try:
        revised = (
            acknowledge_all_drops(plan, note=note)
            if all_drops
            else acknowledge_drop(plan, str(range_), accepted=not reject_drop, note=note)
        )
    except KedgeError as exc:
        raise _fail(str(exc)) from exc

    stored, path = _record_plan(store, revised)
    console = _console()
    outcome = "refused; the range must be kept" if reject_drop else "confirmed"
    typer.echo(f"acknowledged {'every outstanding drop' if all_drops else range_}: {outcome}")
    console.print(f"[green]plan[/green] v{stored.version} [dim]{_plain(path)}[/dim]")
    _print_remaining_blockers(stored, workbook)


@plan_app.command("reject")
def plan_reject(
    workbook: Annotated[Path, typer.Argument(help="The workbook whose latest plan to reject.")],
    reason: Annotated[str, typer.Option("--reason", help="Why the decomposition was turned down.")],
    by: Annotated[
        str | None, typer.Option("--by", help="Who is rejecting. Defaults to the OS user.")
    ] = None,
    withdraw_approval: Annotated[
        bool,
        typer.Option(
            "--withdraw-approval", help="Reject a plan that is approved, taking the approval back."
        ),
    ] = False,
) -> None:
    """Reject a plan outright. Terminal: a rejected plan can never be approved.

    The reason is required and travels with the plan, because "why was this turned down" outlives
    the version it was turned down at — and an edit to a rejected plan carries the rejection
    forward rather than quietly resetting it to draft. The way on is a new proposal.

    Rejecting a plan that is *approved* needs ``--withdraw-approval``, because a notebook may
    already have been scaffolded from it. The approval that is overturned is recorded on the plan.
    """
    workspace = _plan_workspace(workbook)
    store = _plan_store(workspace)
    plan = _require_plan(store, workbook)

    from kedge.plan.review import PlanNotApprovableError, reject

    try:
        rejected = reject(
            plan, by=_reviewer(by), reason=reason, withdraw_approval=withdraw_approval
        )
    except PlanNotApprovableError as exc:
        raise _decision_refused(exc, plan, "--withdraw-approval") from exc

    stored, path = _record_plan(store, rejected)
    _console().print(
        f"[red]rejected[/red] plan v{plan.version} for {_plain(workbook.name)}, "
        f"by {_plain(stored.approval.by)} [dim]recorded as v{stored.version} {_plain(path)}[/dim]"
    )
    typer.echo(f"  reason: {reason}")
    _console().print(
        f'[dim]propose a new one with `kedge plan propose "{_plain(workbook)}"`; this one cannot '
        f"be approved[/dim]"
    )


@plan_app.command("request-changes")
def plan_request_changes(
    workbook: Annotated[Path, typer.Argument(help="The workbook whose latest plan to send back.")],
    note: Annotated[str, typer.Option("--note", help="What needs to change, and why.")],
    by: Annotated[
        str | None, typer.Option("--by", help="Who is asking. Defaults to the OS user.")
    ] = None,
    withdraw_approval: Annotated[
        bool,
        typer.Option(
            "--withdraw-approval",
            help="Send back a plan that is approved, taking the approval back.",
        ),
    ] = False,
) -> None:
    """Send a plan back for changes without editing it and without closing it off.

    Unlike a rejection this is not terminal: the plan can still be revised and approved. The note
    is required, because "changes requested" with nothing said is not a review.

    Sending back a plan that is *approved* needs ``--withdraw-approval``, because a notebook may
    already have been scaffolded from it. The approval that is overturned is recorded on the plan.
    """
    workspace = _plan_workspace(workbook)
    store = _plan_store(workspace)
    plan = _require_plan(store, workbook)

    from kedge.plan.review import PlanNotApprovableError, request_changes

    try:
        returned = request_changes(
            plan, by=_reviewer(by), note=note, withdraw_approval=withdraw_approval
        )
    except PlanNotApprovableError as exc:
        raise _decision_refused(exc, plan, "--withdraw-approval") from exc

    stored, path = _record_plan(store, returned)
    _console().print(
        f"[yellow]changes requested[/yellow] on plan v{plan.version} for "
        f"{_plain(workbook.name)}, by {_plain(stored.approval.by)} "
        f"[dim]recorded as v{stored.version} {_plain(path)}[/dim]"
    )
    typer.echo(f"  note: {note}")


_APPROVAL_STYLE = {
    "draft": "dim",
    "changes_requested": "yellow",
    "approved": "green",
    "rejected": "red",
}


def _approval_cell(state: str) -> str:
    """One approval state as a table cell, styled where this CLI knows the state.

    Looked up rather than indexed: :class:`~kedge.plan.model.ApprovalState` is a schema another
    module owns, and a fifth member arriving must not turn `kedge plan history` into a KeyError
    over a colour.
    """
    style = _APPROVAL_STYLE.get(state, "")
    return f"[{style}]{state}[/{style}]" if style else _plain(state)


def _plan_author(plan: ProcessPlan) -> str:
    """Who wrote a version: the model that generated it, or the human who edited it.

    The model id is shown only for a version a model actually produced. Every review edit goes
    through :func:`kedge.plan.review._revise`, which sets ``generated_by="human"`` and leaves
    ``llm_model`` exactly where it was, so joining the two unconditionally rendered
    ``human (gpt-4o)`` under a column headed *author* — which reads as a claim that a human wrote
    it with GPT-4o. The model that wrote the version this one was derived from is on its own row.
    """
    if plan.generated_by == "llm" and plan.llm_model:
        return f"{plan.generated_by} ({_plain(plan.llm_model)})"
    return plan.generated_by


def _stamp(moment: datetime) -> str:
    """A plan timestamp, in UTC because that is how the store writes them.

    Rendered without the zone it was in, ``created`` reads as local time to everybody who looks at
    it, and a plan approved at 23:40 UTC belongs to the wrong day for most of the world.
    """
    return f"{moment.astimezone(UTC) if moment.tzinfo else moment:%Y-%m-%d %H:%M}"


@plan_app.command("history")
def plan_history(
    workbook: Annotated[Path, typer.Argument(help="The workbook whose plan history to list.")],
) -> None:
    """Every saved version of a plan, oldest first. The change record (PLAN 2.2).

    History is retained rather than overwritten, so when the process changes next quarter this is
    the list the diff is taken across.
    """
    workspace = _plan_workspace(workbook)
    store = _plan_store(workspace)
    try:
        history = store.history()
    except KedgeError as exc:
        raise _fail(str(exc)) from exc
    if not history:
        raise _fail(_no_plan_message(workbook))

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("version")
    table.add_column("created (UTC)")
    table.add_column("author")
    table.add_column("stages", justify="right")
    table.add_column("approval")
    table.add_column("by")
    for plan in history:
        table.add_row(
            f"v{plan.version}",
            _stamp(plan.created_at),
            _plan_author(plan),
            str(len(plan.stages)),
            _approval_cell(plan.approval.state.value),
            _plain(plan.approval.by or ""),
        )
    console = _console()
    console.print(table)
    console.print(f"[dim]{_plain(store.directory)}[/dim]")


# ── plan helpers ─────────────────────────────────────────────────────────────────────────────


def _plan_workspace(workbook: Path) -> Workspace:
    """The workspace for a plan command, refusing a workbook that is not there."""
    if not workbook.is_file():
        raise _fail(f"no such workbook: {workbook}")
    return _workspace_for(workbook)


def _plan_store(workspace: Workspace) -> PlanStore:
    from kedge.plan import PlanStore

    return PlanStore.for_workspace(workspace)


def _no_plan_message(workbook: Path) -> str:
    return (
        f"no process plan saved for {workbook.name}. Propose one with "
        f'`kedge plan propose "{workbook}"`, or ask kedge in the chat to propose one.'
    )


def _require_plan(store: PlanStore, workbook: Path, *, version: int | None = None) -> ProcessPlan:
    """The plan a review verb acts on: a named version, or the latest saved.

    A workbook with no plan, or a version that is not there, is a message naming the command that
    would write one — never a traceback, and never the empty rendering of nothing.
    """
    try:
        plan = store.load(version) if version is not None else store.latest()
    except KedgeError as exc:
        raise _fail(str(exc)) from exc
    if plan is None:
        raise _fail(_no_plan_message(workbook))
    return plan


def _decision_refused(exc: Exception, plan: ProcessPlan, flag: str) -> typer.Exit:
    """A review refusal from :mod:`kedge.plan.review`, in the command line's own vocabulary.

    That module is library code shared with the web routes, so it names the keyword argument that
    would allow what it just refused — ``withdraw_approval=True``, which is not a thing anybody
    can type at a shell. The flag is named here rather than by rewriting its message, so rewording
    there cannot silently leave this pointing at nothing.

    It is named only where it is the answer. :func:`~kedge.plan.review._decide` refuses two
    transitions and only one of them has a way past: an approval can be withdrawn deliberately, a
    rejection is terminal, and offering the flag there would send the user round a loop ending in
    the same message.
    """
    from kedge.plan.model import ApprovalState

    if plan.approval.state is ApprovalState.APPROVED:
        return _fail(f"{exc}\n  on the command line, ask for that with {flag}")
    return _fail(str(exc))


def _plan_in_force(store: PlanStore) -> ProcessPlan | None:
    """The approved plan for this workbook, or None when there is not one.

    Reading it walks the whole history, so a hand-edited version that will not parse surfaces
    here as much as anywhere else — as a message naming the file, never a traceback.
    """
    try:
        return store.latest_approved()
    except KedgeError as exc:
        raise _fail(str(exc)) from exc


def _record_plan(store: PlanStore, plan: ProcessPlan) -> tuple[ProcessPlan, Path]:
    """Write a reviewed plan at the next free version. Never over a version already on disk.

    Every review outcome is recorded the same way, edits and approval decisions alike. The model
    semantics do differ — :func:`~kedge.plan.review.approve` deliberately does not bump the
    version, because an approval is a decision *about* a version rather than a new one — but the
    store is a record of decisions, not of versions, and writing a decision over the file it names
    destroys every earlier decision on that version. Three reviewers in a row left one surviving
    line and nothing on disk showing the other two happened, the dangerous one being a
    `request-changes` that silently un-approved a plan a notebook may already have been scaffolded
    from.

    :meth:`PlanStore.save_next` renumbers and sets ``based_on_version``, so the chain still says
    which version each decision was taken against, and the artifacts are byte-identical to the
    ones the chat path writes — it records every approval through the same method
    (``server/routes.py``).
    """
    try:
        return store.save_next(plan)
    except KedgeError as exc:
        raise _fail(str(exc)) from exc


_INFERRED = " (inferred from the OS user)"
"""What is appended to a reviewer name nobody actually typed. See :func:`_reviewer`."""


def _reviewer(explicit: str | None) -> str:
    """Who is recording a review decision: ``--by`` when given, the OS user when not.

    kedge is single-user and local (SECURITY.md), so there is exactly one plausible answer and
    demanding it on every command would turn an audit trail into paperwork people type ``x`` into.
    The default stays. But the two answers are not the same claim and must not read as though they
    were: ``getpass.getuser()`` consults ``LOGNAME``, ``USER``, ``LNAME`` and ``USERNAME`` before
    it asks the operating system, so on 3.13 it is settable by anyone who can set an environment
    variable — ``USER=mallory kedge plan approve`` records mallory — and under CI it says
    ``runner``, in a container ``root``.

    So an inferred name is recorded as ``philm (inferred from the OS user)``. The marker goes in
    ``approval.by``, because that is the field carrying the identity claim and the identity is
    what is uncertain; the note belongs to the reviewer's own account of what they checked, and
    machine provenance in it would be read months later as something a person wrote.

    The web routes have no OS user to read at all — a browser on loopback cannot say who is at it
    — and record the literal ``"user"``; that is the fallback here too when the OS will not say.
    ``getpass.getuser()`` raised more than ``OSError`` before 3.13, hence the wider catch.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        name = getpass.getuser()
    except (OSError, ImportError, KeyError):  # pragma: no cover - platform dependent
        logger.debug("could not read the OS user; recording the decision as 'user'")
        name = "user"
    return f"{name}{_INFERRED}"


def _confirm_replacing_the_plan_in_force(
    store: PlanStore, plan: ProcessPlan, *, assume_yes: bool
) -> None:
    """Show what approving this plan changes about the one in force, and ask before recording it.

    Approval is the moment the user is standing there, which makes it the last cheap moment to
    notice that the decomposition about to take effect is not the one reviewed last time. Nothing
    is asked when no plan is approved yet, or when the version being approved is the one already
    in force.
    """
    in_force = _plan_in_force(store)
    if in_force is None or in_force.version == plan.version:
        return

    console = _console()
    console.print(
        f"[yellow]plan v{in_force.version} is already approved[/yellow] "
        f"[dim](by {_plain(in_force.approval.by or 'nobody named')})[/dim]; approving "
        f"v{plan.version} puts a different decomposition in force. What differs:"
    )
    typer.echo(_replacement_diff(in_force, plan))
    if assume_yes or typer.confirm(f"Approve v{plan.version} in place of v{in_force.version}?"):
        return
    console.print(
        f"[dim]nothing was recorded; plan v{in_force.version} stays the one in force[/dim]"
    )
    raise typer.Exit(code=1)


def _explicit_analysis(workspace: Workspace, workbook: Path, path: Path) -> WorkbookAnalysis:
    """Load an analysis named with ``--analysis``, refusing one taken from a different workbook.

    A plan takes its whole identity from the analysis it was written against — ``workbook`` and
    ``workbook_sha256`` both — so an analysis of another file lands a plan in this workbook's
    project directory claiming to be for that one. ``kedge plan show alpha.xlsx`` then prints
    "plan for beta.xlsx" while the approval prints "for alpha.xlsx", and nothing downstream
    catches it: ``workbook_sha256`` is documented as tying a plan to the exact file it was written
    for and is read nowhere in ``src/``.

    The digest decides, not the filename: two workbooks are routinely called ``monthly.xlsx``.
    A digest that differs while the filename matches is the *same* workbook, changed since the
    analysis was taken, which is a legitimate thing to be doing badly — a warning, not a refusal.
    """
    if not path.is_file():
        raise _fail(f"no such analysis: {path}")

    from kedge.plan import load_analysis

    try:
        facts = load_analysis(workbook, analysis_path=path, workspace=workspace)
    except KedgeError as exc:
        raise _fail(str(exc)) from exc

    digest = _digest_of(workbook)
    if digest is None or facts.workbook.sha256 == digest:
        return facts
    if facts.workbook.filename.casefold() == workbook.name.casefold():
        _console().print(
            f"[yellow]warning[/yellow] the analysis at {_plain(path)} was taken from a different "
            f"copy of {_plain(workbook.name)} than the one on disk; the plan will be written "
            f"against facts that are already out of date. Re-run `kedge inspect` first if that "
            f"matters."
        )
        return facts
    raise _fail(
        f"the analysis at {path} describes {facts.workbook.filename}, not {workbook.name}. "
        f"A plan takes its workbook identity from its analysis, so this would file a plan for "
        f'{facts.workbook.filename} under {workbook.name}. Run `kedge inspect "{workbook}"` to '
        f"write the analysis this workbook's plan should be built from."
    )


def _warn_if_the_workbook_moved_on(plan: ProcessPlan, workbook: Path) -> None:
    """Say so when the file no longer matches the one the plan was written for.

    A warning rather than a refusal: re-planning after a workbook changes is legitimate, and the
    decomposition may be exactly right for the new file. Approving in silence against a file that
    has moved on is the part that is not — and since nothing downstream reads ``workbook_sha256``
    back, this is the only place the tie is checked at all.
    """
    digest = _digest_of(workbook)
    if digest is None or digest == plan.workbook_sha256:
        return
    _console().print(
        f"[yellow]warning[/yellow] {_plain(workbook.name)} has changed since plan v{plan.version} "
        f"was written for it: the recorded sha256 no longer matches the file on disk. Approving "
        f"records a decision about a decomposition of the earlier file."
    )


def _digest_of(workbook: Path) -> str | None:
    """The workbook's SHA-256, or None when the file will not give one up.

    Goes through the analyser's own :func:`~kedge.analysis.workbook.read_identity` rather than
    hashing here, so the CLI and the artifacts it compares agree on what a workbook's digest is by
    construction. A file that cannot be read is not this function's problem to report: the
    analyser will have more to say about it, and a review verb must not fail over a hash.
    """
    from kedge.analysis.workbook import read_identity

    try:
        return read_identity(workbook).sha256
    except (OSError, ValueError) as exc:
        logger.debug("could not read the identity of %s: %s", workbook, exc)
        return None


def _saved_analysis(workspace: Workspace) -> WorkbookAnalysis | None:
    """The analysis already on disk, or None. Never analyses.

    `plan show` is better with one — an operation claimed by no stage is a review warning that
    needs the facts to spot — but showing a plan is a *review* action, so it must not silently
    spend the seconds an analysis costs, and a corrupt artifact must not stop a plan being read.
    """
    from kedge.analysis.model import WorkbookAnalysis

    path = workspace.analysis_path
    if not path.is_file():
        return None
    try:
        return WorkbookAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("ignoring the analysis at %s: %s", path, exc)
        return None


def _report_blockers(plan: ProcessPlan, workbook: Path) -> None:
    """Print every outstanding approval blocker, each with what would clear it.

    All of them, not the first: a user told only the first will fix it, retry, and be told the
    second — which is the reasoning :class:`~kedge.plan.review.PlanNotApprovableError` carries.
    """
    _console(stderr=True).print(
        f"[bold red]error[/bold red] plan v{plan.version} for {_plain(workbook.name)} "
        f"cannot be approved"
    )
    for blocker in plan.approval_blockers():
        typer.echo(f"  - {blocker}", err=True)
        typer.echo(f"      clear it: {_blocker_remedy(plan, blocker, workbook)}", err=True)


def _blocker_remedy(plan: ProcessPlan, blocker: str, workbook: Path) -> str:
    """The command or edit that would clear one approval blocker.

    Matched back to the drop it names rather than re-deriving the blocker list here, so the two
    cannot drift apart; anything unrecognised falls back to the general advice rather than
    guessing.
    """
    from kedge.plan.model import ApprovalState

    for drop in plan.dropped:
        if repr(drop.range) not in blocker:
            continue
        if drop.rejected:
            return (
                f"the drop was refused, so {drop.range} has to be kept: name it in a stage's "
                f"`sources` in the plan file, or acknowledge it again without --reject"
            )
        return (
            f'kedge plan acknowledge "{workbook}" --range "{drop.range}"'
            f"   (add --reject to keep the range instead)"
        )
    if plan.approval.state is ApprovalState.REJECTED:
        return f'kedge plan propose "{workbook}" — a rejected plan is terminal'
    return f'kedge plan show "{workbook}"'


def _print_remaining_blockers(plan: ProcessPlan, workbook: Path) -> None:
    """Say what still stands after a review edit, so the next step is never a guess."""
    blockers = plan.approval_blockers()
    console = _console()
    if not blockers:
        console.print(
            f'[dim]nothing blocks approval now; `kedge plan approve "{_plain(workbook)}"`[/dim]'
        )
        return
    console.print(f"[yellow]{len(blockers)} blocker(s) still stand[/yellow]")
    for blocker in blockers:
        typer.echo(f"  - {blocker}")


# ── convert ──────────────────────────────────────────────────────────────────────────────────
#
# The verb that finishes a conversion, and the only one besides `plan propose` that needs a model.
#
# Scaffolding an approved plan writes a notebook whose structure is settled and whose arithmetic
# is not: every stage kedge cannot translate comes out as a documented passthrough carrying
# `TODO(kedge)`. Such a notebook *runs*, which is deliberate -- the hand-in machinery works from
# the moment the plan is approved -- and is exactly why nothing on screen counts the holes. This
# is the command that fills them and, more importantly, the command that says how many it could
# not, because a conversion that is two thirds done and silent about it is the failure mode the
# whole verb exists to close.
#
# Headless by construction. It writes through `FileNotebookDriver`, so there is no marimo process,
# no kernel and no browser anywhere near it: a notebook file, an approved plan, and a model
# endpoint. Nothing here approves anything -- an unapproved plan is refused, and the exit code
# says whether every hole was filled.


@app.command()
def convert(
    workbook: Annotated[Path, typer.Argument(help="The workbook whose notebook to finish.")],
    analysis: Annotated[
        Path | None,
        typer.Option(
            "--analysis", help="Convert against this analysis.json instead of the saved one."
        ),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Name a model instead of the configured one.")
    ] = None,
    max_attempts: Annotated[
        int | None,
        typer.Option(
            "--max-attempts",
            help="Attempts per cell, including gate repairs. Defaults to kedge's own cap.",
        ),
    ] = None,
    no_sync: Annotated[
        bool,
        typer.Option(
            "--no-sync",
            help="Do not scaffold first; fill only the holes already in the notebook.",
        ),
    ] = False,
    keep_going: Annotated[
        bool,
        typer.Option("--keep-going", help="Carry on through a model-endpoint failure."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the conversion report as JSON.")
    ] = False,
) -> None:
    """Write the cell bodies the scaffold left unwritten, and report what could not be written.

    Runs the approved plan into the notebook first -- a plan approved since the last open lands as
    the cells it was missing -- then reads the notebook back and asks the model for one body per
    `TODO(kedge)` hole, in the scaffolder's order, gating every answer through kedge's validation
    gate. A stage somebody has already translated carries no marker, is not a hole, and is never
    asked about or overwritten.

    Six outcomes are reported, not two. A hole nobody asked about, one filled first time, one
    filled after the gate sent it back, one the gate refused every time, one answered with prose,
    and one the endpoint never answered are six different things to do next -- and the last is not
    the model's judgement, so it is never counted as one. A model-endpoint failure abandons the
    run by default rather than putting the same dead endpoint five more questions; `--keep-going`
    presses on.

    Exit codes: 0 when every hole was filled, or when there were none, and 1 when anything is
    still unwritten -- so a script can tell a finished conversion from one that needs a person.
    """
    if max_attempts is not None and max_attempts < 1:
        raise _fail(
            f"--max-attempts is {max_attempts}. It is the total number of times one cell may be "
            f"put to the model, the first ask included, so it has to be at least 1."
        )
    workspace = _plan_workspace(workbook)
    store = _plan_store(workspace)
    plan = _plan_in_force(store)
    if plan is None:
        raise _fail(_nothing_to_convert(store, workbook))

    facts = (
        _explicit_analysis(workspace, workbook, analysis)
        if analysis is not None
        else _saved_analysis(workspace)
    )
    # `--json` prints JSON and nothing else. A machine reading stdout has to be able to parse it,
    # so the two lines below are the terminal's rather than the command's.
    if facts is None and not as_json:
        _console().print(
            f"[yellow]warning[/yellow] no analysis saved for {_plain(workbook.name)}, so every "
            f'cell is written against the plan alone. Run `kedge inspect "{_plain(workbook)}"` '
            f"first if the arithmetic matters."
        )

    _ensure_notebook(workspace, announce=not as_json)

    # Imported here, as every other command imports the package it needs: the agent's validation
    # gate and the notebook bridge are several hundred milliseconds that `kedge --help` must not
    # pay for. The attempt cap is kedge's own rather than a literal three repeated here, which is
    # also why the flag defaults to None instead of to a number.
    from kedge.agent.validate import MAX_VALIDATION_ATTEMPTS
    from kedge.notebook.filedriver import FileNotebookDriver
    from kedge.notebook.fill import convert_notebook
    from kedge.plan.propose import completer_from_config

    try:
        completer = completer_from_config(workspace.config)
    except KedgeError as exc:
        raise _fail(str(exc)) from exc

    if not as_json:
        _console().print(
            f"[dim]plan v{plan.version} for {_plain(plan.workbook)}; filling into "
            f"{_plain(workspace.notebook_path.name)}[/dim]"
        )
    try:
        report = asyncio.run(
            convert_notebook(
                plan,
                FileNotebookDriver.for_workspace(workspace),
                completer=completer,
                analysis=facts,
                model=model or workspace.config.model.model,
                max_attempts=max_attempts if max_attempts is not None else MAX_VALIDATION_ATTEMPTS,
                stop_on_error=not keep_going,
                sync=not no_sync,
                workbook_path=workspace.workbook_path,
                handins_dir=workspace.handins_dir,
                contract_path=workspace.contract_path,
            )
        )
    except (KedgeError, OSError) as exc:
        raise _fail(f"the conversion could not run: {exc}") from exc

    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        _print_conversion(report, workspace.notebook_path)
    if not report.complete:
        raise typer.Exit(code=1)


def _nothing_to_convert(store: PlanStore, workbook: Path) -> str:
    """Why there is no approved plan, told apart from there being no plan at all.

    The two need different next steps and reading one as the other sends the user round a loop:
    "propose a plan" is useless advice to somebody who has one sitting unapproved, and "approve
    it" is useless advice to somebody who has none.
    """
    try:
        latest = store.latest()
    except KedgeError:
        latest = None
    if latest is None:
        return _no_plan_message(workbook)
    return (
        f"plan v{latest.version} for {workbook.name} is '{latest.approval.state.value}', not "
        f"'approved', and nothing is written to a notebook before a plan is approved. Read it "
        f'with `kedge plan show "{workbook}"`, then `kedge plan approve "{workbook}"`.'
    )


def _ensure_notebook(workspace: Workspace, *, announce: bool = True) -> None:
    """Make sure there is a notebook file to convert into, creating an empty one if not.

    The placeholder is :data:`kedge.notebook.codegen.EMPTY_NOTEBOOK` -- an app and not one cell.
    This command used to carry its own copy, written to dodge a collision the open sequence's
    placeholder caused: that one declared an unnamed cell doing ``import marimo as mo``, and
    ``kedge_setup`` imports ``mo`` too, so the sync's very first cell breached marimo's
    single-definition rule. Refused whole -- and it is the cell that imports ``pl``, ``kedge.xl``,
    ``kedge.sql`` and every path constant the rest of the notebook reads, so the conversion
    completed, reported nothing wrong, and left a notebook where every stage failed on a name
    that was never bound. That is fixed at the source now (``docs/marimo-api.md`` §4.4), and one
    constant is what keeps the two commands from drifting back apart.
    """
    from kedge.notebook.codegen import EMPTY_NOTEBOOK

    path = workspace.notebook_path
    if path.is_file():
        return
    workspace.ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EMPTY_NOTEBOOK, encoding="utf-8")
    if announce:
        _console().print(f"[dim]created an empty notebook at {_plain(path)}[/dim]")


def _print_conversion(report: FillReport, notebook: Path) -> None:
    """Draw a conversion: what was scaffolded, every hole and its attempts, what is still owed.

    A refused cell is drawn at least as loudly as a filled one. It used to print in ``[dim]``
    above a ``[green]`` success line, which is the wrong way round twice: the refusal is the more
    important fact, and the cell most often refused is ``kedge_setup`` -- the one that imports
    everything below it.
    """
    console = _console()
    if report.refused:
        console.print(
            f"[bold red]{len(report.refused)} cell(s) the notebook would not accept[/bold red] "
            f"{_plain(', '.join(report.refused))}"
        )
        console.print(
            "[yellow]the plan is not fully in the notebook, so it will not run as written.[/yellow]"
            " [dim]Usually a name the notebook already defines -- open it and look for a cell of "
            "your own importing or assigning the same thing.[/dim]"
        )
    elif report.scaffolded_summary:
        console.print(f"[dim]{_plain(report.scaffolded_summary)}[/dim]")
    typer.echo(report.render())
    console.print(f"\n[green]notebook[/green] {_plain(notebook)}")
    if report.holes == 0 and not report.refused:
        console.print(
            "[dim]the notebook had no unwritten cells; nothing was asked of a model[/dim]"
        )
        return
    if report.complete:
        console.print(
            f"[green]{report.filled} cell(s) written[/green] "
            f"[dim]({report.first_time} first time, {report.after_retries} after retries); "
            f"nothing is left unwritten[/dim]"
        )
        return
    if report.unfilled:
        console.print(f"[yellow]{len(report.unfilled)} cell(s) still to write[/yellow]")
        for cell in report.unfilled:
            detail = f" -- {cell.detail}" if cell.detail else ""
            typer.echo(f"  - {cell.name}: {cell.outcome.value}{detail}")
        console.print(
            "[dim]each of these keeps the scaffolder's passthrough, so the notebook still runs "
            "and still carries its TODO(kedge) marker. Run this again, or write them in the "
            "chat.[/dim]"
        )


# ── reconcile ────────────────────────────────────────────────────────────────────────────────


@app.command()
def reconcile(
    notebook: Annotated[Path, typer.Argument(help="The notebook whose outputs are checked.")],
    workbook: Annotated[
        Path, typer.Argument(help="The workbook holding the cached expected values.")
    ],
    absolute_tolerance: Annotated[
        float | None, typer.Option("--abs-tol", help="Override the configured absolute tolerance.")
    ] = None,
    relative_tolerance: Annotated[
        float | None, typer.Option("--rel-tol", help="Override the configured relative tolerance.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the reconciliation report as JSON.")
    ] = False,
) -> None:
    """Compare a notebook's outputs against the workbook's cached values (PLAN M4.5).

    Needs no model. Where the workbook holds no cached values the result is "not reconciled",
    never "passed".
    """
    for path in (notebook, workbook):
        if not path.is_file():
            raise _fail(f"no such file: {path}")

    workspace = _workspace_for(workbook)
    tolerances = workspace.config.reconciliation
    run = _resolve("kedge.reconcile", "reconcile_notebook", "M4.5 (reconciliation)")
    report = run(
        notebook,
        workbook,
        absolute_tolerance=absolute_tolerance
        if absolute_tolerance is not None
        else tolerances.absolute_tolerance,
        relative_tolerance=relative_tolerance
        if relative_tolerance is not None
        else tolerances.relative_tolerance,
        max_mismatch_rows=tolerances.max_mismatch_rows,
    )
    _cache_reconciliation(workspace, report)

    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        raise typer.Exit(code=_reconcile_exit_code(report))

    console = _console()
    console.print(report.headline())
    coverage = (
        f" ({report.cached_value_coverage:.0%} of {report.formula_cell_count or 0:,} formula cells)"
        if report.cached_value_coverage is not None
        else ""
    )
    console.print(
        f"[dim]tolerance abs={report.tolerance.absolute:g} rel={report.tolerance.relative:g}"
        f" · cached values {report.cached_value_status}{coverage}[/dim]"
    )
    for region in report.regions:
        console.print(f"  [{region.colour}]{region.status}[/{region.colour}] {region.headline()}")
        for diagnosis in region.diagnoses:
            # The whole point of the diagnostic: say what the delta looks like, so the user is
            # not left working out for themselves that it was a rounding mode (PLAN 4.5).
            console.print(f"      [dim]likely ({diagnosis.confidence}):[/dim] {diagnosis.headline}")
            if diagnosis.remedy:
                console.print(f"      [dim]remedy:[/dim] {diagnosis.remedy}")
    for note in report.notes:
        console.print(f"  [dim]note:[/dim] {note}")

    raise typer.Exit(code=_reconcile_exit_code(report))


def _cache_reconciliation(workspace: Workspace, report: Any) -> None:
    """Cache the verdict beside the notebook so the hub can show it.

    Only the verdict and when it was taken — the full report is the run's output, not a
    persistent artifact. Written only where the project directory already exists, so reconciling
    a workbook kedge has never opened leaves nothing behind.
    """
    from kedge.registry import reconciliation_path_for

    if not workspace.project_dir.is_dir():
        return
    payload = {
        "status": str(report.status),
        "generated_at": report.generated_at.isoformat(),
        "notebook": report.notebook,
        "headline": report.headline(),
    }
    try:
        reconciliation_path_for(workspace).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.debug("could not cache the reconciliation verdict: %s", exc)


def _reconcile_exit_code(report: Any) -> int:
    """Exit non-zero unless every region genuinely passed.

    `report.passed` is the list of passing regions, not a verdict -- a report with one passing
    and one failing region has a truthy `.passed`. `.status` is the folded verdict, and its
    `__bool__` is true only for PASSED, so a report that could not run exits non-zero rather
    than silently reporting success (PLAN 4.5).
    """
    return 0 if report.status else 1


# ── watch ────────────────────────────────────────────────────────────────────────────────────


@app.command()
def watch(
    workbook: Annotated[Path, typer.Argument(help="The workbook these hand-ins belong to.")],
    directory: Annotated[
        Path | None,
        typer.Option("--dir", help="The folder to watch. Defaults to ingest.watch_dir."),
    ] = None,
    glob: Annotated[
        str | None,
        typer.Option("--glob", help="Filename pattern. Defaults to ingest.watch_glob."),
    ] = None,
    contract: Annotated[
        str | None,
        typer.Option("--contract", help="Record the contract these hand-ins should satisfy."),
    ] = None,
    once: Annotated[
        bool, typer.Option("--once", help="Sweep the folder once and exit, for a scheduled run.")
    ] = False,
    settle: Annotated[
        float | None,
        typer.Option("--settle", help="Seconds a file must stop changing for before it is read."),
    ] = None,
) -> None:
    """Receive hand-ins from a watched folder into the workbook's managed store (PLAN 2.8).

    The production path. Drop and select are the interactive sources; a file landing in a shared
    folder on a schedule is what the process actually looks like, and because a watched hand-in
    arrives as a *path* it produces exactly the reproducible record a browser selection does.

    ``--once`` sweeps and exits, which is the shape a scheduled task wants and is idempotent: a
    file already in the store is skipped by hash rather than received twice. Without it the folder
    is watched until Ctrl-C, sweeping once at the start so files already sitting there are picked
    up too.

    ``--dir`` is relative to where you are standing. A relative ``ingest.watch_dir`` is relative
    to the workbook, so a sweep started by a scheduler finds the same folder you did.
    """
    if not workbook.is_file():
        raise _fail(f"no such workbook: {workbook}")

    console = _console()
    workspace = _workspace_for(workbook)
    ingest = workspace.config.ingest

    folder_path = _watched_folder(workspace, directory)

    scan_once = _resolve("kedge.ingest.watch", "scan_once", "M5 (hand-in intake)")
    start_watching = _resolve("kedge.ingest.watch", "watch", "M5 (hand-in intake)")
    default_settle = _resolve("kedge.ingest.watch", "DEFAULT_SETTLE_SECONDS", "M5 (hand-in intake)")

    pattern = glob if glob is not None else ingest.watch_glob
    settle_seconds = default_settle if settle is None else settle
    received: list[Any] = []

    def report(handin: Any) -> None:
        # Called on the watcher thread once per hand-in. The audit line is the whole point of
        # the managed store, so it is what the terminal shows: this run consumed this file.
        received.append(handin)
        console.print(f"  [green]hand-in[/green] {handin.audit_line()}")

    console.print(f"[dim]store[/dim] {workspace.handins_dir}")
    try:
        if once:
            for handin in scan_once(
                folder_path,
                store_dir=workspace.handins_dir,
                glob=pattern,
                contract=contract,
                copy_on_select=ingest.copy_on_select,
                dedupe=ingest.dedupe_by_hash,
                settle_seconds=settle_seconds,
            ):
                report(handin)
        else:
            # Started before it is announced, and not the other way round: starting sweeps the
            # folder, so a folder that is not there fails here. Printing first would tell the
            # user we were watching something that does not exist and then contradict itself.
            folder = start_watching(
                folder_path,
                report,
                store_dir=workspace.handins_dir,
                glob=pattern,
                contract=contract,
                copy_on_select=ingest.copy_on_select,
                dedupe=ingest.dedupe_by_hash,
                settle_seconds=settle_seconds,
            )
            console.print(
                f"[green]watching[/green] {folder_path} for {pattern}; press Ctrl-C to stop"
            )
            _watch_until_stopped(console, folder)
    except KedgeError as exc:
        raise _fail(str(exc)) from exc

    console.print(f"[bold]{len(received)}[/bold] hand-in(s) received")


def _watched_folder(workspace: Workspace, override: Path | None) -> Path:
    """Decide which folder ``kedge watch`` sweeps, from ``--dir`` or ``ingest.watch_dir``.

    A flag is relative to where the user is standing; a setting is relative to the file that
    carries it. ``watch_dir = "inbox"`` in the kedge.toml beside the workbook means the inbox
    beside the workbook, whatever directory the scheduled task that runs the sweep happens to
    start in -- which is the whole point of a setting over a flag, and is what the message
    below promises when it sends the user to that file. It is the rule
    :attr:`Workspace.handins_dir` already applies to the sibling ``ingest.store_dir``; the base
    differs because a store is an artifact kedge generates, under the project directory, and an
    inbox is somebody else's folder, beside the workbook.
    """
    if override is not None:
        return override.expanduser()
    configured = workspace.config.ingest.watch_dir
    if configured is None:
        raise _fail(
            "no watched folder is configured. Set ingest.watch_dir in a kedge.toml beside the "
            "workbook, or pass --dir."
        )
    configured = configured.expanduser()  # belt and braces: IngestConfig expands it too
    if configured.is_absolute():
        return configured
    return workspace.workbook_path.parent / configured


def _watch_until_stopped(console: Console, folder: Any) -> None:
    """Park the main thread on a started watcher until Ctrl-C, then stop it cleanly.

    watchdog runs on its own thread, so without this the command would return the instant it
    started watching. Ctrl-C is the documented way out and is therefore not an error: the folder
    is stopped, the count is printed, and the exit code stays 0.
    """
    try:
        folder.wait()
    except KeyboardInterrupt:
        console.print("[dim]stopping[/dim]")
    finally:
        folder.stop()


# ── contract ─────────────────────────────────────────────────────────────────────────────────


@contract_app.command("infer")
def contract_infer(
    file: Annotated[
        Path, typer.Argument(help="A representative hand-in to infer a contract from.")
    ],
    out: Annotated[Path | None, typer.Option("--out", help="Write the contract YAML here.")] = None,
    sheet: Annotated[
        str | None,
        typer.Option("--sheet", help="Worksheet to describe. Defaults to the first one."),
    ] = None,
) -> None:
    """Draft a hand-in contract from a real file, for the user to tighten (PLAN M5).

    Drafted through :func:`kedge.contracts.infer.infer_with_notes` rather than :func:`infer`,
    because the notes are what make the YAML worth reading: each column carries the evidence
    the guess was made from, so tightening it is an edit rather than a rewrite.
    """
    if not file.is_file():
        raise _fail(f"no such file: {file}")
    infer_with_notes = _resolve("kedge.contracts.infer", "infer_with_notes", "M5 (contracts)")
    write_yaml = _resolve("kedge.contracts.infer", "write_yaml", "M5 (contracts)")
    destination = out or file.with_suffix(".contract.yaml")
    try:
        contract, notes = infer_with_notes(file, sheet=sheet)
        written = write_yaml(contract, destination, notes=notes)
    except KedgeError as exc:
        raise _fail(str(exc)) from exc
    _console().print(
        f"[green]contract[/green] {written} "
        f"[dim]({len(contract.columns)} column(s), drafted from {file.name})[/dim]"
    )


@contract_app.command("sketch")
def contract_sketch(
    workbook: Annotated[
        Path, typer.Argument(help="The workbook whose pasted sheet describes the hand-in.")
    ],
    sheet: Annotated[
        str | None,
        typer.Option("--sheet", help="The pasted sheet. Defaults to the only data sheet."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the contract YAML here. Defaults to the project's."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace a contract that is already there.")
    ] = False,
) -> None:
    """Sketch a hand-in contract from a workbook sheet, before the first hand-in exists (PLAN M5).

    ``infer`` needs a real export and this does not, which is the whole point: a process fed by
    somebody running a query and pasting the grid has no hand-in until the user makes one, and
    nothing tells them which columns the notebook expects until the check fails. The pasted sheet
    *is* last month's version of that file, so this describes it from the analyser's own column
    profiles.

    A separate verb rather than a flag on ``infer``. The two read the same argument name and mean
    entirely different evidence by it -- a hand-in that happens to be a spreadsheet is a perfectly
    ordinary ``infer`` argument -- and a flag that quietly changes what a file *is* would be the
    kind of surface that makes a wrong contract easy to produce by accident.

    The draft says in the file that it came from a workbook, and it never replaces a contract that
    is already there: that one may have been tightened by somebody who knows the process.
    """
    if not workbook.is_file():
        raise _fail(f"no such workbook: {workbook}")

    from kedge.contracts.sketch import sketch, write_sketch

    workspace = _workspace_for(workbook)
    analyse = _resolve("kedge.analysis.analyse", "analyse", "M1 (the analyser)")
    destination = out or workspace.contract_path
    try:
        drafted = sketch(analyse(workspace.workbook_path), sheet=sheet)
        written = write_sketch(drafted, destination, overwrite=force)
    except KedgeError as exc:
        raise _fail(str(exc)) from exc

    console = _console()
    console.print(
        f"[green]contract[/green] {written} "
        f"[dim]({len(drafted.contract.columns)} column(s) from the "
        f"'{_plain(drafted.sheet)}' sheet of {_plain(workbook.name)})[/dim]"
    )
    console.print(f"  [dim]sheet chosen because {_plain(drafted.reason)}[/dim]")
    for entry in drafted.omitted:
        console.print(f"  [yellow]omitted[/yellow] {_plain(entry)}")
    console.print(
        "  [yellow]this is a sketch of a paste, not a checked contract[/yellow]: read it, then "
        "replace it with `kedge contract infer` once one real export has arrived."
    )


# ── config ───────────────────────────────────────────────────────────────────────────────────


@app.command("config")
def show_config(
    workbook: Annotated[
        Path | None,
        typer.Option("--workbook", help="Resolve config as it would be for this workbook."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the resolved config as JSON.")
    ] = False,
) -> None:
    """Show the resolved configuration and where each value came from.

    Layered config is only debuggable if you can see which file won. The API key is never shown:
    it is not held in configuration at all, only referenced by keyring entry name.
    """
    loaded = _load(project_dir=workbook.resolve().parent if workbook else None)
    flat = _flatten_config(loaded.config)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "files": [str(path) for path in loaded.files],
                    "api_key": {
                        "service": KEYRING_SERVICE,
                        "entry": loaded.config.model.api_key_ref,
                        "status": api_key_status(loaded.config),
                    },
                    "values": {
                        key: {"value": value, "source": loaded.origin(key)}
                        for key, value in flat.items()
                    },
                },
                indent=2,
                default=str,
            )
        )
        return

    console = _console()
    if loaded.files:
        for path in loaded.files:
            console.print(f"[dim]config file[/dim] {path}")
    else:
        console.print("[dim]no config files found; all values are defaults[/dim]")

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("setting")
    table.add_column("value", overflow="fold")
    table.add_column("source")
    for key, value in flat.items():
        origin = loaded.origin(key)
        shown = "default" if origin == "default" else Path(origin).name
        style = "" if origin == "default" else "green"
        table.add_row(
            key, str(value), f"[{style}]{shown}[/{style}]" if style else f"[dim]{shown}[/dim]"
        )
    console.print(table)

    status = api_key_status(loaded.config)
    entry = loaded.config.model.api_key_ref
    if status == "present":
        console.print(
            f"\n[green]api key[/green] found in the OS keyring ({KEYRING_SERVICE}/{entry})"
        )
    elif status == "missing":
        console.print(
            f"\n[yellow]api key[/yellow] not set for {KEYRING_SERVICE}/{entry}. "
            f"Store it with:\n    {keyring_set_command(entry)}"
        )
    else:
        console.print(f"\n[red]api key[/red] the OS keyring is not reachable; cannot check {entry}")


# ── doctor ───────────────────────────────────────────────────────────────────────────────────

_Status = Literal["ok", "warn", "fail"]
_STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red"}


def _check(results: list[dict[str, str]], name: str, status: _Status, detail: str) -> None:
    results.append({"check": name, "status": status, "detail": detail})


@app.command()
def doctor(
    workbook: Annotated[
        Path | None,
        typer.Option("--workbook", help="Also check the workspace for this workbook."),
    ] = None,
    network: Annotated[
        bool, typer.Option("--network/--no-network", help="Probe the configured model endpoint.")
    ] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the checks as JSON.")] = False,
) -> None:
    """Check the local environment: venv, marimo pin, keyring, endpoint, stale markers."""
    results: list[dict[str, str]] = []

    in_venv = sys.prefix != sys.base_prefix
    _check(
        results,
        "python",
        "ok" if in_venv else "warn",
        f"{sys.version.split()[0]} at {sys.executable}"
        + ("" if in_venv else " — not running inside a virtual environment; use `uv run kedge`"),
    )

    marimo_version = installed_marimo_version()
    if marimo_version is None:
        _check(results, "marimo", "fail", "marimo is not installed; run `uv sync`")
    elif marimo_version != MARIMO_PIN:
        _check(
            results,
            "marimo",
            "fail",
            f"{marimo_version} is installed but kedge is pinned to {MARIMO_PIN}. The private "
            f"_code_mode API and the /sse session bootstrap are version-specific; run `uv sync`",
        )
    else:
        declared = _marimo_metadata_pin()
        if declared is not None and declared != MARIMO_PIN:
            _check(
                results,
                "marimo",
                "fail",
                f"{marimo_version} is installed, but kedge.lifecycle.MARIMO_PIN says {MARIMO_PIN} "
                f"while pyproject says {declared}; the two must agree",
            )
        else:
            _check(results, "marimo", "ok", f"{marimo_version} matches the pin")

    _check_bridge(results)

    try:
        loaded = _load()
    except KedgeError as exc:
        _check(results, "config", "fail", str(exc))
        loaded = None
    else:
        files = ", ".join(str(path) for path in loaded.files) or "none found; using defaults"
        _check(results, "config", "ok", files)

    directory = user_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        _check(results, "user directory", "fail", f"{directory} is not writable: {exc}")
    else:
        _check(results, "user directory", "ok", str(directory))

    if loaded is not None:
        _check_keyring(results, loaded.config)
        _check_trust(results, loaded.config)
        if network:
            _check_endpoint(results, loaded.config)
        else:
            _check(results, "model endpoint", "warn", "skipped (--no-network)")

    _check_markers(results)

    if workbook is not None:
        if workbook.is_file():
            workspace = _workspace_for(workbook)
            _check(
                results,
                "workspace",
                "ok",
                f"{workspace.project_dir} (session id {workspace.session_id})",
            )
        else:
            _check(results, "workspace", "fail", f"no such workbook: {workbook}")

    failed = any(result["status"] == "fail" for result in results)
    if as_json:
        typer.echo(json.dumps({"ok": not failed, "checks": results}, indent=2))
    else:
        console = _console()
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("")
        table.add_column("check")
        table.add_column("detail", overflow="fold")
        for result in results:
            style = _STATUS_STYLE[result["status"]]  # type: ignore[index]
            table.add_row(
                f"[{style}]{result['status']}[/{style}]", result["check"], result["detail"]
            )
        console.print(table)
    if failed:
        raise typer.Exit(code=1)


def _check_bridge(results: list[dict[str, str]]) -> None:
    """Introspect marimo's private ``_code_mode`` surface (PLAN 6.1 mitigation 5).

    The check above compares two version strings; this one goes and looks. It is the same
    preflight ``kedge open`` runs before it spawns anything, so a bridge that would fail halfway
    through a conversation is a red line here instead. The report names the installed version
    and every method or parameter that has moved, because "marimo changed" is not actionable and
    "``edit_cell()`` no longer accepts hide_code in 0.24.0" is.
    """
    from kedge.notebook import check_bridge

    report = check_bridge()
    if not report.ok:
        _check(results, "marimo bridge", "fail", report.message())
    elif not report.version_matches_pin:
        _check(
            results,
            "marimo bridge",
            "warn",
            f"marimo {report.version} still exposes everything the bridge drives, but the bridge "
            f"was verified against {report.pinned}; run `uv run pytest -m contract`",
        )
    else:
        _check(
            results,
            "marimo bridge",
            "ok",
            f"marimo {report.version} exposes the private _code_mode surface kedge drives",
        )


def _check_keyring(results: list[dict[str, str]], config: Config) -> None:
    entry = config.model.api_key_ref
    status = api_key_status(config)
    if status == "present":
        _check(results, "keyring", "ok", f"entry {KEYRING_SERVICE}/{entry} is set")
    elif status == "missing":
        _check(
            results,
            "keyring",
            "warn",
            f"no key stored for {KEYRING_SERVICE}/{entry}; set it with `{keyring_set_command(entry)}`",
        )
    else:
        _check(results, "keyring", "fail", "no working keyring backend; the API key cannot be read")


def _check_trust(results: list[dict[str, str]], config: Config) -> None:
    """Report which certificates kedge would verify the model endpoint against.

    Printed whether or not the endpoint is reachable, because it is half of the answer when it
    is not: "0 roots" and "certifi only" are different diagnoses with different fixes.
    """
    try:
        trust = tls.describe(config.model.ca_bundle)
    except ConfigError as exc:
        _check(results, "certificate trust", "fail", str(exc))
        return
    counted = "" if trust.ca_count is None else f", {trust.ca_count} root(s)"
    _check(results, "certificate trust", "ok", f"{trust.detail}{counted}")


def _check_endpoint(results: list[dict[str, str]], config: Config) -> None:
    base_url = config.model.base_url
    try:
        with tls.client(ca_bundle=config.model.ca_bundle, timeout=5.0) as http:
            response = http.get(f"{base_url}/models")
    except ConfigError as exc:
        _check(results, "model endpoint", "fail", str(exc))
        return
    except httpx.HTTPError as exc:
        certificate = tls.certificate_error(exc)
        if certificate is None:
            _check(results, "model endpoint", "fail", f"{base_url} is not reachable: {exc}")
        else:
            _check(results, "model endpoint", "fail", _explain_certificate(base_url, certificate))
        return
    # Any HTTP answer proves reachability; 401 just means we did not send the key.
    _check(results, "model endpoint", "ok", f"{base_url} answered HTTP {response.status_code}")


def _explain_certificate(base_url: str, error: ssl.SSLCertVerificationError) -> str:
    """Turn a certificate failure into the sentence the user actually needs.

    ``unable to get local issuer certificate`` is true and useless. On a corporate machine it
    almost always means a TLS-inspecting proxy re-signed the connection with a root that Python
    has not been told about, and the fastest way to make that obvious is to name the issuer --
    which is usually the proxy vendor, at which point nobody has to guess. Same reasoning as the
    reconciliation triage in PLAN 4.5: say what it looks like rather than making the user work
    it out from the raw error.
    """
    # SChannel's wording ends in a full stop and OpenSSL's does not, so trim rather than end up
    # with "..the trust provider.." on Windows only.
    reason = str(error.verify_message or error).strip().rstrip(".")
    return (
        f"{base_url} presented a certificate kedge could not verify: "
        f"{reason}. This is what a TLS-inspecting proxy looks like. "
        f"kedge already checks the operating system's trust store, so either the proxy's root "
        f"is not installed there, or it was installed for the browser only. To see who signed "
        f"it: {tls.inspect_command(base_url)}. Then ask for that authority as a PEM and set "
        f"`ca_bundle` under `[model]` in your kedge config. Turning verification off is not an "
        f"option kedge offers -- see SECURITY.md."
    )


def _check_markers(results: list[dict[str, str]]) -> None:
    stale: list[str] = []
    live: list[str] = []
    for path, marker in iter_markers():
        if health_check(marker.base_url):
            live.append(f"port {marker.port} ({Path(marker.workbook).name})")
        else:
            stale.append(f"{path.name} -> port {marker.port}")
    if stale:
        _check(
            results,
            "marker files",
            "warn",
            f"{len(stale)} stale marker(s) from crashed runs: {', '.join(stale)}. "
            f"The next `kedge open` on that workbook clears it.",
        )
    elif live:
        _check(
            results,
            "marker files",
            "ok",
            f"{len(live)} live kedge marimo server(s): {', '.join(live)}",
        )
    else:
        _check(results, "marker files", "ok", "no marimo servers recorded")


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _require_bridge() -> None:
    """Refuse to go any further if the installed marimo no longer matches the bridge.

    PLAN 6.1 mitigation 5 asks for the assertion at startup, and this is startup: a marimo whose
    private ``_code_mode`` surface has moved must produce one clear message naming the version
    *before* a server is spawned and a conversation begun, not a ``TypeError`` from inside a tool
    call twenty minutes later. The introspection is cached, so the driver's own call later in the
    open sequence costs nothing.

    Imported inside the function: ``kedge --help`` and ``kedge config`` have no business paying
    for the notebook package's imports.
    """
    from kedge.notebook import BridgeVersionError, verify_bridge

    try:
        verify_bridge()
    except BridgeVersionError as exc:
        raise _fail(str(exc)) from exc


def _load(project_dir: Path | None = None) -> LoadedConfig:
    try:
        return load_config(project_dir=project_dir)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


def _workspace_for(workbook: Path, *, marimo_port: int | None = None) -> Workspace:
    loaded: LoadedConfig | None = None
    if marimo_port is not None:
        base = _load(project_dir=workbook.resolve().parent)
        marimo = base.config.marimo.model_copy(update={"port": marimo_port})
        loaded = replace(
            base,
            config=base.config.model_copy(update={"marimo": marimo}),
            provenance={**base.provenance, "marimo.port": "--port"},
        )
    try:
        return Workspace.for_workbook(workbook, loaded_config=loaded)
    except KedgeError as exc:
        raise _fail(str(exc)) from exc


def _flatten_config(config: Config) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for section, values in config.model_dump(mode="json").items():
        if isinstance(values, dict):
            for key, value in values.items():
                flat[f"{section}.{key}"] = value
        else:
            flat[section] = values
    return flat


def _marimo_metadata_pin() -> str | None:
    """Return the marimo version kedge's own metadata pins it to, if declared."""
    try:
        requirements = metadata.requires("kedge") or []
    except metadata.PackageNotFoundError:
        return None
    for requirement in requirements:
        if requirement.startswith("marimo=="):
            return requirement.removeprefix("marimo==").split(";")[0].strip()
    return None


if __name__ == "__main__":  # pragma: no cover
    app()
