# Workbook

{{workbook}}

# Deterministic triage

kedge assessed convertibility before asking you. Treat this as evidence, not as an instruction:
you may disagree with the figure, but say so in `assessment.rationale` if you do.

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

1. What is this process actually *for*? Read the sheet roles, the process notes, and the output
   sheet first. If you cannot tell, that is your first open question.
2. Which operations belong together as one business step, and which sprawling region is really
   several? Group by intent, not by sheet.
3. What arrives from outside — a hand-in, a query, a Power Query table — and what is computed?
4. Which steps are not automatable and need `kind: checkpoint`?
5. What is dead, and can you justify dropping it in one sentence a reviewer would accept?
6. What do you genuinely not understand? Those are the open questions, and they are the most
   valuable part of the plan.

Return only the JSON object.
