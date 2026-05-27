"""
collect-files — accumulate file paths from LLM replies across multiple turns.

Appends newly mentioned file paths to .1bcoder/collected-files.txt after
each reply. Deduplicates — same path is never written twice. Silent on
turns with no paths; designed for /proc on (persistent mode).

Usage:
  /proc on  collect-files                          # default output file
  /proc on  collect-files .1bcoder/my-list.txt     # custom output path
  /proc off                                        # stop accumulating

After collection:
  /read .1bcoder/collected-files.txt     # review the full collected list
  /parallel /read <files>                # load all collected files at once

Examples:
  > /proc on collect-files
  > ask "which files handle authentication?"
  > ask "which files are involved in the payment flow?"
  > /proc off
  > /read .1bcoder/collected-files.txt
  # → combined deduplicated list from both replies

  Difference from extract-files:
    extract-files  — one-shot, shows files, auto-opens single result
    collect-files  — persistent, silently accumulates across many turns
"""
import sys, re, os

reply = sys.stdin.read()

candidates = re.findall(r'\b[\w./\\-]+\.(?:py|js|ts|java|cs|go|rs|cpp|c|h|rb|php|kt|'
                        r'sql|yaml|yml|toml|json|xml|sh|bat|cfg|conf|env)\b', reply)

seen = set()
files = [f for f in candidates if not (f in seen or seen.add(f))]

if not files:
    sys.exit(0)   # silent: nothing to collect

out = os.path.join(os.getcwd(), ".1bcoder", "collected-files.txt")
os.makedirs(os.path.dirname(out), exist_ok=True)

# read existing to avoid duplicates
existing = set()
if os.path.isfile(out):
    existing = set(open(out).read().splitlines())

new_files = [f for f in files if f not in existing]
if not new_files:
    sys.exit(0)

with open(out, "a", encoding="utf-8") as f:
    for path in new_files:
        f.write(path + "\n")

print(f"[collect-files] +{len(new_files)} → {out}")
for f in new_files:
    print(f"  {f}")
