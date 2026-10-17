"""glossary — LLM-wiki knowledge base builder.

Chunks a folder of files (prose / structured data / source code), asks the
LLM to extract terms per chunk, and builds one markdown file per term
(DEFINITION:/FACTS:/LINK:/SYNONYM: sections) plus a flat glossary.md index.
Designed for small local models: every LLM call that must return a
structured value uses a strict single-line marker + regex extraction, never
free parsing.

Self-contained (stdlib only) for the core index/show/extract/find/relink
pipeline — see concepts/GLOSSARY.md for the full design rationale (Karpathy
llm-wiki inspiration, why code needs a think-step, why --unique is pure
Python not an LLM continuation trick). --synonyms is the one deliberate
exception: an opt-in flag that needs radogast[embed] (soft dependency,
try/except ImportError, same pattern as simargl in deepagent_md.py's
_rag_research) — everything else works with zero extra installs.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow glossary index <path> [flags]
  /flow glossary show <term> [ctx] [--project P]
  /flow glossary extract <query> [--project P]
  /flow glossary find <text> [--project P]
  /flow glossary relink [--project P]
  /flow glossary html [--project P]
  /flow glossary contents [--project P]
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
  --synonyms        after indexing, cluster terms by vector closeness of
                     DEFINITION:+FACTS: CONTENT (not name -- that's what
                     --crosslink covers) and write SYNONYM: on clusters the
                     user confirms interactively. Never auto-merges term
                     files. Needs radogast[embed] (soft dependency, skipped
                     with a message if not installed). Also available on
                     `relink` (see below). Run once at the end against the
                     FULL term list, same reason as --crosslink.
  --synonym-threshold N   cosine similarity threshold for --synonyms (default 0.85)
  --all             read every file again (forgetting what it gave before), not only new and
                     changed ones — see "incremental" below
  --prune           also forget the files of <path> that no longer exist
  --jev             judge with the decision model of /jev (~/.1bcoder/jev.json: model, host,
                     threshold), Python decides on its probabilities:
                       KIND:       is a new term a concept of the problem (domain), a
                                   technical one (technology) or no real term (noise)? —
                                   asked once per term, all new terms of a chunk in one call
                       DEFINITION: does the chunk support the new definition? if not, dropped
                       FACTS:      does the chunk state the new fact? if not, dropped;
                                   is it already in the article? if so, dropped
                     --terms-only asks KIND: only (there are no definitions or facts)
  --jev-model M     the decision model for --jev in this run only (e.g. tev1:4b), instead of the
                     one in ~/.1bcoder/jev.json; it is asked on the host of the glossary model
  --jev-host H      ask the decision model on another host (e.g. localhost:11434 while the
                     glossary model runs on panteon): neither model pushes the other out of
                     memory, and the two machines work at once: KIND: is then judged on a
                     second thread (a pipeline) while the next chunk's terms are listed. On one
                     host jev stays inline — parallel calls there would only compete
  --terms-only      only the TERMS: call per chunk — no DEFINITION:/FACTS: calls and no
                     think-step for code (1 LLM call per chunk instead of 1 + 2 per term):
                     which words are terms and how often they come up, cheaply, for a
                     whole repository or task history. Every mention still goes to the
                     term's SEEN: list (as in a full run), so a later full run on the
                     same project adds definitions and facts to the same files.

  glossary.md is always re-sorted alphabetically at the end of an index run.

  incremental: <glossary>/_index.json keeps, per file, its mtime and size once it is fully
  read. The next run skips the files that have not changed; a changed file first loses every
  entry it gave (SEEN:, DEFINITION:, FACTS:, KIND: carry its [file: ...] tag), then is read
  again; a term left with nothing is removed. A run stopped halfway resumes the same way: the
  file it was reading is not in _index.json yet, so its partial entries are forgotten and it
  is read again — no double counting. --redefine / --refact / --all read every file.

show:     print <term>.md; trailing "ctx" token also appends it to context
          (no interactive confirm — safe to call from an agent ACTION: loop)
extract:  fuzzy subword match of free text against known terms (like
          /map keyword extract -f) — for code-identifier-style queries
find:     full-text search across all terms' DEFINITION:+FACTS:
relink:   pure-Python postprocessing pass, no LLM call — re-sorts glossary.md
          and recomputes every term's LINK: against the current full term
          list. Safe to re-run anytime, including on output from an older
          run (e.g. before this feature existed, or after --crosslink was
          skipped originally). Accepts [--synonyms [--synonym-threshold N]]
          too, same as index -- unlike LINK:, SYNONYM: is never recomputed
          unconditionally here, since it needs the optional embedding
          dependency and relink's whole point is a cheap, always-safe rerun.
html:     write a static glossary.html (index) + one <term>.html per term
          into the same .1bcoder/glossary/<project>/ folder, with real
          <a href> links between them (index -> term, term -> LINK:/SYNONYM:
          related terms, term -> back to index). No server, no build step —
          just double-click glossary.html. Safe to re-run anytime; always
          regenerates from the current .md files, so it never drifts once
          you remember to rerun it after indexing.
contents: write _contents.md — the glossary's clickable table of contents for a markdown
          viewer: every term as a [term](term.md) link with the start of its first definition.
          index, relink and html write it too. (glossary.md stays the plain list of names
          every reader parses.)
filter:   /map-style token grammar, one term.md = one "block"
          \\term  = FACTS line contains term       -term = show only matching lines
          -!term  = hide matching lines             !term = term name must NOT contain
          +term   = show only matching, drop file if none match

Examples:
  /flow glossary index docs --project myapp
  /flow glossary index src --project myapp --crosslink --unique
  /flow glossary index . --project myapp --redefine --refact
  /flow glossary index docs --project myapp --synonyms --synonym-threshold 0.85
  /flow glossary show user-birthdate ctx --project myapp
  /flow glossary extract "fix rule search" --project myapp
  /flow glossary find "amortization" --project myapp
  /flow glossary relink --project myapp
  /flow glossary rule \\index -deprecated --project myapp
"""
import json as _json
import os as _os
import queue as _queue
import threading as _threading
import time as _time
import re as _re
import difflib as _difflib
import html as _html

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


def _glossary_html_path(project: str) -> str:
    return _os.path.join(_glossary_dir(project), "glossary.html")


def _term_html_path(project: str, term: str) -> str:
    return _os.path.join(_glossary_dir(project), _kebab(term) + ".html")


def _contents_md_path(project: str) -> str:
    # not "index.md": a term named "index" has that file. "_" keeps it out of the articles
    return _os.path.join(_glossary_dir(project), "_contents.md")


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


