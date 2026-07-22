"""history — conceptual "blame" for 1bcoder.

Walks every exported task, extracts candidate keywords, fuzzy-matches them
against an existing glossary (built by /flow glossary), structurally chunks
the task's associated commit diffs (by FILE:/@@ boundaries), and asks the
LLM what changed regarding each matched term — appending the answer to a new
HISTORY: section in the matched term's glossary article, tagged [file: ...].

Not "who last touched this line" (git blame) but "how did this business
concept evolve across the whole project, over time, across files" — a
different question needing a different mechanism. See
concepts/HISTORY_FLOW.md for the full design rationale, including the
empirically-measured KV-cache behavior this flow depends on.

Self-contained (stdlib only: os, re, json, urllib.request) — same
portability philosophy as glossary.py. Small helpers (term.md read/write,
fuzzy phrase matching, source tags, path helpers) are duplicated from
glossary.py rather than imported, since /flow files are meant to be
standalone/portable (see concepts/GLOSSARY.md).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow history index --glossary <dir> --tasks <dir> [--host H] [--model M]

index:
  --glossary <dir>   glossary directory (e.g. .1bcoder/glossary/myproject) —
                     must already exist, built by /flow glossary index first.
  --tasks <dir>      tasks/ folder from `simargl export --all`. The sibling
                     commits/ folder (same parent directory) is located
                     automatically — simargl always exports them as siblings.
  --host / --model   Ollama host:port and model, e.g.
                     --host panteon.local:11434 --model gemma3:4b-it-q4_K_M
                     Overrides ~/.1bcoder/history.json if both are set there.
                     If neither the flags nor the config file supply a value,
                     the command refuses to run rather than silently falling
                     back to some default host.

Progress and incremental state:
  A JSON registry (<glossary>/_history/registry.json, {task_name: true})
  tracks fully-processed tasks. Once a task is done, it is never revisited —
  the registry entry is written immediately after each task (not batched at
  the end), so Ctrl+C loses at most the in-progress task.

Examples:
  /flow history index --glossary .1bcoder/glossary/sqlfluff --tasks .simargl/sqlfluff/glossary_export/tasks
  /flow history index --glossary .1bcoder/glossary/sqlfluff --tasks .simargl/sqlfluff/glossary_export/tasks --host localhost:11434 --model gemma3:1b
"""
import os as _os
import re as _re
import json as _json
import urllib.request as _urlreq
import urllib.error as _urlerr

_DEFAULT_TIMEOUT = 300


# ── host/model config (~/.1bcoder/history.json) ─────────────────────────────
# Same pattern as chat.py's visual.json/translate.json — a small JSON sidecar
# independent of whatever host/model the live chat session happens to be on,
# because /flow history deliberately targets a specific, possibly-different
# machine (e.g. a dedicated box running a bigger model) for this batch job.

def _config_path() -> str:
    return _os.path.join(_os.path.expanduser("~"), ".1bcoder", "history.json")


def _load_config() -> dict:
    path = _config_path()
    if not _os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


# ── glossary storage — duplicated from glossary.py, path-based here (the
# --glossary flag is a directory, not a project name resolved relative to
# cwd) rather than glossary.py's project-name convention ───────────────────

def _kebab(term: str) -> str:
    s = term.strip().lower()
    s = _re.sub(r'[^a-z0-9]+', '-', s)
    s = _re.sub(r'-+', '-', s).strip('-')
    return s


def _term_path(glossary_dir: str, term: str) -> str:
    return _os.path.join(glossary_dir, _kebab(term) + ".md")


def _load_terms(glossary_dir: str) -> list:
    path = _os.path.join(glossary_dir, "glossary.md")
    if not _os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]


