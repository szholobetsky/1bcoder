"""autobkup — before-hook that silently backs up a file before editing.

Creates <file>.bkup before /save, /patch, or /fim.
If <file>.bkup already exists, rotates it to <file>.bkup(1), <file>.bkup(2), ...
Always passes — never outputs BLOCK:, so the command always proceeds.

Env vars set by 1bcoder:
  BCODER_FILE   — path of the file being edited (relative to BCODER_WORKDIR)
  BCODER_EVENT  — e.g. "before_save", "before_patch", "before_fim"

Usage in agent file:
  hooks =
      before save autobkup
      before patch autobkup
      before fim autobkup

Usage from session:
  /hook before save autobkup
  /hook before patch autobkup
"""
import sys
import os
import shutil

_ = sys.stdin.read()  # required: consume stdin

file = os.environ.get("BCODER_FILE", "").strip()
workdir = os.environ.get("BCODER_WORKDIR", ".")

if not file:
    sys.exit(0)

# resolve relative path against workdir
if not os.path.isabs(file):
    file = os.path.join(workdir, file)

if not os.path.isfile(file):
    sys.exit(0)  # file doesn't exist yet (new file) — nothing to back up

bkup = file + ".bkup"
if os.path.isfile(bkup):
    n = 1
    while os.path.isfile(f"{bkup}({n})"):
        n += 1
    os.rename(bkup, f"{bkup}({n})")

shutil.copy2(file, bkup)
print(f"ALERT: backed up → {os.path.basename(bkup)}", file=sys.stderr)
# no BLOCK: → command proceeds
