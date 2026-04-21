"""
md — render last LLM reply as Markdown in the terminal.

Requires: pip install rich
Usage:    /proc run md
"""
import sys

if len(sys.argv) > 1 and sys.argv[1]:
    try:
        with open(sys.argv[1], encoding="utf-8") as _f:
            reply = _f.read()
    except OSError as e:
        print(f"[md] cannot read {sys.argv[1]}: {e}", file=sys.stderr)
        sys.exit(1)
else:
    reply = sys.stdin.buffer.read().decode("utf-8", errors="replace")

import io
from rich.console import Console
from rich.markdown import Markdown

Console(file=io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")).print(Markdown(reply))