def _read_term_file(glossary_dir: str, term: str) -> dict:
    """Same format as glossary.py's _read_term_file — must stay in sync,
    since both flows read/write the same term.md files."""
    kterm = _kebab(term)
    path = _term_path(glossary_dir, kterm)
    if not _os.path.isfile(path):
        return {"term": kterm, "definitions": [], "facts": [], "links": [], "history": []}
    text = open(path, encoding="utf-8").read()
    definitions, facts, links, history = [], [], [], []
    m = _re.search(r'DEFINITION:\n(.*?)(?=\nFACTS:|\nLINK:|\nHISTORY:|\Z)', text, _re.DOTALL)
    if m:
        block = m.group(1).strip()
        if block:
            body_lines = [l for l in block.splitlines() if l.strip()]
            bullet_lines = [l for l in body_lines if _re.match(r'^\s*[-*]\s+', l)]
            if bullet_lines and len(bullet_lines) == len(body_lines):
                definitions = [l.lstrip("-*").strip() for l in bullet_lines]
            else:
                definitions = [block]
    m = _re.search(r'FACTS:\n(.*?)(?=\nLINK:|\nHISTORY:|\Z)', text, _re.DOTALL)
    if m:
        facts = [l.lstrip("-*").strip() for l in m.group(1).splitlines() if l.strip()]
    m = _re.search(r'LINK:\n(.*?)(?=\nHISTORY:|\Z)', text, _re.DOTALL)
    if m:
        raw_links = [l.lstrip("-*").strip() for l in m.group(1).splitlines() if l.strip()]
        links = []
        for l in raw_links:
            lm = _re.match(r'\[([^\]]+)\]\([^)]+\)', l)
            links.append(lm.group(1) if lm else l)
    m = _re.search(r'HISTORY:\n(.*)\Z', text, _re.DOTALL)
    if m:
        history = [l.lstrip("-*").strip() for l in m.group(1).splitlines() if l.strip()]
    return {"term": kterm, "definitions": definitions, "facts": facts, "links": links,
            "history": history}


