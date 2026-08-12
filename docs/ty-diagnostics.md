# The `ty` diagnostics, one by one

`ty` is the project's type checker (`CONVENTIONS.md`: "Type checker is `ty` (Astral), not mypy"),
and in CI it is **advisory**: the `types` job in `.github/workflows/ci.yml` carries
`continue-on-error: true`. That comment used to say "six known diagnostics" and nothing tracked
what they were, so nothing could say when the line was safe to delete. This document is that list;
the comment now names the count and the files, and both move together.

**Clearing every entry below is the whole prerequisite for removing `continue-on-error` and
letting the job gate merges.** There is nothing else in the way.

## The measurement

```bash
uv run ty check src/
```

```
Found 6 diagnostics
```

Measured with **ty 0.0.63** on Windows 11 / Python 3.13, against a clean tree at commit
`6138b53` ("chore: release 0.0.3"). Six is what the CI comment claims and six is what it was —
five errors and one warning.

| # | Where | Rule | Real, or dialect noise? |
|---|---|---|---|
| 1 | `analysis/regions.py:632` | `invalid-argument-type` | Dialect — but the annotation behind it is genuinely too loose |
| 2 | `ingest/watch.py:305` | `unresolved-attribute` | Dialect |
| 3 | `ingest/watch.py:306` | `unresolved-attribute` | Dialect |
| 4 | `plan/__init__.py:158` | `unused-ignore-comment` (warning) | Real — the suppression is aimed at the wrong thing |
| 5 | `plan/__init__.py:167` | `call-non-callable` | **Real, and a runtime bug** |
| 6 | `server/hub.py:879` | `invalid-argument-type` | **Real** — and already documented in a comment at the call site |

**Three remain.** Three of the six were cleared during the review that produced this document:

- **#4 and #5** were one bug. `load_analysis` now imports `from kedge.analysis.analyse import
  analyse`, and `kedge.analysis.__init__` re-exports `analyse` so the trap cannot be re-set.
- **#6** was the real protocol mismatch at `hub.py`'s `scaffold_notebook` call.
  `scaffold.CellCreator.create_cell` declared `-> str` while both real drivers return a
  `MutationResult`, so the list coming back held results rather than ids and the `-> list[str]`
  annotation was a lie. The protocol's return is now `object` — the scaffolder does not read it
  — and `scaffold_notebook` returns the cell *names* it wrote, which it knows for certain.

The entries are kept below because they explain what was wrong and why it was invisible.
Re-measured at the end of that review, against the whole remediated tree:

```
Found 3 diagnostics
```

— #1, #2 and #3, all three of them mypy-dialect suppressions ty walks past. Line numbers have
moved with the surrounding edits (#2 and #3 are now `ingest/watch.py:343-344`), so re-measure
before citing any of this.

**All three are one-line fixes**, and clearing them is the whole prerequisite for deleting
`continue-on-error: true` from the `types` job. That was left for the author rather than done
here: turning an advisory job into a gating one is a policy decision about the project, not a
tidy-up, and #1's entry below argues that the honest fix there is to tighten the annotation
rather than to swap the suppression dialect.

## Why the mypy suppressions do not work

Three of the six — #1, #2 and #3 — sit under a `# type: ignore[...]` comment that ty walks
straight past. #4 is the opposite case, and worth noting alongside them: it carries a
`# ty: ignore[...]`, which ty honoured hard enough to report the directive as unused. #5 and #6
carry no suppression at all. That ty reads one dialect and not the other is not a bug in ty, and
the exact rule is worth knowing, because "add an ignore comment" is the wrong reflex here. Probed
against ty 0.0.63:

| Comment | Suppressed? |
|---|---|
| `x.stop()` | no, obviously |
| `x.stop()  # type: ignore` | **yes** — a bare mypy ignore is honoured |
| `x.stop()  # type: ignore[attr-defined]` | **no** |
| `x.stop()  # ty: ignore[unresolved-attribute]` | **yes** |

So a *coded* mypy suppression is honoured only when the code inside the brackets happens to name a
`ty` rule, and mypy's vocabulary (`arg-type`, `attr-defined`) names none of them. The comments in
`regions.py` and `watch.py` are inherited mypy dialect and suppress nothing at all.

Replacing them with `# ty: ignore[...]` would silence the checker, and in both cases below there is
a real annotation fix that is barely longer. Prefer the fix.

## The six

### 1. `src/kedge/analysis/regions.py:632` — `invalid-argument-type`

```
error[invalid-argument-type]: Argument is incorrect
   --> src\kedge\analysis\regions.py:632:17
632 |                 orientation=orientation,  # type: ignore[arg-type]
    |                 ^^^^^^^^^^^^^^^^^^^^^^^ Expected `Literal["column", "row", "block", "single"]`, found `str`
```

`_orientation(rows, cols, count) -> str` returns exactly one of four string literals, and
`LogicalOperation.orientation` is declared `Literal["column", "row", "block", "single"]`. ty is
right that `str` is wider than the field, and the mypy suppression was never doing anything.

