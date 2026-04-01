"""scan-save — persistent proc for /scan agent: append each reply to scan_result.txt.

Fires after every agent turn. Skips turns that contain only "nothing".

Usage (automatic when listed in scan.txt procs section):
    /proc on scan-save                         # default output file
    /proc on scan-save .1bcoder/scan_result.txt  # custom output file
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
