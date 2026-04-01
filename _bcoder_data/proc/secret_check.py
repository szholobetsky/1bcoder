"""secret_check — persistent guard: alert when reply contains sensitive keywords.

Usage:
    /proc on secret_check                      # default keyword list
    /proc on secret_check client=acme,token    # add custom keywords (comma-separated)

Extend KEYWORDS below or pass extras as argv.
Only alerts — does not block. The user decides what to do next.
"""
import sys
import re
import os

KEYWORDS = [
    "google", "microsoft", "anthropic", "openai", "amazon", "apple",
    "facebook", "meta", "nvidia", "oracle", "salesforce", "stripe",
]

# extra keywords passed as "word1,word2" argument
if len(sys.argv) > 1:
    for arg in sys.argv[1:]:
        KEYWORDS += [k.strip() for k in arg.split(",") if k.strip()]

text = sys.stdin.read()

found = [kw for kw in KEYWORDS if re.search(rf'\b{re.escape(kw)}\b', text, re.IGNORECASE)]

if found:
    print(f"ALERT: sensitive keyword(s) in reply: {', '.join(found)} — review before saving or sharing")
