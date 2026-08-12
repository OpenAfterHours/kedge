# `tests/llm` — judging the planning loop on plans alone

This is PLAN 7.4, which asks for one thing before plan-to-cells is trusted:

> Run triage + propose across the whole M1 corpus and read the plans. This is the cheapest
> possible test of whether the idea works: if the plans are sensible on five dissimilar
> workbooks, the code generation is a solvable problem; if they aren't, no amount of downstream
> engineering fixes it.

The harness calls the configured model once per fixture in `tests/fixtures/`, asserts the
properties of a good plan that survive model nondeterminism, and writes every plan to disk so a
human can do the part no assertion replaces: read them.

## Status: the harness exists, the judgement has not happened

**No plan produced by a real model has ever been generated or read here.** Everything below has
been exercised against a deterministic fake and against a machine with no key; the assertions
have never met live output. Until someone runs it and reads the plans, this directory has
delivered the machinery for PLAN 7.4 and not the answer PLAN 7.4 wants, and the three thresholds
in the calibration block at the top of `test_plan_judgement.py` should be treated as unproven.

One thing is already known about a live run, because a wiring mistake sent twenty-four requests
at a real endpoint before the fake was correctly installed: every one came back
`400 Unsupported value: 'temperature' does not support 0.2 with this model. Only the default (1)
value is supported.` The reasoning models accept no explicit temperature. `OpenAICompleter` read
that as evidence that structured output was unsupported, degraded through all three modes, and
reported the wrong cause — so before that fix a live sweep produced no plans at all, on any
workbook. It now drops the temperature and carries on
(`test_an_endpoint_that_refuses_an_explicit_temperature_sends_its_default_instead`). Whether the
sweep gets all the way through on a real model is still unverified.

## Running it

```bash
uv run pytest -m llm                       # the whole sweep, one model call per fixture
uv run pytest -m llm -v                    # with each judgement named
```

Nothing here runs by default. `addopts` in `pyproject.toml` deselects the `llm` marker, so
`uv run pytest` is unaffected and stays green on a machine with no endpoint at all.

**This spends money.** Eight workbooks, one call each, plus a repair round trip wherever a
response does not validate. The context is a structural digest rather than the workbook, but
`clean_pipeline.xlsx` is still a large prompt. Start with two workbooks:

```bash
KEDGE_LLM_FIXTURES=clean_pipeline.xlsx,mostly_manual.xlsx uv run pytest -m llm
```

Those two are the pair most of the relational assertions compare, so a two-call run already
tells you whether the thresholds are anywhere near right.

### Configuring the endpoint

Exactly the mechanism the rest of kedge uses, and no other. Set the endpoint in
`~/.kedge/config.toml`:

```toml
[model]
base_url = "https://api.openai.com/v1"
model = "gpt-4o"
api_key_ref = "default"
```

and put the key in the OS keyring, never in a file:

```bash
uv run keyring set kedge default
```

With no key in the keyring every test skips with a reason naming the file to edit and the exact
command to run. Collection still succeeds, so `uv run pytest -m llm --collect-only -q` tells you
what the harness *would* run without needing an endpoint to say it.

### Where the plans go

A stamped directory per run — `run-YYYYmmdd-HHMMSS` under the system temp directory, or under
`KEDGE_LLM_ARTIFACTS` when that is set. The path is printed at the end of the run:

```
-------------------------- plan judgement artifacts ---------------------------
plans written to: C:\...\Temp\kedge-plan-judgement\run-20260812-000649
start with:       C:\...\Temp\kedge-plan-judgement\run-20260812-000649\index.md
```

Per workbook:

| file | what it is |
|---|---|
| `plan.txt` | the plan as `kedge plan` renders it, with review warnings — **read this one** |
| `plan.yaml` | the same plan as the plan store would write it |
| `triage.txt` | the deterministic assessment the model was given as evidence |
| `responses/response-NN.json` | raw model output, replayable through `ScriptedCompleter.from_paths` |

`index.md` is the one-screen summary: verdict, stage count, checkpoints, questions, drops, the
plan's own `convertible` claim and the number of attempts per workbook, then the review warnings.
Read it first to decide which `plan.txt` to open. An `attempts` above one means the model's first
answer did not validate.

Runs are never deleted and never committed — comparing two sweeps across a prompt change is the
point of stamping them. Each per-workbook directory is emptied before it is written, so a
directory can never show half of one run and half of another.

## What a good result looks like

