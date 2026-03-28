"""
extract-list — convert a vertical list in the last LLM reply to a comma-separated line.

Finds the first bullet/numbered list block in the reply and joins items
into: item1, item2, item3 ...

Strips:  - / * / 1. / 2) / (3) prefixes
Skips:   blank lines, headers, non-list paragraphs

stdout params:
  list=<comma separated items>
  count=N
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
