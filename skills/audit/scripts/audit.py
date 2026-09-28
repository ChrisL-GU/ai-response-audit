# /// script
# requires-python = ">=3.12"
# dependencies = ["typesafe-sdk>=0.7.1"]
# ///
"""Audit how each claim in an AI response is supported by a document.

  uv run audit.py WORK_DIR RESPONSE.md|.txt

Needs WORK_DIR/passages.json (from passages.py). For each claim (sentence or list item)
in the response:

  1. code   shortlists candidate passages: keyword search (BM25) on the claim and its list
            lead-in, plus any passage that contains a quotation from the claim;
  2. Jev    chooses which candidates address the claim's specific point (a Choice, so
            candidates are compared, with "none of these" as an option). Candidates with at
            least PICK_ABOVE of the choice go on to step 3. If none reaches it, every passage
            is checked in groups of SHORTLIST, then the group winners compete;
  3. Jev    labels each chosen passage: restates / infers_from / partly_supports /
            contradicts / unrelated, and picks the passage sentence that is the evidence;
  4. code   checks every quotation in the claim against the document word for word, and
            every page citation against the pages where the quote or source actually is.

Writes WORK_DIR/sentences.json, WORK_DIR/audit.json, and WORK_DIR/report.md (the plain
report); prints the summary and the claims that need attention.

Division of labor: Jev answers narrow judgments (typed, with probabilities); this script
owns search, thresholds, exact-text checks, and the report. The API key comes from
$TYPESAFE_API_KEY, else from `secret-tool lookup service jev key api`.
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
PICK_ABOVE = 0.10  # a candidate with at least this share of Jev's choice is verified
MAX_PICKS = 3  # at most this many passages are verified per claim
SPREAD_BELOW = 0.30  # top choice below this, with SPREAD_COUNT candidates >= 0.05 -> "stated in several places"
SPREAD_COUNT = 4
SUPPORTS_ABOVE = 0.50  # p(restates) + p(infers_from) at or above this -> the passage supports the claim
PARTLY_ABOVE = 0.50  # ... plus p(partly_supports) at or above this -> partly supports
CONTRADICTS_ABOVE = 0.50  # p(contradicts) at or above this -> the passage contradicts the claim
LOW_CONFIDENCE = 0.60  # a deciding verdict whose (grouped) probability is below this is marked "low confidence"
SUBSTANTIVE_ABOVE = 0.50  # Noul below this -> filler (greeting, transition, offer), not audited
QUOTE_WORDS = 8  # a shared run of this many words or more counts as quoting
NEAR_QUOTE = 0.60  # a quotation whose longest match covers this share of its words is "close"
CONCURRENCY = 12  # rate limit is 1,200 requests/minute
SECRET_ATTRS = ["service", "jev", "key", "api"]
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "auditlm"

# --- Judgments -----------------------------------------------------------------
CONTEXT_NOTE = ("`context`, when present, gives the response's section heading, list lead-in, and previous "
                "sentence only to resolve what `sentence` refers to; judge `sentence` itself. A list item "
                "continues its `context.list_lead_in`: the item 'monitoring' under the lead-in 'Ostrom's design "
                "principles:' claims that monitoring is one of Ostrom's design principles.")


def addresses_question(candidates: list[dict]) -> dict:
    criteria = {p["id"]: p["text"] for p in candidates}
    criteria["none"] = ("None of these passages discusses what the sentence is about; at most they share its "
                        "general topic. A passage that gives different facts about the same thing (other "
                        "numbers, names, or conclusions) does address it.")
    return {"addresses": Choice(
        instructions="An AI assistant wrote `sentence` in a response about the document `document`. Which "
                     "passage from the document addresses the specific point `sentence` makes (its claim, "
                     "names, numbers, examples, or attributions), whether the passage agrees with it or not? "
                     + CONTEXT_NOTE,
        criteria=criteria,
    )}


RELATION = Choice(
    instructions="An AI assistant wrote `sentence` in a response about the document `document`. How does "
                 "`passage`, an excerpt from that document, bear on `sentence`? " + CONTEXT_NOTE,
    criteria={
        "restates": "The passage states what the sentence says, including its specific details (names, "
                    "numbers, attributions); the sentence quotes or closely paraphrases it.",
        "infers_from": "The passage does not state the sentence's point outright, but the sentence is a fair "
                       "summary, generalization, or conclusion drawn from what the passage says, and nothing "
                       "in it conflicts with the passage.",
        "partly_supports": "The passage supports part of the sentence, but some of the sentence's details are "
                           "not in the passage or go beyond it: for example, a detail placed in the wrong "
                           "context, an added claim, or two separate points merged into one.",
        "contradicts": "The passage and the sentence disagree about the same point: the passage says something "
                       "that makes the sentence, or one of its specific details (a number, a name, who said "
                       "what, the direction of an effect), false.",
        "unrelated": "The passage does not address the sentence's point.",
    },
)


def evidence_question(passage_sentences: list[str]) -> Choice:
    return Choice(
        instructions="An AI assistant wrote `sentence` in a response about the document `document`. Which "
                     "sentence of `passage` does `sentence` rely on, or conflict with, most directly? "
                     + CONTEXT_NOTE,
        criteria={f"E{i + 1}": s for i, s in enumerate(passage_sentences)},
    )


SUBSTANTIVE = {
    "substantive": Noul(
        instructions="Does `sentence`, from an AI assistant's response about the document `document`, make a "
                     "claim about the document or its subject, rather than being a greeting, a transition, an "
                     "offer of further help, or a remark about the response itself? " + CONTEXT_NOTE,
    ),
}

SUPPORTING = ("restates", "infers_from")


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
        return {"choice": answer.choice, "confidence": answer.confidence,
                "probabilities": answer.probabilities}
    return {"noul": answer.noul}


class Asker:
    """One request per call; cached by exact request so re-runs cost nothing."""

    def __init__(self, client: AsyncTypeSafeClient):
        self.client, self.sem, self.requests, self.cached = client, asyncio.Semaphore(CONCURRENCY), 0, 0

    async def __call__(self, state: dict, questions: dict) -> dict:
        payload = json.dumps({"state": state, "questions": {k: q.model_dump() for k, q in questions.items()}},
                             sort_keys=True)
        path = CACHE_DIR / f"{hashlib.sha256(payload.encode()).hexdigest()[:20]}.json"
        self.requests += 1
        if path.exists():
            self.cached += 1
            return json.loads(path.read_text())
        async with self.sem:
            response = await self.client.system_one(state=state, questions=questions)
        out = {k: serialize(a) for k, a in response.answers.items()}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out))
        return out


def load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Missing {path}.")
    return json.loads(path.read_text())


# --- Sentences (code) -------------------------------------------------------------
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


def response_units(text: str) -> list[dict]:
    """Paragraphs, list items, and table rows, each with its section heading and list lead-in.

    Headings (markdown '#', or a one-line block with no end punctuation) become context,
    not claims. A list item's lead-in is the chain of its parent items, or the paragraph
    line that introduced the list."""
    text = re.sub(r"```.*?```", "\n\n", text, flags=re.S)
    units, section = [], ""
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        first = lines[0].strip()
        if first.startswith("#"):
            section = strip_inline(first.lstrip("#"))
            lines = lines[1:]
        elif (len(lines) == 1 and not LIST_ITEM_RE.match(first) and not TERMINAL_RE.search(first)
              and len(first.split()) <= 12):
            section = strip_inline(first)
            continue
        stack: list[tuple[int, dict]] = []  # (indent, unit) of open list items
        intro = ""  # paragraph text that introduces the list that follows it
        for line in lines:
            s = line.strip().lstrip(">").strip()
            if not s or re.fullmatch(r"[-*_=|:\s]{3,}", s):
                continue
            if s.startswith("#"):
                section = strip_inline(s.lstrip("#"))
                continue
            if s.startswith("|"):  # table row -> "cell; cell"
                units.append({"text": "; ".join(c.strip() for c in s.strip("|").split("|") if c.strip()),
                              "section": section, "lead_in": intro})
                continue
            # A numbered item is the parent of bullets that follow it at the same indentation.
            indent = 2 * (len(line) - len(line.lstrip())) + (0 if re.match(r"\d", s) else 1)
            if LIST_ITEM_RE.match(s):
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                parents = [u["text"] for _, u in stack]
                unit = {"text": LIST_ITEM_RE.sub("", s), "section": section,
                        "lead_in": " › ".join(([intro] if intro else []) + parents)}
                units.append(unit)
                stack.append((indent, unit))
            elif stack:  # continuation of the current list item
                stack[-1][1]["text"] += " " + s
            elif units and units[-1].get("para_of") is block:
                units[-1]["text"] += " " + s
            else:
                units.append({"text": s, "section": section, "lead_in": "", "para_of": block})
                intro = s
    for u in units:
        u.pop("para_of", None)
        u["text"] = strip_inline(u["text"])
        u["lead_in"] = strip_inline(u["lead_in"])
    return units


def split_response(text: str) -> list[dict]:
    sentences, previous = [], ""
    for unit in response_units(text):
        for s in split_sentences(unit["text"]):
            context = {k: v for k, v in (("section", unit["section"]), ("list_lead_in", unit["lead_in"]),
                                          ("previous_sentence", previous)) if v}
            sentences.append({"id": f"S{len(sentences) + 1:02d}", "text": s, "context": context})
            previous = s
    return sentences


# --- Words, search, and exact-text checks (code) ----------------------------------------
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-‐‑'’][A-Za-z0-9]+)*")
STOPWORDS = set("a an and are as at be but by can for from has have in is it its not of on or that the their "
                "them they this those to was were which with would".split())


def norm(word: str) -> str:
    return re.sub(r"[-‐‑'’]", "", word.lower())


SUFFIXES = ("ational", "ations", "ation", "ally", "ings", "ing", "ies", "ied", "al", "ed", "es", "ly", "s")


def stem(word: str) -> str:
    """Crude suffix stripping so search matches 'accident' with 'accidental' (search only)."""
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    return [norm(m.group()) for m in WORD_RE.finditer(text)]


def longest_shared_run(a: list[str], b: list[str]) -> tuple[int, int]:
    """(length, end index in b) of the longest run of words a and b share."""
    best, end, prev = 0, 0, [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, start=1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, end = cur[j], j
        prev = cur
    return best, end


class Document:
    def __init__(self, passages: list[dict]):
        self.passages = passages
        self.by_id = {p["id"]: p for p in passages}
        self.words = {p["id"]: tokens(p["text"]) for p in passages}
        self.sentences = {p["id"]: split_sentences(p["text"]) for p in passages}
        # BM25 index
        self.docs = [[stem(w) for w in self.words[p["id"]] if w not in STOPWORDS] for p in passages]
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
        n = len(self.docs)
        scores = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for w in q:
                if f := self.tf[i].get(w):
                    idf = math.log(1 + (n - self.df[w] + 0.5) / (self.df[w] + 0.5))
                    s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(d) / self.avg))
            scores.append(s)
        order = sorted(range(n), key=lambda i: -scores[i])
        return [self.passages[i]["id"] for i in order[:k] if scores[i] > 0]

    def find_quote(self, quote: str) -> dict:
        """Where a quotation appears word for word, or its closest wording."""
        q = tokens(quote)
        needle, ids, at = " " + " ".join(q) + " ", [], self.joined.find(" " + " ".join(q) + " ")
        while at >= 0:
            start = bisect.bisect_right(self.offsets, at) - 1
            ids += [self.passages[self.owner[i]]["id"] for i in range(start, start + len(q))]
            at = self.joined.find(needle, at + 1)
        if ids:
            ids = list(dict.fromkeys(ids))
            return {"status": "verbatim", "passages": ids, "pages": pages_of(self, ids[:1])}
        best = max(((longest_shared_run(q, self.words[p["id"]])[0], p["id"]) for p in self.passages))
        run, pid = best
        if not run:
            return {"status": "not found", "passages": [], "pages": ""}
        closest = max(self.sentences[pid], key=lambda s: longest_shared_run(q, tokens(s))[0])
        status = "close wording" if run / len(q) >= NEAR_QUOTE else "not found"
        return {"status": status, "passages": [pid], "pages": self.by_id[pid]["pages"],
                "closest": closest, "matched_words": f"{run}/{len(q)}"}


QUOTE_RE = re.compile(r"[“\"]([^”\"]{3,}?)[”\"]")
PAGE_RE = re.compile(r"\b(pp?)\.\s*~?\s*(\d{1,4})(?:\s*[–—-]\s*~?\s*(\d{1,4}))?")


def page_set(pages: str) -> set[int]:
    out = set()
    for part in pages.split(","):
        nums = [int(n) for n in re.findall(r"\d+", part)]
        if len(nums) == 1:
            out.add(nums[0])
        elif len(nums) >= 2:
            out.update(range(nums[0], nums[1] + 1))
    return out


def pages_of(doc: "Document", ids: list[str]) -> str:
    pages = sorted(set().union(*(page_set(doc.by_id[i]["pages"]) for i in ids))) if ids else []
    if not pages:
        return ""
    return str(pages[0]) if pages[0] == pages[-1] else f"{pages[0]}–{pages[-1]}"


def check_quotes(doc: Document, text: str) -> list[dict]:
    return [{"quote": q, **doc.find_quote(q)} for q in QUOTE_RE.findall(text) if len(tokens(q)) >= 2]


def check_pages(doc: Document, text: str, quotes: list[dict], sources: list[str]) -> list[dict]:
    """Compare page citations with where the quoted text (or else the source passage) is."""
    out = []
    for q in quotes:  # a quote found in several places: prefer the occurrence in the claim's sources
        if q["status"] == "verbatim" and len(q["passages"]) > 1:
            q["passages"].sort(key=lambda i: i not in sources)
            q["pages"] = pages_of(doc, q["passages"][:1])
    quoted = [q["passages"][0] for q in quotes if q["status"] == "verbatim"]
    basis, ids = ("quoted text", quoted) if quoted else ("source passage", sources)
    for m in PAGE_RE.finditer(text):
        lo = int(m.group(2))
        cited = set(range(lo, int(m.group(3)) + 1)) if m.group(3) else {lo}
        found = pages_of(doc, ids)
        out.append({"cited": m.group(0), "found": found, "basis": basis,
                    "ok": None if not found else bool(cited & page_set(found))})
    return out


# --- Policy on Jev's answers ------------------------------------------------------------
def picks_from(probabilities: dict) -> tuple[list[tuple[str, float]], float, bool]:
    """(chosen passages with their share, p(none), spread) from an `addresses` Choice."""
    p_none = probabilities.get("none", 0.0)
    ranked = sorted(((k, v) for k, v in probabilities.items() if k != "none"), key=lambda kv: -kv[1])
    picks = [(k, v) for k, v in ranked if v >= PICK_ABOVE][:MAX_PICKS]
    spread = bool(ranked) and ranked[0][1] < SPREAD_BELOW and sum(v >= 0.05 for _, v in ranked) >= SPREAD_COUNT
    if spread and not picks:
        picks = ranked[:MAX_PICKS]
    return picks, p_none, spread


def label(relation: dict, shared: int) -> dict:
    p = relation["probabilities"]
    support = sum(p.get(k, 0.0) for k in SUPPORTING)
    if p.get("contradicts", 0.0) >= CONTRADICTS_ABOVE:
        verdict = "contradicts"
    elif support >= SUPPORTS_ABOVE or shared >= QUOTE_WORDS:
        verdict = "supports"
    elif support + p.get("partly_supports", 0.0) >= PARTLY_ABOVE:
        verdict = "partly supports"
    else:
        verdict = "does not address"
    if verdict == "supports":
        how = ("quoted" if shared >= QUOTE_WORDS else
               "paraphrased" if p.get("restates", 0.0) >= p.get("infers_from", 0.0) else "inferred")
    else:
        how = ""
    # Probability of the verdict as shown: restates and infers_from both count as "supports".
    p_verdict = {"contradicts": p.get("contradicts", 0.0), "supports": support,
                 "partly supports": support + p.get("partly_supports", 0.0),
                 "does not address": p.get("unrelated", 0.0)}[verdict]
    return {"verdict": verdict, "how": how, "p": round(max(p_verdict, 1.0 if shared >= QUOTE_WORDS else 0), 2),
            "shared_words": shared, "jev_choice": relation["choice"],
            "probabilities": {k: round(v, 2) for k, v in p.items()}}


def status_of(links: list[dict], substantive: float) -> str:
    if substantive < SUBSTANTIVE_ABOVE:
        return "filler"
    verdicts = {l["verdict"] for l in links}
    if "contradicts" in verdicts:
        return "conflicting" if "supports" in verdicts else "contradicted"
    if "supports" in verdicts:
        return "supported"
    if "partly supports" in verdicts:
        return "partly supported"
    return "unsupported"


# --- Main ---------------------------------------------------------------------------
async def audit(work: Path, response_path: Path) -> None:
    meta = load(work / "passages.json")
    doc = Document(meta["passages"])
    title = meta["title"]
    if not response_path.exists():
        sys.exit(f"Missing {response_path}.")
    sentences = split_response(response_path.read_text(encoding="utf-8", errors="replace"))
    if not sentences:
        sys.exit(f"No sentences found in {response_path}.")
    (work / "sentences.json").write_text(json.dumps(
        {"response": str(response_path), "sentences": sentences}, indent=2, ensure_ascii=False))

    def state(s: dict, **extra) -> dict:
        return {"document": title, "sentence": s["text"], **({"context": s["context"]} if s["context"] else {}),
                **extra}

    retry = RetryPolicy(max_retries=6, backoff_max=20.0)
    async with AsyncTypeSafeClient(api_key=api_key(), retry=retry) as client:
        ask = Asker(client)

        async def choose(s: dict, candidate_ids: list[str]) -> dict:
            a = await ask(state(s), addresses_question([doc.by_id[i] for i in candidate_ids]))
            return a["addresses"]["probabilities"]

        async def attribute(s: dict, quotes: list[dict]) -> dict:
            """Shortlist -> choose; if none of the shortlist addresses it, check every passage."""
            lead = s["context"].get("list_lead_in", "")
            query = f"{lead} {s['text']}"
            if re.match(r"(it|this|that|these|those|they|such)\b", s["text"], re.I):
                query += " " + s["context"].get("previous_sentence", "")
            forced = [i for q in quotes for i in q["passages"]]
            shortlist = list(dict.fromkeys(forced + doc.search(query, SHORTLIST)))[:SHORTLIST + len(forced)]
            # Verification (step 3) is the real judgment, so any candidate with a real share of the
            # choice is verified, even when "none" leads: Jev tends to answer "none" for a passage
            # that states different facts about the same thing.
            if shortlist:
                picks, p_none, spread = picks_from(await choose(s, shortlist))
                if picks:
                    return {"picks": picks, "spread": spread, "searched": "shortlist", "p_none": round(p_none, 2)}
            # Full check: every passage, in groups; group winners compete in a final choice.
            ids = [p["id"] for p in doc.passages]
            groups = [ids[i:i + SHORTLIST] for i in range(0, len(ids), SHORTLIST)]
            results = await asyncio.gather(*[choose(s, g) for g in groups])
            winners = [k for probs in results for k, _ in picks_from(probs)[0]]
            if len(winners) <= 1:
                return {"picks": [(w, 1.0) for w in winners], "spread": False, "searched": "all passages",
                        "p_none": 0.0 if winners else 1.0}
            picks, p_none, spread = picks_from(await choose(s, winners))
            return {"picks": picks, "spread": spread, "searched": "all passages", "p_none": round(p_none, 2)}

        async def verify(s: dict, pid: str) -> dict:
            questions = {"relation": RELATION}
            passage_sentences = doc.sentences[pid]
            if len(passage_sentences) > 1:
                questions["evidence"] = evidence_question(passage_sentences)
            a = await ask(state(s, passage=doc.by_id[pid]["text"]), questions)
            evidence = (passage_sentences[int(a["evidence"]["choice"][1:]) - 1] if "evidence" in a
                        else passage_sentences[0])
            shared = longest_shared_run(tokens(s["text"]), doc.words[pid])[0]
            return {"passage": pid, "pages": doc.by_id[pid]["pages"], "evidence": evidence,
                    **label(a["relation"], shared)}

        async def audit_sentence(s: dict) -> dict:
            quotes = check_quotes(doc, s["text"])
            sub, attribution = await asyncio.gather(ask(state(s), SUBSTANTIVE), attribute(s, quotes))
            substantive = sub["substantive"]["noul"]
            links = []
            if substantive >= SUBSTANTIVE_ABOVE:
                links = await asyncio.gather(*[verify(s, pid) for pid, _ in attribution["picks"]])
                for link, (_, share) in zip(links, attribution["picks"]):
                    link["share"] = round(share, 2)
            sources = [l["passage"] for l in links if l["verdict"] in ("supports", "partly supports")]
            status = status_of(links, substantive)
            deciding = next((l for l in links if l["verdict"] != "does not address"), None)
            return {**s, "status": status, "substantive": round(substantive, 2),
                    "low_confidence": bool(deciding and deciding["p"] < LOW_CONFIDENCE),
                    "stated_in_several_places": attribution["spread"], "searched": attribution["searched"],
                    "p_none": attribution["p_none"], "passages": links, "quotes": quotes,
                    "page_citations": check_pages(doc, s["text"], quotes, sources)}

        rows = await asyncio.gather(*[audit_sentence(s) for s in sentences])
        print(f"{ask.requests} Jev requests ({ask.cached} cached)", file=sys.stderr)

    # Coverage
    coverage = []
    for p in doc.passages:
        uses = [{"sentence": r["id"], "verdict": l["verdict"], "how": l["how"]}
                for r in rows for l in r["passages"] if l["passage"] == p["id"] and l["verdict"] != "does not address"]
        coverage.append({"id": p["id"], "pages": p["pages"], "used_by": uses})

    statuses = ["supported", "partly supported", "conflicting", "contradicted", "unsupported", "filler"]
    counts = {k: sum(r["status"] == k for r in rows) for k in statuses}
    quote_counts = collections.Counter(q["status"] for r in rows for q in r["quotes"])
    page_bad = [r["id"] for r in rows for c in r["page_citations"] if c["ok"] is False]
    report = {"document": title, "source": meta.get("source"), "response": str(response_path),
              "policy": {k: v for k, v in globals().items() if k.isupper() and isinstance(v, (int, float))},
              "summary": {"claims": len(rows), **counts, "quotes": dict(quote_counts),
                          "page_citation_mismatches": page_bad},
              "sentences": rows, "passages": coverage}
    (work / "audit.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    md = render(report)
    (work / "report.md").write_text(md)
    head, _, rest = md.partition("\n## All claims\n")
    print(head.rstrip())
    print(f"\nFull report: {work / 'report.md'}   Data: {work / 'audit.json'}")


# --- Report (plain; no commentary) -------------------------------------------------------
def id_ranges(ids: list[str]) -> str:
    """P001, P002, P003, P007 -> P001–P003, P007"""
    nums = sorted(int(i[1:]) for i in ids)
    out, start = [], None
    for k, n in enumerate(nums):
        start = n if start is None else start
        if k + 1 == len(nums) or nums[k + 1] != n + 1:
            out.append(f"P{start:03d}" if start == n else f"P{start:03d}–P{n:03d}")
            start = None
    return ", ".join(out)


EXCERPT_WORDS = 40


def excerpt(text: str, claim: str) -> str:
    """The whole sentence, or for a very long one (tables) the window sharing most words with the claim."""
    spans = [m.span() for m in re.finditer(r"\S+", text)]
    if len(spans) <= EXCERPT_WORDS * 3 // 2:
        return text
    wanted = {w for w in tokens(claim) if w not in STOPWORDS}
    hits = [bool(set(tokens(text[a:b])) & wanted) for a, b in spans]
    best = max(range(len(spans) - EXCERPT_WORDS + 1), key=lambda i: sum(hits[i:i + EXCERPT_WORDS]))
    end = best + EXCERPT_WORDS - 1
    return (("… " if best else "") + text[spans[best][0]:spans[end][1]]
            + (" …" if end < len(spans) - 1 else ""))


def claim_block(r: dict) -> str:
    flags = [r["status"]]
    if r["low_confidence"]:
        flags.append("low confidence")
    if r["stated_in_several_places"]:
        flags.append("stated in several places")
    lines = [f"### {r['id']} · {' · '.join(flags)}", "", f"> {r['text']}", ""]
    ctx = [r["context"][k] for k in ("section", "list_lead_in") if k in r["context"]]
    if ctx:
        lines += [f"Context: {' › '.join(ctx)}", ""]
    if r["status"] == "filler":
        return "\n".join(lines)
    for l in r["passages"]:
        if l["verdict"] == "does not address":
            continue
        how = f", {l['how']}" if l["how"] else ""
        lines += [f"- **{l['passage']}, p. {l['pages']}**: {l['verdict']}{how} (p = {l['p']:.2f})",
                  f"  > {excerpt(l['evidence'], r['text'])}"]
    if not any(l["verdict"] != "does not address" for l in r["passages"]):
        searched = "the shortlisted passages" if r["searched"] == "shortlist" else "every passage"
        lines.append(f"- No passage addresses this claim (checked {searched}).")
    for q in r["quotes"]:
        if q["status"] == "verbatim":
            more = f" and {len(q['passages']) - 1} other passage(s)" if len(q["passages"]) > 1 else ""
            lines.append(f"- Quote “{q['quote']}”: verbatim in {q['passages'][0]} (p. {q['pages']}){more}")
        else:
            what = "not verbatim" if q["status"] == "close wording" else "not found in the document"
            lines.append(f"- Quote “{q['quote']}”: {what}.")
            if q.get("closest"):
                lines += [f"  Nearest wording, {q['passages'][0]} (p. {q['pages']}):",
                          f"  > {excerpt(q['closest'], q['quote'])}"]
    for c in r["page_citations"]:
        if c["ok"] is False:
            lines.append(f"- Page citation “{c['cited']}”: the {c['basis']} is on p. {c['found']}")
        elif c["ok"]:
            lines.append(f"- Page citation “{c['cited']}”: matches the {c['basis']} (p. {c['found']})")
    return "\n".join(lines)


def needs_attention(r: dict) -> bool:
    return (r["status"] in ("partly supported", "conflicting", "contradicted", "unsupported")
            or r["low_confidence"] or any(q["status"] != "verbatim" for q in r["quotes"])
            or any(c["ok"] is False for c in r["page_citations"]))


def render(report: dict) -> str:
    s, rows = report["summary"], report["sentences"]
    audited = s["claims"] - s["filler"]
    quotes = s["quotes"]
    out = [f"# Audit: {Path(report['response']).name}", "",
           f"Checked against *{report['document']}*.", "",
           "## Summary", "",
           f"- {s['claims']} claims, {audited} audited ({s['filler']} filler skipped)",
           f"- {s['supported']} supported · {s['partly supported']} partly supported · "
           f"{s['conflicting']} conflicting · {s['contradicted']} contradicted · {s['unsupported']} unsupported",
           f"- Quotations: {quotes.get('verbatim', 0)} verbatim · {quotes.get('close wording', 0)} close wording · "
           f"{quotes.get('not found', 0)} not found",
           f"- Page citations that don't match: {len(s['page_citation_mismatches'])}"]
    used = [c["id"] for c in report["passages"] if c["used_by"]]
    unused = [c["id"] for c in report["passages"] if not c["used_by"]]
    out += [f"- Passages used: {len(used)} of {len(report['passages'])}", ""]
    flagged = [r for r in rows if needs_attention(r)]
    out += ["## Needs attention", ""]
    out += ["\n\n".join(claim_block(r) for r in flagged) if flagged else "Nothing flagged.", ""]
    out += ["## Coverage", "", f"Used: {id_ranges(used) or 'none'}", "", f"Not used: {id_ranges(unused) or 'none'}", ""]
    out += ["## All claims", "", "\n\n".join(claim_block(r) for r in rows), ""]
    out += ["## How to read this", "",
            "Each claim is a sentence or list item from the response. Code shortlists passages by keyword search "
            "(plus any passage a quotation matches); Jev (TypeSafe) chooses which passages address the claim, "
            "checking every passage if none of the shortlist does, then labels each one and picks the sentence "
            "shown as evidence. Evidence and quotations are copied from the document, not paraphrased.",
            "",
            "- supported: a passage restates the claim or it is a fair inference from one; quoted = shares "
            f"{QUOTE_WORDS}+ consecutive words with it.",
            "- partly supported: part of the claim is in a passage, part is not (a detail missing, misplaced, or "
            "merged from two points).",
            "- contradicted: a passage about the same point says something that makes the claim false; "
            "conflicting: one passage supports it and another contradicts it.",
            "- unsupported: no passage addresses it. That does not make it false; it may come from outside the "
            "document.",
            "- low confidence: Jev's label was uncertain. Stated in several places: the document makes this "
            "point in many passages, so no single one is the source.",
            "- Page citations are compared with the PDF's page numbers, which may differ from a journal's "
            "printed page numbers.", ""]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work", type=Path)
    ap.add_argument("response", type=Path)
    args = ap.parse_args()
    asyncio.run(audit(args.work, args.response))


if __name__ == "__main__":
    main()
