# How the audit works, and what its labels mean

## Claims

The response is split into claims: sentences, list items, and table rows. Headings are
not claims. They are kept as context, along with each list item's lead-in (so
"monitoring" under "Ostrom's design principles:" is read as "monitoring is one of the
principles") and the previous sentence. Filler, such as greetings, transitions and offers
of help, is found by Jev and skipped.

## Four steps per claim

| Step | Who | What |
|---|---|---|
| 1. Shortlist | code | Keyword search (BM25, with crude stemming) over the passages, using the claim and its list lead-in, returns the top 20. Any passage containing a quotation from the claim is added. |
| 2. Choose | Jev, Choice | "Which passage addresses the specific point this claim makes, whether it agrees or not?" The options are the shortlisted passages plus "none of these". Every passage with at least 0.10 of the probability is verified, up to 3. If none reaches that, **every** passage is checked in groups of 20, and the group winners compete in a final Choice. |
| 3. Verify | Jev, Choice | For each chosen passage: `restates`, `infers_from`, `partly_supports`, `contradicts`, or `unrelated`. A second Choice picks the sentence of the passage that is the evidence shown in the report. |
| 4. Exact checks | code | Every quotation in the claim is searched for in the document word for word. Every page citation ("p. 12", "pp. 40–44") is compared with the page of the quoted text, or else of the source passage. |

Because Jev compares candidates in step 2 instead of rating every passage on its own, a
passage only gets verified when it is about the claim's specific point. A contradiction
can therefore only come from a passage about the same thing.

## Labels (thresholds in `audit.py`)

Per passage, where p is Jev's probability:

- **contradicts:** p(contradicts) ≥ 0.50.
- **supports:** p(restates) + p(infers_from) ≥ 0.50, or the claim shares 8+ consecutive
  words with the passage. Marked *quoted* (shared words), *paraphrased* (restates ≥
  infers_from) or *inferred*.
- **partly supports:** the above plus p(partly_supports) ≥ 0.50.
- **does not address:** everything else (not shown in the report).

The p shown is the probability of the label as shown. "Supports" combines restates and
infers_from.

Per claim:

| Status | Rule | Reading it |
|---|---|---|
| supported | some passage supports it, none contradicts it | The evidence line shows where. |
| partly supported | only partial support | Part of the claim isn't in the passage: a missing or misplaced detail, or two points merged. The parts may also be spread over the listed passages. |
| contradicted | a passage contradicts it, none supports it | A wrong number or name, a reversed claim, or something attributed to the author that they reject. |
| conflicting | one passage supports it, another contradicts it | Often a claim that simplifies the document. Read both passages. |
| unsupported | no passage addresses it, even after checking every passage | Not in the document. It may still be true, e.g. outside knowledge. |
| filler | Jev: not a claim about the document or its subject | Not audited. |

Extra flags:
- **low confidence:** the deciding label's p is below 0.60.
- **stated in several places:** Jev's choice was spread over many passages, because the
  document makes this point repeatedly.

Quotations are *verbatim*, *close wording* (the longest match covers ≥ 60% of the quote's
words) or *not found*. The report shows the nearest wording in the document.

## Limits

- The shortlist can miss the source of a loose paraphrase. The claim then goes to the
  check of every passage, so a miss costs extra requests, not a wrong label.
- Page numbers are the PDF's page numbers, which may differ from a journal's printed
  page numbers.
- Text extracted from a PDF can garble tables and figures. Table evidence is shown as an
  excerpt of the matching words.
- Labels show where content *can be found* in the document, not what the assistant
  actually read.