def _parse_wikilink_list(block: str) -> list:
    """Parse a bullet list of "[term](term.md)" (current write format) or
    bare "term" (older files, or ones never touched by --crosslink/relink/
    --synonyms) into a plain list of term names. Shared by LINK: and
    SYNONYM:, which use the identical on-disk format."""
    raw = [l.lstrip("-*").strip() for l in block.splitlines() if l.strip()]
    out = []
    for l in raw:
        m = _re.match(r'\[([^\]]+)\]\([^)]+\)', l)
        out.append(m.group(1) if m else l)
    return out


def _read_term_file(project: str, term: str) -> dict:
    """DEFINITION: is a growing list of candidate definitions, one bullet per
    chunk that produced one — never overwritten on an existing term (same
    append-only philosophy as FACTS:), unless --redefine explicitly wipes it.
    Tolerates the older single-paragraph format (pre-dates this change): a
    DEFINITION: block with no bullet lines is treated as one candidate.

    HISTORY: is a separate append-only section from a different producer
    (the /flow history flow, not /flow glossary) — facts are static
    properties of the term, history is temporal task-driven change records.
    /flow glossary itself never writes HISTORY:, but must preserve it
    unchanged whenever it rewrites a term file it doesn't otherwise touch."""
    kterm = _kebab(term)
    path = _term_path(project, kterm)
    if not _os.path.isfile(path):
        return {"term": kterm, "definitions": [], "facts": [], "links": [], "synonyms": [], "kind": [], "seen": [],
                "history": []}
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
                definitions = [block]  # legacy plain-paragraph format
    m = _re.search(r'FACTS:\n(.*?)(?=\nLINK:|\nHISTORY:|\Z)', text, _re.DOTALL)
    if m:
        facts = [l.lstrip("-*").strip() for l in m.group(1).splitlines() if l.strip()]
    m = _re.search(r'LINK:\n(.*?)(?=\nSYNONYM:|\nHISTORY:|\Z)', text, _re.DOTALL)
    if m:
        links = _parse_wikilink_list(m.group(1))
    synonyms = []
    m = _re.search(r'SYNONYM:\n(.*?)(?=\nKIND:|\nSEEN:|\nHISTORY:|\Z)', text, _re.DOTALL)
    if m:
        synonyms = _parse_wikilink_list(m.group(1))
    kind = []
    m = _re.search(r'KIND:\n(.*?)(?=\nSEEN:|\nHISTORY:|\Z)', text, _re.DOTALL)
    if m:
        kind = [l.lstrip("-*").strip() for l in m.group(1).splitlines() if l.strip()]
    seen = []
    m = _re.search(r'SEEN:\n(.*?)(?=\nHISTORY:|\Z)', text, _re.DOTALL)
    if m:
        seen = [l.lstrip("-*").strip() for l in m.group(1).splitlines() if l.strip()]
    m = _re.search(r'HISTORY:\n(.*)\Z', text, _re.DOTALL)
    if m:
        history = [l.lstrip("-*").strip() for l in m.group(1).splitlines() if l.strip()]
    return {"term": kterm, "definitions": definitions, "facts": facts, "links": links,
            "synonyms": synonyms, "kind": kind, "seen": seen, "history": history}


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
    lines.append("")
    lines.append("SYNONYM:")
    for syn in data.get("synonyms", []):
        # same-concept cross-reference confirmed via /flow glossary --synonyms
        # (vector closeness of DEFINITION:+FACTS:, HITL-confirmed) -- distinct
        # from LINK:, which is literal term-name containment, not meaning.
        lines.append(f"- [{syn}]({syn}.md)")
    lines.append("")
    if data.get("kind"):
        # what the decision model (--jev) took the term for, per chunk: domain / technology / noise
        lines.append("KIND:")
        for k in data["kind"]:
            lines.append(f"- {k}")
        lines.append("")
    if data.get("seen"):
        # every chunk the term was found in: its support, and where to look
        lines.append("SEEN:")
        for where in data["seen"]:
            lines.append(f"- {where}")
        lines.append("")
    lines.append("HISTORY:")
    for entry in data.get("history", []):
        lines.append(f"- {entry}")
    with open(_term_path(project, kterm), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_think(project: str, source_label: str, chunk_idx: int, text: str) -> None:
    safe = _re.sub(r'[^\w.\-]', '_', source_label)
    path = _os.path.join(_think_dir(project), f"{safe}_chunk{chunk_idx}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# think: {source_label} chunk {chunk_idx}\n\n{text}")


# ── source provenance tag ────────────────────────────────────────────────────
# Every DEFINITION:/FACTS: candidate is tagged with where it came from, e.g.
# "amortisation is rounded to 2 decimals [file: src/finance.py:142]" — 142 is
# the 1-based line where the chunk that produced this candidate starts (falls
# back to the chunk index if a line number isn't available, e.g. --tabular).
# This is what lets a later cross-reference pass walk term -> source file/line
# instead of only term -> term (which is all LINK: gives today).

_SOURCE_TAG_RE = _re.compile(r'\s*\[file:\s*[^\]]*\]\s*$')


def _source_tag(source_path: str, index) -> str:
    path = source_path.replace(_os.sep, '/')
    if index is None:
        return f" [file: {path}]"
    return f" [file: {path}:{index}]"


def _strip_source_tag(text: str) -> str:
    """Drop a trailing [file: ...] tag before similarity comparison, so two
    facts with identical content but different source tags (e.g. the same
    fact re-derived from an overlapping chunk) still dedup correctly."""
    return _SOURCE_TAG_RE.sub('', text)


# ── incremental indexing ──────────────────────────────────────────────────────
# <glossary>/_index.json: {"files": {path: {"mtime", "size"}}} — a file goes in once it is fully
# read. Every entry a file gives carries its [file: path:line] tag (SEEN: "file: path:line"),
# so a changed or vanished file can be taken out of every article before it is read again.

_MANIFEST = "_index.json"
_ENTRY_SRC_RE = _re.compile(r'.*file:\s*(.+?)(?::\d+)?\]?\s*$')   # the LAST file: — a fact may say "file:" itself


def _manifest_path(project: str) -> str:
    return _os.path.join(_glossary_dir(project), _MANIFEST)


def _load_manifest(project: str) -> dict:
    try:
        with open(_manifest_path(project), encoding="utf-8") as f:
            m = _json.load(f)
        return m if isinstance(m.get("files"), dict) else {"files": {}}
    except (OSError, ValueError, AttributeError):
        return {"files": {}}


def _save_manifest(project: str, m: dict) -> None:
    path = _manifest_path(project)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        _json.dump(m, f, indent=1, ensure_ascii=False)
    _os.replace(path + ".tmp", path)


def _src_key(path: str) -> str:
    """The form a path takes in [file: ...] tags (the path as given, '/' separators)."""
    return path.replace(_os.sep, '/')


def _file_sig(path: str) -> dict:
    st = _os.stat(path)
    return {"mtime": round(st.st_mtime, 3), "size": st.st_size}


def _entry_source(entry: str):
    m = _ENTRY_SRC_RE.search(entry or "")
    return m.group(1).strip() if m else None


def _source_index(project: str) -> dict:
    """source path -> the terms that have an entry from it."""
    idx = {}
    for term in _load_terms(project):
        data = _read_term_file(project, term)
        for key in ("seen", "definitions", "facts", "kind"):
            for e in data.get(key, []):
                src = _entry_source(e)
                if src:
                    idx.setdefault(src, set()).add(term)
    return idx


def _forget_sources(project: str, sources: set, src_index: dict) -> tuple:
    """Take every entry of these files out of the articles; a term left with nothing (no SEEN:,
    DEFINITION:, FACTS:, HISTORY:) is removed. -> (entries removed, terms removed)."""
    terms = set()
    for s in sources:
        terms |= src_index.pop(s, set())
    removed_entries, removed_terms = 0, []
    for term in sorted(terms):
        data = _read_term_file(project, term)
        for key in ("seen", "definitions", "facts", "kind"):
            keep = [e for e in data.get(key, []) if _entry_source(e) not in sources]
            removed_entries += len(data.get(key, [])) - len(keep)
            data[key] = keep
        if not (data["seen"] or data["definitions"] or data["facts"] or data["history"]):
            try:
                _os.remove(_term_path(project, term))
            except OSError:
                pass
            removed_terms.append(term)
        else:
            _write_term_file(project, term, data)
    if removed_terms:
        gone = set(removed_terms)
        left = [t for t in _load_terms(project) if t not in gone]
        with open(_glossary_md_path(project), "w", encoding="utf-8") as f:
            f.write("\n".join(left) + ("\n" if left else ""))
    return removed_entries, removed_terms


# ── jev: the decision model judges, Python decides ───────────────────────────
# One call per question group, many questions per call (jev.call_systemone batches up to 64).

_KIND_LEVELS = {
    "domain": "a concept of the problem the software solves — its subject or business area",
    "technology": "a technical concept: a library, language, protocol, tool, data structure or programming construct",
    "noise": "not a real term: a generic word, a phrase of a template, a typo or a fragment",
}
_JEV_STATS = {}


def _jev_module():
    try:
        import jev as _m
        return _m
    except ImportError:
        import importlib.util as _ilu
        import sys as _sys
        here = _os.path.dirname(_os.path.abspath(getattr(_sys.modules.get("chat"), "__file__", "") or "."))
        spec = _ilu.spec_from_file_location("jev", _os.path.join(here, "jev.py"))
        m = _ilu.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m


def _jev_setup(chat, model: str = None, host: str = None):
    """jev settings with the host resolved; None (and a message) when no decision model is set.
    model: this run's decision model (--jev-model), asked on the glossary model's host unless
    host (--jev-host) names another — the two models then never push each other out of memory."""
    jm = _jev_module()
    settings = jm.load_settings()
    if model:
        settings["model"], settings["host"] = model, ""
    if host:
        settings["host"] = host if "://" in host else "http://" + host
    if not (settings.get("model") or "").strip():
        print("[glossary] --jev: no decision model set — run /jev model list, then /jev model set <name>")
        return None
    settings["host"] = jm.resolve_host(settings, getattr(chat, "base_url", "") or "http://localhost:11434")
    settings["_mod"] = jm
    # a group of questions on a slow host (CPU) takes a minute: jev's interactive default (30 s) is too short
    try:
        settings["timeout"] = max(float(settings.get("timeout") or 0), 300.0)
    except (TypeError, ValueError):
        settings["timeout"] = 300.0
    # one tiny question first: a model missing on the host shows now, not mid-run. On a CPU host the
    # first call also loads the model — minutes of an empty log without this line
    print(f"[glossary] --jev: asking {settings['model']} on {settings['host']} "
          f"(the first call loads the model; up to {settings['timeout']:.0f} s)...")
    t0 = _time.monotonic()
    try:
        jm.call_systemone("glossary", {"probe": {"type": "noul", "instructions": "Is this a word?"}}, settings)
    except jm.JevError as e:
        print(f"[glossary] --jev: {settings['model']} on {settings['host']} does not answer: {e}")
        return None
    print(f"[glossary] --jev: answers ({_time.monotonic() - t0:.0f} s)")
    return settings


# A decision model reads ONE bounded prompt per request — the text and ALL its questions together
# (tev1: 2 050 tokens; a choice question with its three levels is ~110 tokens, so ~10 fit beside a
# chunk) — and never truncates it: over the limit it answers HTTP 400 "... tokens ...". So the
# questions go in groups of _JEV_GROUP; a group still too long is halved, and a single question
# still too long is asked with a shorter text.
_JEV_GROUP = 8
_JEV_STATE_STEPS = (3000, 2000, 1200)


def _jev_call(jset, state: str, questions: dict) -> dict:
    """One request; on "too many tokens": halve the questions, then shorten the text."""
    jm = jset["_mod"]
    try:
        return jm.call_systemone(state, questions, jset)
    except jm.JevConfigError as e:
        if "token" not in str(e).lower():
            raise
    if len(questions) > 1:
        items = list(questions.items())
        half = len(items) // 2
        out = _jev_call(jset, state, dict(items[:half]))
        out.update(_jev_call(jset, state, dict(items[half:])))
        return out
    for cap in _JEV_STATE_STEPS:
        if len(state) > cap:
            try:
                return jm.call_systemone(state[:cap], questions, jset)
            except jm.JevConfigError as e:
                if "token" not in str(e).lower():
                    raise
    _JEV_STATS["too_long"] = _JEV_STATS.get("too_long", 0) + 1
    return {}


def _jev_ask(jset, state: str, questions: dict) -> dict:
    """{id: answer}, possibly partial; {} when jev cannot answer — the items are then kept,
    unverified. An index run is never stopped by jev."""
    if not questions:
        return {}
    jm = jset["_mod"]
    items = list(questions.items())
    out = {}
    try:
        for i in range(0, len(items), _JEV_GROUP):
            out.update(_jev_call(jset, state, dict(items[i:i + _JEV_GROUP])))
    except jm.JevError as e:                     # JevConfigError included (not a length problem)
        _JEV_STATS["unavailable"] = _JEV_STATS.get("unavailable", 0) + 1
        print(f"    [jev] {e} — kept unverified")
    return out


def _p_true(ans) -> float:
    try:
        return float(ans.get("noul", ans.get("value")))
    except (AttributeError, TypeError, ValueError):
        return None


def _jev_kinds(jset, chunk_text: str, terms: list) -> dict:
    """term -> (kind, probability), all terms of a chunk in one call."""
    qs = {f"k{i}": {"type": "choice", "criteria": dict(_KIND_LEVELS),
                    "instructions": f'In this text, what is "{t}"?'} for i, t in enumerate(terms)}
    answers = _jev_ask(jset, chunk_text, qs)
    out = {}
    for i, t in enumerate(terms):
        a = answers.get(f"k{i}")
        if isinstance(a, dict) and a.get("choice") is not None:
            probs = a.get("probabilities") or {}
            ch = str(a["choice"])
            try:
                p = float(probs.get(ch, a.get("confidence") or 0))
            except (TypeError, ValueError):
                p = 0.0
            out[t] = (ch, p)
    return out


def _jev_supported(jset, text: str, term: str, definition: str, facts: list) -> tuple:
    """Does the text support the definition and state each fact? -> (keep_def, kept_facts)."""
    thr = float(jset.get("threshold") or 0.5)
    qs = {}
    if definition:
        qs["d"] = {"type": "noul", "instructions":
                   f'Does the text support this definition of "{term}": {_strip_source_tag(definition)}'}
    for i, f in enumerate(facts):
        qs[f"f{i}"] = {"type": "noul", "instructions": f'Does the text state this about "{term}": {_strip_source_tag(f)}'}
    answers = _jev_ask(jset, text, qs)
    if not answers:
        return bool(definition), facts
    pd = _p_true(answers.get("d")) if definition else None
    keep_def = bool(definition) and (pd is None or pd >= thr)
    if definition and not keep_def:
        _JEV_STATS["definitions_unsupported"] = _JEV_STATS.get("definitions_unsupported", 0) + 1
    kept = []
    for i, f in enumerate(facts):
        p = _p_true(answers.get(f"f{i}"))
        if p is None or p >= thr:
            kept.append(f)
        else:
            _JEV_STATS["facts_unsupported"] = _JEV_STATS.get("facts_unsupported", 0) + 1
    return keep_def, kept


def _jev_new_facts(jset, term: str, existing: list, facts: list) -> list:
    """The facts the article does not already state."""
    if not existing or not facts:
        return facts
    thr = float(jset.get("threshold") or 0.5)
    article = f'Article about "{term}":\n' + "\n".join("- " + _strip_source_tag(e) for e in existing)
    qs = {f"f{i}": {"type": "noul", "instructions": f"Does the article already state this: {_strip_source_tag(f)}"}
          for i, f in enumerate(facts)}
    answers = _jev_ask(jset, article, qs)
    out = []
    for i, f in enumerate(facts):
        p = _p_true(answers.get(f"f{i}"))
        if p is not None and p >= thr:
            _JEV_STATS["facts_duplicate"] = _JEV_STATS.get("facts_duplicate", 0) + 1
        else:
            out.append(f)
    return out


# ── the pipeline: TERMS: on this thread, KIND: on another ─────────────────────
# The glossary model (often a remote host) lists a chunk's terms while the decision model (often
# this machine, --jev-host) judges the terms of the chunk before: a chunk then costs the longer of
# the two calls, not their sum. Both threads write term files, so every read-modify-write of one
# is done under _TERM_LOCK; the judging thread writes only KIND:, and the main thread keeps
# whatever KIND: the file has when it saves (its own copy may be older — a full run reads a term,
# then waits minutes for the definition and the facts). A file goes into _index.json only once
# its kinds are written: the "done" marker comes through the same queue, after its chunks.

_TERM_LOCK = _threading.RLock()


def _host_key(host: str) -> str:
    """localhost:11434, http://localhost:11434/, ollama://localhost:11434 -> one form."""
    h = (host or "").strip().lower()
    for p in ("http://", "https://", "ollama://"):
        if h.startswith(p):
            h = h[len(p):]
    return h.rstrip("/").replace("127.0.0.1", "localhost")
_KIND_QUEUE_SIZE = 4          # chunks the main thread may run ahead of the judging one


def _save_term(project: str, kterm: str, data: dict, is_new: bool) -> None:
    with _TERM_LOCK:
        if not is_new and _os.path.isfile(_term_path(project, kterm)):
            data["kind"] = _read_term_file(project, kterm).get("kind", [])
        _write_term_file(project, kterm, data)
        if is_new:
            _add_term(project, kterm)


def _judge_kinds(project: str, jset: dict, chunk_text: str, tag: str, terms: list) -> None:
    """KIND: for the terms of a chunk that have none yet — one group of questions per call."""
    with _TERM_LOCK:
        todo = [t for t in dict.fromkeys(terms)
                if _os.path.isfile(_term_path(project, _kebab(t)))
                and not _read_term_file(project, _kebab(t)).get("kind")]
    if not todo:
        return
    kinds = _jev_kinds(jset, chunk_text, todo)
    with _TERM_LOCK:
        for t, (k, p) in kinds.items():
            kt = _kebab(t)
            if not _os.path.isfile(_term_path(project, kt)):
                continue                                   # forgotten meanwhile
            data = _read_term_file(project, kt)
            if data.get("kind"):
                continue                                   # judged meanwhile (the same term, an earlier chunk)
            data["kind"] = [f"{k} {p:.2f}{tag}"]
            _write_term_file(project, kt, data)
            _JEV_STATS["kind_" + k] = _JEV_STATS.get("kind_" + k, 0) + 1


def _put(q, item) -> None:
    """Queue.put that Ctrl+C can interrupt: on Windows a blocking put waits on a lock that ignores
    Ctrl+C until the queue frees a place — with a slow jev that is minutes."""
    while True:
        try:
            q.put(item, timeout=0.5)
            return
        except _queue.Full:
            continue


def _kind_worker(project: str, jset: dict, q, on_done, stop=None) -> None:
    """The judging thread: ("chunk", text, tag, terms) -> KIND:; ("done", key, sig) -> on_done; None -> stop.
    stop (an Event) set: nothing more is judged or recorded — the thread ends after its current call."""
    while True:
        item = q.get()
        try:
            if item is None or (stop is not None and stop.is_set()):
                return
            if item[0] == "chunk":
                _judge_kinds(project, jset, item[1], item[2], item[3])
            elif item[0] == "done":
                on_done(item[1], item[2])
                # the main thread's chunk lines run ahead; this one says how far jev is behind them
                print(f"    [jev] judged: {item[1]}  (still queued: {q.qsize()})")
        except Exception as e:                             # the judging thread never takes the run down
            _JEV_STATS["worker_errors"] = _JEV_STATS.get("worker_errors", 0) + 1
            print(f"    [jev] {type(e).__name__}: {e}")
        finally:
            q.task_done()


def term_kind(data: dict):
    """The kind most of a term's KIND: votes say (ties: the higher summed probability); None if unjudged."""
    votes, mass = {}, {}
    for e in data.get("kind", []):
        parts = _strip_source_tag(e).split()
        if not parts:
            continue
        k = parts[0]
        votes[k] = votes.get(k, 0) + 1
        try:
            mass[k] = mass.get(k, 0.0) + float(parts[1])
        except (IndexError, ValueError):
            pass
    if not votes:
        return None
    return max(votes, key=lambda k: (votes[k], mass.get(k, 0.0)))


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


_SYNONYM_THRESHOLD_DEFAULT = 0.85
_SYNONYM_MODEL = "BAAI/bge-small-en-v1.5"


def _synonym_project(project: str, threshold: float = _SYNONYM_THRESHOLD_DEFAULT) -> int:
    """Cluster terms by vector closeness of DEFINITION:+FACTS: CONTENT, not
    term name -- LINK:/--crosslink already covers literal name containment
    (_phrase_in_text) and would never find "TCR" <-> "task-to-code retrieval"
    or "user" <-> "customer". Same radogast primitives already used for
    deepagent_tree/_md/_md plan-node dedup (LivePlanIndex): embed, cosine
    similarity matrix, threshold-based connected components.

    Never auto-merges term files -- each candidate cluster is shown to the
    user and confirmed interactively (same HITL pattern as _dedup_facts's
    --unique prompt), and a confirmed cluster only adds a SYNONYM: cross-
    reference to each member, never rewrites DEFINITION:/FACTS: or deletes
    a file. See concepts/GLOSSARY.md "Synonym ring" for the full rationale.

    radogast is a soft dependency (try/except ImportError), same as simargl
    in deepagent_md.py's _rag_research -- not in glossary.py's own hard
    requirements, since this file is otherwise self-contained/stdlib-only."""
    try:
        from radogast.metrics.drift import embed
        from radogast.metrics.similarity import similarity_matrix, cluster_by_threshold
    except ImportError:
        print("[glossary --synonyms] radogast not installed -- skipped "
              "(pip install radogast[embed])")
        return 0

    terms = _load_terms(project)
    if len(terms) < 2:
        return 0
    term_data = {t: _read_term_file(project, t) for t in terms}
    texts = ["\n".join(term_data[t]["definitions"] + term_data[t]["facts"]) or t for t in terms]
    vecs = embed(texts, _SYNONYM_MODEL, show_progress=True)
    if vecs is None:
        print("[glossary --synonyms] embeddings unavailable -- skipped")
        return 0

    # cluster_by_threshold excludes dot-notation ancestor/descendant pairs
    # (is_ancestor) -- a no-op here since flat kebab-case term names never
    # contain ".", so no synonym pair is ever incorrectly skipped.
    clusters = cluster_by_threshold(terms, similarity_matrix(vecs), threshold)

    updated = 0
    for members in clusters:
        print(f"\n[glossary --synonyms] possible synonym ring ({len(members)} terms):")
        for m in members:
            d = term_data[m]["definitions"]
            preview = d[0][:120] if d else "(no definition)"
            print(f"  {m}: {preview}")
        try:
            ans = input("  confirm as synonyms? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans != "y":
            continue
        for m in members:
            others = sorted(t for t in members if t != m)
            data = term_data[m]
            if data.get("synonyms") != others:
                data["synonyms"] = others
                _write_term_file(project, m, data)
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
        child_lines = list(data["facts"]) + list(data["history"])

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


# folders a walk never enters: dot folders (.git, .1bcoder — the glossary's own output —
# .simargl, .venv…) and tool caches. A folder given explicitly as the source is still read.
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "site-packages"}


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIRS


def _collect(path: str, use_tabular: bool) -> list:
    """Return list of (filepath, bucket, ext, text)."""
    if _os.path.isfile(path):
        candidates = [path]
    elif _os.path.isdir(path):
        candidates = []
        for root, dirs, files in _os.walk(path):
            dirs[:] = sorted(d for d in dirs if not _skip_dir(d))
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
    """Return [(chunk_text, start_line), ...] — start_line is the 1-based line
    number where the chunk begins in `text`, used to tag FACTS:/DEFINITION:
    entries with their source (see _source_tag)."""
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


def _code_chunks(text: str, ext: str, chunk_chars: int, overlap_chars: int) -> list:
    """Split on function/class boundaries instead of raw char slicing, so a
    chunk is never a syntactically meaningless fragment of a function. Falls
    back to _char_chunks for languages without a boundary regex, or when a
    single function/class exceeds the chunk budget.

    Return [(chunk_text, start_line), ...] — same contract as _char_chunks."""
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

    segments = []  # (text, start_line) — start_line is 1-based
    for i in range(len(boundary_lines) - 1):
        seg_start = boundary_lines[i]
        seg = "".join(lines[seg_start:boundary_lines[i + 1]])
        if seg.strip():
            segments.append((seg, seg_start + 1))
    if not segments:
        return _char_chunks(text, chunk_chars, overlap_chars)

    chunks, buf, buf_start = [], "", None
    for seg, seg_start in segments:
        if len(seg) > chunk_chars:
            if buf:
                chunks.append((buf, buf_start))
                buf, buf_start = "", None
            for sub_text, sub_line in _char_chunks(seg, chunk_chars, overlap_chars):
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
            raw = chat._stream_chat(msgs)
        except KeyboardInterrupt:
            raw = None
        # chat.py's real _stream_chat already catches KeyboardInterrupt itself
        # (prints "[interrupted]") and returns None as a sentinel rather than
        # re-raising -- so None, not a caught KeyboardInterrupt, is the normal
        # signal that Ctrl+C fired. Still catch KeyboardInterrupt above too, in
        # case a _stream_chat implementation ever does propagate it directly.
        if raw is None:
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
            continue
        result = raw.strip()
        _check_cancel(chat)
        return result


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
        nf_content = _strip_source_tag(nf)
        dup_of = None
        for ef in existing_facts:
            ratio = _difflib.SequenceMatcher(
                None, nf_content.lower(), _strip_source_tag(ef).lower()).ratio()
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
                   redefine: bool, refact: bool, unique: bool,
                   source_path: str = None, start_line: int = None,
                   terms_only: bool = False, jset: dict = None, kind_queue=None) -> list:
    tag = _source_tag(source_path if source_path is not None else source_label,
                       start_line if start_line is not None else chunk_idx)
    think_text = ""
    if bucket == "code" and not terms_only:
        think_text = _think_code(chat, chunk_text)
        if think_text:
            _write_think(project, source_label, chunk_idx, think_text)

    llm_input = (think_text + "\n\n" + chunk_text) if think_text else chunk_text

    raw_terms = _llm(chat, _TERMS_SYSTEM, llm_input)
    candidates = _extract_terms_marker(raw_terms)
    if bucket == "code":
        candidates = _filter_code_terms(candidates)
    # small models repeat terms in one TERMS: line ("search.py, …, search.py") —
    # each term once per chunk, by its file name, so "Search.py" is a repeat too
    seen_keys = set()
    candidates = [c for c in candidates
                  if _kebab(c) and not (_kebab(c) in seen_keys or seen_keys.add(_kebab(c)))]
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
        data = ({"term": kterm, "definitions": [], "facts": [], "links": [], "synonyms": [], "seen": [], "history": []}
                if is_new else _read_term_file(project, kterm))
        data.setdefault("seen", []).append(tag.strip().strip("[]"))
        if terms_only:
            _save_term(project, kterm, data, is_new)
            if is_new:
                known.add(kterm)
            touched.append(kterm)
            continue

        # DEFINITION: never overwritten on an existing term (same append-only
        # philosophy as FACTS:) — each chunk that mentions the term produces
        # one more candidate definition. --redefine wipes the list and starts
        # fresh (e.g. after upgrading to a better model); --unique fuzzy-dedupes
        # a new candidate against existing ones instead of blindly appending.
        def_prompt = f"Term: {cand}\n\nText:\n{llm_input}"
        new_def = _extract_definition_marker(_llm(chat, _DEFINITION_SYSTEM, def_prompt))
        if new_def:
            new_def = new_def + tag
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
        new_facts = [f + tag for f in new_facts]
        if jset:
            # the chunk must support what was written about the term; the article must not have it already
            keep_def, new_facts = _jev_supported(jset, llm_input, cand, new_def, new_facts)
            if new_def and not keep_def:
                if is_new or redefine:
                    data["definitions"] = []
                elif data["definitions"] and data["definitions"][-1] == new_def:
                    data["definitions"].pop()
            if not (is_new or refact):
                new_facts = _jev_new_facts(jset, cand, data["facts"], new_facts)

        if is_new or refact:
            data["facts"] = new_facts
        elif unique:
            data["facts"].extend(_dedup_facts(data["facts"], new_facts))
        else:
            data["facts"].extend(new_facts)

        _save_term(project, kterm, data, is_new)
        if is_new:
            known.add(kterm)
        touched.append(kterm)

    # --jev: what the terms not judged yet are — on the other thread, while this one reads the next chunk
    if jset:
        terms = [c for c in candidates if _kebab(c)]
        if kind_queue is not None:
            _put(kind_queue, ("chunk", chunk_text, tag, terms))
        else:
            _judge_kinds(project, jset, chunk_text, tag, terms)

    return touched


