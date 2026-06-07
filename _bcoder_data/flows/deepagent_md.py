"""deepagent_md — recursive markdown document tree generator.

Builds a folder of .md files by expanding a topic tree depth-first.
Each node calls the LLM once; deeper levels focus on subsections of
their parent. Optional --web or --rag injects source material before
each generation call.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 GENERATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow deepagent_md <task> [flags] [plan: l1,l2,l3] [list: a1,a2]

Core flags:
  --maxdepth N          tree depth (default 3; 1 = index only)
  --ctx N               main LLM context in K tokens (default 6)
  --max_parent_ctx N    chars of parent section to include (default 500)
  --profile name        /parallel profile — each worker expands one branch

Web research flags (applied per section before generation):
  --web [N]             DDG search, N pages per section (default 3)
  --fix top:A,mid:B,last:C   extract A chars from top / middle / bottom of page
  --scan N              chunk full page and compact each chunk to N chars
                        (chunk size = ctx * 50% * 4 chars)
  --prescan             pre-filter: ask LLM if page is relevant before using it
  --ref                 collect URLs into refs.json (per node); use in compose

RAG flags (simargl local index):
  --rag [name]          project name (default: "default"); cwd = store location
  --rag-store <path>    explicit path to the simargl store root
  --rw N                RAG/web mix: N*10% RAG, (100-N*10)% web  (0-10)
                        omit → RAG wins if >=300 chars (web skipped)
                        --rw 7 = 70% RAG + 30% web
                        --rw 3 = 30% RAG + 70% web
                        --rw 0 = web only (RAG skipped)

Presets (set web defaults; explicit flags override):
  --preset quick        --web 3 --fix top:2000
  --preset balanced     --web 5 --scan 200
  --preset deep         --web 10 --scan 200 --prescan --ref

Structure flags:
  plan: l1,l2,l3        per-depth focus labels (default: overview,analysis,implementation)
  list: a1,a2           aspect list injected into every generation prompt

Worker flags (parallel BFS):
  --ctx-worker N        context for parallel workers (default: same as --ctx)

Generate examples:
  /flow deepagent_md "REST API design with PostgreSQL" --maxdepth 3
  /flow deepagent_md "Hamlet themes" --web 5 --maxdepth 3 plan: overview,analysis,evidence
  /flow deepagent_md "heat equation in Java" --web --fix mid:1500,last:500
  /flow deepagent_md "heat equation in Java" --preset balanced --maxdepth 2
  /flow deepagent_md "hotel room features" --rag hotel --maxdepth 1
  /flow deepagent_md "hotel room features" --rag hotel --rag-store C:\\MyProject --maxdepth 1
  /flow deepagent_md "topic" --profile phones --ctx-worker 4 --maxdepth 3

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 COMPOSE  — assemble generated tree into a single document
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow deepagent_md compose <plan_dir> [--mode MODE] [--plan] [--ref MODE] [output]

Output modes (--mode):
  flat (default)    single composed.md file
  linked            hypertext/ folder — .md files with cross-links
  html              hypertext/ folder — .html files with cross-links
  html_plain        single composed.html (flat; add --plan for anchor TOC)

Extra flags:
  --plan            also write PLAN.md hierarchical outline to stdout
  --ref all         inject per-section references after each section's prose
  --ref distinct    append deduplicated bibliography at the end of the document

Compose examples:
  /flow deepagent_md compose plan1                             flat .md
  /flow deepagent_md compose plan1 --mode linked               hypertext .md + cross-links
  /flow deepagent_md compose plan1 --mode html                 hypertext .html + cross-links
  /flow deepagent_md compose plan1 --mode html_plain --plan    single HTML with anchor TOC
  /flow deepagent_md compose plan1 --plan                      PLAN.md outline only
  /flow deepagent_md compose plan1 --ref all                   .md with inline references
  /flow deepagent_md compose plan1 --ref distinct              .md with bibliography at end
  /flow deepagent_md compose plan1 out.md                      flat to custom filename

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 OUTPUT STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  .1bcoder/planMD/planN/      auto-incremented project-local directory
    index.md                  top-level overview (## 1. Title sections)
    item_1.md                 expansion of section 1
    item_1.1.md               expansion of subsection 1.1
    item_1.1.1.md             ... up to --maxdepth
    refs.json                 URLs per node (created when --ref is used)
    PLAN.md                   outline (created by compose --plan)
    composed.md               flat output (compose flat)
    hypertext/                linked/html output folder
"""
import os as _os
import re as _re

_DEFAULT_MAXDEPTH = 3

# ── prompts ───────────────────────────────────────────────────────────────────

_PROMPT = """\
Write a detailed markdown analysis of: "{title}"
This is part of a larger study on: "{root_task}"
{focus_line}{aspects_block}{ctx_block}
Structure your response with 2-5 sections using this exact header format:
## 1. Section Title
## 2. Section Title

Be specific and concrete. No preamble, no meta-commentary, no repetition of the title."""

_PROMPT_WEB = """\
Write a detailed markdown analysis of: "{title}"
This is part of a larger study on: "{root_task}"
{focus_line}{aspects_block}{ctx_block}
Use the web research below as your primary source. Reference specific facts.
Structure with 2-5 sections using: ## 1. Section Title format.
No preamble or meta-commentary.

{web_context}"""


# ── section parser ────────────────────────────────────────────────────────────

def _parse_sections(content: str) -> list:
    """Extract section titles from ## N. Title or # N. Title headers."""
    titles = []
    for line in content.splitlines():
        m = _re.match(r'^#{1,4}\s+\d+[.)]\s+(.+)', line)
        if m:
            titles.append(m.group(1).strip())
    return titles


# ── plan directory ────────────────────────────────────────────────────────────

def _make_plan_dir(base: str) -> str:
    n = 1
    while True:
        d = _os.path.join(base, f"plan{n}")
        if not _os.path.exists(d):
            _os.makedirs(d)
            return d
        n += 1


def _item_path(plan_dir: str, node_id: str) -> str:
    return _os.path.join(plan_dir, f"item_{node_id}.md")


# ── browser-like page fetch ───────────────────────────────────────────────────

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

_BLOCKED_CODES = {403, 429, 503}


