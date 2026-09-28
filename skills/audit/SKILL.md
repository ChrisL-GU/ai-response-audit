---
name: audit
description: Show which parts of an AI assistant's answer came from a source document (a PDF or Word file the assistant was given) and which did not. Splits the document into passages and labels every claim in the answer as quoted, paraphrased, inferred, partly, or not from the document, with the document's own words as evidence. Uses Jev (TypeSafe). Use when the user wants to know what an AI answer took from a document, what it added from elsewhere, or which parts of the document it used.
argument-hint: "[document.pdf|.docx] [answer.md|.txt]"
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Where did this answer come from?

The scripts produce a plain Markdown report of which parts of an AI answer came from a
source document. Your job is to run them and show the report. **Do not add your own
assessment**: no verdicts of your own, no overruling or softening a label, no summary of
the answer's quality. The report quotes its evidence so the reader can judge for themselves.

Arguments: `$ARGUMENTS`. The document comes first, then the answer file. Strip a leading
`@`. Paths may contain spaces. If the answer was pasted into the chat instead, save it
**verbatim** to `<work_dir>/answer.md` (don't tidy or fix it). Ask if either is missing.

## Run

Run the scripts exactly like this, with no environment prefix. The script gets the API key
from `secret-tool` itself. Quote paths that contain spaces. Use a work directory next to the
document named `<document-stem>-audit/`, with one subdirectory per answer.

1. `uv run ${CLAUDE_SKILL_DIR}/scripts/ingest.py <document> <work_dir> [--title "…"]` splits
   the document into passages.
   - **Word documents** keep their own structure: one passage per paragraph, the heading
     path as the section, and the document's paragraph numbers (¶12) where it has them.
   - **PDFs** become passages of about 90–180 words, with page numbers.
   - The document's title is sent to Jev with every judgment, so check the printed title
     and pass `--title` if it's wrong.
2. `uv run ${CLAUDE_SKILL_DIR}/scripts/audit.py <work_dir> <answer>` writes three files:
   - `report.md`, the full report;
   - `audit.json`, every label with the probabilities behind it;
   - `claims.json`, how the answer was split into claims.

   It prints the report's headline, the claims not from the document, and the parts of the
   document that weren't used. Results are cached, so re-runs of the same input are free.
   A 20-claim answer against a 13,000-word document takes about 70 requests, under one cent.

## Show the results

- Show the printed output to the user as it is (it is Markdown), then give the path to
  `report.md` for the annotated answer and the evidence for every claim.
- If the user asks about a claim, answer from `report.md`, `audit.json` and `passages.json`:
  quote the relevant lines verbatim, with their locators. Explain labels using
  [reference/labels.md](reference/labels.md). Don't re-judge the claim yourself. If the user
  thinks a label is wrong, show which of Jev's probabilities produced it (in `audit.json`),
  and offer to adjust a threshold and re-run.
- If a script fails for lack of an API key, tell the user to run:
  `secret-tool store --label="TypeSafe API key" service jev key api`
