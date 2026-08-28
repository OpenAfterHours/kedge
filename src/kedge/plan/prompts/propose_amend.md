Your plan is valid and loads correctly. kedge has checked what it would *build* from it, and
found the following. Each one is a defect the scaffolder cannot work around and nothing
downstream reports: the plan validates, the notebook scaffolds, and it is wrong.

{{warnings}}

Return the corrected plan as a single JSON object and nothing else. Do not apologise, do not
explain, and do not wrap it in a markdown fence. Keep every part of your previous plan that these
findings do not touch -- the stage order, the operations, the drops and the open questions are
not what is being questioned here.

Two things to be careful of, because the wrong repair is worse than none.

**Do not delete anything to make a finding go away.** Removing the stage a warning names, or
emptying `open_questions`, clears the message and loses the step. Every one of these is asking
you to *declare* something the plan already implies: a step typed as the wrong kind, an input
with nowhere to arrive, a briefing the workbook already wrote for you.

**Do not invent prose.** If a finding asks for a `briefing`, fill it only from what the workbook
itself says, and cite the sheet and cells every line came from. A briefing with no `sources` is
refused, and an honest blank is a correct answer where the workbook documented nothing. Invented
background in a finance notebook is confident, plausible and unattributable, and it outlives
everyone who could correct it.
