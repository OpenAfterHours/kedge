"""Run an eval: grade a conversion against the rubric, print the report.

Three modes, in ascending order of what they prove and of what they cost.

**Grade a conversion that already exists.** No model in the loop::

    uv run python evals/run.py adjustment_signoff
    uv run python evals/run.py adjustment_signoff --notebook path/to/converted.py
    uv run python evals/run.py adjustment_signoff --plan .kedge/plans/v3.yaml

With no ``--notebook`` it grades the case's own reference conversion, which should score full
marks on every gradeable item. That is not a pointless run: it is the only thing that
demonstrates the graders work. A harness whose checks have never seen a passing notebook could
have every one of them inverted, mis-keyed or reading the wrong column, and the first real
conversion it scored would take the blame.

**Sweep models over the planning seam** (``--model``). One model call per repeat: propose a plan
for the workbook and grade it against the structural tier, recording tokens, seconds and -- when
it fails -- why. This is what answers "can this model do this job, and at what cost", and it is
cheap enough to run across a shortlist::

    uv run python evals/run.py adjustment_signoff --model gpt-5.6-terra --model some-other
    uv run python evals/run.py adjustment_signoff --model gpt-5.6-terra --repeats 3
    uv run python evals/run.py adjustment_signoff --model gpt-5.6-terra --dry-run

**Convert with a model and grade the notebook** (``--convert``). Scaffold an approved plan, have
the model fill every ``TODO(kedge)`` hole, render, drive and grade the result. Many calls, so it
costs materially more than a sweep::

    uv run python evals/run.py adjustment_signoff --convert gpt-5.6-terra
    uv run python evals/run.py adjustment_signoff --convert gpt-5.6-terra --plan plan.yaml

The two live modes are the only things here that spend money or touch the network, they are
never the default, and each says what it is about to do before it does it.

**Why the tiers are split across the modes.** ``--model`` grades the structural tier only, and
``--convert`` is what earns the deterministic tier. Handing the committed reference notebook to a
model-written plan would print a near-perfect total made mostly of points a human earned, under a
model's name -- the precise false confidence this whole apparatus exists to remove.

The report always prints its denominator. "20/20" over a rubric of sixteen items is not a pass,
and :meth:`~harness.model.EvalReport.headline` will not render it as though it were.

Exit code is 0 when nothing gradeable failed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

EVAL_ROOT = Path(__file__).resolve().parent
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from harness.grade import CASES, as_json, grade, load_case, load_plan  # noqa: E402

if TYPE_CHECKING:
    from harness.live import ModelSpec
    from harness.sweep import Price

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    if args.model and args.convert:
        parser.error("--model sweeps the planning seam and --convert grades a notebook; pick one")

    case = load_case(args.case)
    if args.model:
        return _sweep(args, case)
    if args.convert:
        return _convert(args, case)
    return _grade(args, case)


def _parser() -> argparse.ArgumentParser:
    """The argument surface, kept in one place so the offline path cannot drift from the live one.

    The offline invocation is exactly what it always was. Everything that spends money is in its
    own group, is named after what it does rather than after a flag, and is off by default.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("case", choices=CASES, help="which eval to run")
    parser.add_argument(
        "--notebook",
        type=Path,
        default=None,
        help="the converted notebook to grade. Defaults to the case's reference conversion.",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=None,
        help="a saved process plan, for the structural tier. Skipped without one.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON rather than text")
    parser.add_argument("-v", "--verbose", action="store_true", help="log at INFO")

    live = parser.add_argument_group(
        "live model runs",
        "These call a configured endpoint and spend money. Neither is the default, and both "
        "state what they are about to do first. API keys come from the OS keyring, never from a "
        "file or the environment.",
    )
    live.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="MODEL",
        help="sweep this model over the planning seam. Repeat for a comparison; write "
        "LABEL=MODEL to name a leg something other than its model id.",
    )
    live.add_argument(
        "--convert",
        metavar="MODEL",
        default=None,
        help="convert with this model and grade the notebook it produces.",
    )
    live.add_argument(
        "--repeats", type=int, default=1, help="runs per model. One sample is noise; default 1."
    )
    live.add_argument("--base-url", default=None, help="override the configured endpoint.")
    live.add_argument(
        "--api-key-ref", default=None, help="keyring entry holding the key, if not the configured."
    )
    live.add_argument(
        "--kedge-home",
        type=Path,
        default=None,
        help="read config from this directory rather than ~/.kedge.",
    )
    live.add_argument("--temperature", type=float, default=0.2, help="sampling temperature.")
    live.add_argument(
        "--prices",
        type=Path,
        default=None,
        help="JSON of per-million-token prices, yours to maintain. Without it, cost is reported "
        "in tokens and seconds only.",
    )
    live.add_argument(
        "--out",
        type=Path,
        default=None,
        help="where --convert writes the generated notebook. Default beside the case.",
    )
    live.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve every model and report what would be called, without calling anything.",
    )
    return parser


def _grade(args: argparse.Namespace, case: Any) -> int:
    """The offline path: grade a conversion that already exists."""
    notebook = args.notebook or case.REFERENCE_NOTEBOOK
    if not notebook.is_file():
        msg = f"no notebook at {notebook}"
        raise SystemExit(msg)

    report = grade(case, notebook=notebook, plan=load_plan(args.plan))
    print(as_json(report) if args.json else report.render())
    return report.exit_code()


