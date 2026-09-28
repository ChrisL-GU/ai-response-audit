# AI Response Audit: Project Resource Document

**Status:** proof of concept built (see §6) · **Owner:** Christopher Lopes · **Last updated:** 2026-09-28

AI Response Audit shows which parts of an AI assistant's answer came from a source document, and
which did not. This document describes what is being built, why, how it works, and how to tell
whether it works, in enough detail that someone else could build their own version. Items marked
**(proposed)** are starting values to confirm. Items marked **TBD** are open.

---

## 1. Executive summary

**Problem.** People increasingly give an AI assistant a document (a paper, a policy, a
story) and ask about it. They have no easy way to tell which parts of the answer came from
the document and which came from the model's training or its own reasoning. Unsourced
content can read exactly as confidently as sourced content.

**Solution.** A Claude Code plugin that takes the source document and the AI's answer and
produces a plain Markdown report. For each claim in the answer, the report says whether it
came from the document, where in the document (with the document's own words as evidence),
and how it was used (quoted, paraphrased, inferred, or partly). Everything else is
marked as not from the document. The judgments come from code and Jev (TypeSafe),
not from a generative LLM, so the auditor is independent of the kind of system it audits.

**Success criteria (confirmed 2026-09-28):**

All accuracy figures are measured against hand labels, not Jev's own probabilities.

1. **Precision of "from the document" ≥ 90%.** Of the claims the tool marks as from the
   document, at least 90% really are. Falsely claiming that the answer used a passage is
   the worse error.
2. **Recall of "from the document" ≥ 80%.** Of the claims that really came from the
   document, the tool finds at least 80%.
3. **Source accuracy ≥ 85%.** For claims correctly marked as from the document, a correct
   passage is among the ones reported.
4. **False "used" ≤ 5% on claims not in the document** (outside knowledge, the
   assistant's own reasoning).
5. **The report lands.** A reader who has not seen the tool can, from the report alone and
   within 2 minutes, answer three questions: roughly how much of the answer came from the
   document, which parts did not, and where a named claim came from. (Judged by the owner
   on each test report.)

---

## 2. User experience and functionality

### Personas

- **Anyone checking an answer.** A student, faculty member, facilitator or staff member
  gave an assistant a document and got an answer. Before relying on the answer or sharing
  it, they want to know what the document actually supports. No technical background is
  assumed beyond running a command in Claude Code.
- **A frequent user.** Someone who reaches for the tool whenever they check a response.
  It must be quick to run, with no setup per use.

### User flow

1. The user runs `/ai-response-audit:audit <document> <answer>` in Claude Code, or runs it with just
   the document and pastes the answer into the chat.
2. The plugin splits the document into passages and the answer into claims, judges each
   claim, and writes `report.md` next to the document.
3. Claude shows the report as it is. If the user asks about a claim, Claude answers from
   the report and the document, quoting both, without adding its own verdicts.

### User stories and acceptance criteria

**US1.** As a user, I want to see which parts of the answer came from the document, so I
know what the document actually supports.
- Every claim in the answer appears in the report, in the answer's original order and
  structure (sections, lists).
- Every claim has exactly one source label from §2.1, and filler is marked as skipped.
- The report opens with the share of the answer that came from the document, as a claim
  count and as a share of the answer's words.

**US2.** As a user, I want to see where in the document each part came from, so I can
check it myself.
- Each claim marked as from the document shows at least one locator: the section heading
  path plus the paragraph number (Word), or the page (PDF).
- Each locator comes with the passage sentence that is the evidence, copied from the
  document word for word. It is never paraphrased.

**US3.** As a user, I want to know how the answer used the document, so I can tell a quote
from an inference.
- Each claim from the document shows how it was used: *quoted*, *paraphrased*,
  *inferred* or *partly*.
- A claim that draws on a passage but alters a detail (a number, a name, who said what,
  the direction of an effect) is labelled *partly* and shows the passage it drew on.

**US4.** As a user, I want content that did not come from the document to be obvious, so
I don't mistake it for sourced content.
- Claims not from the document are labelled as such and listed together in one section.

**US5.** As a user, I want to see which parts of the document the answer ignored.
- The report lists the document's sections with how many claims drew on each, including
  the sections nothing drew on.

**US6.** As a frequent user, I want this to run quickly and cheaply without setup.
- A 2,000-word answer against a 15,000-word document completes in under 2 minutes and costs
  under $0.05 in API calls. Re-runs of unchanged input are served from a cache.

### 2.1 Source labels (the taxonomy)

| Label | Meaning | Counts as "from the document"? |
|---|---|---|
| **quoted** | Uses the document's exact words (8+ consecutive words, or a quotation found word for word). | Yes |
| **paraphrased** | Says what a passage says in other words, including condensing it into a summary. | Yes |
| **inferred** | A conclusion the document supports but doesn't state. | Yes |
| **partly** | Part of the claim comes from the document and part does not, including a claim that draws on a passage but alters a detail (a number, a name, who said what). | Partly |
| **not from the document** | No passage supports it. Includes outside knowledge, the assistant's own reasoning or opinion, and content from the user's question. | No |
| *(skipped)* | Not a claim: greetings, transitions, offers of help. | n/a |

When the evidence is borderline, the claim is labelled **not from the document**, and the
closest passage is shown as a *possible source*. This follows the precision-first
criterion.

### Non-goals (for now)

- **Fact-checking accuracy.** Misquote detection, page-citation checks and contradiction
  flags are out of scope. They existed in the prototype and may return as a later layer.
- **Finer labels.** Separate labels for *summarized*, *changed* (an altered detail) and
  *also widely known* were considered and deferred to keep the first version simple. Add
  them only if test results show they're needed.
- **Knowing what the model actually read.** The tool infers use from content. It can't
  observe the assistant's context or retrieval.
- **Retrieved-excerpt mode.** Auditing against the excerpts a RAG tool retrieved, rather
  than the whole document.
- **A web page or any output other than Markdown.** The Markdown report renders on GitHub,
  which is enough for now (decided 2026-09-28).
- **Other hosts:** claude.ai, Cowork, Claude Code on the web, or a hosted service.
- **Several source documents per audit.**
- **Word-level attribution inside a sentence.** Mixed sentences are labelled *partly*.
- **Handling sensitive documents** (FERPA, unpublished work). See §4.3.

---

## 3. AI system requirements

### 3.1 Principles

- **No generative model in the judgment.** Code handles everything exact: splitting,
  search, word overlap, thresholds and the report. Jev answers narrow, typed questions
  with probabilities. Claude only runs the scripts and relays the report.
- **Select, never generate, evidence.** Evidence shown to the user is selected from the
  document's own sentences, so it can't be misquoted.
- **Compare instead of rating in isolation.** Asking "does passage X support claim Y?" for
  every pair invites false positives: with 100 passages, a claim gets 100 chances to be
  wrongly matched. Instead, a candidate shortlist is compared in one Choice.
- **Give each judgment the state it needs, and no more.** Jev's accuracy drops when the
  state holds unrelated text. It also can't judge what it isn't shown, such as which
  section of the document a passage belongs to.

### 3.2 Tools and APIs

| Need | Tool |
|---|---|
| Typed judgments | TypeSafe System One API, model `jev-latest` (Jev 1.13 at the time of writing), Python SDK `typesafe-sdk>=0.7.1`, primitives `Choice` and `Noul` |
| PDF text | `pypdf>=5` |
| Word text and structure | `python-docx` (paragraph styles give headings and list levels) |
| Keyword search | BM25 implemented in about 40 lines of Python, with light suffix stemming |
| Runtime | Python ≥ 3.12, `uv run` with inline (PEP 723) dependencies |
| Host | Claude Code plugin: `.claude-plugin/plugin.json`, `skills/audit/SKILL.md`, `scripts/` |

Jev limits that shape the design: requests may use up to 64k tokens, with at most 32k for
the state plus the longest single question. The rate limit is 1,200 requests per minute
(may vary). Input costs $0.042 per million tokens, and output is free.

### 3.3 Evaluation strategy

**Test documents** (in `documents/`):

| Document | Type | Size | Structure |
|---|---|---|---|
| *The Tragedy of the Cognitive Commons* (Lovett, 2026) | PDF, academic paper | ~10,700 words, 54 pages | Sections without numbered paragraphs; page numbers |
| *Magnifica Humanitas* (abridged encyclical) | Word | ~13,200 words | Chapters and subheadings; numbered paragraphs (1., 3., …) |
| *Ava Knap Story* | Word, news feature | ~490 words | 5 section headings, 20 paragraphs |

**Test answers (proof of concept):** synthetic answers written with ground truth, 2 per
document. Each answer is written together with an `expected.json` that labels every claim
with its source label, its expected passage(s), and the reason. Each answer must include
at least one of each hard case:

- a close paraphrase with no shared phrases (expected: *paraphrased*);
- a summary that combines two or more passages (expected: *paraphrased*, listing each passage);
- an inference the document supports but doesn't state (expected: *inferred*);
- a changed detail, such as a wrong number, name or attribution (expected: *partly*);
- a mixed sentence, part document and part outside knowledge (expected: *partly*);
- a common-knowledge fact that the document also states. The expected label is whatever
  matches the document, e.g. *paraphrased*. Track these separately, because they can't
  show that the answer used the document;
- outside knowledge on the document's topic that the document does not state;
- the assistant's own opinion, phrased as if it were the document's;
- filler (a greeting, a transition, an offer to help).

**Discipline:**
- Labels are written before the tool runs on an answer.
- One answer per document is held out. Thresholds and question wording are tuned only on
  the others, and the held-out results are reported separately.
- A result counts as a pass only if it passes on the held-out answers.

**Metrics:** the criteria in §1, computed by `tests/check.py`. It also reports a confusion
table of expected versus actual labels, because a *paraphrased* reported as *inferred*
matters much less than an outside-knowledge claim reported as *paraphrased*.

**Known limitation:** synthetic answers written by the tool's builder are likely easier
than real ones. Before judging whether the tool is useful, add at least 2 real answers
from other assistants (ChatGPT, Copilot, Claude), labelled blind.

**Presentation:** for each test report, the owner answers the three questions in
criterion 5 and notes what was hard to find.

---

## 4. Technical specifications

### 4.1 Architecture

```
document ──► ingest ──► passages.json ─────────────────────────────┐
                                                                   ▼
answer ────► segment ──► claims.json ──► for each claim:
                                           1. filler?         (Jev Noul)
                                           2. shortlist       (code: BM25 top 20 + quote matches)
                                           3. choose          (Jev Choice over shortlist + "none")
                                              └─ nothing chosen → check all passages in groups of 20,
                                                 then a final Choice among the group winners
                                           4. relate          (Jev Choice per chosen passage, up to 3)
                                              + evidence       (Jev Choice among that passage's sentences)
                                           5. label           (code: overlap + thresholds → §2.1 label)
                                                               │
                                     audit.json ◄──────────────┘
                                         │
                                         ▼
                                     report.md
```

**Ingest (code).** Split the document into passages, each with a stable ID, a locator, its
section heading path, and its text.
- **Word:**
  - One passage per paragraph.
  - Very short paragraphs (under 25 words) are merged with the next.
  - Paragraphs over 180 words are split at sentence boundaries.
  - Heading styles (Heading1–3) form the section path.
  - If paragraphs start with the document's own numbers ("12."), those numbers are the
    locator (¶12). Otherwise it is the section path and paragraph position.
- **PDF:**
  - Passages of about 90–180 words, built from whole sentences.
  - A sentence cut by a page break is re-joined, keeping both page numbers.
  - The References section is dropped by default.
  - Section headings are detected where possible (TBD: pypdf text alone loses font
    sizes, so this may need PyMuPDF or a heuristic for short standalone lines).
- **Output:** `passages.json` = `{source, title, passages: [{id, locator, section, pages?, text}]}`.

**Segment (code).** Split the answer into claims: sentences, list items and table rows.
- Headings are context, not claims.
- Each list item keeps its lead-in (the parent item, or the line that introduced the
  list), so "monitoring" under "Ostrom's design principles:" is read as a claim.
- Sentences don't split after abbreviations (et al., e.g., p., pp.) or inside quotations.
- A numbered item is the parent of bullets that follow it at the same indentation.
- **Output:** `claims.json` = `[{id, text, context: {section, list_lead_in, previous_sentence}}]`.

**Judge (Jev plus code).** Steps 1–5 above. The question wording is in §4.4; the
thresholds are in §4.5.

**Report (code).** See §4.6.

**Plugin.** `SKILL.md` tells Claude to:
- run `ingest` then `audit`;
- show the printed report as it is;
- answer follow-up questions only by quoting the report and the document, never with its
  own assessment.

### 4.2 Integration points

- TypeSafe API over HTTPS (`api.typesafe.ai`). The key comes from `TYPESAFE_API_KEY`, or
  else the Linux keyring (`secret-tool lookup service jev key api`).
- Retries with backoff on 408, 429 and 5xx (up to 6 retries, 20 s maximum wait).
  Concurrency is capped at 12 requests.
- **Cache:** every request is keyed by a SHA-256 of its exact state and questions and
  stored in `~/.cache/ai-response-audit/`. Re-runs and threshold changes cost nothing.

### 4.3 Security and privacy

- The document text and the answer are sent to TypeSafe. This is acceptable for the proof
  of concept. Before wider use, review TypeSafe's data handling against FERPA and
  university policy (TBD).
- The local cache holds document text in plain files. Document how to clear it.
- The API key is never written to disk by the plugin, and never printed.
- Jev doesn't treat document text as potentially hostile. A document containing
  instructions could skew its judgments. The report is advisory.

### 4.4 Jev questions (as built)

The exact wording is in `skills/audit/scripts/audit.py`. [HOW-IT-WORKS.md](HOW-IT-WORKS.md)
shows complete requests as sent. Every question ends with this context note:

> `context` only resolves what `sentence` refers to; judge `sentence` itself. A list item
> continues its `context.list_lead_in`: under 'Ostrom's principles:', the item 'monitoring'
> claims that monitoring is one of them.

The document title is sent only with the filler question, which has no passage to go on.

**Filler (Noul), state `{document, sentence, context}`:**
> Does `sentence`, from an AI assistant's answer about `document`, make a claim about the
> document or its subject, rather than being a greeting, a transition, an offer of help, or
> a remark about the answer itself?

**Choose (Choice), state `{sentence, context}`.** The options are the candidate passage
IDs, each described by its section path and text, plus `none`:
> Which passage addresses the specific point `sentence` makes (its claim, names, numbers,
> examples, or attributions), whether the passage agrees with it or not?

`none`: "None of these passages discusses what the sentence is about; at most they share
its general topic. A passage that gives different facts about the same thing (other
numbers, names, or conclusions) does address it."

**Relate, state `{sentence, context, passage, passage_section}`.** Three questions in one
request:

- `relation` (Choice):
  > How does `passage` (from the section `passage_section`, when given) bear on `sentence`?

  | Option | Criterion |
  |---|---|
  | `restates` | The sentence says what the passage says, in the same or other words, or condenses it into a shorter or more general statement, without adding claims or changing details. |
  | `infers_from` | The passage doesn't state the sentence's point, but the sentence is a fair conclusion drawn from what it says. |
  | `partly` | Part of the sentence comes from the passage and another part does not: it adds a claim the passage doesn't make, or alters a detail such as a number, a name, who said what, or the direction of an effect. |
  | `unrelated` | The passage does not address the sentence's point. |

- `specific` (Noul):
  > Does `sentence` contain at least one specific fact, detail, example, or statement that
  > appears in `passage`, beyond sharing its topic or general terms?

- `evidence` (Choice). The options are the passage's own sentences:
  > Which sentence of `passage` does `sentence` rely on most directly?

### 4.5 Policy (code; all values proposed, tune on non-held-out answers only)

| Rule | Value |
|---|---|
| Shortlist size (BM25; also the group size for the full check) | 20 |
| A candidate goes to *relate* if its share of the Choice is at least | 0.10 (max 3 per claim) |
| Quoted: consecutive words shared with the passage | ≥ 8 |
| From the document: p(restates + infers_from) | ≥ 0.70 |
| Paraphrased vs inferred: whichever of p(restates), p(infers_from) is larger | — |
| Partly: p(partly) + p(restates) + p(infers_from) | ≥ 0.60, and `specific` ≥ 0.50 |
| Below these thresholds: labelled *not from the document*, closest passage shown as a possible source | — |
| Filler: Noul below | 0.20 (only clear filler goes unchecked) |

The label shown is the best-supported per-passage relation. Probabilities are grouped
before thresholds are applied, because Jev often splits probability between adjacent
options (restates versus infers_from), which makes a single option look uncertain when the
group is clear.

### 4.6 Report specification (`report.md`)

Plain, scannable, no assistant commentary. In order:

1. **Header:** the answer's file name, the document's title, and the date.
2. **Headline (one line):** "**N of M claims (P% of the answer's words) came from the
   document.** K did not." Then one line breaking the document claims down by how they
   were used: *x quoted · y paraphrased · z inferred · w partly*.
3. **The answer, annotated.** The answer in its original order and structure. Each claim
   is followed by a short tag:
   - `[¶12 · paraphrased]` or `[p. 17 · quoted]`;
   - `[¶4 · partly]`, which points to the passage it partly drew on;
   - `[not from the document]`.

   Skipped lines are shown in italics without a tag.
4. **Where each part came from.** One entry per passage used, in document order: its
   locator and section, the claims that used it and how, and the evidence sentence quoted
   word for word.
5. **Not from the document.** A numbered list of those claims, each with its possible
   source if one was close.
5a. **Lines not checked.** Every line judged to be filler, so the reader can see what the
   audit left out.
6. **Parts of the document not used.** A list of sections, each with the number of claims
   that drew on it.
7. **How to read this.** Three to five lines defining the labels, and one line on method.

The terminal output prints sections 1, 2, 5 and 6, plus the path to the full report.

---

## 5. Risks and roadmap

### 5.1 Phased rollout

- **MVP (proof of concept).**
  - The §2.1 taxonomy and Markdown report, for PDF and Word.
  - Synthetic test set for the 3 documents, with 1 held-out answer each.
  - Success criteria reported on the held-out set.
- **v1.1.**
  - 2+ real answers from other assistants, labelled blind.
  - Section-heading detection for PDFs (open; PDF reports list page ranges until then).
  - List lead-ins separated from their list by a blank line (currently not captured; the
    first item sees the lead-in only as its previous sentence, later items not at all).
  - A tuned precision/recall balance.
- **v2.0.**
  - Retrieved-excerpt mode, if the excerpts an assistant retrieved are available.
  - Optional accuracy layer (misquotes, page citations).
  - Other hosts, with a data-handling review first.
  - Several documents per audit.

### 5.2 Technical risks and lessons from the prototype

| Risk / lesson | What happened | Mitigation |
|---|---|---|
| False matches from rating every pair on its own | Rating 7,150 sentence × passage pairs one at a time produced 25 false contradictions, all from passages not about the claim. | Shortlist, then compare in one Choice (§3.1). |
| "none" absorbs disagreeing passages | Jev put 0.63 on "none" when a passage gave a different number for the same thing. | Verify any candidate with ≥ 0.10 share; reworded `none` (§4.4). |
| Sentences cut at page breaks | A fact split across two passages was misjudged. | Re-join sentences across pages at ingest. |
| Short list items judged out of context | "monitoring" was called filler. | Pass the list lead-in and tell Jev that items continue it. |
| Misplaced facts pass as supported | Real items filed under the wrong heading were marked supported: passages carried no section information. | Give Jev each passage's section path (§4.4, relate state). Validate this. |
| Keyword search misses paraphrases | Keyword search had the source in its top 20 for 93% of claims. Adding embeddings didn't help on a single-topic paper (93–95%). | The full check of every passage when nothing is chosen; embeddings are an optional hook if real answers show many misses. |
| Probabilities split across similar labels | Most claims showed as "low confidence". | Group the probabilities before applying thresholds (§4.5). |
| Overfitting to the test set | The prototype was tuned on the same answers it was scored on. | Held-out answers; labels written before runs (§3.3). |
| Common knowledge inflates "used" | Facts that are both widely known and in the document can't be attributed to either source from the text alone. | Precision-first thresholds; track these claims separately in testing; add an *also widely known* label if they distort results. |
| Cost and latency | Prototype: about 230 requests and under $0.01 for a 69-claim answer; the full-check fallback adds about 7 requests per claim. | Cache; cap concurrency; report the request count. |
| Model or API changes | Jev versions and SDKs change. | Pin the SDK version and record the model version in `audit.json`; re-run the test set on upgrade. |

### 5.3 Open questions (TBD)

- The exact success thresholds in §1 (proposed values need the owner's confirmation).
- PDF section-heading detection method.
- Whether *paraphrased* and *inferred* can be told apart reliably enough to show
  separately, or should merge into one label.

---

## 6. Proof of concept: results (2026-09-28)

**Test set:** 6 synthetic answers, 2 per document, with 95 hand-labelled claims (82 claims
plus 13 filler). Labels were written before any run. Thresholds and question wording were
tuned only on the 3 non-held-out answers. The held-out answers were run once, after tuning
ended.

| Metric | Target | Tuning set (41 claims) | Held out (41 claims) | All (82) |
|---|---|---|---|---|
| Precision of "from the document" | ≥ 90% | 100% | 100% | 100% |
| Recall of "from the document" | ≥ 80% | 82.4% | 100% | 90.8% |
| Source accuracy | ≥ 85% | 92.9% | 93.5% | 93.2% |
| False "used" on claims not in the document | ≤ 5% | 0% | 0% | 0% |
| Filler recognized | (not a target) | 5/5 | 3/8 | 8/13 |

**Cost:** the whole build, including every tuning run and one real 66-claim answer, used
1.31M input tokens ($0.055). A single answer costs $0.001–$0.017, at 36–228 requests.
Run time was not measured.

**What the results show:**
- There were no false "used" labels on any of the 17 claims not from the document, which
  included fabricated details, outside knowledge on the topic, and the assistant's
  opinions attributed to the document.
- All 6 missed claims were in the tuning set, and 4 of them were *partly* claims (a changed
  detail or a mixed sentence) that fell to "not from the document". Jev judges that a
  changed fact ("fell by 8%" where the paper says "grew") isn't the passage's fact. The
  report still names the changed passage as the closest passage. This is the
  precision-first trade-off working as designed.
- The widely known fact that is also in the encyclical ("Rerum Novarum is widely seen as
  the founding document…") was labelled not from the document. The other common-knowledge
  claims were labelled by how they match the document.
- The filler misses were short bold list labels ("The common good.") and a lead-in
  sentence. These are debatable as claims, so they weren't tuned for.
- **Caveat:** the answers were written by the tool's builder and are likely easier than
  real ones. The real answer in `tests/fixtures/real/` has no labels yet. Label it, and
  add answers from other assistants, before drawing conclusions (v1.1).

**How the build differs from §4:**
- **Specific-fact question.** Relate gained a Noul: does the claim contain a specific
  fact from the passage, beyond sharing its topic? *Partly* requires it (≥ 0.50). Without
  it, 4 of 7 claims not from the document were labelled *partly*, because they shared a
  topic or term like "legal frameworks". Their p(partly) (0.71–0.94) overlapped with real
  *partly* claims (0.52–0.96), so a threshold alone couldn't separate them.
- **Repeated text.** Passages that repeat an earlier passage word for word are dropped at
  ingest. The abridged encyclical repeats ¶102–111.
- **PDF headings.** Not detected. PDF passages have page numbers but no section, so the
  "not used" part of a PDF report lists page ranges instead (§5.3 still open).
- **Where each part came from** groups entries by locator and shortens long heading paths
  to "first › … › last". The report doesn't show claim IDs, because the reader never sees
  them in the annotated answer.

### Revision after review (2026-09-28)

Three changes came out of reviewing [HOW-IT-WORKS.md](HOW-IT-WORKS.md):

1. **Filler cutoff lowered from 0.50 to 0.20,** so only clear filler goes unchecked. The
   report now lists every unchecked line under "Lines not checked".
2. **Shorter instructions.** The context note was cut from 47 words to 29. The document
   title is now sent only with the filler question. Requests are 2–15% smaller (relate:
   1,177 → 1,005 input tokens).
3. The diagram and docs now describe claims as one per sentence, including list items.
4. **Filler question first.** Step 1 is now answered before any search, so a filler line
   costs 1 request instead of about 8. Labels are unchanged; the encyclical answer went from
   53 to 47 requests.

Re-run on all six test answers (about $0.02):

| Metric | Target | Before | After |
|---|---|---|---|
| Precision of "from the document" | ≥ 90% | 100% | 100% |
| Recall of "from the document" | ≥ 80% | 90.8% | 93.8% |
| Source accuracy | ≥ 85% | 93.2% | 95.1% |
| False "used" | ≤ 5% | 0% | 0% |
| Filler recognized | (not a target) | 8/13 | 6/13 |

- As expected, the lower cutoff checks more borderline lines. Most of them end up "not from
  the document", which is harmless but lengthens that list.
- The held-out answers had been run before this revision, so they're no longer strictly
  unseen. The changes came from the review, not from their results.
- **The real, unlabelled answer shifted more than the test answers.** It went from 58 to
  49 claims from the document, from 7 to 15 partly, and from 1 to 3 not from the document.
  Without hand labels it's unknown which run is closer to the truth. This is the strongest
  argument for labelling real answers (v1.1).

Total Jev spend for the whole project so far: about $0.10.

## Appendix A: Repository layout

```
.claude-plugin/plugin.json, marketplace.json
skills/audit/SKILL.md                    how Claude runs the tool and presents results
skills/audit/scripts/ingest.py           document → passages.json   (PDF, Word, text)
skills/audit/scripts/audit.py            answer → claims.json, audit.json, report.md
skills/audit/reference/labels.md         label definitions for follow-up questions
tests/fixtures/<doc>-<n>-answer.md       synthetic answers (n = 2 is held out)
tests/fixtures/<doc>-<n>-expected.json   per-claim ground truth, written before any run
tests/fixtures/real/                     real assistant answers (unlabelled)
tests/run.sh, tests/check.py             run the test set; metrics and confusion table
docs/examples/                           reports from the test runs
documents/                               test source documents
```
