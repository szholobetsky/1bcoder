"""glossary — LLM-wiki knowledge base builder.

Chunks a folder of files (prose / structured data / source code), asks the
LLM to extract terms per chunk, and builds one markdown file per term
(DEFINITION:/FACTS:/LINK: sections) plus a flat glossary.md index. Designed
for small local models: every LLM call that must return a structured value
uses a strict single-line marker + regex extraction, never free parsing.

Self-contained (stdlib only) — see concepts/GLOSSARY.md for the full design
rationale (Karpathy llm-wiki inspiration, why code needs a think-step, why
--unique is pure Python not an LLM continuation trick).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow glossary index <path> [flags]
  /flow glossary show <term> [ctx] [--project P]
  /flow glossary extract <query> [--project P]
  /flow glossary find <text> [--project P]
  /flow glossary relink [--project P]
  /flow glossary <term1> \\term2 -term3 [--project P]

index flags:
  --project P       glossary namespace (default: "default")
  --chunk N         chunk size in tokens (default 1000, ~4 chars/token)
  --overlap N       sliding overlap in tokens for char-based chunking (default 50)
  --redefine        wipe DEFINITION: for every term touched this run and start
                     its candidate list fresh from the current chunk (e.g.
                     after upgrading to a bigger/better model). Without it,
                     DEFINITION: is append-only — like FACTS:, never
                     overwritten: each chunk that mentions an existing term
                     adds one more candidate definition rather than replacing
                     the old one (a term seen across many chunks ends up with
                     several DEFINITION: candidates, not one frozen guess).
  --refact          rewrite FACTS: from scratch instead of append-only
  --crosslink       after indexing, scan every term's text for other known
                     terms and write LINK: (run once at the end against the
                     FULL term list — not per-chunk, since early terms would
                     otherwise never see terms discovered later in the run)
  --unique          dedupe new DEFINITION:/FACTS: candidates against existing
                     ones (interactive, no LLM) instead of always appending
  --tabular         also index csv/json/yaml/tsv with a schema-profiling prompt

  glossary.md is always re-sorted alphabetically at the end of an index run.

show:     print <term>.md; trailing "ctx" token also appends it to context
          (no interactive confirm — safe to call from an agent ACTION: loop)
extract:  fuzzy subword match of free text against known terms (like
          /map keyword extract -f) — for code-identifier-style queries
find:     full-text search across all terms' DEFINITION:+FACTS:
relink:   pure-Python postprocessing pass, no LLM call — re-sorts glossary.md
          and recomputes every term's LINK: against the current full term
          list. Safe to re-run anytime, including on output from an older
          run (e.g. before this feature existed, or after --crosslink was
          skipped originally).
filter:   /map-style token grammar, one term.md = one "block"
          \\term  = FACTS line contains term       -term = show only matching lines
          -!term  = hide matching lines             !term = term name must NOT contain
          +term   = show only matching, drop file if none match

Examples:
  /flow glossary index docs --project myapp
  /flow glossary index src --project myapp --crosslink --unique
  /flow glossary index . --project myapp --redefine --refact
  /flow glossary show user-birthdate ctx --project myapp
  /flow glossary extract "fix rule search" --project myapp
  /flow glossary find "amortization" --project myapp
  /flow glossary relink --project myapp
  /flow glossary rule \\index -deprecated --project myapp
"""
import os as _os
import re as _re
import difflib as _difflib

# ── config ───────────────────────────────────────────────────────────────────

_DEFAULT_CHUNK_TOKENS   = 1000
_DEFAULT_OVERLAP_TOKENS = 50
_CHARS_PER_TOKEN        = 4
_UNIQUE_THRESHOLD       = 0.8
_CROSSLINK_MIN_LEN      = 5

_PROSE_EXT   = {"txt", "md", "rst", "log"}
_TABULAR_EXT = {"csv", "json", "yaml", "yml", "tsv"}
_IMG_EXT     = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg"}

# boundary regex per language — ported from deepagent_code.py's LANGS table
# (func_re fields), keyed by file extension instead of --lang name.
_CODE_BOUNDARY_RE = {
    "py":    r'(?:def|class)\s+(\w+)',
    "js":    r'(?:function)\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=',
    "ts":    r'(?:function)\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=',
    "java":  r'(?:class|interface)\s+(\w+)',
    "go":    r'(?:func)\s+(\w+)',
    "rb":    r'(?:def|class)\s+(\w+)',
    "php":   r'(?:function|class)\s+(\w+)',
    "kt":    r'(?:fun|class)\s+(\w+)',
    "scala": r'(?:def|class|object)\s+(\w+)',
    "sql":   r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|FUNCTION|PROCEDURE|TRIGGER|VIEW)\s+(\w+)',
}
# code bucket is broader than the boundary-regex table — extensions without a
# regex still get think-step treatment, just fall back to char chunking.
_CODE_EXT = set(_CODE_BOUNDARY_RE) | {"c", "h", "cpp", "hpp", "cc", "cs", "rs", "swift"}