def _process_tabular(chat, project: str, fp: str, text: str, sample_rows: int = 5) -> None:
    lines = [l for l in text.splitlines() if l.strip()][:sample_rows + 1]
    if not lines:
        return
    raw = _llm(chat, _TABULAR_SYSTEM, "\n".join(lines))
    facts = [l.lstrip("-*").strip() for l in raw.splitlines() if l.strip().startswith(("-", "*"))]
    if not facts:
        return
    tag = _source_tag(fp, None)  # whole-file schema, not chunked — no line number
    facts = [f + tag for f in facts]
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
    synonyms, rest = _pop_flag(rest, "--synonyms")
    synonym_threshold, rest = _pop_flag(rest, r'--synonym-threshold\s+([\d.]+)', regex=True)
    synonym_threshold = float(synonym_threshold) if synonym_threshold else _SYNONYM_THRESHOLD_DEFAULT
    terms_only, rest = _pop_flag(rest, "--terms-only")
    read_all, rest = _pop_flag(rest, "--all")
    prune, rest = _pop_flag(rest, "--prune")
    jev_model, rest = _pop_flag(rest, r'--jev-model\s+(\S+)', regex=True)
    jev_host, rest = _pop_flag(rest, r'--jev-host\s+(\S+)', regex=True)
    use_jev, rest = _pop_flag(rest, "--jev")

    path = rest.strip().strip('"\'')
    if not path:
        print("usage: /flow glossary index <path> [--project P] [--chunk 1000] [--overlap 50]")
        print("       [--redefine] [--refact] [--crosslink] [--unique] [--tabular]")
        print("       [--synonyms [--synonym-threshold N]] [--terms-only] [--all] [--prune] [--jev] [--jev-model M] [--jev-host H]")
        return

    unknown = [t for t in path.split() if t.startswith("--")]
    if unknown:
        print(f"[glossary] unknown flag(s) for index: {', '.join(unknown)}")
        print("usage: /flow glossary index <path> [--project P] [--chunk 1000] [--overlap 50]")
        print("       [--redefine] [--refact] [--crosslink] [--unique] [--tabular]")
        print("       [--synonyms [--synonym-threshold N]] [--terms-only] [--all] [--prune] [--jev] [--jev-model M] [--jev-host H]")
        return

    chunk_chars = chunk_tokens * _CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * _CHARS_PER_TOKEN

    jset = None
    if use_jev or jev_model or jev_host:
        jset = _jev_setup(chat, jev_model, jev_host)
        if jset is None:
            return
        _JEV_STATS.clear()

    manifest = _load_manifest(project)
    src_index = None          # source -> terms, built once, only when something has to be forgotten
    if prune:
        root = _src_key(path).rstrip('/')
        gone = [k for k in manifest["files"] if (k == root or k.startswith(root + '/')) and not _os.path.exists(k)]
        if gone:
            src_index = _source_index(project)
            n, dead = _forget_sources(project, set(gone), src_index)
            for k in gone:
                manifest["files"].pop(k, None)
            _save_manifest(project, manifest)
            print(f"[glossary] prune: {len(gone)} vanished file(s), {n} entries and {len(dead)} term(s) removed")

    files = _collect(path, use_tabular)
    if not files:
        print(f"[glossary] no files found at: {path}")
        return
    again = read_all or redefine or refact
    todo = [f for f in files if again or manifest["files"].get(_src_key(f[0])) != _file_sig(f[0])]
    if len(todo) < len(files):
        print(f"[glossary] {len(files) - len(todo)} unchanged file(s) skipped (incremental; --all reads them again)")
    files = todo
    if not files:
        print("[glossary] nothing new or changed")
        return

    total_files = len(files)
    print(f"[glossary] project: {project}  files: {total_files}  "
          f"chunk: {chunk_tokens}t  overlap: {overlap_tokens}t" + ("  terms only" if terms_only else ""))

    def file_done(key, sig):
        with _TERM_LOCK:
            manifest["files"][key] = sig
            _save_manifest(project, manifest)

    # the pipeline only pays when the two models are on two machines: on one host (one Ollama, one
    # CPU/GPU) parallel calls would only compete and swap the models — there jev stays inline
    kind_queue, worker, stop = None, None, _threading.Event()
    if jset and _host_key(jset["host"]) != _host_key(getattr(chat, "base_url", "")):
        kind_queue = _queue.Queue(maxsize=_KIND_QUEUE_SIZE)
        worker = _threading.Thread(target=_kind_worker, args=(project, jset, kind_queue, file_done, stop),
                                   daemon=True)
        worker.start()
        print(f"[glossary] jev on {jset['host']} beside the glossary model: two threads (a pipeline)")

    def finish_file(key, sig):
        if kind_queue is not None:
            _put(kind_queue, ("done", key, sig))        # after the file's chunks: recorded once they are judged
        else:
            file_done(key, sig)

    total_touched = set()
    try:
        for file_idx, (fp, bucket, ext, text) in enumerate(files, 1):
            key = _src_key(fp)
            if src_index is None:
                src_index = _source_index(project)
            if key in src_index:
                # read before (changed since, or a run stopped inside it): its old entries go first
                with _TERM_LOCK:
                    n, dead = _forget_sources(project, {key}, src_index)
                print(f"[glossary] {fp}: {n} old entr{'y' if n == 1 else 'ies'} forgotten"
                      + (f", {len(dead)} term(s) removed" if dead else ""))
            if bucket == "tabular":
                print(f"[glossary] file: {file_idx}/{total_files}  {fp} — tabular")
                with _TERM_LOCK:
                    _process_tabular(chat, project, fp, text)
                finish_file(key, _file_sig(fp))
                continue

            chunks = (_code_chunks(text, ext, chunk_chars, overlap_chars) if bucket == "code"
                      else _char_chunks(text, chunk_chars, overlap_chars))
            chunks = [c for c in chunks if c[0].strip()]
            if not chunks:
                finish_file(key, _file_sig(fp))
                continue

            total_chunks = len(chunks)
            print(f"\n[glossary] file: {file_idx}/{total_files}  {fp} — {bucket}, {total_chunks} chunk(s)")
            for i, (chunk, start_line) in enumerate(chunks, 1):
                print(f"  chunk: {i}/{total_chunks}  file: {file_idx}/{total_files}"
                      + (f"  jev queue: {kind_queue.qsize()}" if kind_queue is not None else ""))
                touched = _process_chunk(chat, project, chunk, bucket,
                                         _os.path.basename(fp), i, total_chunks,
                                         file_idx, total_files,
                                         redefine, refact, unique,
                                         source_path=fp, start_line=start_line,
                                         terms_only=terms_only, jset=jset, kind_queue=kind_queue)
                if touched:
                    print(f"    terms: {', '.join(touched)}")
                total_touched.update(touched)
            finish_file(key, _file_sig(fp))                 # fully read: the next run skips it unless it changes
    except _StopIndexing:
        stop.set()
        print("\n[glossary] stopped by user — work saved so far")
    except KeyboardInterrupt:
        stop.set()
        print("\n[glossary] interrupted — work saved so far; the file being read is read again next run")
    finally:
        if worker is not None and not stop.is_set():        # the kinds still queued are judged, then the thread stops
            try:
                _put(kind_queue, None)
                while worker.is_alive():
                    worker.join(0.5)                        # a bare join() would not hear Ctrl+C either
            except KeyboardInterrupt:
                stop.set()
                print("\n[glossary] interrupted — the files still queued for jev are read again next run")
        # stopped: the queue is dropped unjudged (its files are not recorded); the daemon thread ends
        # after its current jev call
        if worker is not None and stop.is_set():
            try:
                kind_queue.put_nowait(None)                 # wakes a thread waiting on an empty queue
            except _queue.Full:
                pass

    _sort_glossary(project)
    if crosslink:
        n = _relink_project(project)
        print(f"[glossary] crosslink: {n} term file(s) updated")

    if synonyms:
        n = _synonym_project(project, synonym_threshold)
        print(f"[glossary] synonyms: {n} term file(s) updated")
    if _load_terms(project):
        print(f"[glossary] contents: {_write_contents_md(project)}")

    if jset and _JEV_STATS:
        print("[glossary] jev: " + ", ".join(f"{k} {v}" for k, v in sorted(_JEV_STATS.items())))
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
        body = "\n".join(data["definitions"] + data["facts"] + data["history"]).lower()
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
    synonyms, rest = _pop_flag(rest, "--synonyms")
    synonym_threshold, rest = _pop_flag(rest, r'--synonym-threshold\s+([\d.]+)', regex=True)
    synonym_threshold = float(synonym_threshold) if synonym_threshold else _SYNONYM_THRESHOLD_DEFAULT

    _sort_glossary(project)
    n = _relink_project(project)
    print(f"[glossary] project: {project} — glossary.md sorted, {n} term file(s) relinked")

    if synonyms:
        n2 = _synonym_project(project, synonym_threshold)
        print(f"[glossary] synonyms: {n2} term file(s) updated")
    _write_contents_md(project)


