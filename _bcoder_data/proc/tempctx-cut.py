"""tempctx-cut — persistent guard for agent loops: cut agent context when near limit.

Reads BCODER_AGENT_CTX_PCT set by 1bcoder when running inside an agent loop.
Falls back to BCODER_CTX_PCT if not in agent mode.

Usage:
    /proc on tempctx-cut          # default threshold: 75%
    /proc on tempctx-cut 60       # custom threshold %
"""
import sys
import os

threshold = int(sys.argv[1]) if len(sys.argv) > 1 else 75

agent_pct = int(os.environ.get("BCODER_AGENT_CTX_PCT", os.environ.get("BCODER_CTX_PCT", "0")))

if agent_pct >= threshold:
    print(f"ALERT: agent context at {agent_pct}% — running /tempctx cut")
    print("ACTION: /tempctx cut")