# obvious noise identifiers, filtered out of code TERMS candidates before the
# LLM even sees them (loop counters, temp names) — per the user_birthdate vs i
# distinction in concepts/GLOSSARY.md.
_STOP_IDENT = {"i", "j", "k", "n", "x", "y", "z", "tmp", "idx", "cnt", "a", "b", "c"}


# ── glossary storage ────────────────────────────────────────────────────────

def _glossary_dir(project: str) -> str:
    proj = project or "default"
    d = _os.path.join(_os.getcwd(), ".1bcoder", "glossary", proj)
    _os.makedirs(d, exist_ok=True)
    return d


def _glossary_md_path(project: str) -> str:
    return _os.path.join(_glossary_dir(project), "glossary.md")


def _kebab(term: str) -> str:
    s = term.strip().lower()
    s = _re.sub(r'[^a-z0-9]+', '-', s)
    s = _re.sub(r'-+', '-', s).strip('-')
    return s


def _term_path(project: str, term: str) -> str:
    return _os.path.join(_glossary_dir(project), _kebab(term) + ".md")


def _think_dir(project: str) -> str:
    d = _os.path.join(_glossary_dir(project), "_think")
    _os.makedirs(d, exist_ok=True)
    return d


def _load_terms(project: str) -> list:
    path = _glossary_md_path(project)
    if not _os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]


def _add_term(project: str, term: str) -> str:
    kterm = _kebab(term)
    if kterm and kterm not in _load_terms(project):
        with open(_glossary_md_path(project), "a", encoding="utf-8") as f:
            f.write(kterm + "\n")
    return kterm


def _sort_glossary(project: str) -> None:
    terms = sorted(set(_load_terms(project)))
    with open(_glossary_md_path(project), "w", encoding="utf-8") as f:
        f.write("\n".join(terms) + ("\n" if terms else ""))


def _read_term_file(project: str, term: str) -> dict:
    """DEFINITION: is a growing list of candidate definitions, one bullet per
    chunk that produced one — never overwritten on an existing term (same
    append-only philosophy as FACTS:), unless --redefine explicitly wipes it.
    Tolerates the older single-paragraph format (pre-dates this change): a
    DEFINITION: block with no bullet lines is treated as one candidate."""
    kterm = _kebab(term)
    path = _term_path(project, kterm)
    if not _os.path.isfile(path):
        return {"term": kterm, "definitions": [], "facts": [], "links": []}
    text = open(path, encoding="utf-8").read()
    definitions, facts, links = [], [], []
    m = _re.search(r'DEFINITION:\s*\n(.*?)(?=\nFACTS:|\nLINK:|\Z)', text, _re.DOTALL)
    if m:
        block = m.group(1).strip()
        if block:
            body_lines = [l for l in block.splitlines() if l.strip()]
            bullet_lines = [l for l in body_lines if _re.match(r'^\s*[-*]\s+', l)]
            if bullet_lines and len(bullet_lines) == len(body_lines):
                definitions = [l.lstrip("-*").strip() for l in bullet_lines]
            else:
                definitions = [block]  # legacy plain-paragraph format
    m = _re.search(r'FACTS:\s*\n(.*?)(?=\nLINK:|\Z)', text, _re.DOTALL)
    if m:
        facts = [l.lstrip("-*").strip() for l in m.group(1).splitlines() if l.strip()]
    m = _re.search(r'LINK:\s*\n(.*)\Z', text, _re.DOTALL)
    if m:
        raw_links = [l.lstrip("-*").strip() for l in m.group(1).splitlines() if l.strip()]
        links = []
        for l in raw_links:
            # accept both "[term](term.md)" (current write format, clickable
            # in any standard markdown viewer) and a bare "term" (older files,
            # or ones never touched by --crosslink/relink)
            lm = _re.match(r'\[([^\]]+)\]\([^)]+\)', l)
            links.append(lm.group(1) if lm else l)
    return {"term": kterm, "definitions": definitions, "facts": facts, "links": links}