def _fetch_page(url: str, timeout: int = 12) -> bytes | None:
    """Fetch page with browser headers. Falls back to Playwright on 403/429/503."""
    import requests as _r
    content = None
    try:
        resp = _r.get(url, headers=_BROWSER_HEADERS, timeout=timeout)
        if resp.status_code not in _BLOCKED_CODES:
            return resp.content
        # blocked — fall through to Playwright
        print(f"  [fetch] HTTP {resp.status_code} — trying Playwright: {url[:60]}")
    except Exception as e:
        print(f"  [fetch] requests error ({e}) — trying Playwright: {url[:60]}")

    # Playwright fallback with stealth (pip install playwright playwright-stealth)
    try:
        from playwright.sync_api import sync_playwright as _pw
        with _pw() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_BROWSER_HEADERS["User-Agent"],
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = ctx.new_page()
            # apply stealth patches if available
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(page)
            except ImportError:
                pass
            page.goto(url, timeout=20_000, wait_until="domcontentloaded")
            html = page.content()
            browser.close()
        # detect if we still got a block page
        if "Access Denied" in html or "Just a moment" in html or "cf-challenge" in html:
            print(f"  [fetch] Playwright: site still blocking (bot detection)")
            return None
        return html.encode("utf-8")
    except ImportError:
        print("  [fetch] Playwright not installed — pip install playwright && playwright install chromium")
    except Exception as e:
        print(f"  [fetch] Playwright failed: {e}")
    return None


# ── text extraction helpers ───────────────────────────────────────────────────

_DEFAULT_FIX = "top:2000"


def _extract_fix(text: str, spec: str) -> str:
    """Extract text portions per spec: 'top:N', 'mid:N', 'last:N', comma-separated."""
    parts = []
    for item in spec.split(","):
        item = item.strip()
        m = _re.match(r"(top|mid|last):(\d+)", item)
        if not m:
            continue
        where, n = m.group(1), int(m.group(2))
        if where == "top":
            parts.append(text[:n])
        elif where == "last":
            parts.append(text[-n:] if len(text) >= n else text)
        elif where == "mid":
            center = len(text) // 2
            half = n // 2
            start = max(0, center - half)
            parts.append(text[start:start + n])
    result = "\n[...]\n".join(p for p in parts if p.strip())
    return result or text[:2000]


def _scan_compact(chat, text: str, compact_to: int) -> str:
    """Split text into 50%-of-ctx chunks, compact each to compact_to chars."""
    if not text:
        return ""
    chunk_size = max(500, getattr(chat, "num_ctx", 2048) * 2)
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    parts = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        prompt = (
            f"Summarize the following text in {compact_to} characters or less. "
            f"Preserve key facts, numbers, names, and technical details. "
            f"Output ONLY the summary.\n\n{chunk}"
        )
        summary = chat._stream_chat([{"role": "user", "content": prompt}]) or ""
        parts.append(summary.strip()[:compact_to * 2])
    return "\n\n".join(parts)


def _apply_extract(chat, text: str, fix_spec, scan_to) -> str:
    """Apply --scan or --fix to source text. --scan takes priority."""
    if not text:
        return ""
    if scan_to:
        return _scan_compact(chat, text, int(scan_to))
    if fix_spec:
        return _extract_fix(text, fix_spec)
    return _extract_fix(text, _DEFAULT_FIX)


# ── reference tracking ────────────────────────────────────────────────────────

def _refs_path(plan_dir: str) -> str:
    return _os.path.join(plan_dir, "refs.json")


def _load_refs(plan_dir: str) -> list:
    import json as _json
    p = _refs_path(plan_dir)
    if _os.path.isfile(p):
        try:
            return _json.loads(open(p, encoding="utf-8").read())
        except Exception:
            return []
    return []


def _save_ref(plan_dir: str, node_id: str, title: str, url: str):
    import json as _json
    existing = _load_refs(plan_dir)
    existing.append({"node": node_id, "title": title, "url": url})
    open(_refs_path(plan_dir), "w", encoding="utf-8").write(
        _json.dumps(existing, ensure_ascii=False, indent=2)
    )


def _format_refs_block(refs: list) -> str:
    if not refs:
        return ""
    lines = ["\n### References"]
    for i, r in enumerate(refs, 1):
        lines.append(f"{i}. {r.get('title', '—')} — {r.get('url', '')}")
    return "\n".join(lines) + "\n"


# ── RAG research (simargl) ────────────────────────────────────────────────────

