"""ctx_cut — persistent guard: auto-cut context when usage exceeds threshold.

Reads BCODER_CTX_PCT (set by 1bcoder each turn) and emits ACTION:/ctx cut
when the context exceeds the configured percentage. Prevents context overflow
in long sessions without manual intervention.

Usage:
  /proc on ctx_cut            # default threshold: 90%
  /proc on ctx_cut 80         # cut when context reaches 80%
  /proc off                   # disable the guard

Output when triggered:
  ALERT: context at N% — running /ctx cut
  ACTION: /ctx cut

Examples:
  > /proc on ctx_cut 75
  # → context is compressed automatically once it hits 75%

  > /proc on ctx_cut
  > /agent run long-task.txt
  # → long agent run won't hit the hard context wall

  Difference from tempctx-cut:
    ctx_cut       — guards the main session context (BCODER_CTX_PCT)
    tempctx-cut   — guards the agent's temporary context (BCODER_AGENT_CTX_PCT)
"""
import sys
import os

threshold = int(sys.argv[1]) if len(sys.argv) > 1 else 90

ctx_pct = int(os.environ.get("BCODER_CTX_PCT", "0"))

if ctx_pct >= threshold:
    print(f"ALERT: context at {ctx_pct}% — running /ctx cut")
    print("ACTION: /ctx cut")
