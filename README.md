# auditlm

A Claude Code plugin that audits an AI assistant's response against the document it
was supposed to draw on. It splits the document into numbered passages, then uses
[Jev](https://docs.typesafe.ai) to judge every response sentence × passage pair.

For every claim in the response, the report shows its status (supported, partly
supported, contradicted, conflicting or unsupported) with the evidence quoted from the
document. It checks quotations and page citations word for word and lists the passages
the response didn't use. There is no assistant commentary. See
[skills/audit/reference/relations.md](skills/audit/reference/relations.md) for how it works.

## Install

```
/plugin marketplace add /path/to/auditlm
/plugin install auditlm@auditlm
```

Store a TypeSafe API key once (or set `TYPESAFE_API_KEY`):

```
secret-tool store --label="TypeSafe API key" service jev key api
```

## Use

```
/auditlm:audit paper.pdf response.md
```

You can also paste the response into the chat. The scripts can be run on their own:

```
uv run skills/audit/scripts/passages.py paper.pdf paper-audit/
uv run skills/audit/scripts/audit.py paper-audit/ response.md
```

See [skills/audit/reference/relations.md](skills/audit/reference/relations.md) for what
each verdict means.

## Where it runs

It runs in Claude Code on your own machine (CLI or desktop). The scripts need `uv`,
outbound access to `api.typesafe.ai`, and a TypeSafe API key (from `TYPESAFE_API_KEY` or
the keyring). claude.ai chat, Cowork and Claude Code on the web block that domain by
default and have no keyring, so there `api.typesafe.ai` must be allowlisted and the key
supplied as an environment variable.

## Test

`tests/fixtures/` has two responses about *The Tragedy of the Cognitive Commons* (Lovett,
2026), each with hand labels:

- `commons-response.md`: a short synthetic answer with deliberate errors (a reversed
  number, a claim the author rejects, an invented funding source).
- `governance-response.txt`: a real assistant answer, including a made-up page number
  and a misquotation.

```
T="The Tragedy of the Cognitive Commons: How AI Could Disrupt the Regeneration of Professional Expertise (Lovett, 2026)"
uv run skills/audit/scripts/passages.py TragedyOfCognitiveCommons.pdf /tmp/commons-audit --title "$T"
uv run skills/audit/scripts/audit.py /tmp/commons-audit tests/fixtures/commons-response.md
python3 tests/check.py /tmp/commons-audit/audit.json tests/fixtures/commons-expected.json
```

For the second fixture, use another work directory with the same `passages.json`.
