# /// script
# requires-python = ">=3.12"
# dependencies = ["pypdf>=5", "python-docx>=1.1"]
# ///
"""Split a source document into passages, each with a locator and section. Pure code, no model.

  uv run ingest.py DOC.pdf|.docx|.txt|.md WORK_DIR [--title "Document title"] [--keep-references]

Writes WORK_DIR/passages.json:
  {"source", "title", "kind", "passages": [{"id", "locator", "section", "pages"?, "text"}]}

Word documents keep their own structure: one passage per paragraph, the heading path as the
section, and the document's own paragraph numbers ("12.") as locators when it has them
(otherwise "para N"). Short paragraphs join the previous one in the same section; long ones
split at sentence ends.

PDFs (and plain text) become passages of ~90-180 words built from whole sentences, with page
numbers as locators. A sentence cut by a page break is re-joined. By default the text stops at
a References/Bibliography section in the back half; pass --keep-references to keep it.
"""

import json
import re
import sys
from pathlib import Path

TARGET_WORDS = 90  # flush a passage at the next sentence end after this many words
MAX_WORDS = 180  # hard cap for a passage
MIN_WORDS = 25  # PDF: drop fragments shorter than this; Word: join shorter paragraphs to a neighbour

REFS_RE = re.compile(r"^\s*(references|bibliography|works cited|literature cited)\s*$", re.I | re.M)
SENTENCE_RE = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”’)\]]))\s+(?=[\"“‘(\[]?[A-Z0-9])")
TERMINAL_RE = re.compile(r"[.!?:][\"”’)\]]?$")
PARA_NUMBER_RE = re.compile(r"^[.\s ]*(\d{1,4})\s*\.(?:[\s ]*\.)*[\s ]+")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


# --- PDF and plain text -------------------------------------------------------------
def read_pages(path: Path) -> tuple[list[str], str]:
    """Return (page texts, title guess). Plain text is one page unless it has form feeds."""
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        pages = [p.extract_text() or "" for p in reader.pages]
        title = (reader.metadata.title if reader.metadata else None) or ""
    else:
        pages = path.read_text(encoding="utf-8", errors="replace").split("\f")
        title = ""
    if not title.strip():  # first line that isn't an all-caps banner ("AUTHOR ACCEPTED MANUSCRIPT")
        lines = [ln.strip().lstrip("# ") for ln in pages[0].splitlines() if ln.strip()]
        title = next((ln for ln in lines if not ln.isupper()), lines[0] if lines else "")[:200]
    return pages, title.strip()


def cut_references(pages: list[str]) -> list[str]:
    """Drop everything from a References heading on, if it appears in the back half."""
    for i in range(len(pages) - 1, len(pages) // 2 - 1, -1):
        if m := REFS_RE.search(pages[i]):
            return pages[:i] + [pages[i][: m.start()]]
    return pages


def paragraphs(page: str) -> list[str]:
    """Blank lines split paragraphs; single newlines are line wraps."""
    page = re.sub(r"(\w)-\n(\w)", r"\1\2", page)  # re-join hyphenated line breaks
    return [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", page) if p.strip()]


def page_paragraphs(pages: list[str]) -> list[list[tuple[str, int, int]]]:
    """Paragraphs as lists of (sentence, first page, last page). A sentence cut by a page
    break is re-joined, so it lands whole in one passage with both page numbers."""
    out: list[list[tuple[str, int, int]]] = []
    for page_no, page in enumerate(pages, start=1):
        for i, para in enumerate(paragraphs(page)):
            sentences = [(s, page_no, page_no) for s in SENTENCE_RE.split(para)]
            if i == 0 and out and out[-1] and not TERMINAL_RE.search(out[-1][-1][0]):
                text, first, _ = out[-1][-1]
                out[-1][-1] = (f"{text} {sentences[0][0]}", first, page_no)
                out[-1] += sentences[1:]
            else:
                out.append(sentences)
    return out


def pdf_passages(pages: list[str]) -> list[dict]:
    passages, words, start_page, last_page = [], [], None, None

    def flush():
        nonlocal words, start_page
        if len(words) >= MIN_WORDS:
            pages_ref = str(start_page) if start_page == last_page else f"{start_page}–{last_page}"
            passages.append({"id": f"P{len(passages) + 1:03d}", "locator": f"p. {pages_ref}", "section": "",
                             "pages": pages_ref, "text": " ".join(words)})
        words, start_page = [], None

    for para in page_paragraphs(pages):
        for sentence, first, last in para:
            if start_page is None:
                start_page = first
            last_page = last
            words += sentence.split()
            while len(words) > MAX_WORDS:  # run-on text with no sentence breaks
                rest, words = words[MAX_WORDS:], words[:MAX_WORDS]
                flush()
                words, start_page = rest, last
            if len(words) >= TARGET_WORDS:
                flush()
        if len(words) >= MIN_WORDS * 2:  # paragraph end is a natural break
            flush()
    flush()
    return passages


# --- Word ------------------------------------------------------------------------------
def word_paragraphs(path: Path) -> tuple[list[dict], str]:
    """Body paragraphs with their heading path and number, and a title guess."""
    import docx

    d = docx.Document(str(path))
    heads: list[tuple[int, str]] = []  # open headings as (level, text)
    paras, position = [], 0
    for p in d.paragraphs:
        text = clean(p.text)
        if not text:
            continue
        style = p.style.name if p.style is not None else ""
        if m := re.match(r"(?:Heading|Title)\s*(\d*)", style):
            level = int(m.group(1) or 1)
            while heads and heads[-1][0] >= level:
                heads.pop()
            heads.append((level, text))
            continue
        position += 1
        number = PARA_NUMBER_RE.match(text)
        paras.append({"section": " › ".join(t for _, t in heads), "number": int(number.group(1)) if number else None,
                      "position": position, "text": text})
    title = clean(d.core_properties.title or "") or path.stem
    return paras, title


def locator(group: list[dict], numbered: bool) -> str:
    if numbered:
        nums = [p["number"] for p in group if p["number"] is not None]
        if nums:
            return f"¶{nums[0]}" if nums[0] == nums[-1] else f"¶{nums[0]}–{nums[-1]}"
    first, last = group[0]["position"], group[-1]["position"]
    return f"para {first}" if first == last else f"paras {first}–{last}"


def word_passages(paras: list[dict]) -> list[dict]:
    numbered = sum(p["number"] is not None for p in paras) >= len(paras) / 2
    groups: list[list[dict]] = []
    for p in paras:  # a short paragraph joins the previous one in the same section
        if groups and groups[-1][-1]["section"] == p["section"] and len(p["text"].split()) < MIN_WORDS \
                and sum(len(q["text"].split()) for q in groups[-1]) + len(p["text"].split()) <= MAX_WORDS:
            groups[-1].append(p)
        else:
            groups.append([p])
    passages = []
    for g in groups:
        text = " ".join(p["text"] for p in g)
        chunks, words = [], []
        for sentence in SENTENCE_RE.split(text):  # split long paragraphs at sentence ends
            words += sentence.split()
            if len(words) >= MAX_WORDS - 30:
                chunks.append(words)
                words = []
        if words:
            if chunks and len(words) < MIN_WORDS:
                chunks[-1] += words
            else:
                chunks.append(words)
        for c in chunks:
            passages.append({"id": f"P{len(passages) + 1:03d}", "locator": locator(g, numbered),
                             "section": g[0]["section"], "text": " ".join(c)})
    return passages


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("document", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--title", help="override the guessed title (Jev sees it with every judgment)")
    ap.add_argument("--keep-references", action="store_true", help="PDF: don't cut the References section")
    args = ap.parse_args()
    src, out_dir = args.document, args.out_dir
    if not src.exists():
        sys.exit(f"Missing {src}.")
    if src.suffix.lower() == ".docx":
        paras, title = word_paragraphs(src)
        passages, kind, extent = word_passages(paras), "word", f"{len(paras)} paragraphs"
    else:
        pages, title = read_pages(src)
        passages = pdf_passages(pages if args.keep_references else cut_references(pages))
        kind, extent = ("pdf" if src.suffix.lower() == ".pdf" else "text"), f"{len(pages)} pages"
    title = args.title or title
    seen, unique = set(), []
    for p in passages:  # some documents repeat a block of text; keep its first occurrence
        key = re.sub(r"\W+", " ", p["text"].lower()).strip()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    dropped, passages = len(passages) - len(unique), unique
    for i, p in enumerate(passages, start=1):
        p["id"] = f"P{i:03d}"
    if not passages:
        sys.exit(f"No text extracted from {src}. Is it a scanned PDF? Try OCR first.")

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "passages.json"
    out.write_text(json.dumps({"source": str(src), "title": title, "kind": kind, "passages": passages},
                              indent=2, ensure_ascii=False))
    words = sum(len(p["text"].split()) for p in passages)
    note = f" ({dropped} repeated passage{'s' if dropped != 1 else ''} dropped)" if dropped else ""
    print(f"{out}: {len(passages)} passages{note}, {words} words, {extent}. Title: {title!r}")


if __name__ == "__main__":
    main()
