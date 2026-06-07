"""ctx_expand — auto-expand context window by 2048 when usage exceeds threshold.

When context usage reaches the threshold (default 50%), increases num_ctx by 2048
and applies it automatically. Prevents slow degradation from hitting the context wall
without manual /ctx N adjustments.

Usage:
  /proc on ctx_expand           # default threshold: 50%
  /proc on ctx_expand 70        # expand when context reaches 70%
  /proc off                     # disable

Output when triggered:
  ALERT: context at N% — expanding num_ctx 4096 → 6144
  ACTION: /ctx 6144

Examples:
  > /proc on ctx_expand
  # → context window grows automatically as conversation lengthens

  > /proc on ctx_expand 70
  > /agent -t -1 -y refactor auth.py
  # → long agent run expands its own context window as needed
"""
import sys
import os

threshold = int(sys.argv[1]) if len(sys.argv) > 1 else 50

ctx_pct  = int(os.environ.get("BCODER_CTX_PCT",  "0"))
ctx_max  = int(os.environ.get("BCODER_CTX_MAX",  "0"))
ctx_used = int(os.environ.get("BCODER_CTX_USED", "0"))

if ctx_pct >= threshold and ctx_max > 0:
    base    = max(ctx_max, ctx_used)
    new_ctx = base + 2048
    print(f"ALERT: context at {ctx_pct}% -- expanding num_ctx {ctx_max} -> {new_ctx} (used={ctx_used})")
    print(f"num_ctx={new_ctx}")
