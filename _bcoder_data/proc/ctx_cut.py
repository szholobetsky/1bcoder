"""ctx_cut — persistent guard: auto-cut context when usage exceeds threshold.

Usage:
    /proc on ctx_cut            # default threshold: 90%
    /proc on ctx_cut 80         # custom threshold %

Output:
    ACTION: /ctx cut            — when ctx exceeds threshold
"""
import sys
import os

threshold = int(sys.argv[1]) if len(sys.argv) > 1 else 90

ctx_pct = int(os.environ.get("BCODER_CTX_PCT", "0"))

if ctx_pct >= threshold:
    print(f"ALERT: context at {ctx_pct}% — running /ctx cut")
    print("ACTION: /ctx cut")
