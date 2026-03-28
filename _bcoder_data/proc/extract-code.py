"""
extract-code — extract code blocks from last LLM reply.

Finds all fenced code blocks (``` ... ```) and prints them cleanly.
If exactly one block is found and a filename is mentioned nearby,
emits ACTION:/save <file> so the code can be saved directly.

stdout params:
  code_count=N          number of blocks found
  lang=<language>       language of first block (if specified)
  file=<filename>       nearby filename (if detected, single block only)

ACTION: /save <file>    emitted when: 1 block + 1 filename clearly mentioned
"""
import sys, re

reply = sys.stdin.read()

# ── extract fenced code blocks ─────────────────────────────────────────────
# matches ```lang\ncode\n``` or ~~~lang\ncode\n~~~
FENCE = re.compile(r'```(\w*)\n(.*?)```|~~~(\w*)\n(.*?)~~~', re.DOTALL)
blocks = []
for m in FENCE.finditer(reply):
    lang = (m.group(1) or m.group(3) or "").strip()
    code = (m.group(2) or m.group(4) or "").rstrip()
    if code:
        blocks.append((lang, code, m.start()))

if not blocks:
    print("[extract-code] no code blocks found", file=sys.stderr)
    sys.exit(1)

# ── print blocks ────────────────────────────────────────────────────────────
for i, (lang, code, _) in enumerate(blocks, 1):
    label = f" ({lang})" if lang else ""
    print(f"-- block {i}{label} --")
    print(code)
    print()

print(f"code_count={len(blocks)}")
if blocks[0][0]:
    print(f"lang={blocks[0][0]}")

# ── try to find a filename near the first block ────────────────────────────
if len(blocks) == 1:
    _, _, pos = blocks[0]
    # look in 200 chars before the block
    context_before = reply[max(0, pos - 200):pos]
    FILE_RE = re.compile(
        r'\b([\w./\\-]+\.(?:py|js|ts|java|cs|go|rs|cpp|c|h|rb|php|kt|'
        r'sql|yaml|yml|toml|json|xml|sh|bat|md|txt|cfg|conf))\b'
    )
    nearby = FILE_RE.findall(context_before)
    if not nearby:
        # also check after block
        _, code, _ = blocks[0]
        after_start = reply.find(code) + len(code)
        context_after = reply[after_start:after_start + 100]
        nearby = FILE_RE.findall(context_after)

    if len(nearby) == 1:
        fname = nearby[0]
        print(f"file={fname}")
        print(f"ACTION: /save {fname}")
    elif len(nearby) > 1:
        # deduplicate, prefer last one before the block (most likely "update X")
        seen, deduped = set(), []
        for f in nearby:
            if f not in seen:
                seen.add(f)
                deduped.append(f)
        print(f"file={deduped[-1]}")
        # multiple files — don't auto-action, just report
        print(f"[extract-code] multiple files mentioned: {', '.join(deduped)}")