def _rag_research(query: str, project_id: str, store_dir: str,
                  fix_spec=None, scan_to=None, chat=None,
                  top_n: int = 5, top_k: int = 10) -> str:
    """Query simargl file index, read files from .simargl_web/, return text."""
    try:
        from simargl.searcher import search as _sim_search
    except ImportError:
        print("  [rag] simargl not installed")
        return ""

    import os as _os2

    simargl_store = _os2.path.join(store_dir, ".simargl")
    # webindex stores files here:
    web_dir = _os2.path.join(store_dir, ".simargl_web", project_id)

    try:
        result = _sim_search(
            query,
            mode="file",
            top_n=top_n,
            project_id=project_id,
            store_dir=simargl_store,
        )
    except Exception as e:
        print(f"  [rag] search error: {e}")
        return ""

    parts = []
    for f in result.get("files", [])[:top_n]:
        raw_path = f.get("path", "")
        score = f.get("score", 0)
        filename = _os2.path.basename(raw_path)

        # try webindex directory first, then raw path if absolute
        candidates = [
            _os2.path.join(web_dir, filename),
            raw_path if _os2.path.isabs(raw_path) else None,
        ]
        text = ""
        for candidate in candidates:
            if candidate and _os2.path.isfile(candidate):
                try:
                    text = open(candidate, encoding="utf-8", errors="ignore").read()
                    break
                except Exception:
                    continue

        if not text:
            continue

        if chat and (fix_spec or scan_to):
            text = _apply_extract(chat, text, fix_spec, scan_to)
        else:
            # cap per file so total RAG fits in ~50% of context window
            per_file = max(500, (getattr(chat, "num_ctx", 2048) * 2) // top_n) if chat else 3000
            text = text[:per_file]

        parts.append(f"[RAG {filename}  score:{score:.3f}]\n{text}")

    return "\n\n".join(parts)


# ── web research ──────────────────────────────────────────────────────────────

def _web_research(chat, title: str, root_task: str = "", max_chars: int = 2000,
                  web_n: int = 3, fix_spec=None, scan_to=None,
                  prescan: bool = False,
                  plan_dir: str = "", node_id: str = "", use_ref: bool = False,
                  rag_project: str = None, rag_path: str = None,
                  rw_ratio=None) -> str:
    query = f"{title} {root_task}".strip() if root_task else title

    # ── RAG source ────────────────────────────────────────────────────────────
    _RAG_MIN_CHARS = 300
    rag_ctx = ""
    if rag_project and rag_path:
        rag_ctx = _rag_research(query, rag_project, rag_path,
                                fix_spec=fix_spec, scan_to=scan_to, chat=chat)
        if rag_ctx:
            print(f"  [rag] {len(rag_ctx)} chars from {rag_project}")

    # --rw not set → RAG wins if ≥300 chars (old behavior)
    if rw_ratio is None:
        if rag_ctx and len(rag_ctx) >= _RAG_MIN_CHARS:
            return f"[RAG: {rag_project}]\n{rag_ctx}"

    # ── DDG web search ────────────────────────────────────────────────────────
    # skipped if rw_ratio==10 (100% RAG) or no web needed
    web_parts = []
    if rw_ratio is None or rw_ratio < 10:
        try:
            results = chat._web_ddg_search(query)
        except Exception:
            results = []
        if results:
            web_parts = [f"[search: {query}]"]
            for i, (t, url, snippet) in enumerate(results[:web_n + 2], 1):
                web_parts.append(f"[{i}] {t}\n{url}\n{snippet or ''}")
            fetched = 0
            for t, url, _ in results:
                if fetched >= web_n:
                    break
                if not url.startswith("http"):
                    continue
                try:
                    page_bytes = _fetch_page(url)
                    if not page_bytes:
                        continue
                    raw = chat._web_strip_html(page_bytes)
                    if prescan:
                        prescan_prompt = (
                            f'Does this text address "{title}" in the context of "{root_task}"? '
                            f"Reply YES or NO only.\n\n{raw[:1000]}"
                        )
                        verdict = chat._stream_chat([{"role": "user", "content": prescan_prompt}]) or ""
                        if not verdict.strip().upper().startswith("YES"):
                            print(f"  [prescan] skip: {url}")
                            continue
                    page = _apply_extract(chat, raw, fix_spec, scan_to)
                    web_parts.append(f"\n[PAGE from {url}]\n{page}")
                    if use_ref and plan_dir and node_id:
                        _save_ref(plan_dir, node_id, t, url)
                    fetched += 1
                except Exception:
                    continue

    # ── mix RAG + web by budget ───────────────────────────────────────────────
    if rw_ratio is not None:
        total = max(1000, getattr(chat, "num_ctx", 2048) * 2)
        rag_pct = min(1.0, max(0.0, rw_ratio / 10))
        rag_budget = int(total * rag_pct)
        web_budget = total - rag_budget
        parts = []
        if rag_ctx and rag_budget > 0:
            parts.append(f"[RAG: {rag_project}]\n{rag_ctx[:rag_budget]}")
        if web_parts and web_budget > 0:
            web_text = "\n\n".join(web_parts)
            parts.append(f"[WEB]\n{web_text[:web_budget]}")
        return "\n\n".join(parts)

    # rw_ratio is None and RAG was insufficient → return web only
    if not web_parts:
        return rag_ctx
    return "\n\n".join(web_parts)


# ── LLM call ──────────────────────────────────────────────────────────────────

def _extract_parent_section(plan_dir: str, node_id: str, max_chars: int) -> str:
    """Extract the specific section from parent file that this node expands."""
    parts = node_id.split(".")
    if len(parts) < 2:
        # top-level: extract from index.md
        parent_file = _os.path.join(plan_dir, "index.md")
        sec_num = int(parts[0])
    else:
        parent_id   = ".".join(parts[:-1])
        sec_num     = int(parts[-1])
        parent_file = _os.path.join(plan_dir, f"item_{parent_id}.md")

    if not _os.path.isfile(parent_file):
        return ""

    lines = open(parent_file, encoding="utf-8").readlines()
    sec_re = _re.compile(r'^#{1,4}\s+(\d+)[.)]\s+')
    start = end = None
    for i, line in enumerate(lines):
        m = sec_re.match(line)
        if m:
            if int(m.group(1)) == sec_num and start is None:
                start = i
            elif start is not None:
                end = i
                break
    if start is None:
        return ""
    section = "".join(lines[start: end])
    if max_chars > 0:
        section = section[:max_chars]
    return section.strip()


def _generate(chat, title: str, root_task: str, web_ctx: str = "",
              focus: str = "", aspects: list = None, parent_ctx: str = "",
              chat_ctx: str = "") -> str:
    prompt = _build_prompt(title, root_task, web_ctx, focus, aspects or [], parent_ctx, chat_ctx)
    msgs = [
        {"role": "system", "content": "You are a research writer. Follow output format strictly."},
        {"role": "user", "content": prompt},
    ]
    return chat._stream_chat(msgs) or ""


# ── recursive expand ──────────────────────────────────────────────────────────

def _expand(chat, node_id: str, title: str, root_task: str,
            plan_dir: str, depth: int, max_depth: int, use_web: bool,
            plan_labels: list, aspects: list, stats: dict,
            max_parent_ctx: int = 500, chat_ctx: str = "", cfg: dict = None):
    if depth > max_depth:
        return

    filepath = _item_path(plan_dir, node_id)
    indent = "  " * (depth - 1)
    focus = plan_labels[depth - 1] if depth - 1 < len(plan_labels) else ""

    if _os.path.isfile(filepath):
        print(f"{indent}[skip] {node_id} — already exists")
        content = open(filepath, encoding="utf-8").read()
    else:
        display = title[:60] + ("..." if len(title) > 60 else "")
        print(f"\n{indent}[gen] {node_id}: {display}" + (f"  [{focus}]" if focus else ""))

        web_ctx = ""
        if use_web or (cfg or {}).get("rag_project"):
            print(f"{indent}  [web] {title}")
            _cfg = cfg or {}
            web_ctx = _web_research(
                chat, title, root_task,
                web_n=_cfg.get("web_n", 3),
                fix_spec=_cfg.get("fix_spec"),
                scan_to=_cfg.get("scan_to"),
                prescan=_cfg.get("prescan", False),
                plan_dir=plan_dir if _cfg.get("use_ref") else "",
                node_id=node_id if _cfg.get("use_ref") else "",
                use_ref=_cfg.get("use_ref", False),
                rag_project=_cfg.get("rag_project"),
                rag_path=_cfg.get("rag_path"),
                rw_ratio=_cfg.get("rw_ratio"),
            )
            if web_ctx:
                print(f"{indent}  [web] {len(web_ctx)} chars")

        parent_ctx = _extract_parent_section(plan_dir, node_id, max_parent_ctx)
        if parent_ctx:
            print(f"{indent}  [parent ctx] {len(parent_ctx)} chars")
        content = _generate(chat, title, root_task, web_ctx, focus, aspects, parent_ctx, chat_ctx)
        if not content:
            print(f"{indent}  [skip] empty reply")
            return

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{content}")
        stats["files"] += 1
        print(f"{indent}  -> {_os.path.basename(filepath)} ({len(content)} chars)")

    if depth < max_depth:
        sections = _parse_sections(content)
        if not sections:
            print(f"{indent}  [leaf] no ## N. sections found — stopping branch")
            return
        for i, child_title in enumerate(sections, 1):
            _expand(chat, f"{node_id}.{i}", child_title, root_task,
                    plan_dir, depth + 1, max_depth, use_web, plan_labels, aspects, stats,
                    max_parent_ctx, chat_ctx, cfg)


# ── compose flat ─────────────────────────────────────────────────────────────

def _shift_headings(text: str, shift: int) -> str:
    result = []
    for line in text.splitlines():
        m = _re.match(r'^(#{1,6})([ \t].*|$)', line)
        if m:
            new_level = min(len(m.group(1)) + shift, 6)
            line = '#' * new_level + m.group(2)
        result.append(line)
    return "\n".join(result)


def _split_sections(text: str) -> tuple:
    """Split file body into (preamble, [(sec_num, heading_line, body_text), ...])."""
    sec_re = _re.compile(r'^#{1,4}\s+(\d+)[.)]\s+', _re.MULTILINE)
    lines  = text.splitlines(keepends=True)
    preamble_lines = []
    sections = []
    current_num  = None
    current_head = ""
    current_body = []

    for line in lines:
        m = sec_re.match(line)
        if m:
            if current_num is not None:
                sections.append((current_num, current_head, "".join(current_body)))
            elif not sections:
                pass   # still in preamble, close it
            current_num  = int(m.group(1))
            current_head = line
            current_body = []
        elif current_num is None:
            preamble_lines.append(line)
        else:
            current_body.append(line)

    if current_num is not None:
        sections.append((current_num, current_head, "".join(current_body)))

    return "".join(preamble_lines), sections


# ── parallel worker call ──────────────────────────────────────────────────────

_INTERNAL_PARAMS = {"timeout", "num_ctx", "think_exclude", "ask_limit",
                    "ask_show", "run_timeout", "log", "keep_alive"}

def _generate_worker(host: str, model: str, prompt: str,
                     num_ctx: int, params: dict) -> str:
    """Direct HTTP POST to a specific Ollama worker (stream=False, thread-safe)."""
    import requests as _r
    msgs = [
        {"role": "system", "content": "You are a research writer. Follow output format strictly."},
        {"role": "user",   "content": prompt},
    ]
    opts = {"num_ctx": num_ctx}
    opts.update({k: v for k, v in params.items() if k not in _INTERNAL_PARAMS})
    base = host if host.startswith("http") else f"http://{host}"
    body = {"model": model, "messages": msgs, "stream": False, "options": opts}
    keep_alive = params.get("keep_alive")
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    try:
        resp = _r.post(f"{base}/api/chat", json=body, timeout=300)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "") or ""
    except Exception as e:
        print(f"  [worker {host}] error: {e}")
        return ""


