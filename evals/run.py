"""Run an eval: drive a converted notebook, grade it against the rubric, print the report.

    uv run python evals/run.py adjustment_signoff
    uv run python evals/run.py adjustment_signoff --notebook path/to/converted.py
    uv run python evals/run.py adjustment_signoff --plan .kedge/plans/v3.yaml

With no ``--notebook`` it grades the case's own reference conversion, which should score full
marks on every gradeable item. That is not a pointless run: it is the only thing that
demonstrates the graders work. A harness whose checks have never seen a passing notebook could
have every one of them inverted, mis-keyed or reading the wrong column, and the first real
conversion it scored would take the blame.

The structural tier needs a plan and skips without one, which is why the report always prints
its denominator. "20/20" over a rubric of sixteen items is not a pass, and
:meth:`~harness.model.EvalReport.headline` will not render it as though it were.

Exit code is 0 when nothing gradeable failed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from harness.grade import CASES, as_json, grade, load_case, load_plan  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    case = load_case(args.case)
    notebook = args.notebook or case.REFERENCE_NOTEBOOK
    if not notebook.is_file():
        msg = f"no notebook at {notebook}"
        raise SystemExit(msg)

    report = grade(case, notebook=notebook, plan=load_plan(args.plan))
    print(as_json(report) if args.json else report.render())
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
