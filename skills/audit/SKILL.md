---
name: audit
description: Audit an AI assistant's response against the document it was supposed to draw on. Breaks the document into numbered passages and shows, for every claim in the response, which passages support, partly support, or contradict it, with the evidence quoted from the document; checks quotations and page citations word for word; and lists the parts of the document the response did not use. Uses Jev (TypeSafe). Use when the user wants to check an AI answer, summary, or chatbot reply for grounding, attribution, hallucination, or coverage of a source document.
argument-hint: "[document.pdf|.txt|.md] [response.md|.txt]"
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Audit an AI response against its source document

The scripts produce a plain report of how each claim in a response is supported by a
document. Your job is to run them and show the report. **Do not add your own assessment**:
no verdicts of your own, no overruling or softening Jev's labels, no summary of the
response's quality, no suggested fixes. The report quotes its evidence so the reader can
judge each claim themselves.

Arguments: `$ARGUMENTS`. The document comes first, then the response file. Strip a
leading `@`. Paths may contain spaces. If the response was pasted into the chat
instead, save it **verbatim** to `<work_dir>/response.md` (don't tidy or fix it).
Ask if either is missing.

## Run

Run the scripts exactly like this, with no environment prefix. The script gets the API
key from `secret-tool` itself. Quote paths that contain spaces. Use a work directory next
to the document named `<document-stem>-audit/`.

1. `uv run ${CLAUDE_SKILL_DIR}/scripts/passages.py <document> <work_dir> [--title "…"] [--keep-references]`
   splits the document into numbered passages with page numbers (`passages.json`). No
   model is involved. If the printed title is wrong, re-run with `--title`. By default it
   stops at a References section; use `--keep-references` if the response cites the
   bibliography. Re-run it if `passages.json` came from an older version of this skill.
2. `uv run ${CLAUDE_SKILL_DIR}/scripts/audit.py <work_dir> <response>` writes
   `report.md` (the full report), `audit.json` (the data), and `sentences.json` (how the
   response was split into claims). It prints the report's Summary, Needs attention,
   and Coverage sections. Results are cached, so re-runs are free. A 70-claim response
   against a 105-passage paper takes about 230 requests, well under a cent.

To audit a second response against the same document, give it its own work directory
(copy `passages.json` into it), so the reports don't overwrite each other.

## Show the results

- Show the printed output to the user as it is. It is Markdown. Then give the path to
  `report.md` for the full list of claims.
- If the user asks about a claim, answer from `report.md`, `audit.json`, and
  `passages.json`: quote the relevant lines verbatim, with passage IDs and pages. Explain
  what a label means using [reference/relations.md](reference/relations.md). Don't
  re-judge the claim yourself; if the user thinks a label is wrong, say which of Jev's
  probabilities produced it (in `audit.json`) and offer to adjust a threshold and re-run.
- If a script fails for lack of an API key, tell the user to run:
  `secret-tool store --label="TypeSafe API key" service jev key api`
