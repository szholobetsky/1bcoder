"""
extract-files — extract file paths mentioned in last LLM reply.

stdout:
  one filename per line
  file=<first_file>  (param for ACTION substitution)
  ACTION: /read <first_file>   (if exactly one file found)
"""
import sys, re

reply = sys.stdin.read()

# match paths like auth.py, src/auth.py, com/example/Auth.java, config.yaml ...
candidates = re.findall(r'\b[\w./\\-]+\.(?:py|js|ts|java|cs|go|rs|cpp|c|h|rb|php|kt|'
                        r'sql|yaml|yml|toml|json|xml|sh|bat|md|txt|cfg|conf|env)\b', reply)

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
