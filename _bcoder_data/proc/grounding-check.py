"""
grounding-check — verify that identifiers in the LLM reply exist in map.txt.

Compares CamelCase and snake_case identifiers mentioned in the reply against
symbols known from the project map. Low overlap score signals the model may
be hallucinating class or function names that do not exist in the codebase.

Requires: /map index run first (produces .1bcoder/map.txt)
Designed for /proc on (persistent mode): runs silently after every reply.

Usage:
  /map index                      # build project map first (once per session)
  /proc on grounding-check        # enable persistent check
  /proc off                       # disable

Output:
  [grounding] ████████░░ 82%  (9/11 identifiers known)
  [grounding] WARNING: low grounding — verify before acting    (if score < 50%)
  [grounding] unknown: FooService, barMethod, ...              (suspicious names)

Examples:
  > /map index
  > /proc on grounding-check
  > ask "how does UserService.authenticate work?"
  # → high score: model is talking about real code symbols
  # → low score: model is hallucinating class/method names

  > ask "refactor PaymentProcessor to use the new gateway"
  # → before applying /edit or /patch, check the grounding score
  # → unknown identifiers in the reply = likely hallucination risk
"""
import sys, re, os

reply = sys.stdin.read()

workdir  = os.environ.get("BCODER_WORKDIR", os.getcwd())
map_path = os.path.join(workdir, ".1bcoder", "map.txt")
if not os.path.isfile(map_path):
    print("[grounding] no map.txt — run /map index first", file=sys.stderr)
    sys.exit(0)   # exit 0: not an error, just skip

map_text = open(map_path, encoding="utf-8", errors="ignore").read()

# known identifiers from map (4+ chars, mixed case = likely code identifier)
known = set(re.findall(r'\b([A-Za-z_][A-Za-z0-9_]{3,})\b', map_text))

# candidate identifiers in reply: CamelCase or snake_case, 4+ chars
words = set(re.findall(r'\b([A-Z][a-z]+[A-Za-z0-9]*|[a-z]+_[a-z_]+|[a-z]{4,})\b', reply))

# filter out common English words
STOPWORDS = {
    "this", "that", "with", "from", "have", "will", "would", "could", "should",
    "their", "there", "where", "when", "what", "which", "also", "then", "than",
    "just", "more", "some", "into", "your", "been", "were", "they", "each",
    "file", "code", "line", "function", "class", "method", "return", "value",
    "variable", "module", "import", "output", "input", "error", "call",
}
words = {w for w in words if w.lower() not in STOPWORDS}

if not words:
    sys.exit(0)

found   = words & known
unknown = words - known
score   = len(found) / len(words)

bar = "█" * int(score * 10) + "░" * (10 - int(score * 10))
print(f"[grounding] {bar} {score:.0%}  ({len(found)}/{len(words)} identifiers known)")

if unknown and score < 0.5:
    sample = sorted(unknown)[:6]
    print(f"[grounding] unknown: {', '.join(sample)}")
    print("[grounding] WARNING: low grounding — verify before acting")
