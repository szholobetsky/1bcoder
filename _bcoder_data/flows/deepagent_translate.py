"""deepagent_translate — recursively translate a folder or file with the LLM.

Walks a folder (or a single file), chunks each file (markdown-header-aware
for .md, plain non-overlapping char chunks otherwise), translates each
chunk, and writes translated output preserving directory structure and
filenames under a separate output directory.

Without --profile: uses 1bcoder's current active model (chat.model) and
processes every file strictly sequentially.

With --profile <name>: loads worker (host, model) pairs from profiles.txt
and distributes FILES round-robin across workers — each file is fully
owned by exactly one worker for its whole chunk sequence, so chunks of a
single file are never split across models/workers. Different files may
still translate concurrently on different workers.

Usage:
  /flow deepagent_translate <path> --lang <code> --out <dir> [--from <code>] [--chunk N] [--profile <name>]

flags:
  --lang <code>     target language ISO code (e.g. uk)                [required]
  --out <dir>       output root; input file's relative path/name is    [required]
                    mirrored under it (docs/readme.md -> <out>/readme.md)
  --from <code>     source language ISO code (default: en)
  --chunk <N>       chunk size in approximate tokens, ~4 chars/token (default: 1000)
  --profile <name>  profiles.txt profile — one worker per file, round-robin

Examples:
  /flow deepagent_translate docs --lang uk --out docs_uk
  /flow deepagent_translate README.md --lang es --from en --out out_es --chunk 800
  /flow deepagent_translate docs --lang uk --out docs_uk --profile lmtrans
"""

import os as _os
import re as _re

_DEFAULT_CHUNK_TOKENS = 1000
_CHARS_PER_TOKEN = 4

_NOISE_DIRS = {".git", "node_modules", "__pycache__", ".1bcoder", ".venv", "venv"}

_MD_HEADER_RE = _re.compile(r'^#{1,6}\s+.*$')  # matched per-line

# Local copy — chat.py's _LANG_NAMES is private inside a method and chat.py
# is the entry script, not safely importable from a dynamically exec'd flow
# file. Falls back to the raw code itself if not found here.
_LANG_NAMES = {
    "en": "English", "uk": "Ukrainian", "es": "Spanish", "fr": "French", "de": "German",
    "pl": "Polish", "ru": "Russian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "pt": "Portuguese", "it": "Italian", "nl": "Dutch", "tr": "Turkish", "ar": "Arabic",
    "cs": "Czech", "sv": "Swedish", "da": "Danish", "fi": "Finnish", "el": "Greek",
    "he": "Hebrew", "hi": "Hindi", "id": "Indonesian", "vi": "Vietnamese", "ro": "Romanian",
    "hu": "Hungarian", "bg": "Bulgarian", "sk": "Slovak", "sl": "Slovenian", "hr": "Croatian",
    "sr": "Serbian", "th": "Thai",
}


# ── arg parsing ──────────────────────────────────────────────────────────────

_USAGE = ("usage: /flow deepagent_translate <path> --lang <code> --out <dir> "
          "[--from <code>] [--chunk N] [--profile <name>]")


def _pop_flag_value(rest: str, pattern: str):
    m = _re.search(pattern, rest)
    if not m:
        return None, rest
    return m.group(1), (rest[:m.start()] + rest[m.end():]).strip()


def _parse_args(args: str):
    rest = args

    tgt, rest = _pop_flag_value(rest, r'--lang\s+(\S+)')
    out_dir, rest = _pop_flag_value(rest, r'--out\s+(\S+)')
    src, rest = _pop_flag_value(rest, r'--from\s+(\S+)')
    chunk_tokens, rest = _pop_flag_value(rest, r'--chunk\s+(\d+)')
    profile, rest = _pop_flag_value(rest, r'--profile\s+(\S+)')

    path = rest.strip().strip('"\'')

    unknown = [t for t in path.split() if t.startswith("--")]
    if unknown:
        print(f"[deepagent_translate] unknown flag(s): {', '.join(unknown)}")
        print(_USAGE)
        return None

    missing = []
    if not path:
        missing.append("<path>")
    if not tgt:
        missing.append("--lang <code>")
    if not out_dir:
        missing.append("--out <dir>")
    if missing:
        print(f"[deepagent_translate] missing: {', '.join(missing)}")
        print(_USAGE)
        return None

    return {
        "path": path,
        "src": (src or "en"),
        "tgt": tgt,
        "out": out_dir,
        "chunk_tokens": int(chunk_tokens) if chunk_tokens else _DEFAULT_CHUNK_TOKENS,
        "profile": profile,
    }