def _serialize_ctx(messages: list, n: int) -> str:
    """Serialize last N user/assistant messages as readable text block."""
    if not messages or n == 0:
        return ""
    recent = [m for m in messages if m.get("role") in ("user", "assistant")][-n:]
    if not recent:
        return ""
    lines = ["[Conversation context — use these details in your analysis]"]
    for m in recent:
        role = "User" if m["role"] == "user" else "Assistant"
        text = m.get("content", "")[:800]   # cap each message
        lines.append(f"{role}: {text}")
    return "\n".join(lines) + "\n"


def _build_prompt(title: str, root_task: str, web_ctx: str,
                  focus: str, aspects: list, parent_ctx: str,
                  chat_ctx: str = "") -> str:
    focus_line    = f"Focus perspective: {focus}\n" if focus else ""
    aspects_block = ("\nCover these aspects:\n" + "\n".join(f"- {a}" for a in aspects) + "\n") if aspects else ""
    ctx_block     = f"\n{chat_ctx}" if chat_ctx else ""
    parent_block  = f"\nParent section context:\n{parent_ctx}\n" if parent_ctx else ""
    if web_ctx:
        return _PROMPT_WEB.format(title=title, root_task=root_task,
                                  focus_line=focus_line, aspects_block=aspects_block,
                                  ctx_block=ctx_block,
                                  web_context=web_ctx) + parent_block
    return _PROMPT.format(title=title, root_task=root_task,
                          focus_line=focus_line, aspects_block=aspects_block,
                          ctx_block=ctx_block) + parent_block


# ── BFS parallel expansion ────────────────────────────────────────────────────

