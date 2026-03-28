"""
add-save — inject ACTION:/save after each agent reply that contains a code block.

Usage (before /agent run):
  /proc on add-save mytest.java       # append each code block to mytest.java
  /proc on add-save output.txt -w     # overwrite each time instead of appending
  /proc off                           # deactivate after agent run

Arguments:
  argv[1]  target file  (default: output.txt)
  argv[2]  mode         -ab = append to bottom (default), -w = overwrite
"""
import sys, re

target = sys.argv[1] if len(sys.argv) > 1 else "output.txt"
mode   = sys.argv[2] if len(sys.argv) > 2 else "-ab"

reply = sys.stdin.read()

if re.search(r'```', reply):
    print(f"ACTION: /save {target} {mode} code")
