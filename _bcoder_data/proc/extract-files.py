"""
extract-files — extract file paths mentioned in last LLM reply.

Finds all file-looking tokens (path.ext) in the LLM text.
If exactly one file is found, emits ACTION:/read <file> so the file
is opened automatically (in /proc run mode only — not in /proc on).

Usage:
  /proc run extract-files
  /proc on  extract-files        # persistent: show files after every reply

Output params:
  file=<first_file>              for /var set or ACTION substitution
  ACTION: /read <first_file>     auto-opens single result (run mode only)

Supported extensions:
  .py .js .ts .tsx .jsx .mjs .vue .java .cs .go .rs .cpp .c .h .rb .php .kt .kts .scala
  .css .scss .less .html .snap .properties .gradle .ddl .proto .graphql
  .sql .yaml .yml .toml .json .xml .sh .bat .md .txt .cfg .conf .env .ini

Examples:
  > ask "which file handles user login?"
  > /proc run extract-files
  # → auth.py  (opens automatically if only one result)

  > ask "list all files we need to change for this feature"
  > /proc run extract-files
  # → prints each file path; file=<first one>

  > /proc on extract-files
  # → shows mentioned file paths after every LLM turn
  # → combine with /proc on collect-files to accumulate across turns
"""
import sys, re

reply = sys.stdin.read()

# match paths like auth.py, src/auth.py, com/example/Auth.java, config.yaml ...
# front-end (tsx, css, snapshots), JVM build and resource files included: a list of files to
# change for a web feature is mostly those
candidates = re.findall(r'\b[\w./\\-]+\.(?:py|js|ts|tsx|jsx|mjs|vue|java|cs|go|rs|cpp|c|h|rb|php|kt|kts|scala|'
                        r'css|scss|less|html|snap|properties|gradle|ddl|proto|graphql|'
                        r'sql|yaml|yml|toml|json|xml|sh|bat|md|txt|cfg|conf|env|ini)\b', reply)

seen = set()
files = [f for f in candidates if not (f in seen or seen.add(f))]

if not files:
    print("[extract-files] no file paths found", file=sys.stderr)
    sys.exit(1)

for f in files:
    print(f)

print(f"\n[extract-files] {len(files)} file(s) found")
print(f"file={files[0]}")

if len(files) == 1:
    print(f"ACTION: /read {files[0]}")
