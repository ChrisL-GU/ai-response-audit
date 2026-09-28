# What the labels mean, and how they are decided

The report answers one question for every claim in an AI answer: did it come from the
source document, and if so, from where and how?

## Labels

| Label | Meaning |
|---|---|
| **quoted** | Uses the document's exact words: 8 or more consecutive words in common with a passage, or a quotation found in the document word for word. |
| **paraphrased** | Says what a passage says in other words, including condensing it into a summary. |
| **inferred** | A conclusion the document supports but doesn't state. |
| **partly** | Part of the claim is a specific fact from a passage and part isn't: an added claim, or a changed detail. |
| **not from the document** | No passage says it. It may come from the assistant's training, its own reasoning, or the user's question. That doesn't make it wrong. |
| *not checked* (italics) | Not a claim: a greeting, transition, or offer of help. Listed under "Lines not checked" in the report. |

When a match is borderline, the claim is labelled **not from the document**, and the report
names the closest passage. Wrongly saying the answer used the document is treated as the
worse error.

## How a claim is labelled

1. **Filler?** Jev (Noul): is it a claim about the document or its subject? Only a line
   scoring below 0.20 is left unchecked, so borderline lines are still checked.
2. **Shortlist (code).** Keyword search over the passages and their section headings
   returns the top 20. Passages containing a quotation from the claim are always included.
3. **Choose.** Jev (Choice): which of these passages addresses the claim's specific point?
   "None of these" is an option. Every passage with at least 10% of the choice goes on,
   up to 3. If none reaches 10%, every passage is checked in groups of 20, and the group
   winners compete in a final Choice.
4. **Relate.** For each chosen passage, Jev answers three things:
   - a Choice: `restates`, `infers_from`, `partly` or `unrelated`;
   - a Noul: does the claim contain a specific fact from the passage, beyond sharing its
     topic?
   - a Choice: which sentence of the passage is the evidence?
5. **Label (code):**
   - quoted: 8+ shared words, or a verbatim quotation;
   - from the document: p(restates) + p(infers_from) ≥ 0.70. It's *paraphrased* or
     *inferred*, whichever of the two probabilities is larger;
   - partly: the above plus p(partly) ≥ 0.60, **and** the specific-fact Noul ≥ 0.50;
   - otherwise, not from the document. A passage with at least 0.30 is shown as the
     closest passage.

   The claim takes its best passage's label.

Thresholds are constants at the top of `scripts/audit.py`. `audit.json` records every
probability, so a label can always be traced back to the numbers behind it.

## Limits

- The text alone can't tell whether a widely known fact that the document also states
  came from the document or from training. Such claims are labelled by how they match
  the document.
- A claim that changes a detail (a wrong number, the wrong weekday) often lands in
  "not from the document", with the changed passage shown as the closest passage.
  Jev tends to judge that a changed fact is not the passage's fact.
- Jev reads each claim with only its section heading, list lead-in and previous sentence
  as context.
- Labels show where content *can be found* in the document, not what the assistant
  actually read.
