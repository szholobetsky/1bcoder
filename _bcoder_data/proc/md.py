"""
md — render last LLM reply as Markdown in the terminal.

Uses the `rich` library to render headers, bold, italic, code blocks,
and tables with ANSI color directly in the terminal window.
No browser required — output stays in the same terminal session.

Requires: pip install rich

Usage:
  /proc run md              # render last LLM reply
  /proc run md <file>       # render any .md file

Examples:
  > ask "explain async/await with a code example"
  > /proc run md
  # → syntax-highlighted code block, bold headers, rendered inline

  > /proc run md README.md
  # → render any markdown file directly in the terminal

  > /proc run md CHANGELOG.md
  # → useful for browsing documentation without leaving 1bcoder

  See also: mdx — renders in browser with LaTeX equations and Mermaid diagrams
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
