# /// script
# requires-python = ">=3.12"
# dependencies = ["typesafe-sdk>=0.7.1"]
# ///
"""Show which parts of an AI answer came from a source document, and which did not.

  uv run audit.py WORK_DIR ANSWER.md|.txt [--max-spend DOLLARS]

Needs WORK_DIR/passages.json (from ingest.py). For each claim (sentence or list item):

  1. Jev    is it a claim at all, or filler (greeting, transition, offer)?
  2. code   shortlist passages: keyword search (BM25) on the claim and its list lead-in, plus
            any passage containing a quotation from the claim;
  3. Jev    choose which candidates address the claim's point (one Choice, "none" allowed).
            Candidates with at least PICK_ABOVE of the choice go on; if none does, every
            passage is checked in groups, then the group winners compete;
  4. Jev    relate each chosen passage: restates / infers_from / partly / unrelated; whether
            the claim takes a specific fact from it (partly needs this); and the passage
            sentence that is the evidence;
  5. code   label the claim: quoted, paraphrased, inferred, partly, or not from the document.

Writes WORK_DIR/claims.json, WORK_DIR/audit.json, WORK_DIR/report.md, and prints the headline,
the claims not from the document, and the unused parts of the document.

Code owns search, word overlap, thresholds and the report; Jev answers narrow typed questions.
The API key comes from $TYPESAFE_API_KEY, else `secret-tool lookup service jev key api`.
Every live request's input tokens are added to a ledger in the cache directory; with
--max-spend (or $AUDITLM_MAX_SPEND) the run stops before the ledger passes that many dollars.
"""

import argparse
import asyncio
import bisect
import collections
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, RetryPolicy

# --- Policy (code, not model) -------------------------------------------------
SHORTLIST = 20  # candidates per claim from keyword search (also the group size for the full check)
PICK_ABOVE = 0.10  # a candidate with at least this share of Jev's choice is related
MAX_PICKS = 3  # at most this many passages are related per claim
FROM_ABOVE = 0.70  # p(restates) + p(infers_from) at or above this -> from the document
PARTLY_ABOVE = 0.60  # ... plus p(partly) at or above this -> partly from the document
SPECIFIC_ABOVE = 0.50  # partly also needs this p that the claim takes a specific fact from the passage
POSSIBLE_ABOVE = 0.30  # ... at or above this -> shown as a possible source of a claim not from the document
QUOTE_WORDS = 8  # a shared run of this many words or more counts as quoting
FILLER_BELOW = 0.50  # Noul below this -> filler, not audited
CONCURRENCY = 12  # rate limit is 1,200 requests/minute
PRICE_PER_MILLION = 0.042  # Jev input tokens, USD; output tokens are free
SECRET_ATTRS = ["service", "jev", "key", "api"]
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "auditlm"
LEDGER = CACHE_DIR / "spend.json"

FROM_DOCUMENT = ("quoted", "paraphrased", "inferred")

# --- Judgments -----------------------------------------------------------------
CONTEXT_NOTE = ("`context`, when present, gives the answer's section heading, list lead-in, and previous "
                "sentence only to resolve what `sentence` refers to; judge `sentence` itself. A list item "
                "continues its `context.list_lead_in`: the item 'monitoring' under the lead-in 'Ostrom's design "
                "principles:' claims that monitoring is one of Ostrom's design principles.")

FILLER = {
    "claim": Noul(
        instructions="Does `sentence`, from an AI assistant's answer about the document `document`, make a "
                     "claim about the document or its subject, rather than being a greeting, a transition, an "
                     "offer of further help, or a remark about the answer itself? " + CONTEXT_NOTE,
    ),
}


def passage_option(p: dict) -> str:
    return f"[{p['section']}] {p['text']}" if p.get("section") else p["text"]


