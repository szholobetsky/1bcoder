"""action-required — gate that fails if agent reply has no ACTION: and no done signal.

Forces the agent to always either issue a concrete command (ACTION: /...)
or explicitly declare task completion. Prevents the agent from stalling in
a "thinking out loud" loop without taking any real action.

If a likely command is found in the text (e.g. /read, /find used mid-sentence),
the FAIL message includes a formatting hint to help the model self-correct.

Recognized completion signals (case-insensitive):
  "task is done"  "task is complete"  "all done"  "done."  "finished."
  "no further action"  "nothing more to do"

Usage in agent file:
  gates =
      action-required

Usage from session:
  /proc gate on action-required

Output:
  FAIL: no ACTION: command and no completion signal [— did you mean: ACTION: /read]
  FAIL: use ACTION: /command to act or write 'task is done' to finish

Examples:
  > /proc gate on action-required
  > /agent run refactor.txt
  # → agent must issue a command or say done each turn
  # → pure commentary is rejected and the step retried

  Pair with pattern-gate for stricter agents:
    gates =
        action-required
        pattern-gate "I need to" "no planning — act or say done"
        pattern-gate "I would"   "no conditional thoughts — act or say done"
"""
import sys, re

DONE_PHRASES = [
    "task is done", "task is complete", "task complete",
    "all done", "done.", "complete.", "finished.",
    "no further action", "nothing more to do",
]

KNOWN_CMDS = [
    "read", "readln", "find", "edit", "insert", "save", "patch", "fix",
    "run", "tree", "map", "agent", "ask", "script", "diff", "ctx",
]

reply = sys.stdin.read()

has_action = bool(re.search(r'^ACTION:\s*/', reply, re.MULTILINE))
has_done   = any(p in reply.lower() for p in DONE_PHRASES)

if not has_action and not has_done:
    # look for a likely command at the START of a line (intentional invocation)
    # ignore mentions mid-sentence like "they use /read to..."
    suggestion = ""
    for cmd in KNOWN_CMDS:
        if re.search(rf'(?<![A-Za-z])/{cmd}\b', reply):
            suggestion = f" — did you mean: ACTION: /{cmd}"
            break

    print(f"FAIL: no ACTION: command and no completion signal{suggestion}")
    print(f"FAIL: use ACTION: /command to act or write 'task is done' to finish")
