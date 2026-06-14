"""RAG project manager — wraps simargl CLI with a global project registry.

Global registry: ~/.1bcoder/rag_projects.txt
  Format: name: /absolute/path/to/project  # optional comment

Usage:
  /rag index [@r]              index files in cwd or in a registered project (@r = picker)
  /rag search <query>          search indexed files in cwd (--mode file)
  /rag search <query> @r       pick project interactively, then search
  /rag list                    show all registered projects
  /rag add [name] [path]       register cwd (or given path) as a named project
  /rag remove <name>           remove project from registry
  /rag init                    run simargl init wizard in cwd
  /rag ingest [--phase git|tasks] [--force]  run simargl ingest (task DB mode)
  /rag status                  run simargl status in cwd

Workflow for any folder with files:
  1. cd /my/project && /rag init
  2. /rag index          <- builds the file-level vector index
  3. /rag search <query> <- searches via --mode file

Alias: /rag = /flow rag {{args}}

@r in any command triggers the RAG project picker (see chat.py _resolve_at_rag).
"""
import os as _os
import re as _re
import subprocess as _sp

_RAG_PROJECTS_FILE = _os.path.join(_os.path.expanduser("~"), ".1bcoder", "rag_projects.txt")


def _load_projects() -> list[tuple[str, str, str]]:
    """Return list of (name, path, comment). Local .1bcoder/rag_projects.txt overrides global."""
    local = _os.path.join(".1bcoder", "rag_projects.txt")
    pfile = local if _os.path.exists(local) else _RAG_PROJECTS_FILE
    if not _os.path.exists(pfile):
        return []
    result = []
    with open(pfile, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            comment = ""
            if "#" in line:
                line, _, comment = line.partition("#")
                comment = comment.strip()
            if ":" not in line:
                continue
            name, _, path = line.partition(":")
            name, path = name.strip(), path.strip()
            if name and path:
                result.append((name, path, comment))
    return result


def _save_project(name: str, path: str, comment: str = "") -> bool:
    """Append or replace project in global registry. Returns True if replaced."""
    _os.makedirs(_os.path.dirname(_RAG_PROJECTS_FILE), exist_ok=True)
    lines = []
    replaced = False
    if _os.path.exists(_RAG_PROJECTS_FILE):
        with open(_RAG_PROJECTS_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    new_line = f"{name}: {path}" + (f"  # {comment}" if comment else "") + "\n"
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and ":" in stripped:
            existing_name = stripped.split(":")[0].strip()
            if existing_name == name:
                lines[i] = new_line
                replaced = True
                break
    if not replaced:
        if not lines:
            lines = ["# RAG project registry\n",
                     "# Format: name: /absolute/path\n",
                     "#\n"]
        lines.append(new_line)
    with open(_RAG_PROJECTS_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return replaced


def _remove_project(name: str) -> bool:
    """Remove project from global registry. Returns True if found."""
    if not _os.path.exists(_RAG_PROJECTS_FILE):
        return False
    with open(_RAG_PROJECTS_FILE, encoding="utf-8") as f:
        lines = f.readlines()
    new_lines = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and ":" in stripped:
            existing_name = stripped.split(":")[0].strip()
            if existing_name == name:
                found = True
                continue
        new_lines.append(line)
    if found:
        with open(_RAG_PROJECTS_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    return found


def _extract_path(args: str) -> tuple[str, str]:
    """Extract --path <dir> from args string. Returns (cwd, remaining_args)."""
    m = _re.search(r'--path\s+(\S+)', args)
    if m:
        path = m.group(1)
        remaining = (args[:m.start()] + args[m.end():]).strip()
        return path, remaining
    return _os.getcwd(), args


def _run_simargl(cmd: str, cwd: str) -> str:
    """Run a simargl command in given directory, return stdout+stderr."""
    try:
        result = _sp.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=120, encoding="utf-8", errors="replace",
        )
        return (result.stdout or "") + (result.stderr or "")
    except _sp.TimeoutExpired:
        return "[rag] simargl timed out after 120s"
    except Exception as e:
        return f"[rag] failed to run simargl: {e}"


def run(chat, args: str):
    tokens = args.split()
    sub = tokens[0].lower() if tokens else ""

    # ── list ──────────────────────────────────────────────────────────────────
    if sub == "list":
        projects = _load_projects()
        if not projects:
            print("[rag] no projects registered. Use: /rag add <name>")
            return
        print("[rag] registered projects:")
        for name, path, comment in projects:
            tag = f"  # {comment}" if comment else ""
            exists = "✓" if _os.path.isdir(path) else "✗"
            print(f"  {exists} {name}: {path}{tag}")
        return

    # ── add ───────────────────────────────────────────────────────────────────
    if sub == "add":
        name = tokens[1] if len(tokens) > 1 else ""
        path = tokens[2] if len(tokens) > 2 else _os.getcwd()
        if not name:
            name = _os.path.basename(_os.path.abspath(path))
            try:
                name = chat._prompt_input(f"  project name [{name}]:").strip() or name
            except Exception:
                pass
        path = _os.path.abspath(path)
        replaced = _save_project(name, path)
        print(f"[rag] {'updated' if replaced else 'registered'} '{name}' → {path}")
        return

    # ── remove ────────────────────────────────────────────────────────────────
    if sub == "remove":
        name = tokens[1] if len(tokens) > 1 else ""
        if not name:
            print("usage: /rag remove <name>")
            return
        if _remove_project(name):
            print(f"[rag] removed '{name}' from registry")
        else:
            print(f"[rag] '{name}' not found in registry")
        return

    # ── init ──────────────────────────────────────────────────────────────────
    if sub == "init":
        cwd = _os.getcwd()
        print(f"[rag] running simargl init in {cwd}")
        print("[rag] (interactive — follow the wizard)")
        _os.system("simargl init")
        return

    # ── index ─────────────────────────────────────────────────────────────────
    if sub == "index":
        rest = " ".join(tokens[1:])
        cwd, rest = _extract_path(rest)
        print(f"[rag] indexing files in {cwd}")
        out = _run_simargl(f"simargl index {rest}".strip(), cwd)
        print(out)
        return

    # ── ingest ────────────────────────────────────────────────────────────────
    if sub == "ingest":
        rest = " ".join(tokens[1:])
        cwd, rest = _extract_path(rest)
        print(f"[rag] running simargl ingest {rest} in {cwd}".strip())
        out = _run_simargl(f"simargl ingest {rest}".strip(), cwd)
        print(out)
        return

    # ── status ────────────────────────────────────────────────────────────────
    if sub == "status":
        rest = " ".join(tokens[1:])
        cwd, _ = _extract_path(rest)
        out = _run_simargl("simargl status", cwd)
        print(out)
        return

    # ── search ────────────────────────────────────────────────────────────────
    if sub == "search":
        rest = " ".join(tokens[1:]).strip()

        # @r token: replaced by chat._resolve_at_rag before flow runs,
        # so by this point @r is already a path (or removed if cancelled).
        # But if @r slipped through (no resolver), ignore it.
        cwd, rest = _extract_path(rest)
        query = rest.strip().strip('"\'')
        if not query:
            print("usage: /rag search <query>")
            return

        if not _os.path.isdir(cwd):
            print(f"[rag] path not found: {cwd}")
            return

        print(f"[rag] searching '{query}' in {cwd}")
        out = _run_simargl(f'simargl search "{query}" --mode file --sort rank', cwd)

        if not out.strip():
            print("[rag] no results")
            return

        print(out)
        chat.messages.append({"role": "user",      "content": f"[rag search: {query}]\n{out}"})
        chat.messages.append({"role": "assistant", "content": f"[rag results injected into context]"})
        print("[rag] results added to context")
        return

    # ── unknown ───────────────────────────────────────────────────────────────
    print("usage: /rag index | search <query> | list | add [name] [path] | remove <name>")
    print("       /rag init | ingest | status")
    print("       Use @r in command to pick a RAG project interactively")