def choose_question(candidates: list[dict]) -> dict:
    criteria = {p["id"]: passage_option(p) for p in candidates}
    criteria["none"] = ("None of these passages discusses what the sentence is about; at most they share its "
                        "general topic. A passage that gives different facts about the same thing (other "
                        "numbers, names, or conclusions) does address it.")
    return {"addresses": Choice(
        instructions="An AI assistant wrote `sentence` in an answer about the document `document`. Which "
                     "passage from the document addresses the specific point `sentence` makes (its claim, "
                     "names, numbers, examples, or attributions), whether the passage agrees with it or not? "
                     + CONTEXT_NOTE,
        criteria=criteria,
    )}


RELATE = Choice(
    instructions="An AI assistant wrote `sentence` in an answer about the document `document`. How does "
                 "`passage`, an excerpt from that document (from the section `passage_section`, when given), "
                 "bear on `sentence`? " + CONTEXT_NOTE,
    criteria={
        "restates": "The sentence says what the passage says, in the same or other words, or condenses it into "
                    "a shorter or more general statement, without adding claims or changing details.",
        "infers_from": "The passage doesn't state the sentence's point, but the sentence is a fair conclusion "
                       "drawn from what it says.",
        "partly": "Part of the sentence comes from the passage and another part does not: it adds a claim the "
                  "passage doesn't make, or alters a detail such as a number, a name, who said what, or the "
                  "direction of an effect.",
        "unrelated": "The passage does not address the sentence's point.",
    },
)


# "partly" alone is read literally: a claim that only shares the passage's topic or a general term
# ("legal frameworks") counts as partly from it. This narrower question separates the two.
SPECIFIC = Noul(
    instructions="An AI assistant wrote `sentence` in an answer about the document `document`. Does `sentence` "
                 "contain at least one specific fact, detail, example, or statement that appears in `passage`, "
                 "beyond sharing its topic or general terms? " + CONTEXT_NOTE,
)


def evidence_question(passage_sentences: list[str]) -> Choice:
    return Choice(
        instructions="An AI assistant wrote `sentence` in an answer about the document `document`. Which "
                     "sentence of `passage` does `sentence` rely on most directly? " + CONTEXT_NOTE,
        criteria={f"E{i + 1}": s for i, s in enumerate(passage_sentences)},
    )


# --- Plumbing --------------------------------------------------------------------
def api_key() -> str:
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return key
    try:
        key = subprocess.run(["secret-tool", "lookup", *SECRET_ATTRS], capture_output=True,
                             text=True, check=True).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        key = ""
    if not key:
        sys.exit("No API key: set TYPESAFE_API_KEY or store one with "
                 f"`secret-tool store --label='TypeSafe API key' {' '.join(SECRET_ATTRS)}`.")
    return key


def serialize(answer) -> dict:
    if answer.type == "choice":
        return {"choice": answer.choice, "confidence": answer.confidence, "probabilities": answer.probabilities}
    return {"noul": answer.noul}


def spent_tokens() -> int:
    try:
        return json.loads(LEDGER.read_text())["input_tokens"]
    except (FileNotFoundError, KeyError, ValueError):
        return 0


class BudgetExceeded(Exception):
    pass


class Asker:
    """One request per call; cached by exact request so re-runs cost nothing. Live requests are
    added to the spend ledger and refused once it reaches the limit."""

    def __init__(self, client: AsyncTypeSafeClient, max_spend: float | None):
        self.client, self.sem = client, asyncio.Semaphore(CONCURRENCY)
        self.limit_tokens = None if max_spend is None else int(max_spend / PRICE_PER_MILLION * 1e6)
        self.requests = self.cached = self.tokens = 0

    async def __call__(self, state: dict, questions: dict) -> dict:
        payload = json.dumps({"state": state, "questions": {k: q.model_dump() for k, q in questions.items()}},
                             sort_keys=True)
        path = CACHE_DIR / f"{hashlib.sha256(payload.encode()).hexdigest()[:20]}.json"
        self.requests += 1
        if path.exists():
            self.cached += 1
            return json.loads(path.read_text())
        async with self.sem:
            if self.limit_tokens is not None and spent_tokens() >= self.limit_tokens:
                raise BudgetExceeded
            response = await self.client.system_one(state=state, questions=questions)
            used = response.usage.input_tokens or 0
            self.tokens += used
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            LEDGER.write_text(json.dumps({"input_tokens": spent_tokens() + used}))
        out = {k: serialize(a) for k, a in response.answers.items()}
        path.write_text(json.dumps(out))
        return out


