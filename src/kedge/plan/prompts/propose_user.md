# Workbook

{{workbook}}

# Deterministic triage

kedge assessed convertibility before asking you, and this is the figure the plan carries: it is
scored deterministically from the analysis, and whatever you send in `assessment` is replaced by
it. Treat it as evidence about the workbook. If you disagree, raise it as an open question, where
a human reads it.

{{triage}}

# Structural analysis

Facts extracted deterministically from the workbook. Formula regions are R1C1-compressed: a
contiguous block sharing one relative formula is **one logical operation**, whether it spans
five rows or two hundred thousand. Plan against these operations, not against cells.

Column profiles describe shape and distribution. Raw cell values are not included here by
design; where you need a value, say so in an open question rather than assuming one.

```json
{{analysis}}
```

{{seed}}

# Your task

Produce the process plan for this workbook as a single JSON object matching the contract in the
system message. Before you answer, work through this silently:

1. What is this process actually *for*, and what does the workbook already say about that?
   `process_notes` carries every documentation block, cell comment and companion procedure the
   analyser found, each with the sheet and cells it came from. That is the `briefing`, and the
   citations go in `sources` — `Sign-off!A3:A4 (Purpose)`. If the workbook explains nothing,
   leave the briefing empty and make it your first open question. Do not fill the gap yourself.
2. Which operations belong together as one business step, and which sprawling region is really
   several? Group by intent, not by sheet.
3. What arrives from outside — a hand-in, a query, a Power Query table — and what is computed?
   Every input goes in that stage's `sources` with its `origin`: `manual` for anything a person
   types or pastes, `unknown` where you cannot tell. Both are worth saying out loud. A hand-in
   that is not the notebook's first — a re-extract taken *after* something was run — needs a
   `ref` naming it, because it does not exist when the notebook is opened.
4. **What does a person have to go and run somewhere else?** SQL sitting in a cell or in
   `xl/connections.xml`, and any column of `="UPDATE ... "&F17&"..."`, is a `kind: handoff`: the
   notebook hands it over and waits. Set `mutates: true` where running it changes data, so
   nothing downstream appears until somebody confirms it was run.
5. Which steps are not automatable and need `kind: checkpoint`?
6. What is dead, and can you justify dropping it in one sentence a reviewer would accept?
   Careful here: a region the analyser calls dead because nothing in the workbook reads it may
   have a person as its consumer. Generated SQL is the usual case, and dropping it deletes the
   step that changes the data.
7. What do you genuinely not understand? Those are the open questions, and they are the most
   valuable part of the plan.

Return only the JSON object.
