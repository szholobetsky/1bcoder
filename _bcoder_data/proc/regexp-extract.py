"""
regexp-extract — extract all regex matches from last LLM reply.

Usage:
  /proc run regexp-extract <pattern> [-i] [-u] [-g N]

Arguments:
  pattern   Python regex pattern (required)
  -i        case-insensitive match
  -u        unique matches only (deduplicate)
  -g N      return capture group N instead of full match (default: 0 = full)

stdout:
  one match per line
  count=N
  matches=match1,match2,...   (param for downstream use)
  first=<first match>         (param for ACTION substitution)

Examples:
  /proc run regexp-extract \\b[0-9]{3}\\b
  /proc run regexp-extract \\b[A-Z][a-z]+Service\\b -u
  /proc run regexp-extract "def (\\w+)\\(" -g 1 -u
"""
import sys, re

# ── parse args ───────────────────────────────────────────────────────────────
args = sys.argv[1:]
if not args:
    print("usage: /proc run regexp-extract <pattern> [-i] [-u] [-g N]", file=sys.stderr)
    sys.exit(1)

pattern = args[0]
flags = 0
unique = False
group = 0

i = 1
while i < len(args):
    a = args[i]
    if a == "-i":
        flags |= re.IGNORECASE
    elif a == "-u":
        unique = True
    elif a == "-g" and i + 1 < len(args):
        i += 1
        try:
            group = int(args[i])
        except ValueError:
            print(f"[regexp-extract] -g requires an integer, got: {args[i]}", file=sys.stderr)
            sys.exit(1)
    i += 1

# ── match ────────────────────────────────────────────────────────────────────
reply = sys.stdin.read()

try:
    compiled = re.compile(pattern, flags)
except re.error as e:
    print(f"[regexp-extract] invalid pattern: {e}", file=sys.stderr)
    sys.exit(1)

raw_matches = compiled.findall(reply)

# findall returns strings when no groups, tuples when groups present
def pick(m):
    if isinstance(m, tuple):
        try:
            return m[group - 1] if group > 0 else "".join(m)
        except IndexError:
            return ""
    return m

matches = [pick(m) for m in raw_matches]
matches = [m for m in matches if m]  # drop empty

if unique:
    seen = set()
    deduped = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    matches = deduped

# ── output ───────────────────────────────────────────────────────────────────
if not matches:
    print(f"[regexp-extract] no matches for: {pattern}", file=sys.stderr)
    sys.exit(1)

for m in matches:
    print(m)

print(f"\ncount={len(matches)}")
print(f"first={matches[0]}")
print(f"matches={','.join(matches)}")
