"""expr-gate — universal gate proc: FAILs unless a Python boolean expression
evaluates to True. Meant to be used with {{var}} substitution — 1bcoder's own
{{var}} templating (already generic, applied in _route() before any command
dispatch) replaces placeholders with live session-variable values BEFORE this
proc ever runs, so the proc itself just evals plain, already-substituted text.
No new templating logic needed here — reuses the existing mechanism as-is.

Arguments:
  argv[1]   Python boolean expression to evaluate (required), after {{var}}
            substitution, e.g.:  "9 <= 0"   "'foo' in 'foo,bar'"

Output:
  FAIL: <message>    expression is False, or raised an error → gate fails
  (no output)        expression is truthy → gate passes

Notes:
  - Evaluated with eval(expr, {"__builtins__": {}}) — blocks imports/file
    access as a cheap guard, not a hard sandbox: only run scripts you wrote
    yourself, same trust level as any other /proc.
  - Session variables are always strings — cast explicitly where needed:
    int({{i}}) <= 0     not     {{i}} <= 0
  - Same PASS/FAIL semantics as every other gate proc (pattern-gate,
    action-required, ...): FAIL: line = fail, silent = pass. Works identically
    as a /script :IF condition source and as a /proc gate on step-retry
    validator — same expression, same output convention, either place.

Usage in a script (once :IF/labels exist):
  :IF /proc expr-gate "int({{i}}) <= 0" :LOOP_LABEL
  :IF /proc expr-gate "'{{query}}' != 'WRONG'" :EXIT

Usage as an agent gate:
  /proc gate on expr-gate "'{{status}}' == 'ready'"

Examples:
  > /proc run expr-gate "3 in [1, 2, 3]"
  # → no output (passes)
  > /proc run expr-gate "int(9) <= 0"
  # → FAIL: condition is False: int(9) <= 0
  > /proc run expr-gate "1 / 0"
  # → FAIL: expression error: division by zero
"""
import sys

# Explicit allow-list, not a bare {} — an empty __builtins__ blocks everything,
# including the int()/float() casts session vars need (they're always
# strings). Still excludes __import__, open, exec, eval, etc. by omission.
_SAFE_BUILTINS = {
    "int": int, "float": float, "str": str, "bool": bool,
    "len": len, "abs": abs, "min": min, "max": max, "sum": sum,
    "sorted": sorted, "round": round, "any": any, "all": all,
    "set": set, "list": list, "tuple": tuple, "dict": dict,
}

expr = sys.argv[1] if len(sys.argv) > 1 else ""

if not expr:
    sys.exit(0)

try:
    result = eval(expr, {"__builtins__": _SAFE_BUILTINS})
except Exception as e:
    print(f"FAIL: expression error: {e}")
    sys.exit(0)

if not result:
    print(f"FAIL: condition is False: {expr}")