def _expand_bfs(chat, root_sections: list, root_task: str, plan_dir: str,
                max_depth: int, use_web: bool, plan_labels: list, aspects: list,
                max_parent_ctx: int, workers: list, stats: dict, chat_ctx: str = "",
                cfg: dict = None):
    """BFS level-by-level: generate all nodes at same depth in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    # queue: list of (node_id, title, depth)
    queue = [(str(i + 1), t, 1) for i, t in enumerate(root_sections)]

    while queue:
        level_nodes = queue
        queue = []
        depth = level_nodes[0][2]
        focus = plan_labels[depth - 1] if depth - 1 < len(plan_labels) else ""
        n_workers = len(workers) if workers else 1
        print(f"\n[deepagent_md] level {depth}  nodes={len(level_nodes)}  workers={n_workers}")

        def _one_node(args):
            node_id, title, d = args
            filepath = _item_path(plan_dir, node_id)
            if _os.path.isfile(filepath):
                content = open(filepath, encoding="utf-8").read()
                return node_id, title, d, content, True   # skipped

            _cfg = cfg or {}
            web_ctx    = _web_research(
                chat, title, root_task,
                web_n=_cfg.get("web_n", 3),
                fix_spec=_cfg.get("fix_spec"),
                scan_to=_cfg.get("scan_to"),
                prescan=_cfg.get("prescan", False),
                plan_dir=plan_dir if _cfg.get("use_ref") else "",
                node_id=node_id if _cfg.get("use_ref") else "",
                use_ref=_cfg.get("use_ref", False),
                rag_project=_cfg.get("rag_project"),
                rag_path=_cfg.get("rag_path"),
            ) if (use_web or _cfg.get("rag_project")) else ""
            parent_ctx = _extract_parent_section(plan_dir, node_id, max_parent_ctx)
            prompt     = _build_prompt(title, root_task, web_ctx, focus, aspects, parent_ctx, chat_ctx)

            # pick worker round-robin by position in level
            idx = [n[0] for n in level_nodes].index(node_id)
            if workers:
                host, model, _ = workers[idx % len(workers)]
                display_worker = f"{host}/{model}"
                content = _generate_worker(host, model, prompt, chat.num_ctx, chat.params)
            else:
                content = _generate(chat, title, root_task, web_ctx, focus, aspects, parent_ctx, chat_ctx)
                display_worker = "local"

            return node_id, title, d, content, False

        results = []
        total = len(level_nodes)
        done = 0
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = {pool.submit(_one_node, node): node for node in level_nodes}
            for fut in _as_completed(futs):
                r = fut.result()
                results.append(r)
                done += 1
                node_id, title, d, content, skipped = r
                print(f"  [{done}/{total}] node {node_id} done")
                if not content:
                    print(f"  [skip] {node_id} — empty")
                elif not skipped:
                    filepath = _item_path(plan_dir, node_id)
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(f"# {title}\n\n{content}")
                    stats["files"] += 1
                    label = title[:50] + ("..." if len(title) > 50 else "")
                    print(f"  {node_id}: {label} ({len(content)} chars)")

        # sort for deterministic child queue ordering
        results.sort(key=lambda x: tuple(int(p) for p in x[0].split(".")))

        for node_id, title, d, content, skipped in results:
            if content and d < max_depth:
                sections = _parse_sections(content)
                for i, child_title in enumerate(sections, 1):
                    queue.append((f"{node_id}.{i}", child_title, d + 1))


# ── compose helpers ───────────────────────────────────────────────────────────

def _compose_node(plan_dir: str, all_ids: set, nid: str,
                  depth: int, out: list, write_heading: bool = True,
                  refs_by_node: dict = None, ref_mode: str = ""):
    """Depth-first: write file header, then for each section write content + recurse."""
    fpath = _os.path.join(plan_dir, f"item_{nid}.md")
    if not _os.path.isfile(fpath):
        return
    raw   = open(fpath, encoding="utf-8").read()
    rlines = raw.splitlines()
    title  = rlines[0].lstrip("#").strip() if rlines else nid
    body   = "\n".join(rlines[1:])

    if write_heading:
        heading = "#" * min(depth + 1, 6)
        out.append(f"\n{heading} {nid}. {title}\n")

    preamble, sections = _split_sections(body)
    sec_heading_level = "#" * min(depth + 2, 6)  # one deeper than file heading

    if preamble.strip():
        out.append(_shift_headings(preamble, depth) + "\n")

    # insert refs for THIS node immediately after its own prose, before children
    if ref_mode == "all" and refs_by_node and nid in refs_by_node:
        out.append(_format_refs_block(refs_by_node[nid]))

    for sec_num, sec_head, sec_body in sections:
        # replace original "## N. Title" with hierarchical "### nid.N. Title"
        hier_id = f"{nid}.{sec_num}"
        title_m = _re.match(r'^#{1,6}\s+\d+[.)]\s*(.*)', sec_head.rstrip())
        sec_title = title_m.group(1) if title_m else sec_head.strip().lstrip("#").strip()
        out.append(f"{sec_heading_level} {hier_id}. {sec_title}\n")
        if sec_body.strip():
            out.append(_shift_headings(sec_body, depth + 1) + "\n")
        child_nid = f"{nid}.{sec_num}"
        if child_nid in all_ids:
            _compose_node(plan_dir, all_ids, child_nid, depth + 1, out,
                          write_heading=False, refs_by_node=refs_by_node, ref_mode=ref_mode)


def _anchor(nid: str) -> str:
    return "s" + nid.replace(".", "-")


def _compose_html_plain(plan_dir: str, output_file: str, with_toc: bool = False):
    """Single HTML file — flat compose converted to HTML, optionally with anchor TOC."""
    md_tmp = output_file.replace(".html", "_tmp.md")
    _compose(plan_dir, md_tmp)
    try:
        md_text = open(md_tmp, encoding="utf-8").read()
    except OSError as e:
        print(f"[deepagent_md] {e}"); return
    _os.remove(md_tmp)

    index_path = _os.path.join(plan_dir, "index.md")
    title = "Composed Document"
    if _os.path.isfile(index_path):
        first = open(index_path, encoding="utf-8").readline()
        title = first.lstrip("#").strip() or title

    all_ids = _collect_node_ids(plan_dir)

    if with_toc:
        # inject anchor markers before each hierarchical heading in the md
        # headings look like: "## 1. Title", "### 1.2. Title", "#### 1.2.3. Title"
        # we insert <a id="s1-2-3"></a> before them
        def _add_anchor(m):
            hashes, rest_of_line = m.group(1), m.group(2)
            # extract nid from "N. " or "N.M. " pattern
            nid_m = _re.match(r'\s*([\d.]+)[.)]\s+', rest_of_line)
            if nid_m:
                nid = nid_m.group(1).rstrip('.')
                return f'<a id="{_anchor(nid)}"></a>\n{hashes}{rest_of_line}'
            return m.group(0)
        md_text = _re.sub(r'^(#{2,6})([ \t]+[\d].+)$', _add_anchor, md_text, flags=_re.M)

    html_body = _md_to_html(md_text, title)

    if with_toc:
        files_sorted = sorted(all_ids, key=lambda x: tuple(int(p) for p in x.split(".")))
        toc_lines = ["<nav id='toc'><h2>Contents</h2><ul>"]
        prev_depth = 0
        for nid in files_sorted:
            depth  = nid.count(".")
            t      = _read_file_title(plan_dir, nid)
            indent = "&nbsp;" * (depth * 4)
            link   = f'<a href="#{_anchor(nid)}">{nid}. {t}</a>'
            toc_lines.append(f"<li>{indent}{link}</li>")
        toc_lines.append("</ul></nav>")
        toc_html = "\n".join(toc_lines)

        # inject TOC after <body> tag
        html_body = html_body.replace("<body>", f"<body>\n{toc_html}\n<hr>", 1)
        # inject TOC style
        toc_style = ("nav#toc{background:#f6f8fa;padding:1em 1.5em;margin-bottom:2em;"
                     "border-radius:6px}nav#toc ul{list-style:none;padding:0;margin:0}"
                     "nav#toc li{margin:3px 0;line-height:1.5}")
        html_body = html_body.replace("</style>", f"{toc_style}</style>", 1)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_body)
    print(f"[deepagent_md] html_plain -> {output_file}")


def _compose(plan_dir: str, output_file: str, ref_mode: str = ""):
    all_ids = _collect_node_ids(plan_dir)
    top     = sorted([i for i in all_ids if "." not in i], key=int)
    out     = []

    index_path = _os.path.join(plan_dir, "index.md")
    if _os.path.isfile(index_path):
        raw = open(index_path, encoding="utf-8").read()
        title = raw.splitlines()[0] if raw else ""
        out.append(title + "\n")

    # build refs_by_node index if needed
    refs_by_node = None
    if ref_mode in ("all", "distinct"):
        all_refs = _load_refs(plan_dir)
        refs_by_node = {}
        for r in all_refs:
            nid = r.get("node", "")
            refs_by_node.setdefault(nid, []).append(r)

    for nid in top:
        _compose_node(plan_dir, all_ids, nid, depth=1, out=out,
                      refs_by_node=refs_by_node, ref_mode=ref_mode)

    # distinct: global deduplicated bibliography at the end
    if ref_mode == "distinct" and refs_by_node:
        seen_urls = set()
        unique = []
        all_refs = _load_refs(plan_dir)
        for r in all_refs:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique.append(r)
        if unique:
            lines = ["\n## References"]
            for i, r in enumerate(unique, 1):
                lines.append(f"{i}. {r.get('title', '—')} — {r.get('url', '')}")
            out.append("\n".join(lines) + "\n")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"[deepagent_md] composed {len(all_ids)} files -> {output_file}")


# ── compose hypertext (linked / html) ────────────────────────────────────────

def _read_file_title(plan_dir: str, nid: str) -> str:
    """Read first line of item_N.md and return title text."""
    fpath = _os.path.join(plan_dir, f"item_{nid}.md")
    try:
        with open(fpath, encoding="utf-8") as f:
            return f.readline().lstrip("#").strip()
    except OSError:
        return nid


def _extract_plan(plan_dir: str, all_ids: set) -> str:
    """Build a hierarchical outline: N.M.K. Title per line."""
    files = sorted(all_ids, key=lambda x: tuple(int(p) for p in x.split(".")))
    lines = []
    index_path = _os.path.join(plan_dir, "index.md")
    if _os.path.isfile(index_path):
        with open(index_path, encoding="utf-8") as f:
            root_title = f.readline().lstrip("#").strip()
        lines.append(root_title)
        lines.append("")
    for nid in files:
        depth  = nid.count(".")          # 0 for "1", 1 for "1.2", etc.
        indent = "  " * depth
        title  = _read_file_title(plan_dir, nid)
        lines.append(f"{indent}{nid}. {title}")
    return "\n".join(lines)


def _collect_node_ids(plan_dir: str) -> set:
    ids = set()
    for fname in _os.listdir(plan_dir):
        m = _re.match(r'^item_([\d.]+)\.md$', fname)
        if m:
            ids.add(m.group(1))
    return ids


def _child_ids(node_id: str, all_ids: set) -> list:
    """Return direct children of node_id, sorted."""
    prefix = node_id + "."
    depth  = node_id.count(".") + 2   # children have one more dot
    kids   = [i for i in all_ids
              if i.startswith(prefix) and i.count(".") == depth - 1]
    return sorted(kids, key=lambda x: tuple(int(p) for p in x.split(".")))


def _top_level_ids(all_ids: set) -> list:
    return sorted([i for i in all_ids if "." not in i], key=int)


def _inject_links(content: str, node_id: str, all_ids: set, ext: str,
                  plan_dir: str = "", show_title: bool = False) -> str:
    """After each ## N. section block insert links to child files that exist."""
    lines   = content.splitlines(keepends=True)
    result  = []
    sec_num = None

    def _link_label(nid: str) -> str:
        if show_title and plan_dir:
            t = _read_file_title(plan_dir, nid)
            return f"{nid} {t}"
        return nid

    def child_links_block(parent_nid: str, sec_n: int) -> str:
        child_nid = f"{parent_nid}.{sec_n}" if parent_nid else str(sec_n)
        kids = _child_ids(child_nid, all_ids)
        if not kids:
            direct = str(sec_n) if not parent_nid else child_nid
            if direct in all_ids:
                fname = f"item_{direct}{ext}"
                return f"\n- → [{_link_label(direct)}]({fname})\n"
            return ""
        lines = [f"- → [{_link_label(k)}](item_{k}{ext})" for k in kids]
        return "\n" + "\n".join(lines) + "\n"

    i = 0
    while i < len(lines):
        m = _re.match(r'^(#{1,4})\s+(\d+)[.)]\s+', lines[i])
        if m:
            # close previous section with links
            if sec_num is not None:
                lnk = child_links_block(node_id, sec_num)
                if lnk:
                    result.append(lnk)
            sec_num = int(m.group(2))
        result.append(lines[i])
        i += 1

    # close last section
    if sec_num is not None:
        lnk = child_links_block(node_id, sec_num)
        if lnk:
            result.append(lnk)

    return "".join(result)


