"""rude_words — persistent guard: alert when LLM reply contains profanity.

Designed for shared or professional environments where LLM output must stay
clean before it is displayed, logged, or forwarded. Only alerts — does not
block. The user decides what to do next.

Language sets:
  (default)   English profanity list
  ua          + Ukrainian politically charged terms

Usage:
  /proc on rude_words            # English word list
  /proc on rude_words ua         # add Ukrainian word list
  /proc off                      # disable

To add custom words: edit WORDS_EN or WORDS_UA in the file itself.

Examples:
  > /proc on rude_words
  > ask "explain this error in plain language"
  # → ALERT if the model uses profanity in the explanation

  > /proc on rude_words ua
  # → also watches for Ukrainian politically charged terms in replies

  > /proc on rude_words
  > /agent run customer-support.txt
  # → guard replies before they are shown to end users
"""
import sys
import re

lang = sys.argv[1] if len(sys.argv) > 1 else "en"

WORDS_EN = [
    r'\bfuck\b', r'\bshit\b', r'\bbitch\b', r'\basshole\b',
    r'\bdamn\b', r'\bcrap\b', r'\bpiss\b', r'\bdick\b',
]

WORDS_UA = [
    r'\bпутін\b', r'\bросі\b', r'\bрадян\b', r'\bкомун\b', r'\bросc\b',
    r'и', r'\bмоск\b', r'\bднр\b',
]

patterns = WORDS_EN[:]
if lang == "ua":
    patterns += WORDS_UA

reply = sys.stdin.read()

for pat in patterns:
    if re.search(pat, reply, re.IGNORECASE):
        print("ALERT: reply contains profanity — review before sharing or saving")
        break