def _write_term_file(glossary_dir: str, term: str, data: dict) -> None:
    """Same format as glossary.py's _write_term_file — must stay in sync."""
    kterm = _kebab(term)
    lines = [f"# {kterm}", "", "DEFINITION:"]
    for d in data.get("definitions", []):
        lines.append(f"- {d}")
    lines.append("")
    lines.append("FACTS:")
    for fact in data.get("facts", []):
        lines.append(f"- {fact}")
    lines.append("")
    lines.append("LINK:")
    for link in data.get("links", []):
        lines.append(f"- [{link}]({link}.md)")
    lines.append("")
    lines.append("HISTORY:")
    for entry in data.get("history", []):
        lines.append(f"- {entry}")
    with open(_term_path(glossary_dir, kterm), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── source tag — duplicated from glossary.py's _source_tag ─────────────────

def _source_tag(source_path: str, index) -> str:
    path = source_path.replace(_os.sep, '/')
    if index is None:
        return f" [file: {path}]"
    return f" [file: {path}:{index}]"


# ── keyword <-> known-term matching ──────────────────────────────────────────
# glossary.py's extract() is deliberately NOT used here: its own docstring
# says it is for fused code-identifier-style queries ("RuleIndex" as one
# token), not space-separated prose phrases ("payment allocation") — using
# it here would silently fail to match most LLM-proposed multi-word
# keywords against kebab-case term names. _phrase_in_text below (same
# approach glossary.py's own --crosslink pass uses for prose) checks
# hyphen-joined and space-joined forms; _fused_match (glossary.py's
# _split_identifier + subset check) is also tried, for the case where a
# candidate keyword IS a single fused identifier-style token. Combining
# both gives better coverage than either alone.

def _split_identifier(name: str) -> list:
    parts = _re.split(r'[_\-]+', name)
    result = []
    for part in parts:
        if not part:
            continue
        s = _re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', part)
        s = _re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
        result.extend(w.lower() for w in s.split('_') if len(w) >= 2)
    seen = {}
    for w in result:
        seen.setdefault(w, None)
    return list(seen)


def _phrase_in_text(term: str, text_lower: str) -> bool:
    if not term:
        return False
    return term in text_lower or " ".join(term.split("-")) in text_lower


def _terms_matching(keyword: str, known_terms: list) -> list:
    """Return every known term that matches `keyword`, via phrase-containment
    (multi-word aware) or fused-identifier subword-subset (single-token
    aware). Order: known_terms order, for determinism."""
    kw_lower = keyword.strip().lower()
    kw_kebab = _kebab(keyword)
    kw_subwords = frozenset(w for w in _split_identifier(keyword) if len(w) >= 4)
    hits = []
    for term in known_terms:
        term_lower = term.lower()
        if term_lower == kw_kebab or _phrase_in_text(term_lower, kw_lower):
            hits.append(term)
            continue
        if kw_subwords:
            term_subwords = frozenset(_split_identifier(term))
            if term_subwords and term_subwords <= kw_subwords:
                hits.append(term)
    return hits


# ── diff chunking — same (chunk_text, start_line) contract as glossary.py's
# _code_chunks/_char_chunks, but boundaries are FILE:/@@ markers (this
# module's own exported-diff format) instead of function/class regexes ─────

def _char_chunks(text: str, chunk_chars: int, overlap_chars: int = 0) -> list:
    if chunk_chars <= 0 or len(text) <= chunk_chars:
        return [(text, 1)]
    step = max(chunk_chars - overlap_chars, 1)
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        start_line = text.count('\n', 0, start) + 1
        chunks.append((text[start:end], start_line))
        if end >= n:
            break
        start += step
    return chunks


def _diff_chunks(text: str, chunk_chars: int = 4000) -> list:
    """Split a simargl-exported commit file on its FILE:/@@ boundaries —
    each hunk is a natural unit (small adjacent ones merge up to
    chunk_chars, oversized ones fall back to _char_chunks). Same shape as
    glossary.py's _code_chunks, boundary condition swapped for our own
    export format instead of source-code function/class regexes."""
    lines = text.splitlines(keepends=True)
    boundary_lines = [0]
    for i, line in enumerate(lines):
        if i == 0:
            continue
        if line.startswith("FILE: ") or line.startswith("@@"):
            boundary_lines.append(i)
    boundary_lines.append(len(lines))
    boundary_lines = sorted(set(boundary_lines))

    segments = []
    for i in range(len(boundary_lines) - 1):
        seg_start = boundary_lines[i]
        seg = "".join(lines[seg_start:boundary_lines[i + 1]])
        if seg.strip():
            segments.append((seg, seg_start + 1))
    if not segments:
        return _char_chunks(text, chunk_chars)

    chunks, buf, buf_start = [], "", None
    for seg, seg_start in segments:
        if len(seg) > chunk_chars:
            if buf:
                chunks.append((buf, buf_start))
                buf, buf_start = "", None
            for sub_text, sub_line in _char_chunks(seg, chunk_chars):
                chunks.append((sub_text, seg_start + sub_line - 1))
            continue
        if buf and len(buf) + len(seg) > chunk_chars:
            chunks.append((buf, buf_start))
            buf, buf_start = seg, seg_start
        else:
            if not buf:
                buf_start = seg_start
            buf += seg
    if buf:
        chunks.append((buf, buf_start))
    return chunks


# ── registry: task_id -> done ────────────────────────────────────────────────
# JSON, not YAML: no YAML precedent anywhere in _bcoder_data/ for persisted
# run state (only ever used for user-authored input files, e.g. webcrawl.py's
# --columns), and /flow files are stdlib-only by design — json is stdlib,
# yaml (PyYAML) is not. Deliberate exception to a general YAML-for-sidecars
# preference, justified by that constraint.

def _registry_path(glossary_dir: str) -> str:
    d = _os.path.join(glossary_dir, "_history")
    _os.makedirs(d, exist_ok=True)
    return _os.path.join(d, "registry.json")


def _load_registry(glossary_dir: str) -> dict:
    path = _registry_path(glossary_dir)
    if not _os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _mark_done(glossary_dir: str, task_name: str) -> None:
    path = _registry_path(glossary_dir)
    reg = _load_registry(glossary_dir)
    reg[task_name] = True
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(reg, f, indent=2, ensure_ascii=False)


# ── raw LLM call — independent /api/generate, no `context` param ───────────
# Empirically verified (concepts/HISTORY_FLOW.md): Ollama/llama.cpp reuses
# the KV-cache automatically across INDEPENDENT /api/generate calls that
# share an identical prompt prefix — 60-130x speedup on prompt_eval, no
# `context` continuation needed. This is why every per-term question below
# is sent as its own independent call (prefix = chunk + framing, suffix =
# the one new question) rather than growing a single conversation.

def _raw_generate(host: str, model: str, prompt: str, timeout: int = _DEFAULT_TIMEOUT) -> str:
    payload = _json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0},
    }).encode("utf-8")
    req = _urlreq.Request(f"http://{host}/api/generate", data=payload,
                          headers={"Content-Type": "application/json"})
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            body = _json.loads(resp.read())
    except (_urlerr.URLError, TimeoutError, OSError) as e:
        print(f"    [history] LLM call failed ({host}/{model}): {e}")
        return ""
    return (body.get("response") or "").strip()


