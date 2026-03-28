"""
md — render last LLM reply as Markdown in the terminal.

Requires: pip install rich
Usage:    /proc run md
"""
import sys

reply = sys.stdin.buffer.read().decode("utf-8", errors="replace")

from rich.console import Console
from rich.markdown import Markdown

Console().print(Markdown(reply))