# ── _contents.md — the clickable table of contents for a markdown viewer ─────
# glossary.md stays a plain list of names (every reader parses it); this one is for people.

def _split_source(entry: str):
    """'text [file: a/b.md:12]' -> ('text', 'a/b.md', '12'); no tag -> (entry, '', '')."""
    m = _re.match(r'^(.*?)\s*\[file:\s*([^\]]*?)(?::(\d+))?\]\s*$', entry or "")
    return (m.group(1), m.group(2).strip(), m.group(3) or "") if m else (entry or "", "", "")


def _source_href(path: str) -> str:
    # sources are relative to the cwd of the run; the glossary is <cwd>/.1bcoder/glossary/<project>/
    return "../../../" + path.replace("\\", "/")


def _write_contents_md(project: str) -> str:
    terms = _load_terms(project)
    lines = [f"# Glossary — {project}", "",
             f"{len(terms)} term(s). Regenerated by every `/flow glossary index`, `relink`, `html`"
             + (" · [glossary.html](glossary.html)" if _os.path.isfile(_glossary_html_path(project)) else ""),
             ""]
    for term in terms:
        defs = _read_term_file(project, term)["definitions"]
        gist = _split_source(defs[0])[0] if defs else ""
        if len(gist) > 140:
            gist = gist[:137].rstrip() + "…"
        lines.append(f"- [{term}]({term}.md)" + (f" — {gist}" if gist else ""))
    path = _contents_md_path(project)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _cmd_contents(chat, rest: str) -> None:
    project, _ = _pop_project(rest)
    if not _load_terms(project):
        print(f"[glossary] no terms indexed yet (project: {project})")
        return
    print(f"[glossary] contents: {_write_contents_md(project)}")


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


