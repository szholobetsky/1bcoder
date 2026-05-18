"""Scan images for a given object using the configured vision model.
Usage: /flow visual_search <object> [path: <dir_or_glob>]
       /visual_search <object> [path: <dir_or_glob>]
"""
import os as _os
import re as _re
import glob as _glob
import base64 as _b64

_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}


def _collect_files(pattern: str) -> list:
    pattern = pattern.strip()
    if _os.path.isdir(pattern):
        files = []
        for root, _, fnames in _os.walk(pattern):
            for fn in sorted(fnames):
                if _os.path.splitext(fn)[1].lower() in _IMAGE_EXTS:
                    files.append(_os.path.join(root, fn).replace("\\", "/"))
        return files
    matched = sorted(_glob.glob(pattern, recursive=True))
    return [f.replace("\\", "/") for f in matched
            if _os.path.splitext(f)[1].lower() in _IMAGE_EXTS]


def run(chat, args: str):
    pm = _re.search(r'\bpath:\s*(\S+)', args)
    if pm:
        path_arg = pm.group(1)
        obj = (args[:pm.start()] + args[pm.end():]).strip()
    else:
        path_arg = None
        obj = args.strip()

    if not obj:
        print("usage: /flow visual_search <object> [path: <dir_or_glob>]")
        return

    files = _collect_files(path_arg) if path_arg else _collect_files(".")
    if not files:
        loc = path_arg or "current directory"
        print(f"[visual_search] no image files found in {loc}")
        return

    print(f"[visual_search] object: {obj}")
    print(f"[visual_search] scanning {len(files)} image(s)...")

    matched = []
    for i, fpath in enumerate(files, 1):
        if not _os.path.isfile(fpath):
            print(f"  [{i}/{len(files)}] skip (not found): {fpath}")
            continue
        try:
            with open(fpath, "rb") as f:
                b64 = _b64.b64encode(f.read()).decode("ascii")
        except OSError as e:
            print(f"  [{i}/{len(files)}] read error: {fpath}: {e}")
            continue
        try:
            reply = chat._visual_run(b64, f'Contains "{obj}"? Reply YES or NO only.').strip().upper()
        except Exception as e:
            print(f"  [{i}/{len(files)}] visual error: {e}")
            continue
        label = "YES" if reply.startswith("YES") else "NO "
        print(f"  [{i}/{len(files)}] {label}  {fpath}")
        if reply.startswith("YES"):
            matched.append(fpath)

    print()
    if matched:
        print(f"[visual_search] found {len(matched)} match(es):")
        for f in matched:
            print(f"  {f}")
    else:
        print(f'[visual_search] no images containing "{obj}" found')