# ── file collection ──────────────────────────────────────────────────────────

def _is_probably_binary(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(2048)
        if b"\x00" in chunk:
            return True
        chunk.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True
    except Exception:
        return True


def _collect_files(path: str):
    """Return (files, binary_skipped_count)."""
    if _os.path.isfile(path):
        candidates = [path]
    elif _os.path.isdir(path):
        candidates = []
        for root, dirs, files in _os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in _NOISE_DIRS)
            for fname in sorted(files):
                candidates.append(_os.path.join(root, fname))
    else:
        return [], 0

    files, binary_skipped = [], 0
    for fp in candidates:
        if _is_probably_binary(fp):
            print(f"[deepagent_translate] skip (binary): {fp}")
            binary_skipped += 1
            continue
        files.append(fp)
    return files, binary_skipped


# ── chunking ─────────────────────────────────────────────────────────────────

def _char_chunks(text: str, chunk_chars: int) -> list:
    """Sequential, non-overlapping char chunks. Overlap is intentionally not
    supported — chunks are concatenated verbatim into the output file, and
    overlap would duplicate text there. Prefers breaking at the last
    newline/space within the tail 20% of the window for a cleaner cut."""
    if chunk_chars <= 0 or len(text) <= chunk_chars:
        return [text] if text else []
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            window_start = start + int(chunk_chars * 0.8)
            for i in range(end - 1, max(window_start, start), -1):
                if text[i] in "\n ":
                    end = i + 1
                    break
        chunks.append(text[start:end])
        start = end
    return chunks


def _md_sections(text: str) -> list:
    """Split text into header-delimited segments at every line matching
    _MD_HEADER_RE (any level, # through ######)."""
    lines = text.splitlines(keepends=True)
    boundaries = [0]
    for i, line in enumerate(lines):
        if i == 0:
            continue
        if _MD_HEADER_RE.match(line.rstrip("\n")):
            boundaries.append(i)
    boundaries.append(len(lines))
    boundaries = sorted(set(boundaries))

    segments = []
    for i in range(len(boundaries) - 1):
        seg = "".join(lines[boundaries[i]:boundaries[i + 1]])
        if seg.strip():
            segments.append(seg)
    return segments


def _md_chunks(text: str, chunk_chars: int) -> list:
    """Greedy-pack consecutive header segments into <= chunk_chars chunks.
    A section exceeding the budget alone is sub-split with _char_chunks."""
    segments = _md_sections(text)
    if not segments:
        return _char_chunks(text, chunk_chars)

    chunks, buf = [], ""
    for seg in segments:
        if len(seg) > chunk_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_char_chunks(seg, chunk_chars))
            continue
        if buf and len(buf) + len(seg) > chunk_chars:
            chunks.append(buf)
            buf = seg
        else:
            buf += seg
    if buf:
        chunks.append(buf)
    return chunks


def _chunk_file(text: str, ext: str, chunk_chars: int) -> list:
    chunks = _md_chunks(text, chunk_chars) if ext == "md" else _char_chunks(text, chunk_chars)
    return [c for c in chunks if c.strip()]


# ── profile loading + direct HTTP call (for --profile parallel mode) ────────