# ── static html view — index + one page per term, real <a href> links ───────
# Renders straight from _read_term_file's parsed dict, the same source LINK:/
# SYNONYM: already resolve against for the .md cross-links — so the html view
# never invents structure the .md files don't already have. Self-contained,
# stdlib only (no external CSS/JS/fonts), so glossary.html works by itself
# opened via file:// with no server, matching how the .md files are already
# read directly off disk.

_HTML_STYLE = """<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; max-width: 860px;
         margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
  h1 { font-size: 1.6rem; }
  h2 { font-size: 1.05rem; margin-top: 1.6rem; border-bottom: 1px solid #8888; padding-bottom: .25rem; }
  a { color: #3b7dd8; text-decoration: none; }
  a:hover { text-decoration: underline; }
  ul { padding-left: 1.3rem; }
  li { margin: .25rem 0; }
  .back { display: inline-block; margin-bottom: 1rem; font-size: .9rem; }
  .empty { color: #888; font-style: italic; }
  .index-count { color: #888; font-size: .9rem; }
  .gist { color: #888; }
  a.src { font-family: Consolas, monospace; font-size: .85rem; color: #888; }
</style>"""


def _esc(s: str) -> str:
    return _html.escape(s, quote=False)


def _html_page(title_html: str, body_html: str) -> str:
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<title>{title_html}</title>\n{_HTML_STYLE}\n</head>\n<body>\n{body_html}"
            f"</body>\n</html>\n")


