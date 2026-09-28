"""Compare an audit.json against hand labels.

  python3 tests/check.py WORK_DIR/audit.json tests/fixtures/<name>-expected.json

Per expected sentence (matched by its first words):
  expect   acceptable statuses, "|" separated
  not      statuses that would be wrong
  evidence text that should appear in a passage Jev found to address the claim
  quote    acceptable statuses for the claim's quotation(s)
  page_ok  whether its page citation should match
Prints one line per sentence; exits non-zero on any failure.
"""

import json
import sys
from pathlib import Path

audit, expected = (json.load(open(p)) for p in sys.argv[1:3])
passages = {p["id"]: p["text"] for p in json.load(open(Path(sys.argv[1]).with_name("passages.json")))["passages"]}
rows = audit["sentences"]
fails = 0
for exp in expected["sentences"]:
    row = next((r for r in rows if r["text"].startswith(exp["starts"])), None)
    if row is None:
        print(f"MISSING  {exp['starts']!r}")
        fails += 1
        continue
    problems = []
    if "expect" in exp and row["status"] not in exp["expect"].split("|"):
        problems.append(f"status {row['status']!r}, expected {exp['expect']!r}")
    if "not" in exp and row["status"] in exp["not"].split("|"):
        problems.append(f"status {row['status']!r} should not be {exp['not']!r}")
    found = [passages[l["passage"]] for l in row["passages"] if l["verdict"] != "does not address"]
    missing = [e for e in exp.get("evidence", []) if not any(e in text for text in found)]
    if "quote" in exp and not any(q["status"] in exp["quote"].split("|") for q in row["quotes"]):
        problems.append(f"quote check {[q['status'] for q in row['quotes']]}, expected {exp['quote']!r}")
    if "page_ok" in exp and not any(c["ok"] is exp["page_ok"] for c in row["page_citations"]):
        problems.append(f"page check {[c['ok'] for c in row['page_citations']]}, expected {exp['page_ok']}")
    note = f"  (expected evidence not found: {'; '.join(missing)})" if missing else ""
    print(f"{'FAIL' if problems else 'ok  '}  {row['id']} {row['status']:<17} {'; '.join(problems)}{note}")
    fails += bool(problems)
print(f"\n{len(expected['sentences']) - fails}/{len(expected['sentences'])} as expected")
sys.exit(1 if fails else 0)
