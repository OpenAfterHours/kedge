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

**Convert from the model's own plan** (``--plan-from``). The composed path, and the only one a hub
user actually takes: propose a plan with a model, approve it, scaffold *that*, fill its holes, and
grade the whole thing::

    uv run python evals/run.py adjustment_signoff --convert gpt-5.6-terra --plan-from gpt-5.6-terra

The live modes are the only things here that spend money or touch the network, they are never the
default, and each says what it is about to do before it does it.

**Why the tiers are split across the modes.** ``--model`` grades the structural tier only, and
``--convert`` is what earns the deterministic tier.

**And what the refusals are actually about.** One composition is confounded: a model's plan graded
alongside a notebook the model did not write. That prints a near-perfect total made mostly of
points a human earned, under a model's name -- the precise false confidence this apparatus exists
to remove, and almost invisible in a tidy table. It is refused, by name: ``--plan-from`` without
``--convert`` would do exactly that against the committed reference conversion. ``--model`` with
``--convert`` is refused too, for a duller reason -- a sweep and a conversion are two different
reports and neither is a section of the other.

The *reverse* composition carries no such confound, because both halves are the model's, and it is
:data:`--plan-from`. Its score is reported under a heading of its own and is **not** comparable
with a ``--convert`` figure: one measures cell bodies against a human's plan, the other measures a
whole conversion. Expect the composed number to be materially lower, and read the gap as the
subject rather than as noise.

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
    from harness.live import MeteredCall, ModelSpec
    from harness.sweep import Price

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    _refuse_confounded(parser, args)

    case = load_case(args.case)
    if args.model:
        return _sweep(args, case)
    if args.convert:
        return _convert(args, case)
    return _grade(args, case)


