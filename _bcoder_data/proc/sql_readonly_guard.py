"""sql_readonly_guard — guard against destructive and mutating SQL statements.

Two modes depending on how it is invoked:

  Hook mode (/hook before run)   stdin = shell command about to be executed
    → BLOCK if destructive DML/DDL found (prevents /run from executing it)

  Proc mode (/proc on)           stdin = LLM reply text
    → ALERT if reply suggests mutating or destructive SQL (advisory only)

Destructive patterns (BLOCK in hook mode, ALERT in proc mode):
  DELETE FROM, DROP TABLE, DROP DATABASE, TRUNCATE TABLE, ALTER TABLE

Mutating patterns (ALERT only in both modes):
  UPDATE ..., INSERT INTO

Usage:
  /hook before run sql_readonly_guard.py    # block before /run executes SQL
  /proc on sql_readonly_guard               # alert if LLM reply contains write SQL
  /proc off                                 # disable proc mode

Examples:
  > /hook before run sql_readonly_guard.py
  > /run SELECT * FROM users
  # → allowed, passes through

  > /run DELETE FROM orders WHERE 1=1
  # → BLOCK: destructive DML detected — confirm manually before running

  > /proc on sql_readonly_guard
  > ask "how do I clean up duplicate rows?"
  # → ALERT if the LLM replies with DELETE or TRUNCATE

  Combined (both hook and proc active):
  > /hook before run sql_readonly_guard.py
  > /proc on sql_readonly_guard
  # → warns during planning AND blocks at execution
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
