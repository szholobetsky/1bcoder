"""action-required — gate proc that fails if the agent reply contains neither
an ACTION: command nor a completion signal.

If a likely command is found in the text (e.g. /read, /find), includes it
in the FAIL message as a formatting hint to help the model self-correct.

Usage in agent file:
    gates =
        action-required

Usage from session:
    /proc gate on action-required
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