def _md_to_html(md_text: str, title: str) -> str:
    try:
        import markdown as _md
        body = _md.markdown(md_text, extensions=["fenced_code", "tables"])
    except ImportError:
        # minimal fallback — headings + paragraphs + links
        body = md_text
        body = _re.sub(r'^#{6}\s+(.+)$', r'<h6>\1</h6>', body, flags=_re.M)
        body = _re.sub(r'^#{5}\s+(.+)$', r'<h5>\1</h5>', body, flags=_re.M)
        body = _re.sub(r'^#{4}\s+(.+)$', r'<h4>\1</h4>', body, flags=_re.M)
        body = _re.sub(r'^#{3}\s+(.+)$', r'<h3>\1</h3>', body, flags=_re.M)
        body = _re.sub(r'^#{2}\s+(.+)$', r'<h2>\1</h2>', body, flags=_re.M)
        body = _re.sub(r'^#{1}\s+(.+)$',  r'<h1>\1</h1>', body, flags=_re.M)
        body = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', body)
        body = _re.sub(r'\*(.+?)\*',     r'<em>\1</em>', body)
        body = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', body)
        body = _re.sub(r'^> (.+)$', r'<blockquote>\1</blockquote>', body, flags=_re.M)
        # unordered lists: consecutive "- item" lines → <ul><li>
        def _ul(m):
            items = _re.findall(r'^- (.+)$', m.group(0), _re.M)
            return "<ul>\n" + "\n".join(f"<li>{i}</li>" for i in items) + "\n</ul>"
        body = _re.sub(r'(?:^- .+\n?)+', _ul, body, flags=_re.M)
        paras = _re.split(r'\n{2,}', body.strip())
        body  = "\n".join(
            p if _re.match(r'^<[h1-6]|<block|<ul', p) else f"<p>{p}</p>"
            for p in paras
        )
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            f"<style>body{{font-family:sans-serif;max-width:860px;margin:2em auto;padding:0 1em}}"
            f"a{{color:#0366d6}}blockquote{{border-left:3px solid #ccc;padding-left:1em;color:#555}}"
            f"ul{{list-style:none;padding-left:0}}li{{margin:4px 0}}"
            f"pre{{background:#f6f8fa;padding:1em;overflow:auto}}</style></head>"
            f"<body>{body}</body></html>")


def _compose_hypertext(plan_dir: str, mode: str, show_title: bool = False):
    """mode: 'linked' (md with links) or 'html'."""
    ext     = ".html" if mode == "html" else ".md"
    out_dir = _os.path.join(plan_dir, "hypertext")
    _os.makedirs(out_dir, exist_ok=True)

    all_ids = _collect_node_ids(plan_dir)
    count   = 0

    # process index.md
    index_src = _os.path.join(plan_dir, "index.md")
    if _os.path.isfile(index_src):
        raw    = open(index_src, encoding="utf-8").read()
        linked = _inject_links(raw, "", all_ids, ext, plan_dir, show_title)
        title  = raw.splitlines()[0].lstrip("#").strip() if raw else "Index"
        out    = _os.path.join(out_dir, f"index{ext}")
        content = _md_to_html(linked, title) if mode == "html" else linked
        open(out, "w", encoding="utf-8").write(content)
        count += 1

    # process item files
    for fname in _os.listdir(plan_dir):
        m = _re.match(r'^item_([\d.]+)\.md$', fname)
        if not m:
            continue
        nid     = m.group(1)
        raw     = open(_os.path.join(plan_dir, fname), encoding="utf-8").read()
        linked  = _inject_links(raw, nid, all_ids, ext, plan_dir, show_title)
        title   = raw.splitlines()[0].lstrip("#").strip() if raw else nid
        outname = f"item_{nid}{ext}"
        out     = _os.path.join(out_dir, outname)
        content = _md_to_html(linked, title) if mode == "html" else linked
        open(out, "w", encoding="utf-8").write(content)
        count += 1

    print(f"[deepagent_md] hypertext ({mode}): {count} files -> {out_dir}")