def load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Missing {path}.")
    return json.loads(path.read_text())


# --- Answer -> claims (code) ------------------------------------------------------
SENTENCE_RE = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”’)\]]))\s+(?=[\"“‘(\[]?[A-Z0-9])")
ABBREVIATION_RE = re.compile(r"(?:\b(?:et al|e\.g|i\.e|cf|vs|pp?|fig|figs|no|vol|ch|sec|eq|dr|mr|mrs|ms|prof|"
                             r"st|approx|ca|jr|sr)|(?<![A-Za-z])[A-Z])\.[\"”’)\]]?$", re.I)
LIST_ITEM_RE = re.compile(r"^(?:[-*+•]|\d+[.)])\s+")
TERMINAL_RE = re.compile(r"[.!?:;][\"”’)\]]?$")


def split_sentences(text: str) -> list[str]:
    """Split at sentence ends, but not after abbreviations such as 'et al.' or 'p.'"""
    out: list[str] = []
    for piece in SENTENCE_RE.split(text):
        if out and ABBREVIATION_RE.search(out[-1]):
            out[-1] += " " + piece
        else:
            out.append(piece)
    return [s.strip() for s in out if s.strip()]


def strip_inline(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links and images -> their text
    text = re.sub(r"(\*\*|__|\*|_|`)(?=\S)(.+?)(?<=\S)\1", r"\2", text)  # emphasis, inline code
    return re.sub(r"\s+", " ", text).strip()


def answer_units(text: str) -> list[dict]:
    """Headings, paragraphs, list items and table rows, in order, with list depth and marker.

    Headings (markdown '#', or a one-line block with no end punctuation) are context, not
    claims. A list item's lead-in is the chain of its parent items, or the paragraph line
    that introduced the list. A numbered item is the parent of bullets that follow it at
    the same indentation."""
    text = re.sub(r"```.*?```", "\n\n", text, flags=re.S)
    units, section = [], ""
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        first = lines[0].strip()
        if first.startswith("#"):
            section = strip_inline(first.lstrip("#"))
            units.append({"kind": "heading", "text": section})
            lines = lines[1:]
        elif (len(lines) == 1 and not LIST_ITEM_RE.match(first) and not TERMINAL_RE.search(first)
              and len(first.split()) <= 12):
            section = strip_inline(first)
            units.append({"kind": "heading", "text": section})
            continue
        stack: list[tuple[int, dict]] = []  # (indent key, unit) of open list items
        intro, para = "", None
        for line in lines:
            s = line.strip().lstrip(">").strip()
            if not s or re.fullmatch(r"[-*_=|:\s]{3,}", s):
                continue
            if s.startswith("#"):
                section = strip_inline(s.lstrip("#"))
                units.append({"kind": "heading", "text": section})
                continue
            if s.startswith("|"):  # table row -> "cell; cell"
                units.append({"kind": "row", "text": "; ".join(c.strip() for c in s.strip("|").split("|") if c.strip()),
                              "section": section, "lead_in": intro, "depth": 0})
                continue
            indent = 2 * (len(line) - len(line.lstrip())) + (0 if re.match(r"\d", s) else 1)
            if m := LIST_ITEM_RE.match(s):
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                marker = m.group().strip()
                unit = {"kind": "item", "text": LIST_ITEM_RE.sub("", s), "section": section,
                        "lead_in": " › ".join(([intro] if intro else []) + [u["text"] for _, u in stack]),
                        "depth": len(stack), "marker": marker if marker[0].isdigit() else "-"}
                units.append(unit)
                stack.append((indent, unit))
                para = None
            elif stack:  # continuation of the current list item
                stack[-1][1]["text"] += " " + s
            elif para is not None:
                para["text"] += " " + s
            else:
                para = {"kind": "para", "text": s, "section": section, "lead_in": ""}
                units.append(para)
                intro = s
    for u in units:
        u["text"] = strip_inline(u["text"])
        if "lead_in" in u:
            u["lead_in"] = strip_inline(u["lead_in"])
    return units


def split_answer(text: str) -> tuple[list[dict], list[dict]]:
    """(units with their claim IDs, claims with context)."""
    units, claims, previous = answer_units(text), [], ""
    for u in units:
        if u["kind"] == "heading":
            continue
        u["claims"] = []
        for s in split_sentences(u["text"]):
            context = {k: v for k, v in (("section", u["section"]), ("list_lead_in", u["lead_in"]),
                                          ("previous_sentence", previous)) if v}
            claim = {"id": f"S{len(claims) + 1:02d}", "text": s, "context": context}
            claims.append(claim)
            u["claims"].append(claim["id"])
            previous = s
    return units, claims


def split_response(text: str) -> list[dict]:
    """The claims alone (used by the tests)."""
    return split_answer(text)[1]


# --- Words and search (code) --------------------------------------------------------
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-‐‑'’][A-Za-z0-9]+)*")
STOPWORDS = set("a an and are as at be but by can for from has have in is it its not of on or that the their "
                "them they this those to was were which with would".split())
SUFFIXES = ("ational", "ations", "ation", "ally", "ings", "ing", "ies", "ied", "al", "ed", "es", "ly", "s")


def norm(word: str) -> str:
    return re.sub(r"[-‐‑'’]", "", word.lower())


def stem(word: str) -> str:
    """Crude suffix stripping so search matches 'accident' with 'accidental' (search only)."""
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    return [norm(m.group()) for m in WORD_RE.finditer(text)]


def longest_shared_run(a: list[str], b: list[str]) -> int:
    best, prev = 0, [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, start=1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


class Document:
    def __init__(self, passages: list[dict]):
        self.passages = passages
        self.by_id = {p["id"]: p for p in passages}
        self.order = {p["id"]: i for i, p in enumerate(passages)}
        self.words = {p["id"]: tokens(p["text"]) for p in passages}
        self.sentences = {p["id"]: split_sentences(p["text"]) for p in passages}
        # BM25 index over each passage's section heading and text
        self.docs = [[stem(w) for w in tokens(p.get("section", "")) + self.words[p["id"]] if w not in STOPWORDS]
                     for p in passages]
        self.avg = sum(map(len, self.docs)) / max(len(self.docs), 1)
        self.df = collections.Counter(w for d in self.docs for w in set(d))
        self.tf = [collections.Counter(d) for d in self.docs]
        # whole-document word stream, for quotations that cross a passage boundary
        self.stream, self.owner = [], []
        for i, p in enumerate(passages):
            self.stream += self.words[p["id"]]
            self.owner += [i] * len(self.words[p["id"]])
        self.joined = " " + " ".join(self.stream) + " "
        self.offsets = [0]
        for w in self.stream:
            self.offsets.append(self.offsets[-1] + len(w) + 1)

    def search(self, query: str, k: int, k1: float = 1.5, b: float = 0.75) -> list[str]:
        q = [stem(w) for w in tokens(query) if w not in STOPWORDS]
        n, scores = len(self.docs), []
        for i, d in enumerate(self.docs):
            s = 0.0
            for w in q:
                if f := self.tf[i].get(w):
                    idf = math.log(1 + (n - self.df[w] + 0.5) / (self.df[w] + 0.5))
                    s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(d) / self.avg))
            scores.append(s)
        order = sorted(range(n), key=lambda i: -scores[i])
        return [self.passages[i]["id"] for i in order[:k] if scores[i] > 0]

    def quote_passages(self, quote: str) -> list[str]:
        """Passages in which a quotation appears word for word (2+ words)."""
        q = tokens(quote)
        if len(q) < 2:
            return []
        needle, ids = " " + " ".join(q) + " ", []
        at = self.joined.find(needle)
        while at >= 0:
            start = bisect.bisect_right(self.offsets, at) - 1
            ids += [self.passages[self.owner[i]]["id"] for i in range(start, start + len(q))]
            at = self.joined.find(needle, at + 1)
        return list(dict.fromkeys(ids))


QUOTE_RE = re.compile(r"[“\"]([^”\"]{3,}?)[”\"]")


# --- Policy on Jev's answers ------------------------------------------------------------
def picks_from(probabilities: dict) -> list[tuple[str, float]]:
    ranked = sorted(((k, v) for k, v in probabilities.items() if k != "none"), key=lambda kv: -kv[1])
    return [(k, v) for k, v in ranked if v >= PICK_ABOVE][:MAX_PICKS]


def verdict_of(relation: dict, specific: float, shared: int, quoted_here: bool) -> dict:
    p = relation["probabilities"]
    p_from = p.get("restates", 0.0) + p.get("infers_from", 0.0)
    p_partly = p_from + p.get("partly", 0.0)
    if shared >= QUOTE_WORDS or quoted_here:
        verdict = "quoted"
    elif p_from >= FROM_ABOVE:
        verdict = "paraphrased" if p.get("restates", 0.0) >= p.get("infers_from", 0.0) else "inferred"
    elif p_partly >= PARTLY_ABOVE and specific >= SPECIFIC_ABOVE:
        verdict = "partly"
    elif p_partly >= POSSIBLE_ABOVE:
        verdict = "possible"
    else:
        verdict = "unrelated"
    return {"verdict": verdict, "p_from": round(p_from, 2), "p_partly": round(p_partly, 2), "specific": round(specific, 2),
            "shared_words": shared, "probabilities": {k: round(v, 2) for k, v in p.items()}}


RANK = {"quoted": 4, "paraphrased": 3, "inferred": 3, "partly": 2, "possible": 1, "unrelated": 0}


def label_of(links: list[dict], claim: float) -> str:
    if claim < FILLER_BELOW:
        return "skipped"
    best = max(links, key=lambda l: (RANK[l["verdict"]], l["p_from"]), default=None)
    if best is None or RANK[best["verdict"]] < RANK["partly"]:
        return "not from the document"
    return best["verdict"]


# --- Main ---------------------------------------------------------------------------
async def audit(work: Path, answer_path: Path, max_spend: float | None) -> None:
    meta = load(work / "passages.json")
    doc = Document(meta["passages"])
    title = meta["title"]
    if not answer_path.exists():
        sys.exit(f"Missing {answer_path}.")
    units, claims = split_answer(answer_path.read_text(encoding="utf-8", errors="replace"))
    if not claims:
        sys.exit(f"No claims found in {answer_path}.")
    (work / "claims.json").write_text(json.dumps({"answer": str(answer_path), "claims": claims},
                                                 indent=2, ensure_ascii=False))

    def state(c: dict, **extra) -> dict:
        return {"document": title, "sentence": c["text"], **({"context": c["context"]} if c["context"] else {}),
                **extra}

    retry = RetryPolicy(max_retries=6, backoff_max=20.0)
    async with AsyncTypeSafeClient(api_key=api_key(), retry=retry) as client:
        ask = Asker(client, max_spend)

        async def choose(c: dict, ids: list[str]) -> dict:
            a = await ask(state(c), choose_question([doc.by_id[i] for i in ids]))
            return a["addresses"]["probabilities"]

        async def attribute(c: dict, forced: list[str]) -> tuple[list[tuple[str, float]], str]:
            """Shortlist -> choose; if no candidate gets a real share, check every passage in groups."""
            query = f"{c['context'].get('list_lead_in', '')} {c['text']}"
            if re.match(r"(it|this|that|these|those|they|such)\b", c["text"], re.I):
                query += " " + c["context"].get("previous_sentence", "")
            shortlist = list(dict.fromkeys(forced + doc.search(query, SHORTLIST)))[:SHORTLIST + len(forced)]
            if shortlist and (picks := picks_from(await choose(c, shortlist))):
                return picks, "shortlist"
            ids = [p["id"] for p in doc.passages]
            groups = [ids[i:i + SHORTLIST] for i in range(0, len(ids), SHORTLIST)]
            winners = [k for probs in await asyncio.gather(*[choose(c, g) for g in groups]) for k, _ in picks_from(probs)]
            if len(winners) <= 1:
                return [(w, 1.0) for w in winners], "all passages"
            return picks_from(await choose(c, winners)), "all passages"

        async def relate(c: dict, pid: str, quoted_ids: set[str]) -> dict:
            p = doc.by_id[pid]
            questions = {"relation": RELATE, "specific": SPECIFIC}
            sentences = doc.sentences[pid]
            if len(sentences) > 1:
                questions["evidence"] = evidence_question(sentences)
            extra = {"passage": p["text"], **({"passage_section": p["section"]} if p.get("section") else {})}
            a = await ask(state(c, **extra), questions)
            evidence = sentences[int(a["evidence"]["choice"][1:]) - 1] if "evidence" in a else sentences[0]
            shared = longest_shared_run(tokens(c["text"]), doc.words[pid])
            return {"passage": pid, "locator": p["locator"], "section": p.get("section", ""),
                    "evidence": evidence, **verdict_of(a["relation"], a["specific"]["noul"], shared, pid in quoted_ids)}

        async def audit_claim(c: dict) -> dict:
            quoted_ids = {i for q in QUOTE_RE.findall(c["text"]) for i in doc.quote_passages(q)}
            filler, (picks, searched) = await asyncio.gather(ask(state(c), FILLER),
                                                             attribute(c, sorted(quoted_ids, key=doc.order.get)))
            is_claim = filler["claim"]["noul"]
            links = []
            if is_claim >= FILLER_BELOW:
                links = await asyncio.gather(*[relate(c, pid, quoted_ids) for pid, _ in picks])
                for link, (_, share) in zip(links, picks):
                    link["share"] = round(share, 2)
            links = sorted(links, key=lambda l: (-RANK[l["verdict"]], -l["p_from"]))
            return {**c, "label": label_of(links, is_claim), "claim_probability": round(is_claim, 2),
                    "searched": searched, "passages": links}

        try:
            rows = await asyncio.gather(*[audit_claim(c) for c in claims])
        except BudgetExceeded:
            sys.exit(f"Stopped: the spend ledger reached the limit of ${max_spend:.2f} "
                     f"({spent_tokens():,} input tokens). Nothing was written.")
        run = {"requests": ask.requests, "cached": ask.cached, "input_tokens": ask.tokens,
               "cost_usd": round(ask.tokens * PRICE_PER_MILLION / 1e6, 4),
               "ledger_usd": round(spent_tokens() * PRICE_PER_MILLION / 1e6, 4)}
        print(f"{run['requests']} Jev requests ({run['cached']} cached), {run['input_tokens']:,} new input tokens, "
              f"${run['cost_usd']:.4f} this run; ledger ${run['ledger_usd']:.4f}", file=sys.stderr)

    by_id = {r["id"]: r for r in rows}
    report = {"document": title, "source": meta.get("source"), "kind": meta.get("kind"), "answer": str(answer_path),
              "policy": {k: v for k, v in globals().items() if k.isupper() and isinstance(v, (int, float))},
              "run": run, "units": units, "claims": rows, "summary": summarize(rows)}
    (work / "audit.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    md = render(report, doc, by_id)
    (work / "report.md").write_text(md)
    print(terminal_view(md))
    print(f"\nFull report: {work / 'report.md'}")


def summarize(rows: list[dict]) -> dict:
    audited = [r for r in rows if r["label"] != "skipped"]
    words = lambda rs: sum(len(r["text"].split()) for r in rs)
    counts = collections.Counter(r["label"] for r in rows)
    from_doc = [r for r in audited if r["label"] in FROM_DOCUMENT]
    return {"claims": len(audited), "skipped": counts["skipped"], "from_document": len(from_doc),
            "partly": counts["partly"], "not_from_document": counts["not from the document"],
            "by_label": {k: counts[k] for k in FROM_DOCUMENT + ("partly",)},
            "word_share_from_document": round(words(from_doc) / max(words(audited), 1), 3)}


# --- Report (plain; no commentary) -------------------------------------------------------
def tag(r: dict) -> str:
    if r["label"] == "skipped":
        return ""
    if r["label"] == "not from the document":
        return "`[not from the document]`"
    best = r["passages"][0]
    also = [l["locator"] for l in r["passages"][1:] if l["verdict"] in FROM_DOCUMENT and l["locator"] != best["locator"]]
    where = ", ".join(dict.fromkeys([best["locator"]] + also[:1]))
    return f"`[{where} · {r['label']}]`"


def claim_md(r: dict) -> str:
    return f"*{r['text']}*" if r["label"] == "skipped" else f"{r['text']} {tag(r)}"


def short_section(section: str) -> str:
    """First and last heading of a long path: 'CHAPTER TWO › … › The principle of subsidiarity'."""
    parts = section.split(" › ")
    return section if len(parts) <= 2 else f"{parts[0]} › … › {parts[-1]}"


def short(text: str, n: int = 110) -> str:
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + " …"


def render(report: dict, doc: Document, by_id: dict) -> str:
    s, rows = report["summary"], report["claims"]
    by = s["by_label"]
    out = [f"# Where this answer came from: {Path(report['answer']).name}", "",
           f"Source document: *{report['document']}*", "",
           f"**{s['from_document']} of {s['claims']} claims ({round(s['word_share_from_document'] * 100)}% of the "
           f"answer's words) came from the document.** {s['partly']} came partly from it, and "
           f"{s['not_from_document']} did not.", "",
           f"From the document: {by['quoted']} quoted · {by['paraphrased']} paraphrased · {by['inferred']} inferred"
           f" · plus {by['partly']} partly", ""]

    out += ["## The answer, annotated", ""]
    for u in report["units"]:
        if u["kind"] != "item" and out[-1] != "":
            out.append("")  # end of a list
        if u["kind"] == "heading":
            out += [f"**{u['text']}**", ""]
            continue
        body = " ".join(claim_md(by_id[i]) for i in u["claims"])
        if u["kind"] == "item":
            out.append(f"{'   ' * u['depth']}{u['marker']} {body}")
        else:
            out += [body, ""]
    out.append("")

    out += ["## Where each part came from", ""]
    uses = collections.defaultdict(list)
    for r in rows:
        if r["label"] in FROM_DOCUMENT + ("partly",):
            for l in r["passages"]:
                if l["verdict"] in FROM_DOCUMENT + ("partly",):
                    uses[l["passage"]].append((r, l))
    groups: dict[tuple[str, str], list] = {}
    for pid in sorted(uses, key=doc.order.get):
        p = doc.by_id[pid]
        groups.setdefault((p["locator"], p.get("section", "")), []).extend(uses[pid])
    for (locator, section), items in groups.items():
        out.append(f"**{locator}**" + (f" · {short_section(section)}" if section else ""))
        for r, l in items:
            out += [f"- *{l['verdict']}*: {short(r['text'])}", f"  > {l['evidence']}"]
        out.append("")
    if not uses:
        out += ["Nothing in the answer came from the document.", ""]

    out += ["## Not from the document", ""]
    nots = [r for r in rows if r["label"] == "not from the document"]
    for n, r in enumerate(nots, 1):
        possible = next((l for l in r["passages"] if l["verdict"] in ("possible", "partly")), None)
        hint = f" *Closest passage: {possible['locator']}.*" if possible else ""
        out.append(f"{n}. {r['text']}{hint}")
    if not nots:
        out.append("Every claim came at least partly from the document.")
    out.append("")

    out += ["## Parts of the document not used", ""]
    counts = collections.Counter(l["passage"] for r in rows if r["label"] != "not from the document"
                                 for l in r["passages"] if l["verdict"] in FROM_DOCUMENT + ("partly",))
    sections: dict[str, list[dict]] = {}
    for p in doc.passages:
        sections.setdefault(p.get("section") or "", []).append(p)
    if len(sections) > 1 or "" not in sections:
        used = {name for name, ps in sections.items() if any(counts[p["id"]] for p in ps)}
        tops: dict[str, list[str]] = {}
        for name in sections:
            tops.setdefault(name.split(" › ")[0], []).append(name)
        out.append(f"The answer drew on {len(used)} of the document's {len(sections)} sections.")
        out.append("")
        for top, names in tops.items():
            unused = [n for n in names if n not in used]
            if not unused:
                continue
            label = top or "Opening"
            if len(unused) == len(names):
                out.append(f"- **{label}**: not used")
            else:
                leaves = [n.split(" › ")[-1] if n != top else "its introduction" for n in unused]
                out.append(f"- **{label}**: {len(unused)} of {len(names)} parts not used: {'; '.join(leaves)}")
    else:  # no sections (PDF): unused runs of passages, by page
        used_n = sum(1 for p in doc.passages if counts[p["id"]])
        out += [f"The answer drew on {used_n} of the document's {len(doc.passages)} passages. Stretches not used:", ""]
        runs, current = [], []
        for p in doc.passages:
            if counts[p["id"]]:
                if current:
                    runs.append(current)
                current = []
            else:
                current.append(p)
        if current:
            runs.append(current)
        for r_ in runs:
            first, last = r_[0].get("pages", ""), r_[-1].get("pages", "")
            span = first.split("–")[0] + ("" if first == last else "–" + last.split("–")[-1])
            out.append(f"- {'pp.' if '–' in span else 'p.'} {span} ({len(r_)} passage{'s' if len(r_) != 1 else ''})")
        if not runs:
            out.append("Every passage was used.")
    out.append("")

    out += ["## How to read this", "",
            "- **quoted**: uses the document's exact words. **paraphrased**: says what a passage says in other "
            "words, including summarizing it. **inferred**: a conclusion the document supports but doesn't state.",
            "- **partly**: part of the claim is in the document and part isn't, or a detail was changed.",
            "- **not from the document**: no passage says it. It may come from the assistant's training, its own "
            "reasoning, or the question. That doesn't make it wrong.",
            "- Lines in *italics* aren't claims and weren't checked.",
            "- Method: keyword search finds candidate passages; Jev (TypeSafe) picks which ones the claim draws "
            "on and how; the evidence lines are copied from the document. When the match is borderline, the "
            "claim is counted as not from the document.", ""]
    return "\n".join(out)


def terminal_view(md: str) -> str:
    """Headline, claims not from the document, unused parts."""
    head = md.split("\n## The answer, annotated")[0]
    rest = md.split("## Not from the document", 1)[1].split("\n## How to read this")[0]
    return head.rstrip() + "\n\n## Not from the document" + rest.rstrip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work", type=Path)
    ap.add_argument("answer", type=Path)
    ap.add_argument("--max-spend", type=float, default=float(os.environ["AUDITLM_MAX_SPEND"])
                    if os.environ.get("AUDITLM_MAX_SPEND") else None,
                    help="stop before the spend ledger passes this many US dollars")
    args = ap.parse_args()
    asyncio.run(audit(args.work, args.answer, args.max_spend))


if __name__ == "__main__":
    main()
