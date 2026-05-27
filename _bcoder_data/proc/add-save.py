"""
add-save — inject ACTION:/save after each agent reply that contains a code block.

Activate before /agent run to automatically accumulate generated code into
a file across multiple agent turns. Fires only when the reply contains at
least one fenced code block (``` ... ```).

Usage:
  /proc on add-save myfile.py          # append each code block to myfile.py
  /proc on add-save output.txt -w      # overwrite each time instead of appending
  /proc off                            # deactivate after agent run

Arguments:
  argv[1]   target file (default: output.txt)
  argv[2]   save mode:
              -ab   append to bottom (default)
              -w    overwrite each time

Examples:
  > /proc on add-save results.py
  > /agent run code-gen.txt
  # → every agent turn that outputs code appends it to results.py
  # → one file, all code blocks collected in order

  > /proc on add-save report.md -w
  > /agent run summarise.txt
  # → each turn overwrites report.md with the latest version

  > /proc off
  # → stop collecting after the agent run is done
"""
import sys, re

target = sys.argv[1] if len(sys.argv) > 1 else "output.txt"
mode   = sys.argv[2] if len(sys.argv) > 2 else "-ab"

reply = sys.stdin.read()

if re.search(r'```', reply):
    print(f"ACTION: /save {target} {mode} code")