def _write_term_file(project: str, term: str, data: dict) -> None:
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
        # standard CommonMark link, not Obsidian-style [[wikilinks]] (not
        # part of any markdown spec) — renders clickable in VS Code/GitHub
        # previews while still regex-parseable by _read_term_file above.
        lines.append(f"- [{link}]({link}.md)")
    with open(_term_path(project, kterm), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_think(project: str, source_label: str, chunk_idx: int, text: str) -> None:
    safe = _re.sub(r'[^\w.\-]', '_', source_label)
    path = _os.path.join(_think_dir(project), f"{safe}_chunk{chunk_idx}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# think: {source_label} chunk {chunk_idx}\n\n{text}")


# ── subword fuzzy match — ported from chat.py _split_identifier (2324-2355) ──
# and the -f fuzzy branch of /map keyword extract (chat.py 9895-9909).        #

def _split_identifier(name: str) -> list:
    parts = _re.split(r'[_\-]+', name)
    result = []
    for part in parts:
        if not part:
            continue
        s = _re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', part)
        s = _re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
        result.extend(w.lower() for w in s.split('_') if len(w) >= 2)
    seen: dict = {}
    for w in result:
        seen.setdefault(w, None)
    return list(seen)


def extract(query_text: str, terms: list) -> list:
    """Fuzzy subword match: which known `terms` are referenced in query_text.
    Used by the `extract` query subcommand — matches code-identifier-style
    queries (subwords fused into one token, e.g. "RuleIndex") against known
    terms. NOT used for --crosslink: crosslink matches term names inside
    generated PROSE, where a multi-word term's subwords appear as separate
    space-separated words, never fused into a single token — this function
    would (and empirically did, in a real run) return zero matches for that
    case. See _phrase_in_text/_relink_project below for the crosslink path."""
    token_re = _re.compile(r'[a-zA-Z_][a-zA-Z0-9_\-]{1,}')
    term_parts = {t: frozenset(_split_identifier(t)) for t in terms}
    seen: dict = {}
    for i, m in enumerate(token_re.finditer(query_text)):
        query_parts = frozenset(w for w in _split_identifier(m.group()) if len(w) >= 5)
        if not query_parts:
            continue
        for t, tp in term_parts.items():
            if query_parts <= tp and t not in seen:
                seen[t] = i
    return sorted(seen, key=lambda t: seen[t])


def _phrase_in_text(term: str, text_lower: str) -> bool:
    """Whether `term` (a kebab-case term name) appears as a contiguous
    phrase in already-lowercased text — either hyphen-joined (the term's own
    form) or space-joined (how it naturally appears in prose DEFINITION:/
    FACTS: text). This is the crosslink matcher: a different problem from
    extract()'s fused-identifier fuzzy match."""
    if not term:
        return False
    return term in text_lower or " ".join(term.split("-")) in text_lower


def _relink_project(project: str) -> int:
    """Recompute LINK: for every term file against the FULL current term
    list, and retroactively strip leaked prompt text out of DEFINITION:
    (see _clean_definition) and FACTS: (see _LEAKED_LABEL_PREFIXES) — older
    runs, or runs predating those fixes, may have it baked in. Pure Python,
    no LLM call — safe and cheap to re-run anytime.

    LINK: is recomputed here rather than per-chunk during indexing: a term
    discovered early in a run has no way to see terms discovered later in
    the same run if checked incrementally, so an end-of-run pass over the
    complete term list is required for correct (not just chronologically
    partial) links."""
    terms = _load_terms(project)
    updated = 0
    for term in terms:
        data = _read_term_file(project, term)
        changed = False

        cleaned_defs = [_clean_definition(d) for d in data["definitions"]]
        cleaned_defs = [d for d in cleaned_defs if d]
        if cleaned_defs != data["definitions"]:
            data["definitions"] = cleaned_defs
            changed = True

        cleaned_facts = [f for f in data["facts"]
                         if not f.lower().startswith(_LEAKED_LABEL_PREFIXES)]
        if cleaned_facts != data["facts"]:
            data["facts"] = cleaned_facts
            changed = True

        text = "\n".join(data["definitions"] + data["facts"]).lower()
        candidates = [t for t in terms if t != term and len(t) >= _CROSSLINK_MIN_LEN]
        links = sorted(t for t in candidates if _phrase_in_text(t, text))
        if links != data["links"]:
            data["links"] = links
            changed = True

        if changed:
            _write_term_file(project, term, data)
            updated += 1
    return updated


# ── filter-token grammar — ported from map_query.find_map() (158-234) ───────
# "block" = one term.md file; line 1 (term name) ~ pos_file; FACTS lines ~   #
# child lines.                                                               #

def _query_filter(project: str, tokens: list) -> list:
    pos_file, neg_file = [], []
    pos_child, neg_block = [], []
    show_lines, hide_lines, must_show_lines = [], [], []

    for t in tokens:
        if t.startswith("\\!") and len(t) > 2:
            neg_block.append(t[2:].lower())
        elif t.startswith("\\") and len(t) > 1:
            pos_child.append(t[1:].lower())
        elif t.startswith("-!") and len(t) > 2:
            hide_lines.append(t[2:].lower())
        elif t.startswith("-") and len(t) > 1:
            show_lines.append(t[1:].lower())
        elif t.startswith("+") and len(t) > 1:
            must_show_lines.append(t[1:].lower())
        elif t.startswith("!") and len(t) > 1:
            neg_file.append(t[1:].lower())
        else:
            pos_file.append(t.lower())

    hits = []
    for term in _load_terms(project):
        data = _read_term_file(project, term)
        fname = term.lower()
        child_lines = list(data["facts"])

        if pos_file and not all(tok in fname for tok in pos_file):
            continue
        if any(tok in fname for tok in neg_file):
            continue
        if pos_child and not any(all(tok in line.lower() for tok in pos_child)
                                  for line in child_lines):
            continue
        if neg_block:
            children_text = "\n".join(child_lines).lower()
            if any(tok in children_text for tok in neg_block):
                continue

        shown = child_lines
        if show_lines:
            shown = [l for l in shown if any(tok in l.lower() for tok in show_lines)]
        if must_show_lines:
            shown = [l for l in shown if any(tok in l.lower() for tok in must_show_lines)]
            if not shown:
                continue
        if hide_lines:
            shown = [l for l in shown if not any(tok in l.lower() for tok in hide_lines)]

        hits.append((term, shown))
    return hits


# ── file collection — modified port of scan.py::_collect (55-82): adds a    #
# code/tabular/prose/skip bucket classification and a binary-decode guard   #
# (chat.py:1460 pattern) instead of scan.py's plain extension whitelist.    #

def _classify_ext(ext: str):
    ext = ext.lstrip(".").lower()
    if ext in _CODE_EXT:
        return "code", ext
    if ext in _TABULAR_EXT:
        return "tabular", ext
    if ext in _PROSE_EXT:
        return "prose", ext
    if ext in _IMG_EXT:
        return "skip", ext
    return None, ext


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


def _collect(path: str, use_tabular: bool) -> list:
    """Return list of (filepath, bucket, ext, text)."""
    if _os.path.isfile(path):
        candidates = [path]
    elif _os.path.isdir(path):
        candidates = []
        for root, _dirs, files in _os.walk(path):
            for fname in sorted(files):
                candidates.append(_os.path.join(root, fname))
    else:
        return []

    results = []
    for fp in candidates:
        bucket, ext = _classify_ext(_os.path.splitext(fp)[1])
        if bucket is None or bucket == "skip":
            continue
        if bucket == "tabular" and not use_tabular:
            continue
        if _is_probably_binary(fp):
            print(f"[glossary] skip (binary): {fp}")
            continue
        try:
            text = open(fp, encoding="utf-8", errors="ignore").read()
        except OSError as e:
            print(f"[glossary] skip (read error) {fp}: {e}")
            continue
        results.append((fp, bucket, ext, text))
    return results


# ── chunking ─────────────────────────────────────────────────────────────────

def _char_chunks(text: str, chunk_chars: int, overlap_chars: int) -> list:
    if chunk_chars <= 0 or len(text) <= chunk_chars:
        return [text]
    step = max(chunk_chars - overlap_chars, 1)
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        chunks.append(text[start:end])
        if end >= n:
            break
        start += step
    return chunks


def _code_chunks(text: str, ext: str, chunk_chars: int, overlap_chars: int) -> list:
    """Split on function/class boundaries instead of raw char slicing, so a
    chunk is never a syntactically meaningless fragment of a function. Falls
    back to _char_chunks for languages without a boundary regex, or when a
    single function/class exceeds the chunk budget."""
    func_re = _CODE_BOUNDARY_RE.get(ext)
    if not func_re:
        return _char_chunks(text, chunk_chars, overlap_chars)

    lines = text.splitlines(keepends=True)
    pattern = _re.compile(func_re)
    boundary_lines = [0]
    for i, line in enumerate(lines):
        if i == 0:
            continue
        if not line[:1].isspace() and pattern.search(line):
            boundary_lines.append(i)
    boundary_lines.append(len(lines))
    boundary_lines = sorted(set(boundary_lines))

    segments = []
    for i in range(len(boundary_lines) - 1):
        seg = "".join(lines[boundary_lines[i]:boundary_lines[i + 1]])
        if seg.strip():
            segments.append(seg)
    if not segments:
        return _char_chunks(text, chunk_chars, overlap_chars)

    chunks, buf = [], ""
    for seg in segments:
        if len(seg) > chunk_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_char_chunks(seg, chunk_chars, overlap_chars))
            continue
        if buf and len(buf) + len(seg) > chunk_chars:
            chunks.append(buf)
            buf = seg
        else:
            buf += seg
    if buf:
        chunks.append(buf)
    return chunks


