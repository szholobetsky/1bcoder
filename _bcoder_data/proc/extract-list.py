"""
extract-list — convert a vertical list in the LLM reply to a comma-separated line.

Finds the first bullet or numbered list block and joins items onto one line.
Useful for piping into /var, /parallel, or agent templates that expect
a comma-separated list rather than a rendered bullet list.

Recognized prefixes: -  *  +  1.  2)  (3)
Stops at first blank line after the list starts.

Usage:
  /proc run extract-list

Output params:
  list=<item1, item2, item3, ...>
  count=N

Examples:
  > ask "list the top 5 Python web frameworks"
  > /proc run extract-list
  # → Flask, Django, FastAPI, Tornado, Starlette
  # → list=Flask, Django, FastAPI, Tornado, Starlette
  # → count=5

  > /var set frameworks list
  # → saves the comma-separated string into the 'frameworks' variable

  > ask "what steps are needed to deploy this?"
  > /proc run extract-list
  > /var set steps list
  > /agent run deploy.txt   # agent sees the steps as a compact variable
"""
import sys, re

reply = sys.stdin.read()

# match bullet or numbered list lines
LIST_LINE = re.compile(
    r'^\s*(?:'
    r'\d+[.)]\s+'       # 1. or 1)
    r'|\(\d+\)\s+'      # (1)
    r'|[-*+]\s+'        # - * +
    r')\s*(.+)'
)

items = []
in_list = False

for line in reply.splitlines():
    m = LIST_LINE.match(line)
    if m:
        text = m.group(1).strip()
        # strip trailing punctuation like trailing comma/semicolon
        text = text.rstrip(',;')
        if text:
            items.append(text)
            in_list = True
    else:
        # stop at first blank line after list started
        if in_list and not line.strip():
            break

if not items:
    print("[extract-list] no list found", file=sys.stderr)
    sys.exit(1)

result = ", ".join(items)
print(result)
print(f"\ncount={len(items)}")
print(f"list={result}")
