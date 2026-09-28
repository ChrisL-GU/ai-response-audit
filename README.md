# AI Response Audit

A Claude Code plugin that shows which parts of an AI assistant's answer came from a source
document, and which did not.

Give it the document the assistant was given (PDF or Word) and the assistant's answer. It
labels every claim in the answer as **quoted**, **paraphrased**, **inferred**, **partly**, or
**not from the document**. Each label comes with the passage it came from, quoted word for
word. The judgments come from code and [Jev](https://docs.typesafe.ai) (TypeSafe), not from a
generative model. The output is a plain Markdown report, with no assistant commentary.

- What it is and how it works: [docs/PRD.md](docs/PRD.md)
- What the labels mean: [skills/audit/reference/labels.md](skills/audit/reference/labels.md)
- Example reports: [docs/examples/](docs/examples/)

## Install

```
/plugin marketplace add /path/to/ai-response-audit
/plugin install ai-response-audit@ai-response-audit
```

Store a TypeSafe API key once, or set `TYPESAFE_API_KEY`:

```
secret-tool store --label="TypeSafe API key" service jev key api
```

## Use

```
/ai-response-audit:audit paper.pdf answer.md
```

You can also paste the answer into the chat. The scripts can be run on their own:

```
uv run skills/audit/scripts/ingest.py paper.pdf paper-audit/
uv run skills/audit/scripts/audit.py paper-audit/ answer.md      # writes paper-audit/report.md
```

## Where it runs

It runs in Claude Code on your own machine (CLI or desktop). The scripts need `uv`,
outbound access to `api.typesafe.ai`, and a TypeSafe API key. Document text is sent to
TypeSafe, and cached locally in `~/.cache/ai-response-audit/`. Delete that folder to clear the cache.

## Test

`tests/fixtures/` has six synthetic answers, two for each document in `documents/`, each with
per-claim labels written before any run. Answer 2 for each document is held out. It also
has one real assistant answer, which has no labels.

```
tests/run.sh /tmp/ai-response-audit-runs all                    # ingest the documents, audit every answer
python3 tests/check.py /tmp/ai-response-audit-runs --set holdout # precision, recall, source, false "used"
```

Set `AIRESPONSEAUDIT_MAX_SPEND=<dollars>` to stop before the spend ledger (`~/.cache/ai-response-audit/spend.json`)
passes that amount.
