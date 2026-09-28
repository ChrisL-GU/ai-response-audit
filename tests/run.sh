#!/usr/bin/env bash
# Run the audit on the test answers.   tests/run.sh WORK_ROOT tune|holdout|all
# Ingests each test document once, then audits each answer in WORK_ROOT/<answer>/.
set -euo pipefail
root=$1; set=${2:-tune}
here=$(cd "$(dirname "$0")/.." && pwd)
scripts=$here/skills/audit/scripts
declare -A doc=(
  [ava]="$here/documents/Ava Knap Story.docx"
  [mh]="$here/documents/Magnifica Humanitas Abridged.docx"
  [tcc]="$here/documents/TragedyOfCognitiveCommons.pdf")
declare -A title=(
  [ava]="Exploring the Mind: Gonzaga Senior Ava Knap Blends Neuroscience, Art, and Athletics"
  [mh]="Magnifica Humanitas: Encyclical Letter of Pope Leo XIV on Safeguarding the Human Person in the Time of Artificial Intelligence (abridged)"
  [tcc]="The Tragedy of the Cognitive Commons: How AI Could Disrupt the Regeneration of Professional Expertise (Lovett, 2026)")
for exp in "$here"/tests/fixtures/*-expected.json; do
  name=$(basename "$exp" -expected.json); key=${name%%-*}
  holdout=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['holdout'])" "$exp")
  [[ $set == all || ( $set == holdout && $holdout == True ) || ( $set == tune && $holdout == False ) ]] || continue
  [[ -f $root/_docs/$key/passages.json ]] || uv run -q "$scripts/ingest.py" "${doc[$key]}" "$root/_docs/$key" --title "${title[$key]}" >/dev/null
  mkdir -p "$root/$name" && cp "$root/_docs/$key/passages.json" "$root/$name/"
  echo "== $name" >&2
  uv run -q "$scripts/audit.py" "$root/$name" "$here/tests/fixtures/$name-answer.md" >/dev/null
done
