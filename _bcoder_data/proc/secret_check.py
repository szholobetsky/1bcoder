"""secret_check — persistent guard: alert when reply contains sensitive keywords.

Watches LLM replies for well-known company/brand names and custom terms that
may indicate the model is leaking training examples or client-specific data.
Only alerts — does not block. The user decides what to do next.

Built-in keywords: google, microsoft, anthropic, openai, amazon, apple,
  facebook, meta, nvidia, oracle, salesforce, stripe

Usage:
  /proc on secret_check                         # built-in keyword list
  /proc on secret_check client=acme,invoice     # add custom keywords
  /proc off                                     # disable

Arguments:
  argv[n]   one or more comma-separated extra keywords: word1,word2,word3

Examples:
  > /proc on secret_check client=mycompany,projectname
  > ask about authentication flow
  # → ALERT if reply mentions "mycompany" or "projectname"

  > /proc on secret_check apikey,password,secret
  # → alert if LLM unexpectedly outputs credential-like terms

  Extend the KEYWORDS list in the file itself for persistent custom terms.
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
