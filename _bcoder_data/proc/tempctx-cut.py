"""tempctx-cut — persistent guard for agent loops: cut agent context when near limit.

Reads BCODER_AGENT_CTX_PCT (set per turn during /agent run). Falls back to
BCODER_CTX_PCT if not running inside an agent. Emits ACTION:/tempctx cut
to compress the agent's working context without touching the main session.

Usage:
  /proc on tempctx-cut           # default threshold: 75%
  /proc on tempctx-cut 60        # custom threshold %
  /proc off                      # disable

Output when triggered:
  ALERT: agent context at N% — running /tempctx cut
  ACTION: /tempctx cut

Examples:
  > /proc on tempctx-cut 70
  > /agent run deep-analysis.txt
  # → agent's temporary context is compressed automatically at 70%

  > /proc on tempctx-cut
  > /proc on ctx_cut 90
  # → dual guard: tempctx for agent loop + ctx_cut for main session

  Difference from ctx_cut:
    ctx_cut       — guards the main session context (BCODER_CTX_PCT)
    tempctx-cut   — guards the agent's temporary context (BCODER_AGENT_CTX_PCT)
"""
import sys
import os

threshold = int(sys.argv[1]) if len(sys.argv) > 1 else 75

agent_pct = int(os.environ.get("BCODER_AGENT_CTX_PCT", os.environ.get("BCODER_CTX_PCT", "0")))

if agent_pct >= threshold:
    print(f"ALERT: agent context at {agent_pct}% — running /tempctx cut")
    print("ACTION: /tempctx cut")