# ── LLM helpers — strict marker extraction (small models are noisy) ─────────
# Same principle as /fim's fenced-code regex (chat.py:5878) and ladder.py's   #
# tiered extraction: the model must wrap payload in a marker; everything     #
# outside the marker is discarded unconditionally, never parsed as data.     #

class _StopIndexing(Exception):
    """Raised on user 'q' (1bcoder Ctrl+C prompt) or a web Stop button —
    unwinds out of the file/chunk/term loops in _cmd_index cleanly. Nothing
    special to save on the way out: every term is already written to disk
    immediately after it's processed."""
    pass


def _check_cancel(chat) -> None:
    """No-op for 1bcoder's real Chat object (which has no such attribute —
    Ctrl+C/KeyboardInterrupt is 1bcoder's own cancel path, handled below).
    A web UI (vyrii) can't send SIGINT to a background thread, so it sets
    chat._glossary_should_cancel to a callable instead (Stop button ->
    cancel_flag["stop"] = True) — checked before/after every LLM call so a
    Stop click takes effect within one call, not after the whole job."""
    should_cancel = getattr(chat, "_glossary_should_cancel", None)
    if should_cancel is not None and should_cancel():
        raise _StopIndexing()


def _on_ctrl_c() -> str:
    """Prompt on KeyboardInterrupt during an LLM call.
    Returns 'retry:<hint>' (hint may be empty), 'skip', or 'quit'.
    Same UX convention as deepagent_code.py's _on_interrupt — one blocking
    input() prompt, safe EOFError fallback for any non-interactive caller."""
    try:
        ans = input("\n[glossary] interrupted — repeat this step? [Y/n/q]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "skip"
    if ans.startswith("q"):
        return "quit"
    if ans.startswith("n"):
        return "skip"
    # default (Enter, or anything starting with 'y') — retry, optional hint
    try:
        hint = input("  comment (optional, guides the retry): ").strip()
    except (EOFError, KeyboardInterrupt):
        hint = ""
    return f"retry:{hint}"


def _llm(chat, system: str, prompt: str) -> str:
    _check_cancel(chat)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    while True:
        try:
            result = (chat._stream_chat(msgs) or "").strip()
            _check_cancel(chat)
            return result
        except KeyboardInterrupt:
            action = _on_ctrl_c()
            if action == "quit":
                raise _StopIndexing()
            if action == "skip":
                return ""
            hint = action.split(":", 1)[1]
            if hint:
                print(f"  [glossary] retrying with comment: {hint}")
                msgs = [{"role": "system", "content": system},
                        {"role": "user", "content": prompt + f"\n\nAdditional instruction: {hint}"}]
            else:
                print("  [glossary] retrying...")


_TERMS_SYSTEM = (
    "You extract glossary terms from text.\n"
    "Output EXACTLY one line starting with TERMS: followed by a comma-separated "
    "list of the important terms, entities, and identifiers in the text below.\n"
    "Skip trivial words, loop counters, and generic short names.\n"
    "Do not explain your answer. Do not add any other text."
)

_DEFINITION_SYSTEM = (
    "You write a concise glossary definition.\n"
    "Output EXACTLY in this format:\nDEFINITION: <one or two sentences>\n"
    "Base the definition only on the text given below. Do not add any other text."
)

_FACTS_SYSTEM = (
    "You extract concrete facts about a term from text.\n"
    "Output ONLY a list of facts, one per line, each starting with '- '.\n"
    "Base each fact only on the text given. Do not add any other text, no preamble."
)

_THINK_CODE_SYSTEM = (
    "You are analyzing a fragment of source code to help build a glossary.\n"
    "Answer briefly and concretely, based ONLY on what is in the code:\n"
    "- Inputs and outputs: what data does this code receive and return?\n"
    "- What does it do, step by step?\n"
    "- Important identifiers and terms — skip trivial loop counters and temp "
    "names (e.g. i in 'for i := 1 to 10'), keep meaningful ones (e.g. "
    "'user_birthdate := date.random()' is important, both as an identifier "
    "and as a fact).\n"
    "- If evident from the code, the apparent purpose and any domain/business "
    "terms. If not clearly evident, say so — do not guess.\n"
    "Do not invent details that are not present in the code."
)

_TABULAR_SYSTEM = (
    "You are documenting the schema of a structured data file.\n"
    "Given column headers and sample rows, output ONLY a list, one line per "
    "column:\n- <column name>: <apparent type> — examples: <a few sample values>\n"
    "No other text."
)


def _extract_terms_marker(raw: str) -> list:
    m = _re.search(r'TERMS:\s*(.+)', raw)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


# Some small-model replies echo a fragment of the system instructions after
# the actual definition (observed in real runs against gemma3:1b — a blank
# line, then e.g. "Base the definition only on the text given below. Do not
# add any other text."). Two defenses: cut at the first blank line (the
# prompt asks for "one or two sentences", a single paragraph), and also
# drop any paragraph that matches a known instruction phrase in case the
# leak isn't cleanly separated by a blank line.
_LEAKED_INSTRUCTION_MARKERS = (
    "base the definition only on",
    "output only",
    "output exactly",
    "do not add any other text",
    "do not explain",
    "no other text",
)


def _clean_definition(definition: str) -> str:
    paragraphs = _re.split(r'\n\s*\n', definition)
    kept = []
    for p in paragraphs:
        low = p.lower()
        cut_at = len(p)
        for marker in _LEAKED_INSTRUCTION_MARKERS:
            idx = low.find(marker)
            if idx != -1:
                cut_at = min(cut_at, idx)
        trimmed = p[:cut_at].strip()
        if trimmed:
            kept.append(trimmed)
        if cut_at < len(p):
            break  # a leak was found — nothing after it is trustworthy
    return "\n\n".join(kept).strip()


def _extract_definition_marker(raw: str) -> str:
    m = _re.search(r'DEFINITION:\s*(.+?)(?:\n\s*\n|\Z)', raw, _re.DOTALL)
    definition = m.group(1).strip() if m else raw.strip()
    return _clean_definition(definition)


# The facts prompt embeds "Term: X" / "Definition: Y" as plain-text context
# for the model — a small model sometimes echoes those exact labelled lines
# back as if they were bullet facts (observed in a real run: "- Term: Region",
# "- Definition: ..."). Drop any bullet whose content is just that echo.
_LEAKED_LABEL_PREFIXES = ("term:", "definition:", "define:")


def _extract_facts_marker(raw: str) -> list:
    facts = []
    for line in raw.splitlines():
        m = _re.match(r'^\s*[-*]\s*(.+)', line)
        if not m:
            continue
        fact = m.group(1).strip()
        if fact and not fact.lower().startswith(_LEAKED_LABEL_PREFIXES):
            facts.append(fact)
    return facts


def _filter_code_terms(terms: list) -> list:
    return [t for t in terms if t.lower() not in _STOP_IDENT and len(t) > 1]


# ── dedup — --unique is pure Python, no LLM continuation trick ──────────────
# (a "show existing facts + <<<fill in the middle>>>" marker was considered  #
# and rejected: gemma3:1b is not a FIM-pretrained model, so it won't reliably #
# reproduce known facts verbatim before diverging into new ones. difflib +   #
# a human-in-the-loop confirm is the reliable alternative — mirrors how      #
# /fim actually works: regenerate + programmatic diff, not a magic marker.)  #

def _dedup_facts(existing_facts: list, new_facts: list, label: str = "fact") -> list:
    to_append = []
    for nf in new_facts:
        dup_of = None
        for ef in existing_facts:
            ratio = _difflib.SequenceMatcher(None, nf.lower(), ef.lower()).ratio()
            if ratio >= _UNIQUE_THRESHOLD:
                dup_of = ef
                break
        if dup_of is None:
            to_append.append(nf)
            continue
        print(f"\n[glossary --unique] possible duplicate {label}:")
        print(f"  existing: {dup_of}")
        print(f"  new     : {nf}")
        try:
            ans = input("  keep first or replace? [K/r]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "k"
        if ans == "r":
            existing_facts.remove(dup_of)
            to_append.append(nf)
    return to_append


# ── index: per-chunk pipeline ────────────────────────────────────────────────

def _think_code(chat, code_chunk: str) -> str:
    return _llm(chat, _THINK_CODE_SYSTEM, code_chunk)


def _process_chunk(chat, project: str, chunk_text: str, bucket: str,
                   source_label: str, chunk_idx: int, total_chunks: int,
                   file_idx: int, total_files: int,
                   redefine: bool, refact: bool, unique: bool) -> list:
    think_text = ""
    if bucket == "code":
        think_text = _think_code(chat, chunk_text)
        if think_text:
            _write_think(project, source_label, chunk_idx, think_text)

    llm_input = (think_text + "\n\n" + chunk_text) if think_text else chunk_text

    raw_terms = _llm(chat, _TERMS_SYSTEM, llm_input)
    candidates = _extract_terms_marker(raw_terms)
    if bucket == "code":
        candidates = _filter_code_terms(candidates)
    if not candidates:
        return []

    known = set(_load_terms(project))
    touched = []

    total_terms = len(candidates)
    for term_idx, cand in enumerate(candidates, 1):
        kterm = _kebab(cand)
        if not kterm:
            continue
        print(f"    term: {term_idx}/{total_terms}  {cand}  "
              f"chunk: {chunk_idx}/{total_chunks}  file: {file_idx}/{total_files}")
        is_new = kterm not in known
        data = ({"term": kterm, "definitions": [], "facts": [], "links": []}
                if is_new else _read_term_file(project, kterm))

        # DEFINITION: never overwritten on an existing term (same append-only
        # philosophy as FACTS:) — each chunk that mentions the term produces
        # one more candidate definition. --redefine wipes the list and starts
        # fresh (e.g. after upgrading to a better model); --unique fuzzy-dedupes
        # a new candidate against existing ones instead of blindly appending.
        def_prompt = f"Term: {cand}\n\nText:\n{llm_input}"
        new_def = _extract_definition_marker(_llm(chat, _DEFINITION_SYSTEM, def_prompt))
        if is_new or redefine:
            data["definitions"] = [new_def] if new_def else []
        elif new_def:
            if unique:
                data["definitions"].extend(
                    _dedup_facts(data["definitions"], [new_def], label="definition"))
            else:
                data["definitions"].append(new_def)

        facts_prompt = (f"Term: {cand}\nDefinition: {'; '.join(data['definitions'])}"
                        f"\n\nText:\n{llm_input}")
        new_facts = _extract_facts_marker(_llm(chat, _FACTS_SYSTEM, facts_prompt))

        if is_new or refact:
            data["facts"] = new_facts
        elif unique:
            data["facts"].extend(_dedup_facts(data["facts"], new_facts))
        else:
            data["facts"].extend(new_facts)

        _write_term_file(project, kterm, data)
        if is_new:
            _add_term(project, kterm)
            known.add(kterm)
        touched.append(kterm)

    return touched


def _process_tabular(chat, project: str, fp: str, text: str, sample_rows: int = 5) -> None:
    lines = [l for l in text.splitlines() if l.strip()][:sample_rows + 1]
    if not lines:
        return
    raw = _llm(chat, _TABULAR_SYSTEM, "\n".join(lines))
    facts = [l.lstrip("-*").strip() for l in raw.splitlines() if l.strip().startswith(("-", "*"))]
    if not facts:
        return
    term = _os.path.splitext(_os.path.basename(fp))[0]
    kterm = _add_term(project, term)
    data = _read_term_file(project, kterm)
    if not data["definitions"]:
        data["definitions"] = [f"Schema for {_os.path.basename(fp)}"]
    data["facts"] = facts
    _write_term_file(project, kterm, data)
    print(f"  [tabular] {fp} -> {kterm}.md ({len(facts)} column(s))")


# ── subcommands ──────────────────────────────────────────────────────────────

def _pop_flag(rest: str, name: str, regex: bool = False):
    if regex:
        m = _re.search(name, rest)
        if not m:
            return None, rest
        return m.group(1), (rest[:m.start()] + rest[m.end():]).strip()
    if name in rest:
        return True, rest.replace(name, "").strip()
    return False, rest


def _pop_project(rest: str):
    m = _re.search(r'--project\s+(\S+)', rest)
    if not m:
        return "default", rest
    return m.group(1), (rest[:m.start()] + rest[m.end():]).strip()


def _cmd_index(chat, rest: str) -> None:
    project, rest = _pop_project(rest)

    chunk_tokens, rest = _pop_flag(rest, r'--chunk\s+(\d+)', regex=True)
    chunk_tokens = int(chunk_tokens) if chunk_tokens else _DEFAULT_CHUNK_TOKENS

    overlap_tokens, rest = _pop_flag(rest, r'--overlap\s+(\d+)', regex=True)
    overlap_tokens = int(overlap_tokens) if overlap_tokens else _DEFAULT_OVERLAP_TOKENS

    redefine, rest = _pop_flag(rest, "--redefine")
    refact, rest = _pop_flag(rest, "--refact")
    crosslink, rest = _pop_flag(rest, "--crosslink")
    unique, rest = _pop_flag(rest, "--unique")
    use_tabular, rest = _pop_flag(rest, "--tabular")

    path = rest.strip().strip('"\'')
    if not path:
        print("usage: /flow glossary index <path> [--project P] [--chunk 1000] [--overlap 50]")
        print("       [--redefine] [--refact] [--crosslink] [--unique] [--tabular]")
        return

    unknown = [t for t in path.split() if t.startswith("--")]
    if unknown:
        print(f"[glossary] unknown flag(s) for index: {', '.join(unknown)}")
        print("usage: /flow glossary index <path> [--project P] [--chunk 1000] [--overlap 50]")
        print("       [--redefine] [--refact] [--crosslink] [--unique] [--tabular]")
        return

    chunk_chars = chunk_tokens * _CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * _CHARS_PER_TOKEN

    files = _collect(path, use_tabular)
    if not files:
        print(f"[glossary] no files found at: {path}")
        return

    total_files = len(files)
    print(f"[glossary] project: {project}  files: {total_files}  "
          f"chunk: {chunk_tokens}t  overlap: {overlap_tokens}t")

    total_touched = set()
    try:
        for file_idx, (fp, bucket, ext, text) in enumerate(files, 1):
            if bucket == "tabular":
                print(f"[glossary] file: {file_idx}/{total_files}  {fp} — tabular")
                _process_tabular(chat, project, fp, text)
                continue

            chunks = (_code_chunks(text, ext, chunk_chars, overlap_chars) if bucket == "code"
                      else _char_chunks(text, chunk_chars, overlap_chars))
            chunks = [c for c in chunks if c.strip()]
            if not chunks:
                continue

            total_chunks = len(chunks)
            print(f"\n[glossary] file: {file_idx}/{total_files}  {fp} — {bucket}, {total_chunks} chunk(s)")
            for i, chunk in enumerate(chunks, 1):
                print(f"  chunk: {i}/{total_chunks}  file: {file_idx}/{total_files}")
                touched = _process_chunk(chat, project, chunk, bucket,
                                         _os.path.basename(fp), i, total_chunks,
                                         file_idx, total_files,
                                         redefine, refact, unique)
                if touched:
                    print(f"    terms: {', '.join(touched)}")
                total_touched.update(touched)
    except _StopIndexing:
        print("\n[glossary] stopped by user — work saved so far")

    _sort_glossary(project)
    if crosslink:
        n = _relink_project(project)
        print(f"[glossary] crosslink: {n} term file(s) updated")

    print(f"\n[glossary] done — {len(total_touched)} term(s) touched"
          + (f": {', '.join(sorted(total_touched))}" if total_touched else ""))


def _cmd_show(chat, rest: str) -> None:
    project, rest = _pop_project(rest)
    tokens = rest.split()
    add_ctx = bool(tokens) and tokens[-1].lower() == "ctx"
    if add_ctx:
        tokens = tokens[:-1]
    term = " ".join(tokens).strip()
    if not term:
        print("usage: /flow glossary show <term> [ctx] [--project P]")
        return

    kterm = _kebab(term)
    path = _term_path(project, kterm)
    if not _os.path.isfile(path):
        print(f"[glossary] no such term: {kterm}  (project: {project})")
        return
    content = open(path, encoding="utf-8").read()
    print(content)
    if add_ctx:
        chat.messages.append({"role": "user", "content": f"[glossary: {kterm}]\n{content}"})
        print(f"[glossary] added {kterm} to context")


def _cmd_extract(chat, rest: str) -> None:
    project, rest = _pop_project(rest)
    query = rest.strip()
    if not query:
        print("usage: /flow glossary extract <query> [--project P]")
        return
    hits = extract(query, _load_terms(project))
    if not hits:
        print("[glossary] no matching terms")
        return
    print(f"[glossary] {len(hits)} match(es): {', '.join(hits)}")


def _cmd_find(chat, rest: str) -> None:
    project, rest = _pop_project(rest)
    text = rest.strip()
    if not text:
        print("usage: /flow glossary find <text> [--project P]")
        return
    words = [w.lower() for w in _re.findall(r'\w+', text) if len(w) >= 3]
    if not words:
        print("[glossary] query too short")
        return

    results = []
    for term in _load_terms(project):
        data = _read_term_file(project, term)
        body = "\n".join(data["definitions"] + data["facts"]).lower()
        if any(w in body for w in words):
            snippet = (data["definitions"][0][:150] if data["definitions"]
                      else (data["facts"][0][:150] if data["facts"] else ""))
            results.append((term, snippet))

    if not results:
        print(f"[glossary] no matches for: {text}")
        return
    for term, snippet in results:
        print(f"\n{term}")
        print(f"  {snippet}")


def _cmd_relink(chat, rest: str) -> None:
    project, rest = _pop_project(rest)
    _sort_glossary(project)
    n = _relink_project(project)
    print(f"[glossary] project: {project} — glossary.md sorted, {n} term file(s) relinked")


def _cmd_filter(project_and_tokens: str) -> None:
    project, rest = _pop_project(project_and_tokens)
    tokens = rest.split()
    if not tokens:
        print("usage: /flow glossary <term1> \\term2 -term3 [--project P]")
        return
    hits = _query_filter(project, tokens)
    if not hits:
        print("[glossary] no matches")
        return
    for term, lines in hits:
        print(f"\n{term}")
        for l in lines:
            print(f"  - {l}")


# ── entry point ──────────────────────────────────────────────────────────────

def run(chat, args: str) -> None:
    args = args.strip()
    if not args:
        print(__doc__)
        return

    parts = args.split(None, 1)
    sub, rest = parts[0], (parts[1] if len(parts) > 1 else "")

    if sub == "index":
        _cmd_index(chat, rest)
    elif sub == "show":
        _cmd_show(chat, rest)
    elif sub == "extract":
        _cmd_extract(chat, rest)
    elif sub == "find":
        _cmd_find(chat, rest)
    elif sub == "relink":
        _cmd_relink(chat, rest)
    else:
        _cmd_filter(args)