**What it needs.** Narrow the helper's return annotation to the same `Literal`, and delete the
suppression. The literal is spelled out in `analysis/model.py:228`; declaring a shared alias there
and using it in both places would stop the two drifting. `analysis/model.py` is the analyser's
contract, so adding an alias to it is a cross-cutting change and should be announced as one.

### 2 and 3. `src/kedge/ingest/watch.py:305–306` — `unresolved-attribute`

```
error[unresolved-attribute]: Object of type `~None` has no attribute `stop`
305 |         observer.stop()  # type: ignore[attr-defined]
306 |         observer.join(timeout_seconds)  # type: ignore[attr-defined]
```

`self._observer` is declared `object | None`, deliberately, because the watchdog import is
function-local and the class it yields is platform-dependent — `WindowsApiObserver` here,
`InotifyObserver` on Linux. Narrowing past the `is None` guard leaves `object`, which has neither
method. Nothing is wrong at runtime; the annotation just says less than the code knows.

**What it needs.** watchdog ships `py.typed`, and every platform variant derives from one base:

```python
if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver
```

Then `self._observer: BaseObserver | None = None`, and both suppressions go. The import stays
behind `TYPE_CHECKING`, so the runtime cost of the function-local import is preserved.

### 4. `src/kedge/plan/__init__.py:158` — `unused-ignore-comment` (warning) — CLEARED

```
warning[unused-ignore-comment]: Unused `ty: ignore` directive
158 |         from kedge.analysis import analyse  # ty: ignore[unresolved-import]
```

This one is already in ty's dialect, and ty is telling us the import resolves fine. It does — just
not to what the suppression assumed. See the next entry; the two are one problem.

**What it needs.** Fixing #5 removes this line's reason to exist.

### 5. `src/kedge/plan/__init__.py:167` — `call-non-callable` — CLEARED

```
error[call-non-callable]: Object of type `<module 'kedge.analysis.analyse'>` is not callable
167 |     return analyse(Path(workbook))
```

**This is a real defect, not a typing quibble.** `kedge/analysis/__init__.py` re-exports the model
types but not the `analyse` function, so `from kedge.analysis import analyse` binds the
*submodule* `kedge.analysis.analyse`, and calling it raises
`TypeError: 'module' object is not callable`. Confirmed at the interpreter:

```console
$ uv run python -c "from kedge.analysis import analyse; print(type(analyse), analyse)"
<class 'module'> <module 'kedge.analysis.analyse' from '...\src\kedge\analysis\analyse.py'>
```

The path is reachable: it is the fallback in `load_analysis` taken when no analysis file exists
yet and the planner has to produce one. `kedge inspect` does not go through it — `cli.py` resolves
`kedge.analysis.analyse` and pulls the attribute off it — which is why the corpus never caught it.

**What it needed, and got.** `from kedge.analysis.analyse import analyse`, with the now-pointless
`ty: ignore` deleted alongside it. Re-exporting `analyse` from the package `__init__` would have
worked too and would make the shadowing impossible to hit again, but it widens the analyser's
public surface, which `analysis/__init__.py` documents deliberately. The missing test over the
no-saved-analysis branch — whose absence is how a `TypeError` on a live path survived to a
release — now exists: `kedge plan propose` on a workbook with no `analysis.json` runs it, in
`tests/unit/test_cli.py::test_propose_analyses_the_workbook_when_none_has_been_saved`.

### 6. `src/kedge/server/hub.py:879` — `invalid-argument-type`

```
error[invalid-argument-type]: Argument to function `scaffold_notebook` is incorrect
879 |         cells = await scaffold_notebook(plan, driver, handins_dir=workspace.handins_dir)
info: protocol member `create_cell` is incompatible
info:     incompatible return types: `CoroutineType[Any, Any, MutationResult]` is not assignable to `CoroutineType[Any, Any, str]`
```

Real, known, and already written up in a comment immediately above the call:
`scaffold.CellCreator.create_cell` is declared `-> str` while
`driver.NotebookDriver.create_cell` returns a `MutationResult`, so the list that comes back holds
results rather than cell ids. The hub only counts them, so its message is honest either way, but
the seam is genuinely mismatched.

**What it needs.** A decision in `kedge.notebook`, not in the server: either the `CellCreator`
protocol adopts `MutationResult`, or the driver grows a thin `-> str` method for scaffolding to
call. The protocol exists to keep the scaffolder testable without a live kernel and narrow enough
that it cannot delete or run anything, so whichever way it goes should preserve both.

## Order of attack

1. ~~**#5**, on its own, with a test. It is a live bug and has nothing to do with the rest.~~ Done,
   except for the test.
2. ~~**#4** falls out of #5.~~ Done.
3. **#2 and #3** — one annotation, no behaviour change, no cross-cutting risk.
4. **#1** — as above, but touching `analysis/model.py` if the alias is shared.
5. **#6** last, because it is a design decision about a seam rather than a fix.

Then delete `continue-on-error: true` from the `types` job, along with the comment above it that
points here, and correct the count that comment states on the way past.
