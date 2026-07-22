"""tool-allowlist-gate — generic PASS/FAIL gate: reject any ACTION whose
command is not on an explicit allow-list.

An agent file's `tools =` section is informational only — nothing in
1bcoder's agent loop (_run_agent_loop / _agent_exec) checks a proposed
ACTION against it before executing it. This gate is the real, enforced
version: gate procs run BEFORE the action-execution loop (chat.py's
_run_agent_loop runs gates, then extracts and executes ACTION lines), so a
FAIL here rejects the whole turn and retries it with feedback — nothing in
that reply ever reaches _agent_exec.

Built for deepagent_architect's research step, paired with the `research`
agent (see _bcoder_data/agents/research.txt) — but generic. Usable with any
agent that needs a hard restriction instead of relying on model obedience,
e.g. /ask itself, or a future agent with a different allow-list entirely.

Allow-list format: comma-separated entries, each a bare command ("read") or
a "command subcommand" pair ("map find") to restrict to that subcommand
only. A bare entry matches that command with any arguments; a two-word
entry matches only that specific subcommand. Quote the whole spec as one
argument since it contains spaces.

Usage:
  /proc gate on tool-allowlist-gate
      (no args — uses the default list, matching the `research` agent)
  /proc gate on tool-allowlist-gate "read,find,tree,map find,map trace"

Output:
  FAIL: [tool-allowlist] not in allow-list: /edit          → retries turn
  (no output) = every ACTION in the reply is allowed
"""
import sys
import re

_ACTION_RE = re.compile(r'ACTION:\s*(/\S+(?:[ \t]+[^\n]+)?)', re.MULTILINE)

_DEFAULT_ALLOW = (
    "read,readln,tree,find,"
    "map find,map trace,map deps,"
    "web search,web fetch,"
    "rag search,rag list,rag status,"
    "flow simargl_files,"
    "flow glossary show,flow glossary extract,flow glossary find"
)


def _parse_allow(spec: str) -> list:
    return [tuple(p.strip().split()) for p in spec.split(",") if p.strip()]


def _allowed(action: str, allow_patterns: list) -> bool:
    tokens = action.strip().lstrip("/").split()
    if not tokens:
        return False
    for pattern in allow_patterns:
        if tuple(tokens[:len(pattern)]) == pattern:
            return True
    return False


reply = sys.stdin.read()

spec = " ".join(sys.argv[1:]).strip()
allow_spec = spec if spec else _DEFAULT_ALLOW
allow_patterns = _parse_allow(allow_spec)

actions = _ACTION_RE.findall(reply)
if not actions:
    sys.exit(0)  # no ACTION in this reply -- nothing to check

blocked = [a for a in actions if not _allowed(a, allow_patterns)]
if blocked:
    cmds = ", ".join(a.split()[0] for a in blocked)
    print(f"FAIL: [tool-allowlist] not in allow-list: {cmds}")
    print(f"  allowed: {allow_spec}")