def _load_translate_profile(name: str):
    """Return list of (host, model) pairs for a --profile name, read from
    profiles.txt (project-local .1bcoder/profiles.txt takes precedence over
    ~/.1bcoder/profiles.txt) — same file/format chat.py's _load_profile reads
    (chat.py:1888-1910), duplicated locally since a dynamically exec'd flow
    file cannot safely import chat.py as a module. The worker spec's third
    field (filename, used by /parallel) is irrelevant here and ignored —
    each worker translates many files, not one fixed output file."""
    candidates = [
        _os.path.join(_os.getcwd(), ".1bcoder", "profiles.txt"),
        _os.path.join(_os.path.expanduser("~"), ".1bcoder", "profiles.txt"),
    ]
    for profiles_file in candidates:
        if not _os.path.exists(profiles_file):
            continue
        with open(profiles_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pname, _sep, rest = line.partition(":")
                if pname.strip() != name:
                    continue
                rest = rest.split("#")[0]
                workers = []
                for spec in rest.split():
                    parts = spec.split("|", 2)
                    if len(parts) >= 2:
                        workers.append((parts[0], parts[1]))
                if workers:
                    return workers
    return None


def _parse_host(host_str: str):
    """Local copy of chat.py's parse_host (chat.py:1403-1413)."""
    s = host_str.rstrip("/")
    if s.startswith("ollama://"):
        return "http://" + s[len("ollama://"):], "ollama"
    if s.startswith("openai://"):
        return "http://" + s[len("openai://"):], "openai"
    if not s.startswith(("http://", "https://")):
        s = "http://" + s
    return s, "ollama"


def _call_llm_direct(host: str, model: str, messages: list, timeout: int = 300):
    """Non-streaming direct HTTP call to a worker's own host/model. Mirrors
    the /parallel command's call_one HTTP shape (chat.py:9552-9578), kept
    local because chat._stream_chat is NOT safe to call concurrently from
    worker threads — it mutates shared self.* streaming/stats state and
    prints tokens live to stdout as they arrive."""
    import requests
    url, provider = _parse_host(host)
    if provider == "openai":
        resp = requests.post(f"{url}/v1/chat/completions",
                              json={"model": model, "messages": messages, "stream": False},
                              timeout=timeout)
        resp.raise_for_status()
        return (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
    resp = requests.post(f"{url}/api/chat",
                          json={"model": model, "messages": messages, "stream": False},
                          timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("message", {}).get("content", "")


# ── LLM message building ─────────────────────────────────────────────────────

def _build_messages(chunk_text: str, src: str, tgt: str, model: str) -> list:
    """Message list for chat._stream_chat. TranslateGemma gets the exact
    verified single-user-message template (two literal blank lines before
    the text) per https://ollama.com/library/translategemma. Any other
    model gets a short system+user split instead, per-chunk, with no
    repeated instructions baked into every chunk's user content."""
    src_name = _LANG_NAMES.get(src.lower(), src)
    tgt_name = _LANG_NAMES.get(tgt.lower(), tgt)
    is_translategemma = "translategemma" in (model or "").lower()

    if is_translategemma:
        prompt = (
            f"You are a professional {src_name} ({src}) to {tgt_name} ({tgt}) translator. "
            f"Your goal is to accurately convey the meaning and nuances of the original "
            f"{src_name} text while adhering to {tgt_name} grammar, vocabulary, and cultural "
            f"sensitivities.\n"
            f"Produce only the {tgt_name} translation, without any additional explanations or "
            f"commentary. Please translate the following {src_name} text into {tgt_name}:\n\n\n"
            f"{chunk_text}"
        )
        return [{"role": "user", "content": prompt}]

    system = (
        f"You are a professional translator from {src_name} ({src}) to {tgt_name} ({tgt}). "
        f"Translate the user's text faithfully, preserving meaning, tone, and any markdown or "
        f"code formatting. Output ONLY the {tgt_name} translation — no explanations, no "
        f"commentary, no repeated source text."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": chunk_text}]


# ── summary ──────────────────────────────────────────────────────────────────

def _print_summary(files_done, total_files, chunks_done, binary_skipped, other_skipped) -> None:
    print(f"\n[deepagent_translate] done — files: {files_done}/{total_files}  "
          f"chunks translated: {chunks_done}  "
          f"skipped: {binary_skipped} binary, {other_skipped} other")


# ── per-file / per-worker driving logic ──────────────────────────────────────

def _translate_file(fp, file_idx, total_files, in_root, out_dir, chunk_chars,
                     src, tgt, model, llm_call, stats, tag="") -> str:
    """Translate one file's chunks strictly sequentially (a single file is
    ALWAYS handled start-to-finish within one call — never split across
    workers), writing the destination file incrementally.
    Returns "ok" | "skipped" | "stopped"; increments stats["chunks_done"]
    in place as each chunk is written."""
    relpath = _os.path.relpath(fp, in_root)
    ext = _os.path.splitext(fp)[1].lstrip(".").lower()
    prefix = f"[{tag}] " if tag else ""

    try:
        with open(fp, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError as e:
        print(f"{prefix}[deepagent_translate] skip (read error) {fp}: {e}")
        return "skipped"

    chunks = _chunk_file(text, ext, chunk_chars)
    if not chunks:
        return "skipped"

    total_chunks = len(chunks)
    print(f"{prefix}[deepagent_translate] file: {file_idx}/{total_files}  "
          f"{relpath}  ({total_chunks} chunk(s))")

    dest_path = _os.path.join(out_dir, relpath)
    _os.makedirs(_os.path.dirname(dest_path) or ".", exist_ok=True)

    with open(dest_path, "w", encoding="utf-8") as out_f:
        for i, chunk in enumerate(chunks, 1):
            print(f"{prefix}Translate Chunk: {i}/{total_chunks} File: {file_idx}/{total_files}")
            messages = _build_messages(chunk, src, tgt, model)
            try:
                translated = llm_call(messages)
            except KeyboardInterrupt:
                translated = None
            except Exception as e:
                print(f"{prefix}[deepagent_translate] error translating chunk {i}/{total_chunks} "
                      f"of {relpath}: {e}")
                translated = None
            if translated is None:
                print(f"{prefix}[deepagent_translate] stopped — {stats['chunks_done']} chunk(s) "
                      f"already written are safe on disk (partial file kept: {dest_path})")
                return "stopped"
            if i > 1:
                out_f.write("\n\n")
            out_f.write(translated.strip())
            out_f.flush()
            stats["chunks_done"] += 1
    return "ok"


def _run_worker(files_with_idx, total_files, in_root, out_dir, chunk_chars,
                 src, tgt, model, llm_call, tag=""):
    """Process an ordered list of (global_file_idx, path) items strictly
    sequentially — the unit of work for one worker (thread), whether that
    worker is the sole sequential run or one of several --profile workers."""
    stats = {"files_done": 0, "chunks_done": 0, "other_skipped": 0}
    for file_idx, fp in files_with_idx:
        status = _translate_file(fp, file_idx, total_files, in_root, out_dir, chunk_chars,
                                  src, tgt, model, llm_call, stats, tag)
        if status == "ok":
            stats["files_done"] += 1
        elif status == "skipped":
            stats["other_skipped"] += 1
        elif status == "stopped":
            break
    return stats


# ── entry point ──────────────────────────────────────────────────────────────

def run(chat, args: str) -> None:
    args = args.strip()
    if not args:
        print(__doc__)
        return

    parsed = _parse_args(args)
    if parsed is None:
        return

    path, src, tgt = parsed["path"], parsed["src"], parsed["tgt"]
    out_dir, chunk_tokens, profile_name = parsed["out"], parsed["chunk_tokens"], parsed["profile"]
    chunk_chars = chunk_tokens * _CHARS_PER_TOKEN

    files, binary_skipped = _collect_files(path)
    if not files:
        print(f"[deepagent_translate] no files found at: {path}")
        return

    # relpath root: the directory itself for a dir input, or the file's
    # parent for a single-file input (so output is <out>/<basename>)
    in_root = path if _os.path.isdir(path) else (_os.path.dirname(path) or ".")
    total_files = len(files)
    indexed_files = list(enumerate(files, 1))  # global (file_idx, path) pairs

    if profile_name:
        workers = _load_translate_profile(profile_name)
        if not workers:
            print(f"[deepagent_translate] profile '{profile_name}' not found or empty")
            return

        n = len(workers)
        print(f"[deepagent_translate] profile: {profile_name}  {n} worker(s)  "
              f"{src} -> {tgt}  files: {total_files}  chunk: {chunk_tokens}t  out: {out_dir}")

        # Round-robin FILE assignment — each file is fully owned by one
        # worker for its entire chunk sequence, so chunks of a single file
        # are never split across workers/models.
        buckets = [[] for _ in range(n)]
        for k, item in enumerate(indexed_files):
            buckets[k % n].append(item)

        import concurrent.futures

        def _make_llm_call(host, model):
            return lambda messages: _call_llm_direct(host, model, messages)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futures = [
                pool.submit(_run_worker, buckets[w_idx], total_files, in_root, out_dir,
                            chunk_chars, src, tgt, model, _make_llm_call(host, model),
                            tag=f"{model}@{host}")
                for w_idx, (host, model) in enumerate(workers)
            ]
            results = [fut.result() for fut in concurrent.futures.as_completed(futures)]

        files_done = sum(r["files_done"] for r in results)
        chunks_done = sum(r["chunks_done"] for r in results)
        other_skipped = sum(r["other_skipped"] for r in results)
    else:
        print(f"[deepagent_translate] model: {chat.model}  {src} -> {tgt}  "
              f"files: {total_files}  chunk: {chunk_tokens}t  out: {out_dir}")

        def llm_call(messages):
            try:
                return chat._stream_chat(messages)
            except KeyboardInterrupt:
                return None

        stats = _run_worker(indexed_files, total_files, in_root, out_dir, chunk_chars,
                             src, tgt, chat.model, llm_call)
        files_done = stats["files_done"]
        chunks_done = stats["chunks_done"]
        other_skipped = stats["other_skipped"]

    _print_summary(files_done, total_files, chunks_done, binary_skipped, other_skipped)