def _sweep(args: argparse.Namespace, case: Any) -> int:
    """Phase 1: propose a plan with each model and grade the structural tier.

    Imported here rather than at module scope so that the offline path -- the one that runs on
    every commit -- does not depend on the live machinery loading at all.
    """
    from harness.sweep import as_json as sweep_json
    from harness.sweep import render, sweep

    specs = _specs(args)
    if args.dry_run:
        return _dry_run(args, specs)

    _announce(f"{len(specs)} model(s) x {args.repeats} repeat(s) = that many plan proposals")
    report = sweep(
        specs,
        case_name=args.case,
        repeats=args.repeats,
        kedge_home=args.kedge_home,
        temperature=args.temperature,
        prices=_prices(args.prices),
    )
    print(sweep_json(report) if args.json else render(report))
    return report.exit_code()


def _convert(args: argparse.Namespace, case: Any) -> int:
    """Phase 2: scaffold the plan, have the model fill every hole, then drive and grade it."""
    from harness.convert import convert_and_grade
    from harness.live import ModelSpec, resolve_model

    plan = load_plan(args.plan or _reference_plan(case))
    if plan is None:
        msg = (
            "no plan to convert: pass --plan, or put a plan.yaml beside the case's reference "
            "conversion"
        )
        raise SystemExit(msg)

    spec = ModelSpec(
        model=args.convert, base_url=args.base_url, api_key_ref=args.api_key_ref, label=None
    )
    if args.dry_run:
        return _dry_run(args, [spec])

    resolved = resolve_model(spec, kedge_home=args.kedge_home)
    if not resolved.ready:
        print(f"{spec.name}: {resolved.failure.value} -- {resolved.detail}")
        return 1

    _announce(
        f"one conversion with {spec.name}: a completion per scaffolded hole, plus a retry per "
        f"validation failure"
    )
    completer = resolved.metered()
    notebook = args.out or (EVAL_ROOT / args.case / f"converted-{_slug(args.convert)}.py")
    report = convert_and_grade(
        case,
        plan,
        completer=completer,
        notebook=notebook,
        model=args.convert,
        temperature=args.temperature,
    )
    print(report.render())
    if completer is not None:
        print(f"\ncost: {completer.usage.describe()} over {completer.seconds:.1f}s")
    return report.exit_code()


def _dry_run(args: argparse.Namespace, specs: list[ModelSpec]) -> int:
    """Resolve every model and say which legs would run, without spending anything.

    Worth its own mode rather than being discovered by a bill. A sweep across a shortlist is
    exactly where one model id turns out not to be enabled for the key, and finding that out
    after the other five have been paid for is a poor way to learn it.
    """
    from harness.live import resolve_model

    print(f"dry run: {len(specs)} model(s) x {args.repeats} repeat(s), nothing will be called\n")
    blocked = 0
    for spec in specs:
        resolved = resolve_model(spec, kedge_home=args.kedge_home)
        if resolved.ready:
            print(f"  {spec.name}: ready")
        else:
            blocked += 1
            print(f"  {spec.name}: {resolved.failure.value} -- {resolved.detail}")
    if blocked:
        print(f"\n{blocked} of {len(specs)} would not run; those legs report as skips.")
    return 0


def _reference_plan(case: Any) -> Path | None:
    """The case's own ``plan.yaml``, if it has one.

    Derived from where the reference conversion lives rather than declared on the case, so a new
    case gets the behaviour by putting the file in the obvious place. ``--convert`` needs *a*
    plan and the reference one is the right default: it isolates the code-generation seam, which
    is the whole point of grading a conversion separately from grading the plan that shaped it.
    """
    beside = Path(case.REFERENCE_NOTEBOOK).parent / "plan.yaml"
    return beside if beside.is_file() else None


def _specs(args: argparse.Namespace) -> list[ModelSpec]:
    """One :class:`~harness.live.ModelSpec` per ``--model``, accepting ``LABEL=MODEL``."""
    from harness.live import ModelSpec

    specs = []
    for entry in args.model:
        label, _, model = entry.partition("=")
        specs.append(
            ModelSpec(
                model=model or label,
                base_url=args.base_url,
                api_key_ref=args.api_key_ref,
                label=label if model else None,
            )
        )
    return specs


def _prices(path: Path | None) -> dict[str, Price] | None:
    """Read the caller's price table, or nothing.

    Never committed and never defaulted. A price table living in this repository would be wrong
    within a month and wrong invisibly, and somebody would read a cost column that had quietly
    become fiction.
    """
    if path is None:
        return None
    from harness.sweep import Price

    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        model: Price(
            prompt=float(entry["prompt"]),
            completion=float(entry["completion"]),
            currency=str(entry.get("currency", "USD")),
        )
        for model, entry in raw.items()
    }


def _announce(what: str) -> None:
    """Say what is about to be spent, before spending it."""
    print(f"live run: {what}. This calls the configured endpoint and costs money.\n")


def _slug(model: str) -> str:
    """A filename-safe form of a model id, which routinely carries slashes and colons."""
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in model)


if __name__ == "__main__":
    raise SystemExit(main())