# ── prompts ──────────────────────────────────────────────────────────────────

_KEYWORDS_PROMPT = (
    "You extract glossary terms from text.\n"
    "Output EXACTLY one line starting with TERMS: followed by a comma-separated "
    "list of the important terms, entities, and identifiers in the text below, "
    "ordered from MOST important to LEAST important.\n"
    "Skip trivial words. Do not explain your answer. Do not add any other text.\n\n"
    "Text:\n{text}"
)

_DIFF_FRAMING = (
    "You are reading a code change (unified diff format).\n"
    "A line starting with '-' means that code was REMOVED.\n"
    "A line starting with '+' means that code was ADDED.\n"
    "A line starting with '@@' marks a hunk boundary and may show the "
    "enclosing function or class.\n"
    "Base your answer only on what is shown below — do not guess at code you "
    "cannot see, and do not invent details.\n\n"
)


def _extract_keywords(host: str, model: str, task_text: str) -> list:
    raw = _raw_generate(host, model, _KEYWORDS_PROMPT.format(text=task_text))
    # case-insensitive: small models (e.g. gemma3:1b) don't reliably keep the
    # marker's exact case ("Terms:" instead of "TERMS:") — glossary.py's own
    # _extract_terms_marker has the same case-sensitive pattern and likely
    # shares this fragility; not touched here since it's out of this flow's
    # scope, but worth flagging for that file too.
    m = _re.search(r'TERMS:\s*(.+)', raw, _re.IGNORECASE)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def _term_question_prompt(chunk_text: str, term: str, unmatched: list = None) -> str:
    question = f"\n\nПеревір, що тут змінилось для терміна: {term}."
    if unmatched:
        question += f" Також зверни увагу, чи стосується це: {', '.join(unmatched)}."
    return _DIFF_FRAMING + chunk_text + question


# ── index: main task loop ────────────────────────────────────────────────────

def _pop_value(rest: str, pattern: str):
    m = _re.search(pattern, rest)
    if not m:
        return None, rest
    return m.group(1), (rest[:m.start()] + rest[m.end():]).strip()


