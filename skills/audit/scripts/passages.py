# /// script
# requires-python = ">=3.12"
# dependencies = ["pypdf>=5"]
# ///
"""Split a document into numbered passages with page references. Pure code, no model.

  uv run passages.py DOC.pdf|.txt|.md WORK_DIR [--title "Document title"] [--keep-references]

Writes WORK_DIR/passages.json:
  {"source", "title", "passages": [{"id": "P001", "pages": "3", "text": "..."}]}

Passages are built from whole sentences (~90-180 words) and keep paragraph breaks when
the source has them. By default they stop at a References/Bibliography section in the
back half of the document; pass --keep-references if the response may cite it.
"""

import json
import re
import sys
from pathlib import Path

TARGET_WORDS = 90  # flush a passage at the next sentence end after this many words
MAX_WORDS = 180  # hard cap, even mid-paragraph
MIN_WORDS = 25  # drop fragments (headers, captions, page furniture)

REFS_RE = re.compile(r"^\s*(references|bibliography|works cited|literature cited)\s*$", re.I | re.M)
SENTENCE_RE = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”’)\]]))\s+(?=[\"“‘(\[]?[A-Z0-9])")
TERMINAL_RE = re.compile(r"[.!?:][\"”’)\]]?$")


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


def build_passages(pages: list[str]) -> list[dict]:
    passages, words, start_page, last_page = [], [], None, None

    def flush():
        nonlocal words, start_page
        if len(words) >= MIN_WORDS:
            pages_ref = str(start_page) if start_page == last_page else f"{start_page}–{last_page}"
            passages.append({"id": f"P{len(passages) + 1:03d}", "pages": pages_ref,
                             "text": " ".join(words)})
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


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("document", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--title", help="override the guessed title (Jev sees it with every pair)")
    ap.add_argument("--keep-references", action="store_true", help="don't cut the References section")
    args = ap.parse_args()
    src, out_dir = args.document, args.out_dir
    pages, title = read_pages(src)
    title = args.title or title
    passages = build_passages(pages if args.keep_references else cut_references(pages))
    if not passages:
        sys.exit(f"No text extracted from {src}. Is it a scanned PDF? Try OCR first.")

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "passages.json"
    out.write_text(json.dumps({"source": str(src), "title": title, "passages": passages},
                              indent=2, ensure_ascii=False))
    words = sum(len(p["text"].split()) for p in passages)
    print(f"{out}: {len(passages)} passages, {words} words, {len(pages)} pages. Title: {title!r}")


if __name__ == "__main__":
    main()
