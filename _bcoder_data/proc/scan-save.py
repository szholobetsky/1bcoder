"""scan-save — persistent proc for /scan agents: append each reply to a file.

Fires after every agent turn. Skips empty replies or turns that contain
only "nothing" (common agent sentinel for "no findings this turn").
Used automatically in /scan-type agent files; can be activated manually.

Usage:
  /proc on scan-save                               # default: .1bcoder/scan_result.txt
  /proc on scan-save .1bcoder/audit.txt            # custom output file
  /proc off                                        # stop accumulating

Output per matching turn:
  ACTION: /save <file> -ab    # appends the reply to the target file

Examples:
  > /proc on scan-save .1bcoder/audit.txt
  > /agent run security-scan.txt
  # → each turn's findings are appended to audit.txt
  # → turns that output "nothing" are skipped silently

  > /read .1bcoder/scan_result.txt
  # → review all collected findings after the agent run

  Typical scan agent file (scan.txt):
    procs = scan-save
    ...
"""
import os
import sys

target = sys.argv[1] if len(sys.argv) > 1 else ".1bcoder/scan_result.txt"
reply  = sys.stdin.read().strip()

if reply and reply.lower() != "nothing":
    # Print path on first save so the user knows where to find the output
    if not os.path.exists(target):
        print(f"ALERT: scan output → {target}", file=sys.stderr)
    print(f"ACTION: /save {target} -ab")