def _refuse_confounded(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse the combinations that would print one model's name over another author's marks.

    Narrowed from a blanket ban on "a model in the loop for the plan *and* a notebook being
    graded", which caught the composed path -- a model's plan, scaffolded, filled by the same
    model -- along with the thing it was aimed at. That path is the only one a hub user takes and
    it has no confound in it: every point on the board is the model's.

    What is left is named rather than approximated:

    - ``--plan-from`` without ``--convert`` *is* the confounded composition. The committed
      reference conversion would be graded against a model-written plan and the deterministic
      tier -- 45 of the rubric's points, all of them a human's -- would land under the model's
      name.
    - ``--model`` with ``--convert`` is two reports, not one. A sweep tabulates several models
      over the planning seam; a conversion grades one notebook. Neither is a section of the other.
    - ``--plan`` with ``--plan-from`` names two different plans for one conversion.
    - ``--plan`` with no ``--notebook`` is the *same* confound as the first, reached offline and
      by accident rather than by asking for it. ``--notebook`` defaults to the committed reference
      conversion, so ``--plan a-model-plan.yaml`` alone grades a model's plan alongside a human's
      cell bodies. Measured on the plan from a real hub run: **49/66, 74%** -- of which 45 points
      are the reference notebook's deterministic tier and 4 are the plan's. The same plan graded
      with the notebook it actually produced scores 6.
    """
    if args.project is not None and (args.notebook is not None or args.plan is not None):
        parser.error(
            "--project resolves the notebook and the plan out of one directory; naming either "
            "of them as well says two different things about which conversion is being graded."
        )
    if args.project is not None and (args.convert or args.model):
        parser.error(
            "--project grades a conversion that already exists on disk. --convert makes a new "
            "one and --model sweeps the planning seam; neither reads a project directory."
        )
    if args.plan is not None and args.notebook is None and not args.convert:
        parser.error(
            "--plan on its own grades the committed reference conversion against the plan you "
            "named, so the deterministic tier -- 45 of the rubric's points, all of them a "
            "human's -- would be printed over a plan somebody else wrote. Name the notebook that "
            "plan produced with --notebook, or point --project at the .kedge directory holding "
            "both. To grade the plan alone, use --model to sweep the planning seam."
        )
    if args.model and args.convert:
        parser.error(
            "--model sweeps the planning seam and --convert grades a notebook; pick one. For the "
            "composed path -- a model's plan, scaffolded, and its holes filled by the same model "
            "-- use --convert MODEL --plan-from MODEL."
        )
    if args.plan_from and not args.convert:
        parser.error(
            "--plan-from says who proposes the plan for a conversion, so it needs --convert. On "
            "its own it would grade the committed reference conversion against a model-written "
            "plan: a near-perfect total made mostly of points a human earned, printed under a "
            "model's name."
        )
    if args.plan_from and args.plan:
        parser.error(
            "--plan reads a plan off disk and --plan-from asks a model for one; pick one. The "
            "conversion is scaffolded from exactly one plan and the report has to say which."
        )
    # A flag a mode silently ignores is worse than one it refuses. `--convert m --plan-from m
    # --repeats 3` announced "2 model(s) x 3 repeat(s)" and then did one proposal and one pass,
    # which is a cost estimate a reader would act on and a number that was never true.
    if args.convert:
        for flag, value, instead in (
            ("--repeats", args.repeats != 1, "run the command again; a conversion is one pass"),
            (
                "--notebook",
                args.notebook is not None,
                "--out names where the conversion is written",
            ),
            ("--json", args.json, "the conversion report is text only; --json is the sweep's"),
        ):
            if value:
                parser.error(f"{flag} does nothing with --convert. {instead.capitalize()}.")


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
        help="a saved process plan, for the structural tier. Skipped without one. Needs "
        "--notebook beside it: on its own it would grade the reference conversion against "
        "somebody else's plan.",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="a .kedge project directory, as the hub leaves it. Resolves the notebook and the "
        "latest approved plan from it, so grading a real conversion is one flag rather than two "
        "long paths. Mutually exclusive with --notebook and --plan.",
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
        "--plan-from",
        metavar="MODEL",
        default=None,
        help="propose the plan for --convert with this model rather than reading the case's "
        "committed one. The composed path: model plan, scaffolded, filled by a model. Its score "
        "is reported as such and is not comparable with a plain --convert figure.",
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
    notebook, plan_path = (
        _from_project(args.project)
        if args.project is not None
        else (args.notebook or case.REFERENCE_NOTEBOOK, args.plan)
    )
    if not notebook.is_file():
        msg = f"no notebook at {notebook}"
        raise SystemExit(msg)

    report = grade(case, notebook=notebook, plan=load_plan(plan_path))
    print(as_json(report) if args.json else report.render())
    return report.exit_code()


def _from_project(project: Path) -> tuple[Path, Path | None]:
    """Resolve the notebook and the plan out of a ``.kedge`` directory.

    The hub leaves both, in known places, and until this existed grading a real conversion meant
    typing two long paths and getting the pairing right by hand -- which is exactly the step that
    stopped anybody doing it. The composed path had never been graded once.

    The **latest approved** plan, not the latest plan. An unapproved one was never scaffolded, so
    grading a notebook against it reports a shape the notebook was never asked to have.
    """
    if not project.is_dir():
        msg = f"no project directory at {project}"
        raise SystemExit(msg)

    notebooks = sorted(project.glob("*.py"))
    if not notebooks:
        msg = f"{project} holds no notebook (*.py at its top level)"
        raise SystemExit(msg)
    if len(notebooks) > 1:
        names = ", ".join(path.name for path in notebooks)
        msg = f"{project} holds more than one notebook ({names}); name one with --notebook"
        raise SystemExit(msg)

    approved = [
        path
        for path in sorted(project.glob("plans/plan-v*.yaml"))
        if getattr(load_plan(path).approval, "state", None) == "approved"
    ]
    if not approved:
        print(
            f"note: {project} holds no approved plan, so the structural tier is skipped in full "
            f"and the notebook is graded on its own.\n"
        )
    return notebooks[0], approved[-1] if approved else None


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
    """Phase 2: scaffold the plan, have the model fill every hole, then drive and grade it.

    Two shapes, and the difference is one flag. Without ``--plan-from`` the plan is the case's own
    approved one and the score is about cell bodies alone -- that is the baseline. With it, the
    plan is proposed first, by a model, and the score is about the whole conversion. The report
    says which it was, on a line of its own, because the two numbers are not comparable and the
    only thing between a reader and treating them as though they were is that line.
    """
    from harness.convert import convert_and_grade, no_plan_proposed
    from harness.live import ModelSpec, resolve_model

    spec = ModelSpec(
        model=args.convert,
        base_url=args.base_url,
        api_key_ref=args.api_key_ref,
        label=f"{args.convert} (cells)" if args.plan_from else None,
    )
    planner = (
        ModelSpec(
            model=args.plan_from,
            base_url=args.base_url,
            api_key_ref=args.api_key_ref,
            label=f"{args.plan_from} (plan)",
        )
        if args.plan_from
        else None
    )
    if args.dry_run:
        return _dry_run(args, [planner, spec] if planner is not None else [spec])

    resolved = resolve_model(spec, kedge_home=args.kedge_home)
    if not resolved.ready:
        print(f"{spec.name}: {resolved.failure.value} -- {resolved.detail}")
        return 1

    planner_cost = ""
    completer = None
    # Every exit below this point states what has already been billed, which is why the printing
    # sits in a `finally` rather than after the conversion. A plan proposal costs money before a
    # single cell is asked for, and a run that ended between the two -- refused, unreachable, or
    # proposing something kedge will not scaffold -- used to report neither the reason nor the
    # bill.
    try:
        if planner is None:
            source = args.plan or _reference_plan(case)
            plan = load_plan(source)
            if plan is None:
                msg = (
                    "no plan to convert: pass --plan, or put a plan.yaml beside the case's "
                    "reference conversion"
                )
                raise SystemExit(msg)
            # Named on this path too. The structural tier is graded against *the plan*, so a
            # plain --convert total is the model's cell bodies over a human's plan and a quarter
            # of its points are the human's. That is the same confound --plan-from without
            # --convert is refused for, only smaller, and a reader is owed the sentence either
            # way rather than a caveat that appears on one mode and not the other.
            plan_origin, plan_is_the_models = f"read from {source}", False
        else:
            _announce(f"one plan proposal with {planner.name}, before any cell is asked for")
            plan, plan_origin, planner_cost, failure = _propose(args, planner)
            plan_is_the_models = True
            if plan is None:
                report = no_plan_proposed(
                    case, plan_origin=plan_origin, detail=failure, plan_is_the_models=True
                )
                print(report.render())
                return report.exit_code()
            _keep_plan(args, plan)

        _announce(
            f"one conversion with {spec.name}: a completion per scaffolded hole, plus a retry "
            f"per validation failure"
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
            plan_origin=plan_origin,
            plan_is_the_models=plan_is_the_models,
        )
        print(report.render())
        return report.exit_code()
    finally:
        _report_cost(planner_cost, completer)


def _report_cost(planner_cost: str, completer: Any) -> None:
    """State what was billed, whatever the run did next.

    Called from a ``finally``, so a conversion that ends at the plan still says what the proposal
    cost. The bill arrives whether or not the run produced a report, and a mode that spends money
    owes the reader that number on every path out of it -- including the ones that end in a
    ``SystemExit``.
    """
    lines = []
    if planner_cost:
        lines.append(f"plan seam cost: {planner_cost}")
    if completer is not None:
        lines.append(f"cell seam cost: {completer.usage.describe()} over {completer.seconds:.1f}s")
    if lines:
        print("\n" + "\n".join(lines))


def plan_failure_detail(model: str, call: MeteredCall) -> str:
    """Why the composed path never got a plan, attributed rather than assumed.

    The distinction ``evals/README.md`` calls the point of the whole apparatus, applied one level
    up. A model that produced no plan may have refused the JSON schema, timed out, been
    rate-limited, had no key in the keyring, or written prose no repair round could turn into a
    plan -- and only the last two are its judgement.
    :attr:`~harness.live.Failure.about_the_model` already decides that, so this quotes it rather
    than re-deciding it, and the endpoint's own text travels with the verdict because a
    classification with no evidence is unactionable.

    Args:
        model: The model that was asked.
        call: What came back, already classified by :func:`~harness.sweep.propose_once`.

    Returns:
        One sentence for :attr:`~harness.convert.ConversionReport.detail`.
    """
    from harness.live import Failure

    if call.failure is Failure.TRIAGE_REFUSED:
        # Its own bucket, and neither of the other two. The model was never asked: kedge read the
        # workbook, decided it was not convertible, and declined to propose. Filing that under
        # "the integration, the account or the endpoint" sends a reader to a proxy that answered
        # nothing, and filing it under the model's judgement blames a model that never saw the
        # question. The finding is about the workbook, or about triage.
        blame = "kedge's own triage declined to plan this workbook; the model was never asked"
    elif call.failure.about_the_model:
        blame = "the model's own output never validated as a plan"
    else:
        blame = (
            "a fact about the integration, the account or the endpoint -- not the model's "
            "judgement, and not a conversion that graded badly"
        )
    return f"no plan from {model}: {call.failure.value} ({blame}). {call.detail}".rstrip()


def _propose(args: argparse.Namespace, planner: ModelSpec) -> tuple[Any, str, str, str]:
    """Ask a model for a plan and approve it, so the scaffolder will take it.

    Approval is a human act everywhere else and it is performed here, deliberately and in one
    place, because a scaffold is what is being measured and ``build_cells`` refuses an unapproved
    plan. Two consequences worth stating rather than discovering. Every outstanding ``dropped``
    range is acknowledged, which is what a reviewer clicking through the card would do -- and a
    plan that drops the wrong thing still loses ``does_not_drop_the_sql_column``, so nothing is
    laundered by the acknowledgement. And a plan with a blocker that acknowledgement does not
    clear is not talked into approval: it comes back as ``NO_PLAN`` with the blocker quoted,
    because a plan kedge would refuse to scaffold is a result about the model, not a run to fudge.

    Returns:
        The approved plan or ``None``; where it came from; what the request cost; and, when there
        is no plan, the attributed reason.
    """
    from harness.live import resolve_model
    from harness.sweep import Bench, propose_once

    origin = f"proposed by {planner.model}"
    resolved = resolve_model(planner, kedge_home=args.kedge_home)
    bench = Bench.load(args.case, temperature=args.temperature)
    plan, call = propose_once(resolved, bench)
    cost = f"{call.usage.describe()} over {call.seconds:.1f}s"

    if plan is None:
        return None, origin, cost, plan_failure_detail(planner.model, call)

    from kedge.plan.model import PlanError
    from kedge.plan.review import acknowledge_all_drops, approve

    try:
        approved = approve(
            acknowledge_all_drops(plan, note="Acknowledged by evals/run.py --plan-from."),
            by="evals/run.py --plan-from",
        )
    except (PlanError, ValueError) as exc:
        return (
            None,
            origin,
            cost,
            f"{planner.model} proposed a plan kedge will not scaffold, so no cell was ever "
            f"asked for: {exc}",
        )
    return approved, origin, cost, ""


def _keep_plan(args: argparse.Namespace, plan: Any) -> None:
    """Write the model's plan beside the notebook it was converted into.

    Same argument as keeping the notebook: a conversion nobody can read afterwards cannot be
    argued with, and on the composed path half of what is being argued about is the plan.
    """
    from kedge.plan.store import plan_to_yaml

    notebook = args.out or (EVAL_ROOT / args.case / f"converted-{_slug(args.convert)}.py")
    path = notebook.with_name(f"plan-from-{_slug(args.plan_from)}.yaml")
    path.write_text(plan_to_yaml(plan), encoding="utf-8")
    print(f"plan written to {path}\n")


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