def _render_index_html(project: str, terms: list, gists: dict = None) -> str:
    gists = gists or {}
    items = "\n".join(f'    <li><a href="{_kebab(t)}.html">{_esc(t)}</a>'
                      + (f' <span class="gist">— {_esc(gists[t])}</span>' if gists.get(t) else "")
                      + "</li>" for t in terms)
    body = (f'  <h1>Glossary — {_esc(project)}</h1>\n'
            f'  <p class="index-count">{len(terms)} term(s)</p>\n'
            f'  <ul>\n{items}\n  </ul>\n')
    return _html_page(f"Glossary — {_esc(project)}", body)


def _html_section(title: str, item_html_list: list) -> str:
    if not item_html_list:
        return f'  <h2>{title}</h2>\n  <p class="empty">(none)</p>\n'
    items = "\n".join(f"    <li>{item}</li>" for item in item_html_list)
    return f'  <h2>{title}</h2>\n  <ul>\n{items}\n  </ul>\n'


def _source_link_html(path: str, line: str) -> str:
    label = path + (f":{line}" if line else "")
    return f'<a class="src" href="{_html.escape(_source_href(path))}">{_esc(label)}</a>'


def _entry_html(entry: str) -> str:
    """An entry with its [file: path:line] tag as a link to the source file."""
    text, path, line = _split_source(entry)
    return _esc(text) + (" " + _source_link_html(path, line) if path else "")


