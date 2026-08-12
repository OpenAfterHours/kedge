"""The ``kedge`` command line.

``kedge open <workbook>`` is the shortest path when you already know which workbook you want, and
``kedge hub`` is the one for when you do not: it starts the same server with nothing open and puts
a browser on the landing page, where every workbook kedge has seen is listed with its status and a
new one can be added by browsing or dropping. Neither replaces the other and they share every line
of the sequence that follows.

The rest of the commands exist because the milestones underneath are independently useful.
``inspect`` is an Excel archaeology tool that needs no model at all, ``reconcile`` is a diff
between a notebook's output and the workbook's cached values, ``watch`` is the unattended
hand-in path with no browser anywhere near it, and ``config`` and ``doctor`` are the two things
anyone debugging a local tool with this many moving parts asks for first.

This is the only module in kedge permitted to print. Everything else logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import typer
from rich.console import Console
from rich.table import Table

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


def _console(*, stderr: bool = False) -> Console:
    return Console(stderr=stderr, highlight=False)


def _fail(message: str) -> typer.Exit:
    _console(stderr=True).print(f"[bold red]error[/bold red] {message}")
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
        typer.Option("--plan", help="Scaffold from a saved plan instead of proposing a new one."),
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
    """
    console = _console()
    if not workbook.is_file():
        raise _fail(f"no such workbook: {workbook}")
    if plan is not None and not plan.is_file():
        raise _fail(f"no such plan: {plan}")
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


def _print_open_event(console: Console) -> Callable[[Any], None]:
    """Draw the hub's open-progress frames onto the terminal.

    The hub streams these into a browser; on the command line the same frames become one line per
    step, so `kedge open` says what it is doing for the several seconds it takes rather than
    sitting silent (PLAN M3).
    """
    marks = {"running": "[dim]···[/dim]", "ok": "[green]ok [/green]", "skipped": "[dim]-- [/dim]"}

    def draw(event: Any) -> None:
        if getattr(event, "type", "") != "open_progress":
            if getattr(event, "type", "") == "error":
                console.print(f"[bold red]error[/bold red] {event.message}")
            return
        if event.state == "running":
            return  # The terminal has no spinner to update; report outcomes only.
        mark = marks.get(event.state, "[bold red]FAIL[/bold red]")
        detail = f" [dim]{event.detail}[/dim]" if event.detail else ""
        console.print(f"  {mark} {event.step}{detail}")

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