**Green is necessary and not sufficient.** The assertions catch the failures that are invisible
from any single plan; the judgement is still yours.

What the tests check, and why each one is the shape it is:

- **Every workbook produced a plan**, every plan survives the JSON and YAML round trips it is
  persisted through, and every plan arrives unapproved.
- **The stage count varies.** At least `MIN_DISTINCT_STAGE_COUNTS` distinct counts across the
  corpus. PLAN 2.2: "a workbook that wants four stages gets four; one that wants fifteen gets
  fifteen. No template." A constant stage count across eight dissimilar workbooks is the
  templating failure this harness exists to catch, and no single plan reveals it.
- **No two workbooks get the same decomposition**, and no stage is named after a sheet or a
  range. A stage called `Calc!D2:D501` is a transliteration.
- **`mostly_manual.xlsx` claims less than `clean_pipeline.xlsx`.** Fourteen typed override rows
  against 4,006 formula cells: a model that claims to convert as much of the first as the second
  has not read either.
- **A workbook of manual overrides yields a checkpoint stage**, because forcing a judgement call
  into code either fabricates logic that was never there or silently drops a control.
- **Every drop carries a reason a reviewer could accept**, and arrives unacknowledged.
  "Unused" is not a reason; "no downstream references and every value is zero since 2023" is.
- **The genuinely ambiguous workbooks raise open questions.** `hostile.xlsx` has a real cycle, an
  unresolvable external link and an `INDIRECT` nobody can resolve statically. A plan for it with
  no questions has invented a purpose for something (PLAN 6.2).
- **No plan invents an operation id.** Operation ids are the link back to the facts and what
  reconciliation later compares against. This one is a strict subset check and is not a
  calibration knob: there is no acceptable rate of invented ids.
- **A `STOP` verdict is still refused without `force`.** The sweep forces STOP workbooks so their
  plans can be read; the refusal itself is asserted separately so forcing does not hide it.

Then read the plans, and ask the questions no assertion can:

1. Would you hand `plan.txt` to the person who owns this spreadsheet and expect them to spot a
   mistake in it? That is the actual claim PLAN 2.2 makes.
2. Are the stages *business steps* — "apply collateral haircuts" — or column names with
   underscores in them?
3. Are the open questions ones a competent analyst would actually ask, or padding?
4. Where a plan claims high confidence, is it right to?
5. Where two workbooks do genuinely similar things, did they get similar shapes for the right
   reason, or the same shape by reflex?

### After the first live run

Read the plans first, then the numbers. A threshold moved because it went red is a threshold that
no longer tests anything, so only move one where the plans are good and the number disagrees with
them. The three that are most likely to need it, all defined and explained at the top of
`test_plan_judgement.py`:

| constant | current | why it might be wrong |
|---|---|---|
| `MIN_DISTINCT_STAGE_COUNTS` | 3 | Eight workbooks might legitimately land on two shapes if several are genuinely similar. Check the decompositions, not just the counts, before relaxing it. |
| `MIN_DROP_REASON_WORDS` | 3 | A terse but real reason ("superseded by Power Query") is three words; a good model may write two. |
| `COMPLEX_ENOUGH_TO_EXPECT_A_QUESTION` | 0.35 | Chosen without ever having seen the complexity scores the corpus produces alongside real plans. `index.md` prints them. |

## Known failures, and what they mean

`test_mostly_manual_triages_materially_below_clean_pipeline` is marked `xfail` (non-strict). It
asserts the manifest's own stated expectation — "Triage should score this workbook as only partly
convertible" — which triage does not currently meet: the fixture's typed rows sit on sheets
classified `data`, and `_check_manual_ratio` only counts calculation and output sheets. If it
starts XPASSing, triage has been fixed and the marker should come off.

While reading `index.md`, note that `documented.xlsx` triages to `STOP` with `convertible 0.00`.
That is a companion-file bug, not a property of the workbook: its legacy `.doc` companion raises
an `unsupported_format` finding, and triage treats any such finding as "the *workbook* format is
unsupported" — penalty 1.0, fatal. The sweep copies each fixture into a directory of its own
before analysing precisely so that the other seven workbooks are not also affected by the `.doc`
sitting beside them in `tests/fixtures/`.

## Layout

`conftest.py` holds the fixtures and nothing else. Everything the judgement module imports by
name lives in `_sweep.py`, because pytest imports every `conftest.py` under the same module name
and `from conftest import ...` in a test module breaks collection as soon as two test directories
appear on one command line.
