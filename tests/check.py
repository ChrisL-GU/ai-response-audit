"""Score audit results against hand labels.

  python3 tests/check.py WORK_ROOT [--set tune|holdout|all] [--details]

Expects WORK_ROOT/<answer>/audit.json and passages.json for each tests/fixtures/<answer>-expected.json
(tests/run.sh creates them). "From the document" means quoted, paraphrased, inferred or partly.

  precision  of the claims the tool marks as from the document, the share that really are
  recall     of the claims that really are from the document, the share the tool marks so
  source     of the true positives, the share where an expected evidence snippet is in a reported passage
  false used of the claims not from the document, the share the tool marks as from it
Skipped (filler) claims are scored separately and left out of the four metrics.
"""

import argparse
import collections
import json
import re
from pathlib import Path

FROM = {"quoted", "paraphrased", "inferred", "partly"}
LABELS = ["quoted", "paraphrased", "inferred", "partly", "not", "skipped"]
TARGETS = {"precision": 0.90, "recall": 0.80, "source": 0.85, "false used": 0.05}

ap = argparse.ArgumentParser()
ap.add_argument("root", type=Path)
ap.add_argument("--set", default="all", choices=["tune", "holdout", "all"])
ap.add_argument("--details", action="store_true", help="list every claim whose label differs")
args = ap.parse_args()
fixtures = Path(__file__).parent / "fixtures"
norm = lambda t: re.sub(r"\s+", " ", t.replace(" ", " "))
short = lambda l: "not" if l == "not from the document" else l

tp = fp = fn = tn = src_ok = skip_ok = skip_n = 0
confusion = collections.Counter()
common, misses = [], []
for exp_path in sorted(fixtures.glob("*-expected.json")):
    exp = json.loads(exp_path.read_text())
    if args.set != "all" and exp["holdout"] != (args.set == "holdout"):
        continue
    name = exp_path.name.replace("-expected.json", "")
    audit = json.loads((args.root / name / "audit.json").read_text())
    passages = {p["id"]: p["text"] for p in json.loads((args.root / name / "passages.json").read_text())["passages"]}
    for e in exp["claims"]:
        r = next(c for c in audit["claims"] if c["text"].startswith(e["starts"]))
        want, got = e["label"], short(r["label"])
        confusion[want, got] += 1
        if want != got:
            misses.append((name, r["id"], want, got, r["text"]))
        if want == "skipped":
            skip_n += 1
            skip_ok += got == "skipped"
            continue
        if e.get("common"):
            common.append((name, r["id"], want, got))
        truly, marked = want in FROM, got in FROM
        tp += truly and marked
        fp += marked and not truly
        fn += truly and not marked
        tn += not truly and not marked
        if truly and marked:
            texts = [norm(passages[l["passage"]]) for l in r["passages"] if l["verdict"] in FROM | {"quoted"}]
            src_ok += any(norm(s) in t for s in e.get("evidence", []) for t in texts)

metrics = {"precision": tp / max(tp + fp, 1), "recall": tp / max(tp + fn, 1),
           "source": src_ok / max(tp, 1), "false used": fp / max(fp + tn, 1)}
print(f"Set: {args.set}  ({tp + fp + fn + tn} claims scored, {skip_n} filler)\n")
for k, v in metrics.items():
    ok = v <= TARGETS[k] if k == "false used" else v >= TARGETS[k]
    print(f"  {k:<11} {v:6.1%}   target {'≤' if k == 'false used' else '≥'} {TARGETS[k]:.0%}   {'pass' if ok else 'FAIL'}")
print(f"  filler      {skip_ok}/{skip_n} skipped correctly")
print(f"\n  counts: {tp} true from-document, {fp} false from-document, {fn} missed, {tn} correctly not\n")
print("Confusion (rows = expected, columns = tool):")
print("  " + " " * 12 + "".join(f"{l[:11]:>12}" for l in LABELS))
for w in LABELS:
    print(f"  {w:<12}" + "".join(f"{confusion[w, g] or '·':>12}" for g in LABELS))
if common:
    print("\nCommon-knowledge claims that the document also states:")
    for name, cid, want, got in common:
        print(f"  {name} {cid}: expected {want}, tool {got}")
if args.details and misses:
    print("\nLabel differences:")
    for name, cid, want, got, text in misses:
        print(f"  {name} {cid}: expected {want:<11} tool {got:<11} {text[:90]}")
