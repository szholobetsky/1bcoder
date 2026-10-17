"""loop_stream — pure helpers for the /loop command (stream from anything, act per element).

(c) 2026 Stanislav Zholobetskyi
Institute for Information Recording, National Academy of Sciences of Ukraine, Kyiv
Створено в рамках аспірантського дослідження на тему:
"Інтелектуальна технологія підтримки розробки та супроводу програмних продуктів"

source -> cutter -> action -> output. This module holds everything that does not
need the chat session: argument parsing, collecting files, cutting text into
elements with stable ids and breadcrumbs, the jsonl log (= resume state),
deterministic rendering, and the radogast (embedding) analysis.

Deliberately does NOT import chat.py or do any network I/O.
See concepts/LOOP.md in the simrgl repo for the design.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass


class LoopError(Exception):
    """Bad /loop arguments, bad source, or a missing optional dependency."""


# ── element ───────────────────────────────────────────────────────────────────

@dataclass
class Element:
    id: str        # stable, readable: concepts/JEV.md#3, logs/app.log@2, last@1a2b3c4d#5
    source: str    # id base (file path, last@<sha8>, cmd@<sha8>)
    index: int     # 1-based position in the whole stream
    total: int     # number of elements in the whole stream
    text: str
    crumb: str     # "concepts/JEV.md, section 3 of 12, part 2 of 5"
    sha: str       # short hash of text


def sha8(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", errors="replace")).hexdigest()[:8]


# ── argument parsing ──────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r'(?:[\w-]+:)*"(?:[^"\\]|\\.)*"|\S+')


def tokenize_args(s: str) -> list[tuple[str, bool]]:
    """Split /loop arguments. Returns [(token, was_quoted)].

    A token is either `prefix:prefix:"quoted text"` (quotes removed, prefixes
    kept, \\" unescaped) or a run of non-space characters. Backslashes are kept
    as typed (shlex would eat them on Windows and break regex cutters)."""
    out = []
    for m in _TOKEN_RE.finditer(s or ""):
        tok = m.group(0)
        q = tok.find('"')
        if q >= 0 and tok.endswith('"') and len(tok) - q >= 2 and re.fullmatch(r'(?:[\w-]+:)*', tok[:q]):
            inner = tok[q + 1:-1].replace('\\"', '"')
            out.append((tok[:q] + inner, True))
        else:
            out.append((tok, False))
    return out


_VALUE_FLAGS = {"--llm", "--jev", "--radogast", "--keep", "--ctx", "--max-tokens",
                "--out", "--rg-min", "--rg-dup"}


def _num(flag, raw, typ, lo=None, hi=None):
    try:
        v = typ(raw)
    except (TypeError, ValueError):
        raise LoopError(f"{flag} needs a {'number' if typ is float else 'whole number'}, got '{raw}'")
    if lo is not None and v < lo or hi is not None and v > hi:
        rng = f"{lo}..{hi}" if hi is not None else f">= {lo}"
        raise LoopError(f"{flag} must be {rng}, got {v}")
    return v


def parse_loop_args(s: str) -> dict:
    """Parse everything after `/loop`. Raises LoopError with a usage hint."""
    toks = tokenize_args(s)
    a = {"source": None, "path": None, "cmd": None, "glob": None, "recursive": False,
         "cutter": None, "llm": None, "jev": None, "jev_is_question": False,
         "radogast": None, "keep": "pass", "ctx": 0, "max_tokens": None, "out": None,
         "yes": False, "fresh": False, "rg_min": None, "rg_dup": 0.85}
    sources = []
    dir_val = None
    file_vals = []
    i = 0
    while i < len(toks):
        tok, quoted = toks[i]
        if not quoted and tok in _VALUE_FLAGS:
            if i + 1 >= len(toks):
                raise LoopError(f"{tok} needs a value")
            val, vq = toks[i + 1]
            i += 2
            if tok == "--llm":
                if not val.strip():
                    raise LoopError("--llm needs a prompt")
                a["llm"] = val
            elif tok == "--jev":
                if not val.strip():
                    raise LoopError("--jev needs a rubric name/number or a quoted question")
                a["jev"] = val
                a["jev_is_question"] = vq
            elif tok == "--radogast":
                a["radogast"] = val
            elif tok == "--keep":
                if val not in ("pass", "fail", "all"):
                    raise LoopError(f"--keep must be pass, fail or all, got '{val}'")
                a["keep"] = val
            elif tok == "--ctx":
                a["ctx"] = _num(tok, val, int, 0)
            elif tok == "--max-tokens":
                a["max_tokens"] = _num(tok, val, int, 16)
            elif tok == "--out":
                a["out"] = val
            elif tok == "--rg-min":
                a["rg_min"] = _num(tok, val, float, -1.0, 1.0)
            elif tok == "--rg-dup":
                a["rg_dup"] = _num(tok, val, float, 0.0, 1.0)
            continue
        i += 1
        if quoted and not tok.startswith(("cmd:", "split:", "dir:", "file:")):
            raise LoopError(f"unexpected quoted argument: \"{tok}\"")
        if tok == "-y":
            a["yes"] = True
        elif tok == "--fresh":
            a["fresh"] = True
        elif tok == "-r":
            a["recursive"] = True
        elif tok == "last":
            sources.append("last")
        elif tok.startswith("cmd:"):
            a["cmd"] = tok[4:].strip()
            if not a["cmd"]:
                raise LoopError('cmd: needs a command, e.g. cmd:"/find mcp -c"')
            sources.append("cmd")
        elif tok.startswith("dir:"):
            dir_val = tok[4:]
            if not dir_val:
                raise LoopError("dir: needs a path")
            sources.append("dir")
        elif tok.startswith("file:"):
            if not tok[5:]:
                raise LoopError("file: needs a path or a glob")
            file_vals.append(tok[5:])
        elif tok.startswith("split:"):
            a["cutter"] = tok[6:]
            if not a["cutter"]:
                raise LoopError('split: needs a cutter: "## ", re:"<regex>", blank, line, tokens:N')
            validate_cutter(a["cutter"])
        else:
            raise LoopError(f"unknown argument: {tok}")
    if len(file_vals) > 1:
        raise LoopError("only one file: is allowed")
    if dir_val is not None:
        a["path"] = dir_val
        a["glob"] = file_vals[0] if file_vals else None
    elif file_vals:
        sources.append("file")
        a["path"] = file_vals[0]
    if a["recursive"] and dir_val is None:
        raise LoopError("-r only works with dir:")
    if len(sources) != 1:
        raise LoopError("exactly one source is required: dir:<path> [file:<glob>] [-r] | "
                        "file:<path> | last | cmd:\"<command>\""
                        + (f" (got {', '.join(sources)})" if sources else ""))
    a["source"] = sources[0]
    if a["rg_min"] is not None and not a["radogast"]:
        raise LoopError("--rg-min needs --radogast <task.yaml>")
    return a


def action_sig(args: dict) -> str:
    """Short hash of everything that changes what a record means.
    A resume with a different action must not silently mix results."""
    spec = {k: args.get(k) for k in ("llm", "jev", "jev_is_question", "radogast",
                                     "keep", "cutter", "ctx", "rg_min")}
    return sha8(json.dumps(spec, sort_keys=True, ensure_ascii=False))


# ── text helpers ──────────────────────────────────────────────────────────────

def est_tokens(text: str) -> int:
    """Rough token estimate: ASCII ~4 chars/token, non-ASCII (Cyrillic...) ~2."""
    if not text:
        return 0
    n_ascii = sum(1 for ch in text if ord(ch) < 128)
    return n_ascii // 4 + (len(text) - n_ascii) // 2


def _norm_nl(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text(path: str) -> str | None:
    """File text (BOM stripped, newlines normalized), or None for a binary file."""
    with open(path, "rb") as f:
        head = f.read(8192)
    if b"\x00" in head:
        return None
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return _norm_nl(f.read())


# directories never descended into (same set as /find, plus dot-dirs)
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".venv", "venv", "env",
    ".1bcoder", "dist", "build", ".mypy_cache", ".pytest_cache",
})


def _natural_key(s: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def make_id_base(path: str, workdir: str) -> str:
    """Posix path relative to workdir; absolute (posix) if outside it."""
    ap = os.path.abspath(path)
    try:
        rel = os.path.relpath(ap, os.path.abspath(workdir))
    except ValueError:            # other drive on Windows
        return ap.replace("\\", "/")
    if rel == ".." or rel.startswith(".." + os.sep) or rel.startswith("../"):
        return ap.replace("\\", "/")
    return rel.replace("\\", "/")


def collect_dir(path: str, glob: str | None, recursive: bool, workdir: str) -> list[tuple[str, str]]:
    """[(id_base, text)] for the files in `path` matching `glob` (on the file name).

    Sorted naturally by relative path (item_2.9 before item_2.10); binary files
    and SKIP_DIRS / dot-directories below `path` are skipped."""
    if not os.path.isdir(path):
        raise LoopError(f"not a directory: {path}")
    found = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                found.append(os.path.join(dirpath, fn))
    else:
        for fn in os.listdir(path):
            p = os.path.join(path, fn)
            if os.path.isfile(p):
                found.append(p)
    if glob:
        found = [p for p in found if fnmatch.fnmatch(os.path.basename(p), glob)]
    out = []
    for p in found:
        try:
            text = read_text(p)
        except OSError:
            continue
        if text is None:
            continue
        out.append((make_id_base(p, workdir), text))
    out.sort(key=lambda t: _natural_key(t[0]))
    return out


# ── cutters ───────────────────────────────────────────────────────────────────

_TOKENS_RE = re.compile(r"^tokens:(\d+)$")
_HEADING_RE = re.compile(r"^#{2,6}\s+\S")


def validate_cutter(cutter: str) -> None:
    if cutter in ("blank", "line"):
        return
    m = _TOKENS_RE.match(cutter)
    if m:
        if int(m.group(1)) < 16:
            raise LoopError("tokens:N needs N >= 16")
        return
    if cutter.startswith("tokens:"):
        raise LoopError(f"bad cutter '{cutter}' — use tokens:N, e.g. tokens:3000")
    if cutter.startswith("re:"):
        if not cutter[3:]:
            raise LoopError('re: needs a regex, e.g. re:"^## \\d+\\."')
        try:
            re.compile(cutter[3:], re.MULTILINE)
        except re.error as e:
            raise LoopError(f"bad regex in split:{cutter}: {e}")


def _split_tokens(text: str, n: int) -> list[str]:
    """Chunks of about n tokens, cut on line boundaries (a single over-long line
    is cut on whitespace, never mid-word)."""
    pieces = []
    for line in text.split("\n"):
        if est_tokens(line) <= n:
            pieces.append(line)
            continue
        cur = ""
        for w in re.split(r"(?<=\s)", line):
            if cur and est_tokens(cur + w) > n:
                pieces.append(cur.rstrip())
                cur = w
            else:
                cur += w
        if cur:
            pieces.append(cur)
    chunks, cur, cur_t = [], [], 0
    for p in pieces:
        t = est_tokens(p) + 1
        if cur and cur_t + t > n:
            chunks.append("\n".join(cur))
            cur, cur_t = [], 0
        cur.append(p)
        cur_t += t
    if cur:
        chunks.append("\n".join(cur))
    return [c.strip("\n") for c in chunks if c.strip()]


def _cut_at(text: str, starts: list[int]) -> tuple[str, list[str]]:
    starts = sorted(set(starts))
    if not starts:
        return text, []
    pre = text[:starts[0]]
    secs = [text[s:e] for s, e in zip(starts, starts[1:] + [len(text)])]
    return pre, secs


def _first_heading_pos(text: str) -> int | None:
    pos = 0
    for line in text.split("\n"):
        if _HEADING_RE.match(line):
            return pos
        pos += len(line) + 1
    return None


def _split_sections(text: str, cutter: str) -> tuple[str, list[str]]:
    """Heading-style cut: (preamble, [sections])."""
    if cutter.startswith("re:"):
        rx = re.compile(cutter[3:], re.MULTILINE)
        starts = [m.start() for m in rx.finditer(text)
                  if m.start() == 0 or text[m.start() - 1] == "\n"]
    else:
        starts, pos = [], 0
        for line in text.split("\n"):
            if line.startswith(cutter):
                starts.append(pos)
            pos += len(line) + 1
    pre, secs = _cut_at(text, starts)
    # an unnumbered first ##+ heading counts as section 1 (same rule as
    # deepagent_md._parse_sections): the model often writes "## Overview"
    # before "## 2. ...", and that section must not vanish into the preamble.
    if cutter.startswith("re:") or cutter.lstrip().startswith("#"):
        hp = _first_heading_pos(pre)
        if hp is not None:
            secs = [pre[hp:]] + secs
            pre = pre[:hp]
    return pre, secs


def _only_titles(text: str) -> bool:
    """True if every non-blank line is a markdown heading (e.g. a lone '# Title')."""
    lines = [l for l in text.split("\n") if l.strip()]
    return bool(lines) and all(l.lstrip().startswith("#") for l in lines)


def split_text(text: str, cutter: str | None) -> list[str]:
    """Cut text into pieces. cutter: None | '## '-style prefix | re:<rx> |
    blank | line | tokens:N. Text before the first heading becomes its own
    piece only if it holds real content; a preamble of headings only (the
    file's own '# Title' line) is prepended to the first section instead."""
    text = _norm_nl(text or "")
    if not cutter:
        return [text] if text.strip() else []
    if cutter == "line":
        return [l.rstrip() for l in text.split("\n") if l.strip()]
    if cutter == "blank":
        return [p.strip("\n") for p in re.split(r"\n(?:[ \t]*\n)+", text) if p.strip()]
    m = _TOKENS_RE.match(cutter)
    if m:
        return _split_tokens(text, int(m.group(1)))
    validate_cutter(cutter)
    pre, secs = _split_sections(text, cutter)
    secs = [s.strip("\n") for s in secs]
    if not secs:
        return [text.strip("\n")] if text.strip() else []
    if pre.strip():
        if _only_titles(pre):
            secs[0] = pre.strip("\n") + "\n\n" + secs[0]
        else:
            secs = [pre.strip("\n")] + secs
    return [s for s in secs if s.strip()]


def _is_tokens_cutter(cutter) -> bool:
    return bool(cutter and _TOKENS_RE.match(cutter))


def build_elements(sources: list[tuple[str, str]], cutter: str | None,
                   max_tokens: int) -> list[Element]:
    """sources: [(id_base, text)] -> materialized stream.

    ids: base (no cutter), base#N (heading/blank/line cutter), base@N (tokens),
    base#N@K / base@K (oversized piece re-split by tokens)."""
    max_tokens = max(16, int(max_tokens or 0) or 2048)
    raw = []   # (id, base, text, crumb)
    tok_cut = _is_tokens_cutter(cutter)
    if tok_cut:
        cutter = f"tokens:{min(int(cutter[7:]), max_tokens)}"
    for base, text in sources:
        pieces = split_text(text, cutter)
        np_ = len(pieces)
        for n, piece in enumerate(pieces, 1):
            if cutter:
                sep = "@" if tok_cut else "#"
                kind = "chunk" if tok_cut else ("line" if cutter == "line" else
                                                "paragraph" if cutter == "blank" else "section")
                pid = f"{base}{sep}{n}"
                crumb = f"{base}, {kind} {n} of {np_}"
            else:
                pid, crumb = base, base
            if est_tokens(piece) > max_tokens:
                parts = _split_tokens(piece, max_tokens)
                for k, part in enumerate(parts, 1):
                    raw.append((f"{pid}@{k}", base, part, f"{crumb}, part {k} of {len(parts)}"))
            else:
                raw.append((pid, base, piece, crumb))
    total = len(raw)
    return [Element(id=i_, source=b, index=k, total=total, text=t, crumb=c, sha=sha8(t))
            for k, (i_, b, t, c) in enumerate(raw, 1)]


# ── jsonl log (= resume state) ────────────────────────────────────────────────

def jsonl_path(out: str) -> str:
    return out + ".jsonl"


def read_log(jsonl: str) -> list[dict]:
    """All records; corrupt/truncated lines (e.g. the last one after a crash) are ignored."""
    if not jsonl or not os.path.isfile(jsonl):
        return []
    recs = []
    with open(jsonl, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and "id" in r:
                recs.append(r)
    return recs


def latest_records(records: list[dict]) -> list[dict]:
    """Last record per id (a retried element replaces its failed attempt),
    ordered by stream index, then by first appearance."""
    last, first = {}, {}
    for n, r in enumerate(records):
        last[r["id"]] = r
        first.setdefault(r["id"], n)
    return sorted(last.values(), key=lambda r: (r.get("index", 0), first[r["id"]]))


def read_done_ids(jsonl: str) -> set[str]:
    """Ids already processed successfully (errored ones are retried on resume)."""
    return {r["id"] for r in latest_records(read_log(jsonl)) if not r.get("error")}


def log_sigs(jsonl: str) -> set[str]:
    return {r.get("sig") for r in read_log(jsonl) if r.get("sig")}


def append_jsonl(jsonl: str, record: dict) -> None:
    d = os.path.dirname(os.path.abspath(jsonl))
    os.makedirs(d, exist_ok=True)
    # a crash may have left a truncated last line without "\n" — start a new
    # line so this record is not glued onto the broken one
    lead = ""
    if os.path.isfile(jsonl) and os.path.getsize(jsonl) > 0:
        with open(jsonl, "rb") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                lead = "\n"
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write(lead + json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


# ── rendering ─────────────────────────────────────────────────────────────────

RENDER_EXTS = (".md", ".csv", ".txt")


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def jev_verdict(rec: dict):
    j = rec.get("jev")
    if not isinstance(j, dict):
        return None
    return j.get("passed")


def _jev_line(rec: dict) -> str:
    j = rec.get("jev")
    if not isinstance(j, dict):
        return ""
    verdict = {True: "PASS", False: "FAIL", None: "--"}[j.get("passed")]
    if "criteria" in j:
        failed = [c["id"] for c in j["criteria"] if c.get("passed") is False]
        s = f"jev {j.get('rubric', '')}: {verdict}"
        if failed:
            s += f" ({', '.join(failed)})"
        if j.get("error"):
            s += f" — jev unavailable: {j['error']}"
        return s
    v = j.get("value")
    return f"jev: {verdict}" + (f" p(true)={v:.3f}" if isinstance(v, (int, float)) else "")


def _body(rec: dict) -> str:
    if rec.get("llm") is not None:
        return rec["llm"]
    return rec.get("text") or ""


def render_records(records: list[dict], fmt: str) -> str:
    """Deterministic rendering of (kept) records — no model calls."""
    recs = [r for r in latest_records(records) if r.get("kept") and not r.get("error")]
    fmt = fmt.lower().lstrip(".")
    if fmt == "csv":
        crit_ids = []
        for r in recs:
            j = r.get("jev") or {}
            for c in j.get("criteria", []) if isinstance(j, dict) else []:
                if c["id"] not in crit_ids:
                    crit_ids.append(c["id"])
        has_q = any(isinstance(r.get("jev"), dict) and "criteria" not in r["jev"] for r in recs)
        has_rg = any(r.get("rg") for r in recs)
        has_llm = any(r.get("llm") is not None for r in recs)
        header = ["id", "source", "index", "verdict"]
        if has_q:
            header.append("p_true")
        for cid in crit_ids:
            header += [cid, f"{cid}:pass"]
        if has_rg:
            header += ["rg_sim", "rg_oos"]
        header.append("llm" if has_llm else "text")
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        for r in recs:
            j = r.get("jev") if isinstance(r.get("jev"), dict) else {}
            row = [r["id"], r.get("source", ""), r.get("index", ""), _fmt(jev_verdict(r))]
            if has_q:
                row.append(_fmt(j.get("value")) if "criteria" not in j else "")
            by_id = {c["id"]: c for c in j.get("criteria", [])}
            for cid in crit_ids:
                c = by_id.get(cid, {})
                row += [_fmt(c.get("value")), _fmt(c.get("passed"))]
            if has_rg:
                rg = r.get("rg") or {}
                oos = rg.get("oos") or {}
                row += [_fmt(rg.get("sim")), " ".join(f"{k}:{v}" for k, v in sorted(oos.items()))]
            row.append(_body(r))
            w.writerow(row)
        return buf.getvalue()
    if fmt == "txt":
        return "\n\n".join(_body(r).strip() for r in recs if _body(r).strip()) + ("\n" if recs else "")
    # md
    parts = []
    for r in recs:
        lines = [f"## {r['id']}"]
        meta = []
        jl = _jev_line(r)
        if jl:
            meta.append(jl)
        rg = r.get("rg")
        if rg and rg.get("sim") is not None:
            s = f"radogast sim={rg['sim']:.3f}"
            if rg.get("oos"):
                s += " out-of-scope: " + ", ".join(f"{k}×{v}" for k, v in sorted(rg["oos"].items()))
            meta.append(s)
        if meta:
            lines.append("")
            lines += [f"> {m}" for m in meta]
        body = _body(r).strip()
        if body:
            lines += ["", body]
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + ("\n" if parts else "")


def render(jsonl: str, out: str) -> tuple[str, str | None]:
    """Render the jsonl log into `out` by its extension. Returns (text, warning).
    Unknown extension -> plain text + a warning."""
    ext = os.path.splitext(out)[1].lower()
    warning = None
    fmt = ext if ext in RENDER_EXTS else ".txt"
    if ext not in RENDER_EXTS:
        warning = f"unknown output extension '{ext or '(none)'}' — rendered as plain text (.md, .csv, .txt are known)"
    text = render_records(read_log(jsonl), fmt)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if fmt == ".csv":
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
    else:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    return text, warning


def summary_table(records: list[dict]) -> str:
    recs = latest_records(records)
    n = len(recs)
    kept = sum(1 for r in recs if r.get("kept") and not r.get("error"))
    errs = sum(1 for r in recs if r.get("error"))
    lines = [f"[loop] summary: {n} element(s), kept {kept}, errors {errs}"]
    jrecs = [r for r in recs if isinstance(r.get("jev"), dict)]
    if jrecs:
        p = sum(1 for r in jrecs if r["jev"].get("passed") is True)
        f = sum(1 for r in jrecs if r["jev"].get("passed") is False)
        lines.append(f"  jev verdict      PASS {p:>4}   FAIL {f:>4}")
        crit = {}
        for r in jrecs:
            for c in r["jev"].get("criteria", []):
                d = crit.setdefault(c["id"], [0, 0, 0])
                d[0 if c.get("passed") is True else 1 if c.get("passed") is False else 2] += 1
        w = max([len(k) for k in crit] + [14])
        for cid, (cp, cf, cn) in crit.items():
            lines.append(f"  {cid:<{w}}   PASS {cp:>4}   FAIL {cf:>4}" + (f"   n/a {cn}" if cn else ""))
    sims = [r["rg"]["sim"] for r in recs if r.get("rg") and r["rg"].get("sim") is not None]
    if sims:
        lines.append(f"  radogast sim     mean {sum(sims) / len(sims):.3f}   min {min(sims):.3f}   max {max(sims):.3f}")
        oos = sum(1 for r in recs if r.get("rg") and r["rg"].get("oos"))
        if oos:
            lines.append(f"  out-of-scope hits in {oos} element(s)")
    if any(r.get("llm") is not None for r in recs):
        lines.append(f"  llm replies      {sum(1 for r in recs if r.get('llm'))}")
    return "\n".join(lines)


# ── radogast ──────────────────────────────────────────────────────────────────

RG_MISSING = "radogast not installed (pip install radogast[embed])"


def rg_load(task_yaml: str):
    """(Target, cfg) from a radogast task.yaml."""
    try:
        from radogast.target import load_target
        from radogast import config as rg_config
    except ImportError:
        raise LoopError(RG_MISSING)
    if not os.path.isfile(task_yaml):
        raise LoopError(f"radogast target not found: {task_yaml}")
    try:
        target = load_target(task_yaml)
    except Exception as e:
        raise LoopError(f"cannot read radogast target {task_yaml}: {e}")
    if not (target.goal or "").strip():
        raise LoopError(f"radogast target {task_yaml} has no goal")
    return target, rg_config.load()


def rg_goal_text(target) -> str:
    goal = re.sub(r"^\s*#+\s*", "", target.goal or "").strip()
    return (goal + " " + " ".join(target.key_terms or [])).strip()


def rg_embed(texts: list[str], target, cfg: dict):
    """(goal_vec, vecs) — one batch call for the goal and every element."""
    try:
        from radogast.metrics.drift import embed
    except ImportError:
        raise LoopError(RG_MISSING)
    model = cfg.get("embedding_model", "BAAI/bge-small-en-v1.5")
    v = embed([rg_goal_text(target)] + list(texts), model, show_progress=len(texts) > 50)
    if v is None:
        raise LoopError(RG_MISSING)
    return v[0], v[1:]


def oos_hits(text: str, terms: list[str]) -> dict[str, int]:
    """Copy of radogast.plan_analyzer._out_of_scope_hits (private there)."""
    hits = {}
    for term in terms or []:
        n = len(re.findall(re.escape(term), text, re.IGNORECASE))
        if n > 0:
            hits[term] = n
    return hits


def rg_element_scores(texts: list[str], vecs, goal_vec, target) -> list[dict]:
    """Per element: cosine similarity to the goal + out-of-scope term counts.
    Annotations only — filtering is the caller's job (--rg-min)."""
    out = []
    for t, v in zip(texts, vecs):
        out.append({"sim": round(float(sum(a * b for a, b in zip(v, goal_vec))), 4),
                    "oos": oos_hits(t, target.out_of_scope)})
    return out


def rg_stream_report(ids: list[str], texts: list[str], vecs, goal_vec, target,
                     threshold: float = 0.85, farthest: int = 5) -> dict:
    """Stream-level analysis: duplicate clusters, key terms absent everywhere,
    the elements farthest from the goal."""
    import numpy as np
    from radogast.metrics.similarity import similarity_matrix, cluster_by_threshold
    from radogast.metrics.coverage import term_coverage
    vecs = np.asarray(vecs)
    m = similarity_matrix(vecs)
    # positional ids: cluster_by_threshold skips "ancestor" pairs by dotted-id
    # prefix, which would misfire on file paths like a.md / a.md#1
    pos = [str(i) for i in range(len(ids))]
    clusters = []
    for members in cluster_by_threshold(pos, m, threshold):
        idx = sorted(int(x) for x in members)
        pair = max((float(m[a, b]) for a in idx for b in idx if a < b), default=0.0)
        clusters.append({"ids": [ids[i] for i in idx], "max_sim": round(pair, 4)})
    clusters.sort(key=lambda c: -c["max_sim"])
    # the most similar pairs — union-find chains a whole topic into one cluster
    # at a low threshold, the pairs still show which elements actually repeat
    n = len(ids)
    pairs = sorted(((float(m[i, j]), i, j) for i in range(n) for j in range(i + 1, n)), reverse=True)[:5]
    top_pairs = [(ids[i], ids[j], round(s, 4)) for s, i, j in pairs]
    absent = []
    for term in target.key_terms or []:
        if all(term_coverage(t, [term]).get(term) == "absent" for t in texts):
            absent.append(term)
    sims = [float(np.dot(v, goal_vec)) for v in vecs]
    order = sorted(range(len(ids)), key=lambda i: sims[i])[:farthest]
    return {"clusters": clusters, "absent_terms": absent,
            "farthest": [(ids[i], round(sims[i], 4)) for i in order],
            "top_pairs": top_pairs, "threshold": threshold}


def format_rg_report(rep: dict) -> str:
    lines = [f"[loop] radogast stream report"]
    cl = rep.get("clusters") or []
    lines.append(f"  duplicate clusters (sim >= {rep.get('threshold')}): {len(cl)}")
    for k, c in enumerate(cl, 1):
        lines.append(f"    #{k}  {len(c['ids'])} elements, max sim {c['max_sim']:.3f}")
        for i in c["ids"]:
            lines.append(f"        {i}")
    if rep.get("top_pairs"):
        lines.append("  most similar pairs:")
        for a, b, s in rep["top_pairs"]:
            lines.append(f"    {s:.3f}  {a}  <->  {b}")
    ab = rep.get("absent_terms") or []
    lines.append("  key terms absent from every element: " + (", ".join(ab) if ab else "none"))
    lines.append("  farthest from goal:")
    for i, s in rep.get("farthest") or []:
        lines.append(f"    {s:.3f}  {i}")
    return "\n".join(lines)
