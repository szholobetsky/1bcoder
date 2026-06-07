"""scan — shorten a large file or directory into a single summary markdown.

Reads text files chunk by chunk, summarizes each chunk (or filters by query),
and writes the result to cwd/.1bcoder/scan/compact_K.md.

With --rounds N, re-compacts the output until it fits --target chars.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow scan <path> [flags]

Core flags:
  --query "text"   Filter mode: skip chunks not relevant to the query.
                   Without --query: general summarization of every chunk.
  --chunk N        Chunk size in chars (default 4000, ≈1K tokens).
  --summary M      Max chars per chunk summary (default 400).

File collection flags:
  --ext a,b,c      File extensions for dir scan (default: txt,md,py,rst,
                   js,ts,java,cpp,c,h,log,csv,json,yaml,yml,html,css).
  --all-ext        Include all files regardless of extension.

Recursion flags (opt-in):
  --rounds N       Number of compaction passes (default 1 — no recursion).
  --target N       Stop recursion when output length < N chars (default 8000).

Output:
  cwd/.1bcoder/scan/compact_K.md   (K auto-incremented)

Progress printed per chunk:
  Scan: 3/47 — 412 chars

Final line always:
  [scan] output: <filepath>

Examples:
  /flow scan README.md
  /flow scan ./docs --ext md,txt --chunk 2000 --summary 200
  /flow scan app.log --query "authorization violation"
  /flow scan ./logs --all-ext --query "error"
  /flow scan big_book.txt --rounds 3 --target 4000
"""
import os as _os
import re as _re

_DEFAULT_EXTENSIONS = "txt,md,py,rst,js,ts,java,cpp,c,h,log,csv,json,yaml,yml,html,css"
_DEFAULT_CHUNK   = 4000
_DEFAULT_SUMMARY = 400
_DEFAULT_TARGET  = 8000
_DEFAULT_ROUNDS  = 1


# ── file collection ───────────────────────────────────────────────────────────

def _collect(path: str, extensions) -> list:
    """Return list of (label, text).
    extensions=None means accept all files.
    Single file → one item. Dir → one per matched file (sorted).
    """
    if _os.path.isfile(path):
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception as e:
            print(f"[scan] could not read {path}: {e}")
            text = ""
        return [(_os.path.basename(path), text)]

    if not _os.path.isdir(path):
        print(f"[scan] not found: {path}")
        return []

    pairs = []
    for root, _, files in _os.walk(path):
        for fname in sorted(files):
            if extensions is None or any(fname.endswith("." + ext) for ext in extensions):
                fp = _os.path.join(root, fname)
                try:
                    text = open(fp, encoding="utf-8", errors="ignore").read()
                    pairs.append((fp, text))
                except Exception as e:
                    print(f"[scan] skip {fp}: {e}")
    return pairs


# ── output file ───────────────────────────────────────────────────────────────

def _next_compact_path() -> str:
    out_dir = _os.path.join(_os.getcwd(), ".1bcoder", "scan")
    _os.makedirs(out_dir, exist_ok=True)
    existing = [f for f in _os.listdir(out_dir)
                if f.startswith("compact_") and f.endswith(".md")]
    nums = []
    for f in existing:
        m = _re.search(r"compact_(\d+)", f)
        if m:
            nums.append(int(m.group(1)))
    n = max(nums, default=0) + 1
    return _os.path.join(out_dir, f"compact_{n}.md")


# ── LLM helpers ───────────────────────────────────────────────────────────────

def _llm(chat, prompt: str) -> str:
    msgs = [
        {"role": "system", "content": "You are a precise summarizer. Follow instructions exactly."},
        {"role": "user",   "content": prompt},
    ]
    return (chat._stream_chat(msgs) or "").strip()


def _is_relevant(chat, chunk: str, query: str) -> bool:
    prompt = (
        f'Does this text contain information relevant to: "{query}"?\n'
        f"Reply YES or NO only.\n\n{chunk}"
    )
    answer = _llm(chat, prompt)
    return answer.upper().startswith("YES")


def _summarize(chat, chunk: str, summary_chars: int, query: str = "") -> str:
    if query:
        prompt = (
            f"Summarize the following text in {summary_chars} characters or less, "
            f'focusing on: "{query}". '
            f"Preserve specific facts, names, numbers. Output ONLY the summary.\n\n{chunk}"
        )
    else:
        prompt = (
            f"Summarize the following text in {summary_chars} characters or less. "
            f"Preserve key facts, numbers, names, and technical details. "
            f"Output ONLY the summary.\n\n{chunk}"
        )
    return _llm(chat, prompt)


# ── single compaction pass ────────────────────────────────────────────────────

