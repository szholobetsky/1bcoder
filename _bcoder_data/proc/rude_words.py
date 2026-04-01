"""rude_words — persistent guard: alert when LLM reply contains profanity.

Usage:
    /proc on rude_words            # English word list
    /proc on rude_words ua         # add Ukrainian word list

The guard only alerts — it does not block. The user decides what to do next.
Extend WORDS below with your own terms.
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