# ── main ──────────────────────────────────────────────────────────────────────

def run(chat, args: str):
    args = args.strip()

    if args.startswith("compose"):
        rest  = args[7:].strip()
        mode  = "flat"
        for flag in ("--mode html_plain", "--mode linked", "--mode html"):
            if flag in rest:
                mode = flag.split()[1]
                rest = rest.replace(flag, "").strip()
                break

        # strip --ref before splitting to avoid it landing in plan_name
        ref_mode = ""
        if "--ref distinct" in rest:
            ref_mode = "distinct"
            rest = rest.replace("--ref distinct", "").strip()
        elif "--ref all" in rest:
            ref_mode = "all"
            rest = rest.replace("--ref all", "").strip()

        parts = rest.split()
        if not parts:
            print("usage: /flow deepagent_md compose <plan_dir> [output.md] [--mode flat|linked|html]")
            return
        show_plan  = "--plan" in rest
        rest_clean = rest.replace("--plan", "").strip()
        parts      = rest_clean.split()

        plan_name = parts[0] if parts else ""
        if not plan_name or plan_name.startswith("--"):
            print("usage: /flow deepagent_md compose <plan_dir> [--mode flat|linked|html] [--plan] [--ref all|distinct]")
            return
        base = _os.path.join(_os.getcwd(), ".1bcoder", "planMD")
        plan_dir = plan_name if _os.path.isabs(plan_name) else _os.path.join(base, plan_name)
        if not _os.path.isdir(plan_dir):
            print(f"[deepagent_md] not found: {plan_dir}")
            return

        all_ids = _collect_node_ids(plan_dir)

        if show_plan:
            plan_text = _extract_plan(plan_dir, all_ids)
            plan_file = _os.path.join(plan_dir, "PLAN.md")
            open(plan_file, "w", encoding="utf-8").write(plan_text)
            print(plan_text)
            print(f"\n[deepagent_md] plan saved -> {plan_file}")

        if mode in ("linked", "html"):
            _compose_hypertext(plan_dir, mode, show_title=show_plan)
        elif mode == "html_plain":
            out = parts[1] if len(parts) > 1 else _os.path.join(plan_dir, "composed.html")
            _compose_html_plain(plan_dir, out, with_toc=show_plan)
        elif mode != "plan_only":
            out = parts[1] if len(parts) > 1 else _os.path.join(plan_dir, "composed.md")
            _compose(plan_dir, out, ref_mode=ref_mode)
        return

    # ── preset (sets defaults, explicit flags override) ───────────────────────
    preset_web_n  = 3
    preset_fix    = None
    preset_scan   = None
    preset_prescan = False
    preset_ref    = False
    pm = _re.search(r'--preset\s+(\S+)', args)
    if pm:
        preset = pm.group(1).lower()
        args = (args[:pm.start()] + args[pm.end():]).strip()
        if preset == "quick":
            preset_web_n, preset_fix = 3, "top:2000"
        elif preset == "balanced":
            preset_web_n, preset_scan = 5, 200
        elif preset == "deep":
            preset_web_n, preset_scan, preset_prescan, preset_ref = 10, 200, True, True

    # ── web ──────────────────────────────────────────────────────────────────
    use_web = "--web" in args
    web_n = preset_web_n
    wm = _re.search(r'--web\s+(\d+)', args)
    if wm:
        web_n = int(wm.group(1))
        args = (args[:wm.start()] + args[wm.end():]).strip()
    elif use_web:
        args = args.replace("--web", "").strip()

    # ── fix ───────────────────────────────────────────────────────────────────
    fix_spec = preset_fix
    fm = _re.search(r'--fix\s+(\S+)', args)
    if fm:
        fix_spec = fm.group(1)
        args = (args[:fm.start()] + args[fm.end():]).strip()

    # ── scan ──────────────────────────────────────────────────────────────────
    scan_to = preset_scan
    sm = _re.search(r'--scan\s+(\d+)', args)
    if sm:
        scan_to = int(sm.group(1))
        args = (args[:sm.start()] + args[sm.end():]).strip()

    # ── prescan ───────────────────────────────────────────────────────────────
    use_prescan = preset_prescan or "--prescan" in args
    args = args.replace("--prescan", "").strip()

    # ── ref ───────────────────────────────────────────────────────────────────
    use_ref = preset_ref or "--ref" in args
    args = args.replace("--ref", "").strip()

    # ── rag ───────────────────────────────────────────────────────────────────
    # --rag           → project="default", store=cwd
    # --rag <name>    → project=name,      store=cwd
    # --rag <name> --rag-store <path>  → explicit store
    rag_project = None
    rag_path    = None
    ragm = _re.search(r'--rag(?!-)(?:\s+([^-\s]\S*))?', args)
    if ragm:
        rag_project = ragm.group(1) or "default"
        rag_path    = _os.getcwd()
        args = (args[:ragm.start()] + args[ragm.end():]).strip()
    # optional explicit store path (quoted or unquoted)
    rsm = _re.search(r'--rag-store\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))', args)
    if rsm:
        if rag_project:
            rag_path = rsm.group(1) or rsm.group(2) or rsm.group(3)
        args = (args[:rsm.start()] + args[rsm.end():]).strip()

    # ── rw (rag/web mix ratio) ────────────────────────────────────────────────
    # --rw N  → RAG gets N*10% of context budget, web gets (100-N*10)%
    # None    → current behavior: RAG wins if ≥300 chars (web skipped)
    rw_ratio = None
    rwm = _re.search(r'--rw\s+([\d.]+)', args)
    if rwm:
        rw_ratio = float(rwm.group(1))
        args = (args[:rwm.start()] + args[rwm.end():]).strip()

    maxdepth = _DEFAULT_MAXDEPTH
    m = _re.search(r'--maxdepth\s+(\d+)', args)
    if m:
        maxdepth = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    ctx_worker_n = 0
    m = _re.search(r'--ctx-worker\s+(\d+)', args)
    if m:
        ctx_worker_n = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    ctx_n = 6
    m = _re.search(r'--ctx\s+(\d+)', args)
    if m:
        ctx_n = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    max_parent_ctx = 500
    m = _re.search(r'--max_parent_ctx\s+(\d+)', args)
    if m:
        max_parent_ctx = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    profile_name = None
    m = _re.search(r'--profile\s+(\S+)', args)
    if m:
        profile_name = m.group(1)
        args = (args[:m.start()] + args[m.end():]).strip()
    workers = None
    if profile_name:
        import sys as _sys
        import os as _os2
        # find _load_profile from the main chat module
        _chat_dir = _os2.path.dirname(_os2.path.abspath(__file__))
        _root = _os2.path.dirname(_os2.path.dirname(_chat_dir))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        try:
            import importlib as _il
            _chat_mod = _il.import_module("chat")
            workers = _chat_mod._load_profile(profile_name)
        except Exception:
            pass
        if not workers:
            # try directly via chat object
            try:
                from chat import _load_profile as _lp
                workers = _lp(profile_name)
            except Exception:
                pass
        if workers:
            print(f"[deepagent_md] profile  : {profile_name} ({len(workers)} workers)")
            for h, mdl, _ in workers:
                print(f"               {h}  {mdl}")
        else:
            print(f"[deepagent_md] profile '{profile_name}' not found — running single")

    plan_m = _re.search(r'\bplan:\s*(.+?)(?:\s+(?:list:|$))', args + " ")
    list_m = _re.search(r'\blist:\s*(.+?)(?:\s*$)', args)
    anchor = len(args)
    if plan_m: anchor = min(anchor, plan_m.start())
    if list_m: anchor = min(anchor, list_m.start())

    plan_labels = [l.strip() for l in plan_m.group(1).split(',') if l.strip()] if plan_m \
                  else ['overview', 'analysis', 'implementation']
    aspects     = [l.strip() for l in list_m.group(1).split(',') if l.strip()] if list_m else []
    task = args[:anchor].strip().strip("\"'")
    if not task:
        print("usage: /flow deepagent_md <task> [--web] [--maxdepth N]")
        print("       /flow deepagent_md compose <plan_dir> [output.md]")
        print('  e.g. /flow deepagent_md "heat equation as Cauchy problem in Java" --web --maxdepth 3')
        return

    base = _os.path.join(_os.getcwd(), ".1bcoder", "planMD")
    _os.makedirs(base, exist_ok=True)
    plan_dir = _make_plan_dir(base)

    chat_ctx        = _serialize_ctx(getattr(chat, "messages", []), ctx_n)
    chat_ctx_worker = _serialize_ctx(getattr(chat, "messages", []), ctx_worker_n)

    print(f"[deepagent_md] task          : {task}")
    print(f"[deepagent_md] maxdepth      : {maxdepth}")
    print(f"[deepagent_md] chat ctx      : {ctx_n} msgs local / {ctx_worker_n} msgs workers ({len(chat_ctx)} chars)")
    print(f"[deepagent_md] parent ctx    : {'unlimited' if max_parent_ctx == 0 else max_parent_ctx}")
    print(f"[deepagent_md] plan          : {' -> '.join(plan_labels)}")
    if aspects:
        print(f"[deepagent_md] aspects  : {', '.join(aspects)}")
    print(f"[deepagent_md] web      : {use_web}" + (f" (n={web_n})" if use_web else ""))
    if fix_spec:
        print(f"[deepagent_md] fix      : {fix_spec}")
    if scan_to:
        print(f"[deepagent_md] scan     : compact to {scan_to} chars")
    if use_prescan:
        print(f"[deepagent_md] prescan  : on")
    if use_ref:
        print(f"[deepagent_md] ref      : tracking -> refs.json")
    print(f"[deepagent_md] dir      : {plan_dir}")

    if rag_project:
        print(f"[deepagent_md] rag       : project={rag_project} path={rag_path}")
    cfg = {
        "web_n":       web_n,
        "fix_spec":    fix_spec,
        "scan_to":     scan_to,
        "prescan":     use_prescan,
        "use_ref":     use_ref,
        "rag_project": rag_project,
        "rag_path":    rag_path,
        "rw_ratio":    rw_ratio,
    }

    saved = dict(chat.params)
    if "temperature" not in chat.params:
        chat.params["temperature"] = 0.8

    stats = {"files": 0}

    # generate index
    index_path = _os.path.join(plan_dir, "index.md")
    if _os.path.isfile(index_path):
        print("[deepagent_md] resuming from existing index.md")
        index_content = open(index_path, encoding="utf-8").read()
    else:
        print(f"\n[gen] index: {task}")
        web_ctx = ""
        if use_web or cfg.get("rag_project"):
            print("  [web] searching root topic...")
            web_ctx = _web_research(
                chat, task, task,
                web_n=cfg["web_n"],
                fix_spec=cfg["fix_spec"],
                scan_to=cfg["scan_to"],
                prescan=cfg["prescan"],
                plan_dir=plan_dir if cfg["use_ref"] else "",
                node_id="index" if cfg["use_ref"] else "",
                use_ref=cfg["use_ref"],
                rag_project=cfg.get("rag_project"),
                rag_path=cfg.get("rag_path"),
            )
        index_content = _generate(chat, task, task, web_ctx,
                                  focus=plan_labels[0] if plan_labels else "",
                                  aspects=aspects, chat_ctx=chat_ctx)
        if not index_content:
            print("[deepagent_md] failed to generate index — stopping")
            chat.params = saved
            return
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(f"# {task}\n\n{index_content}")
        stats["files"] += 1
        print(f"  -> index.md ({len(index_content)} chars)")

    top = _parse_sections(index_content)
    if not top:
        print("[deepagent_md] no ## N. sections in index.md — check LLM output format")
        chat.params = saved
        return

    print(f"\n[deepagent_md] {len(top)} top-level sections, expanding to depth {maxdepth}...")

    if workers:
        _expand_bfs(chat, top, task, plan_dir,
                    max_depth=maxdepth, use_web=use_web,
                    plan_labels=plan_labels, aspects=aspects,
                    max_parent_ctx=max_parent_ctx, workers=workers,
                    stats=stats, chat_ctx=chat_ctx_worker, cfg=cfg)
    else:
        for i, title in enumerate(top, 1):
            _expand(chat, str(i), title, task, plan_dir,
                    depth=1, max_depth=maxdepth, use_web=use_web,
                    plan_labels=plan_labels, aspects=aspects, stats=stats,
                    max_parent_ctx=max_parent_ctx, chat_ctx=chat_ctx, cfg=cfg)

    chat.params = saved
    total = stats["files"]
    print(f"\n[deepagent_md] done: {total} files generated in {plan_dir}")
    print(f"[deepagent_md] to join: /flow deepagent_md compose {_os.path.basename(plan_dir)}")
