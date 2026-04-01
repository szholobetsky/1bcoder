"""pattern-gate — universal gate proc that fails if reply matches a regexp.

Usage in agent file:
    gates =
        pattern-gate "from dual" "no FROM DUAL allowed"
        pattern-gate "select \*" "use explicit columns"
        pattern-gate "@Query" "use CriteriaBuilder only"

Usage from session:
    /proc gate on pattern-gate "select \*" "use explicit columns"

Arguments:
    argv[1] — regexp pattern to search for
    argv[2] — failure message (optional, defaults to pattern name)
"""
import sys
import re

pattern = sys.argv[1] if len(sys.argv) > 1 else ""
message = sys.argv[2] if len(sys.argv) > 2 else f"forbidden pattern found: {pattern}"

if not pattern:
    sys.exit(0)

reply = sys.stdin.read()

if re.search(pattern, reply, re.IGNORECASE):
    print(f"FAIL: {message}")
