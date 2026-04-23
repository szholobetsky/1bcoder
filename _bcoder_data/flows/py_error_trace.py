"""Analyse a Python traceback: extract file:line locations, read code context, explain the error. Usage: /flow py_error_trace [-f <file>]"""
import re as _re
import os as _os


def _extract_locations(traceback: str) -> list[tuple[str, int]]:
    """Extract (filepath, lineno) pairs from a Python traceback."""
    locations = []
    seen = set()
    for m in _re.finditer(r'File "([^"]+)", line (\d+)', traceback):
        path, line = m.group(1), int(m.group(2))
        # skip stdlib and site-packages
        norm = path.replace("\\", "/")
        if any(x in norm for x in ("/lib/", "site-packages", "<frozen", "<string")):
            continue
        key = (norm, line)
        if key not in seen:
            seen.add(key)
            locations.append((path, line))
    return locations


def _read_context(path: str, lineno: int, context: int = 10) -> str:
    """Read lines around lineno from file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        start = max(0, lineno - context - 1)
        end   = min(len(lines), lineno + context)
        numbered = []
        for i, l in enumerate(lines[start:end], start=start+1):
            marker = ">>>" if i == lineno else "   "
            numbered.append(f"{marker} {i:4d} | {l.rstrip()}")
        return "\n".join(numbered)
    except OSError:
        return f"(could not read {path})"


def run(chat, args: str):
    # resolve traceback source: -f <file>, empty → last_reply, else inline args
    traceback_text = ""
    m = _re.match(r"-f\s+(\S+)", args.strip())
    if m:
        fpath = m.group(1)
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                traceback_text = f.read()
            print(f"[py_error_trace] reading traceback from {fpath}")
        except OSError as e:
            print(f"[py_error_trace] cannot read file: {e}"); return
    elif args.strip():
        traceback_text = args
    else:
        traceback_text = chat._last_output
        if not traceback_text:
            print("usage: /flow py_error_trace [-f <file>]  (or run a command first with /run)")
            return
        print(f"[py_error_trace] using last /run output as traceback")

    locations = _extract_locations(traceback_text)
    if not locations:
        print("[py_error_trace] no file:line references found in traceback")
        return

    print(f"[py_error_trace] found {len(locations)} location(s)")

    sections = [f"## Traceback\n```\n{traceback_text.strip()}\n```"]
    for path, lineno in locations:
        print(f"[py_error_trace] reading {path}:{lineno}")
        code_ctx = _read_context(path, lineno)
        sections.append(f"## {path} (line {lineno})\n```python\n{code_ctx}\n```")

    combined = "\n\n".join(sections)
    prompt = (
        f"Analyse this Python error traceback and the relevant code locations.\n"
        f"Explain: what caused the error, which line is the root cause, and what needs to be fixed.\n\n"
        f"{combined}"
    )
    temp_msgs = [{"role": "system", "content": chat._role},
                 {"role": "user",   "content": prompt}]
    chat._sep("AI")
    reply = chat._stream_chat(temp_msgs)
    if reply:
        chat.last_reply = reply
        chat._last_output = reply
        chat.messages.append({"role": "user",      "content": "[py_error_trace]"})
        chat.messages.append({"role": "assistant", "content": reply})
