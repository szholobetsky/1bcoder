"""pattern-gate — universal gate proc that fails if reply matches a regexp.

When used as a gate, a FAIL causes the agent to retry the current step
with the failure message injected as feedback — forcing self-correction.
Works in both agent files (gates = section) and interactive sessions.

Arguments:
  argv[1]   regexp pattern to search for in the reply (required)
  argv[2]   failure message sent back to the agent (default: pattern text)

Output:
  FAIL: <message>    if pattern matches → agent retries current step
  (no output)        if pattern does not match → step passes

Usage in agent file:
  gates =
      pattern-gate "from dual"           "no FROM DUAL in queries"
      pattern-gate "select \*"           "use explicit column names"
      pattern-gate "@Query"              "use CriteriaBuilder only"
      pattern-gate "console\.log"        "remove debug logs before committing"
      pattern-gate "TODO|FIXME|HACK"     "resolve all placeholders before finishing"

Usage from session:
  /proc gate on pattern-gate "select \*" "use explicit columns"
  /proc gate on pattern-gate "print("    "use logging, not print"

Examples:
  > /proc gate on pattern-gate "TODO" "no TODOs allowed in output"
  > ask the agent to write production code
  # → if agent outputs TODO, the step is retried with the FAIL message

  Multiple gates in one agent:
    gates =
        action-required
        pattern-gate "I would suggest" "no suggestions — act directly"
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