def _cmd_index(rest: str) -> None:
    glossary_dir, rest = _pop_value(rest, r'--glossary\s+(\S+)')
    tasks_dir, rest = _pop_value(rest, r'--tasks\s+(\S+)')
    host, rest = _pop_value(rest, r'--host\s+(\S+)')
    model, rest = _pop_value(rest, r'--model\s+(\S+)')

    if not glossary_dir or not tasks_dir:
        print("usage: /flow history index --glossary <dir> --tasks <dir> [--host H] [--model M]")
        return

    cfg = _load_config()
    host = host or cfg.get("host")
    model = model or cfg.get("model")
    if not host or not model:
        print("[history] host/model not set — pass --host/--model or configure "
              "~/.1bcoder/history.json ({\"host\": \"...\", \"model\": \"...\"})")
        return

    tasks_dir = tasks_dir.rstrip("/\\")
    if not _os.path.isdir(tasks_dir):
        print(f"[history] tasks dir not found: {tasks_dir}")
        return
    commits_dir = _os.path.join(_os.path.dirname(tasks_dir), "commits")
    if not _os.path.isdir(commits_dir):
        print(f"[history] commits dir not found next to tasks dir: {commits_dir}")
        print("[history] run `simargl export --all` (not --join) to get this layout")
        return
    if not _os.path.isdir(glossary_dir):
        print(f"[history] glossary dir not found: {glossary_dir}")
        return

    known_terms = _load_terms(glossary_dir)
    if not known_terms:
        print(f"[history] no terms in glossary: {glossary_dir} — run /flow glossary index first")
        return

    registry = _load_registry(glossary_dir)
    task_files = sorted(f for f in _os.listdir(tasks_dir) if f.endswith(".txt"))
    total_tasks = len(task_files)
    print(f"[history] glossary: {glossary_dir}  tasks: {total_tasks}  "
          f"host: {host}  model: {model}")

    try:
        for task_idx, fname in enumerate(task_files, 1):
            task_name = fname[:-4]
            if registry.get(task_name):
                continue
            print(f"\n[history] task: {task_idx}/{total_tasks}  {task_name}")

            task_text = open(_os.path.join(tasks_dir, fname), encoding="utf-8").read()
            raw_keywords = _extract_keywords(host, model, task_text)
            if not raw_keywords:
                _mark_done(glossary_dir, task_name)
                continue

            matched, seen = [], set()
            for kw in raw_keywords:
                for hit in _terms_matching(kw, known_terms):
                    if hit not in seen:
                        seen.add(hit)
                        matched.append(hit)
            unmatched = [kw for kw in raw_keywords if not _terms_matching(kw, known_terms)]

            if not matched:
                _mark_done(glossary_dir, task_name)
                continue

            commit_files = sorted(
                f for f in _os.listdir(commits_dir)
                if f.startswith(task_name + "_") and f.endswith(".txt")
            )
            if not commit_files:
                _mark_done(glossary_dir, task_name)
                continue

            for cf_idx, cfname in enumerate(commit_files, 1):
                cpath = _os.path.join(commits_dir, cfname)
                diff_text = open(cpath, encoding="utf-8").read()
                chunks = _diff_chunks(diff_text)
                total_chunks = len(chunks)
                for chunk_idx, (chunk_text, start_line) in enumerate(chunks, 1):
                    print(f"    file: {cf_idx}/{len(commit_files)}  "
                          f"chunk: {chunk_idx}/{total_chunks}")
                    for term_idx, term in enumerate(matched, 1):
                        print(f"      term: {term_idx}/{len(matched)}  {term}")
                        extra = unmatched if term_idx == 1 else None
                        prompt = _term_question_prompt(chunk_text, term, extra)
                        answer = _raw_generate(host, model, prompt)
                        if not answer:
                            continue
                        tag = _source_tag(cpath, start_line)
                        data = _read_term_file(glossary_dir, term)
                        data["history"].append(answer + tag)
                        _write_term_file(glossary_dir, term, data)

            _mark_done(glossary_dir, task_name)
    except KeyboardInterrupt:
        print("\n[history] stopped by user — progress saved (registry updated per task)")
        return

    print(f"\n[history] done — {total_tasks} task(s) scanned")


# ── entry point ──────────────────────────────────────────────────────────────

def run(chat, args: str) -> None:
    args = args.strip()
    if not args:
        print(__doc__)
        return
    parts = args.split(None, 1)
    sub, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    if sub == "index":
        _cmd_index(rest)
    else:
        print(__doc__)