def _seen_html(where: str) -> str:
    m = _re.match(r'^file:\s*(.*?)(?::(\d+))?$', where.strip())
    return _source_link_html(m.group(1), m.group(2) or "") if m else _esc(where)


def _render_term_html(term: str, data: dict) -> str:
    body = (
        _html_section("Definition", [_entry_html(d) for d in data["definitions"]])
        + _html_section("Facts", [_entry_html(f) for f in data["facts"]])
        + _html_section("Related terms", [f'<a href="{_kebab(l)}.html">{_esc(l)}</a>' for l in data["links"]])
        + _html_section("Synonyms", [f'<a href="{_kebab(s)}.html">{_esc(s)}</a>' for s in data["synonyms"]])
        + (_html_section("Kind (jev)", [_entry_html(k) for k in data["kind"]]) if data.get("kind") else "")
        + (_html_section("Seen in", [_seen_html(w) for w in data["seen"]]) if data.get("seen") else "")
        + _html_section("History", [_esc(h) for h in data["history"]])
    )
    header = (f'  <a class="back" href="glossary.html">&larr; back to index</a>\n'
              f'  <h1>{_esc(term)}</h1>\n')
    return _html_page(f"{_esc(term)} — glossary", header + body)


def _cmd_html(chat, rest: str) -> None:
    project, rest = _pop_project(rest)
    terms = _load_terms(project)
    if not terms:
        print(f"[glossary] no terms indexed yet (project: {project})")
        return
    gists = {}
    for term in terms:
        data = _read_term_file(project, term)
        if data["definitions"]:
            gist = _split_source(data["definitions"][0])[0]
            gists[term] = gist if len(gist) <= 140 else gist[:137].rstrip() + "…"
        with open(_term_html_path(project, term), "w", encoding="utf-8") as f:
            f.write(_render_term_html(term, data))
    with open(_glossary_html_path(project), "w", encoding="utf-8") as f:
        f.write(_render_index_html(project, terms, gists))
    _write_contents_md(project)
    print(f"[glossary] html: glossary.html + {len(terms)} term page(s) written to {_glossary_dir(project)}"
          " (and _contents.md)")


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
    elif sub == "html":
        _cmd_html(chat, rest)
    elif sub == "contents":
        _cmd_contents(chat, rest)
    else:
        _cmd_filter(args)
