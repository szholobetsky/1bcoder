"""sql_readonly_guard — guard for write SQL statements (DML + DDL).

Two modes depending on how it is invoked:

  /hook before run sql_readonly_guard.py   — stdin = shell command being executed
                                             BLOCK if write SQL found

  /proc on sql_readonly_guard              — stdin = LLM reply text
                                      ALERT if reply suggests destructive or mutating SQL
"""
import sys
import re

text = sys.stdin.read()

# Destructive — BLOCK when used as hook, ALERT when used as proc
DESTRUCTIVE = re.compile(
    r'\b(DELETE\s+FROM|DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE\s+TABLE|ALTER\s+TABLE)\b',
    re.IGNORECASE,
)

# Mutating — always ALERT (even in proc mode)
MUTATING = re.compile(
    r'\b(UPDATE\s+\w|INSERT\s+INTO)\b',
    re.IGNORECASE,
)

import os
is_hook = bool(os.environ.get("BCODER_EVENT"))

match_d = DESTRUCTIVE.search(text)
match_m = MUTATING.search(text)

if match_d:
    if is_hook:
        print(f"BLOCK: destructive DML detected ({match_d.group().strip()}) — confirm manually before running")
    else:
        print(f"ALERT: reply suggests destructive SQL ({match_d.group().strip()}) — review before running")
elif match_m:
    print(f"ALERT: reply suggests mutating SQL ({match_m.group().strip()}) — review before applying")