def _run_pass(chat, source_label: str, full_text: str,
              query: str, chunk_size: int, summary_chars: int,
              pass_n: int) -> tuple:
    """Compact full_text → return (output_text, out_path)."""
    chunks = [full_text[i:i + chunk_size]
              for i in range(0, len(full_text), chunk_size)]
    total = len(chunks)
    mode = "filter" if query else "summary"
    print(f"\n[scan] pass {pass_n}  input={len(full_text)} chars  "
          f"chunks={total}  mode={mode}")

    summaries = []
    skipped = 0
    for idx, chunk in enumerate(chunks, 1):
        print(f"Scan: {idx}/{total}")
        if query:
            if not _is_relevant(chat, chunk, query):
                print(f"  skip — not relevant")
                skipped += 1
                continue
            text = _summarize(chat, chunk, summary_chars, query)
        else:
            text = _summarize(chat, chunk, summary_chars)

        if text:
            label = text[:80].replace("\n", " ") + ("..." if len(text) > 80 else "")
            print(f"  {len(text)} chars — {label}")
            summaries.append(text)

    output_text = "\n\n".join(summaries)
    out_path = _next_compact_path()

    header_lines = [f"# Scan: {source_label}", f"Pass: {pass_n}"]
    if query:
        header_lines.append(f"Query: {query}")
    header_lines += [
        f"Chunks: {total}  skipped: {skipped}  matched: {total - skipped}",
        f"Input: {len(full_text)} chars → Output: {len(output_text)} chars",
        "",
    ]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(header_lines) + "\n\n" + output_text)

    print(f"[scan] pass {pass_n} done: {len(full_text)} → {len(output_text)} chars"
          f"  saved: {out_path}")
    return output_text, out_path


# ── main ──────────────────────────────────────────────────────────────────────

def run(chat, args: str):
    args = args.strip()

    # ── parse flags ───────────────────────────────────────────────────────────
    query = ""
    m = _re.search(r'--query\s+(?:"([^"]+)"|\'([^\']+)\')', args)
    if m:
        query = m.group(1) or m.group(2)
        args = (args[:m.start()] + args[m.end():]).strip()

    chunk_size = _DEFAULT_CHUNK
    m = _re.search(r'--chunk\s+(\d+)', args)
    if m:
        chunk_size = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    summary_chars = _DEFAULT_SUMMARY
    m = _re.search(r'--summary\s+(\d+)', args)
    if m:
        summary_chars = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    target_chars = _DEFAULT_TARGET
    m = _re.search(r'--target\s+(\d+)', args)
    if m:
        target_chars = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    max_rounds = _DEFAULT_ROUNDS
    m = _re.search(r'--rounds\s+(\d+)', args)
    if m:
        max_rounds = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    all_ext = "--all-ext" in args
    args = args.replace("--all-ext", "").strip()

    extensions = None if all_ext else [
        e.strip().lstrip(".") for e in _DEFAULT_EXTENSIONS.split(",") if e.strip()
    ]
    m = _re.search(r'--ext\s+(\S+)', args)
    if m:
        if not all_ext:
            extensions = [e.strip().lstrip(".") for e in m.group(1).split(",") if e.strip()]
        args = (args[:m.start()] + args[m.end():]).strip()

    # path: remaining arg (strip quotes)
    path = args.strip().strip("\"'")
    if not path:
        print("usage: /flow scan <path> [--query 'text'] [--chunk N] [--summary M]")
        print("       [--rounds N] [--target N] [--ext a,b,c] [--all-ext]")
        return

    print(f"[scan] path        : {path}")
    print(f"[scan] query       : {query or '(none — general summary)'}")
    print(f"[scan] chunk       : {chunk_size} chars")
    print(f"[scan] summary     : {summary_chars} chars/chunk")
    print(f"[scan] rounds      : {max_rounds}" + (" (no recursion)" if max_rounds == 1 else ""))
    if max_rounds > 1:
        print(f"[scan] target      : {target_chars} chars")
    if _os.path.isdir(path):
        print(f"[scan] extensions  : {'ALL' if all_ext else (', '.join(extensions) if extensions else 'ALL')}")

    # ── collect ───────────────────────────────────────────────────────────────
    pairs = _collect(path, extensions)
    if not pairs:
        print(f"[scan] no files found at: {path}")
        return

    print(f"[scan] files       : {len(pairs)}")

    full_text = ""
    for label, content in pairs:
        if len(pairs) > 1:
            full_text += f"\n\n--- {label} ---\n{content}"
        else:
            full_text += content

    print(f"[scan] total input : {len(full_text)} chars")

    # ── passes ────────────────────────────────────────────────────────────────
    source_label = path
    final_path = None
    current_text = full_text

    for pass_n in range(1, max_rounds + 1):
        current_text, out_path = _run_pass(
            chat, source_label, current_text,
            query, chunk_size, summary_chars, pass_n,
        )
        final_path = out_path
        source_label = out_path

        if max_rounds > 1:
            if len(current_text) <= target_chars:
                print(f"[scan] target reached ({len(current_text)} <= {target_chars} chars)")
                break
            if pass_n < max_rounds:
                print(f"[scan] still {len(current_text)} chars > target {target_chars}"
                      f" — starting pass {pass_n + 1}")

    if max_rounds > 1 and len(current_text) > target_chars:
        print(f"[scan] max rounds ({max_rounds}) reached — output may still exceed target")

    print(f"\n[scan] output: {final_path}")
