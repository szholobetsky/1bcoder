"""
collect-files — extract file paths from LLM reply and append to a list file.

Good for /proc on (persistent mode): accumulates mentioned files across
multiple turns. Results go to .1bcoder/collected-files.txt

After collection, use:
  /read .1bcoder/collected-files.txt   to review
  /parallel /read <files>              to load all at once
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
