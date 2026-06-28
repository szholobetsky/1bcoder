"""Crawl URLs, save pages or extract structured data.

Modes:
  extract (default) — XPath columns → CSV
  pages             — each page as .txt file (URL structure preserved)
  combine           — all pages as one .txt file
  mirror            — save .html files with relative links
  llm               — LLM filter + reduce: 2 calls per page (filter relevance, then extract)

Usage:
  /flow webcrawl <url> name:"//h1/text()" price:"//span/text()" [--out result.csv]
  /flow webcrawl <url> --columns cols.yaml [--out result.csv]
  /flow webcrawl <url> --mode pages   --out ./pages/  [--depth 2]
  /flow webcrawl <url> --mode combine --out all.txt   [--depth 2]
  /flow webcrawl <url> --mode mirror  --out ./mirror/ [--depth 2]
  /flow webcrawl <url> --mode pages   --filter mysite.com/docs --out ./docs/

  LLM-driven crawl:
  /flow webcrawl <url> --mode llm --task "find cheapest rail steel shovel" [--out result.csv]
  /flow webcrawl <url> --mode llm --task "..." --format log --out findings.txt
  /flow webcrawl <url> --mode llm --task "..." --columns cols.yaml --out result.csv
  /flow webcrawl <url> --mode pages --filter llm --task "..." --out ./pages/

  --filter llm (or --filter smart) — LLM decides per-page relevance; works with any mode.
  --mode llm   — implies LLM filter + LLM reduce (2 calls/page); --filter llm redundant here.

  --format log        — output: running log "URL: ...\n<free-text extract>" per relevant page
  --format structured — output: CSV or MD table (auto-detected from --out extension)
                        fields from --columns, or LLM infers schema from --task

  Add --ask to any mode for LLM summary after completion.

--columns YAML format (cols.yaml):
  name:     "//h1/text()"
  price:    "//span[@class='price']/text()"
  sku:      "//*[@class='sku']/text()"
  category: "//nav[@class='breadcrumb']/a[last()]/text()"

  Note: use single quotes inside XPath for attribute values — @class='price'
  Each column runs its XPath on every page; rows are zipped across columns.
"""
import re as _re
import os as _os
import csv as _csv
from collections import deque as _deque
from itertools import zip_longest as _zip_longest
from urllib.request import urlopen as _urlopen, Request as _Request
from urllib.parse import urljoin as _urljoin, urlparse as _urlparse


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: int = 12) -> bytes | None:
    try:
        req = _Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "")
            if "html" not in ct and "xml" not in ct and "text" not in ct:
                return None
            return r.read()
    except Exception as e:
        print(f"[webcrawl] skip {url}: {e}")
        return None


def _extract_links(tree, base_url: str, domain: str) -> list[str]:
    links = []
    for href in tree.xpath("//a/@href"):
        abs_url = _urljoin(base_url, href)
        p = _urlparse(abs_url)
        if p.scheme in ("http", "https") and p.netloc == domain:
            links.append(abs_url.split("#")[0])
    return links


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

def _to_text(tree) -> str:
    for tag in tree.xpath("//script|//style|//nav|//header|//footer|//aside"):
        p = tag.getparent()
        if p is not None:
            p.remove(tag)
    body = tree.find(".//body")
    node = body if body is not None else tree
    try:
        raw = node.text_content()          # lxml.html elements
    except AttributeError:
        raw = "".join(node.itertext())     # lxml.etree elements
    lines = [ln.strip() for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ---------------------------------------------------------------------------
# URL → local file path
# ---------------------------------------------------------------------------

def _url_to_relpath(url: str, ext: str) -> str:
    path = _urlparse(url).path.strip("/")
    if not path:
        return "index" + ext
    if path.endswith("/"):
        path = path.rstrip("/") + "/index"
    root, _ = _os.path.splitext(path)
    return root + ext


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _keyword_match(task: str, text: str) -> bool:
    """Fast pre-filter: True if any task keyword (>=3 chars) appears in page text."""
    words = [w.lower() for w in _re.findall(r'\w+', task) if len(w) >= 3]
    if not words:
        return True  # no usable keywords → don't block
    low = text.lower()
    return any(w in low for w in words)


_SYS_FILTER = (
    "You are a strict relevance filter. "
    "Your only job is to check if a web page contains words or topics related to the given task. "
    "You MUST reply with a single word: YES or NO. "
    "Do not write anything else. Do not explain. Do not summarize. "
    "Reply YES only if you see words or topics clearly related to the task. "
    "Reply NO if the page is about something different."
)
_SYS_REDUCE = "You extract only relevant information concisely from web pages."


def _llm_filter_page(chat, task: str, url: str, text: str) -> bool:
    # step 1: fast keyword check — no LLM call needed if no match
    if not _keyword_match(task, text):
        print("[webcrawl] [prefilter] no keywords")
        return False
    # step 2: LLM relevance check
    snippet = text[:400]
    prompt = (
        f"Task: {task}\n"
        f"URL: {url}\n"
        f"Page excerpt:\n{snippet}\n\n"
        f"Does this page contain words or topics related to '{task}'?\n"
        "Reply with one word only: YES or NO."
    )
    msgs = [{"role": "system", "content": _SYS_FILTER},
            {"role": "user",   "content": prompt}]
    raw = chat._stream_chat(msgs) or ""
    print()
    answer = raw.strip().upper()
    return answer.startswith("YES") or answer.startswith("Y")


def _llm_infer_schema(chat, task: str) -> list[str]:
    """Ask LLM what fields to extract for this task. Returns field name list."""
    prompt = (f"Task: {task}\n\n"
              "List the data fields to extract from web pages for this task.\n"
              "Output ONLY a comma-separated list of short lowercase field names. No explanation.\n"
              "Example: name, price, material, availability")
    msgs = [{"role": "user", "content": prompt}]
    raw = chat._stream_chat(msgs) or ""
    print()
    fields = [f.strip().lower().replace(" ", "_") for f in raw.split(",") if f.strip()]
    return [f for f in fields if f != "url"][:8]


def _llm_reduce_structured(chat, task: str, url: str, text: str, fields: list[str]) -> list[str] | None:
    """Extract structured row. Returns list of values (one per field), or None if not relevant."""
    fields_str = ", ".join(fields)
    prompt = (f"Task: {task}\nURL: {url}\n\n"
              f"Page content:\n{text[:3000]}\n\n"
              f"Extract these fields: {fields_str}\n"
              f"Output ONE line with values separated by | in this order: {fields_str}\n"
              "Use empty string for missing values. Output ONLY the data line, nothing else.\n"
              "If the page is not relevant, output: NOT RELEVANT")
    msgs = [{"role": "system", "content": "Output only a pipe-separated data line or NOT RELEVANT."},
            {"role": "user",   "content": prompt}]
    raw = chat._stream_chat(msgs) or ""
    print()
    line = raw.strip().splitlines()[-1] if raw.strip() else ""
    if not line or "NOT RELEVANT" in line.upper():
        return None
    return [v.strip() for v in line.split("|")]


def _llm_reduce_log(chat, task: str, url: str, text: str) -> str:
    """Extract free-form relevant summary. Returns empty string if not relevant."""
    prompt = (f"Task: {task}\nURL: {url}\n\n"
              f"Page content:\n{text[:3000]}\n\n"
              "Extract only information relevant to the task in 1-5 sentences.\n"
              "If nothing relevant, write exactly: NOT RELEVANT")
    msgs = [{"role": "system", "content": _SYS_REDUCE},
            {"role": "user",   "content": prompt}]
    raw = chat._stream_chat(msgs) or ""
    print()
    result = raw.strip()
    return "" if "NOT RELEVANT" in result.upper() else result


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------

def _parse_inline_columns(args: str) -> dict[str, str]:
    """Parse name:"//xpath" pairs — XPath must use single quotes for attributes."""
    return {m.group(1): m.group(2)
            for m in _re.finditer(r'(\w+):"([^"]*)"', args)}


def _load_yaml_columns(path: str) -> dict[str, str]:
    try:
        import yaml as _yaml
        with open(path, encoding="utf-8") as f:
            return _yaml.safe_load(f)
    except ImportError:
        # fallback: naive key: "value" parser
        result = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = _re.match(r'\s*(\w+)\s*:\s*["\']?(.+?)["\']?\s*$', line)
                if m:
                    result[m.group(1)] = m.group(2).strip('"\'')
        return result


def _strip_flags(args: str) -> str:
    for pat in (r'\w+:"[^"]*"', r"--columns\s+\S+", r"--mode\s+\S+",
                r"--depth\s+\d+", r"--output\s+\S+", r"--out\s+\S+", r"--filter\s+\S+",
                r'--task\s+"[^"]*"', r"--task\s+\S+", r"--format\s+\S+",
                r"--limit\s+\d+", r"-N\s+\d+",
                r"--ask"):
        args = _re.sub(pat, "", args)
    return args.strip()


# ---------------------------------------------------------------------------
# BFS crawler
# ---------------------------------------------------------------------------

def _normalize_filter(f: str) -> str:
    """Ensure filter has scheme and no trailing slash."""
    if not f.startswith("http"):
        f = "https://" + f
    return f.rstrip("/")


def _crawl(start_url: str, max_depth: int, url_filter: str | None = None, max_pages: int = 0):
    """Yield (url, depth, raw_bytes, lxml_tree) for each reachable page.
    max_pages=0 means unlimited."""
    try:
        from lxml import etree as _et
    except ImportError:
        raise ImportError("lxml not installed — run: pip install lxml")

    prefix = _normalize_filter(url_filter) if url_filter else None
    domain = _urlparse(start_url).netloc
    queue = _deque([(start_url, 0)])
    visited: set[str] = set()
    seen: set[str] = {start_url}   # prevents duplicate queue entries
    yielded = 0

    while queue:
        url, depth = queue.popleft()
        if url in visited:
            continue
        if prefix and not url.startswith(prefix):
            continue
        visited.add(url)

        print(f"[webcrawl] ({depth}/{max_depth}) {url}")
        raw = _fetch(url)
        if raw is None:
            continue

        try:
            tree = _et.fromstring(raw, _et.HTMLParser())
        except Exception as e:
            print(f"[webcrawl] parse error {url}: {e}")
            continue

        yield url, depth, raw, tree
        yielded += 1
        if max_pages and yielded >= max_pages:
            print(f"[webcrawl] limit reached ({max_pages})")
            break

        if depth < max_depth:
            for link in _extract_links(tree, url, domain):
                if link not in seen:
                    seen.add(link)
                    queue.append((link, depth + 1))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def _mode_extract(start_url, max_depth, columns, out_path, url_filter=None, max_pages=0):
    rows = []
    col_names = list(columns.keys())
    xpaths = list(columns.values())

    for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter, max_pages):
        results = []
        for xp in xpaths:
            nodes = tree.xpath(xp)
            vals = []
            for n in nodes:
                text = n.text_content().strip() if hasattr(n, "text_content") else str(n).strip()
                if text:
                    vals.append(text)
            results.append(vals)

        for values in _zip_longest(*results, fillvalue=""):
            rows.append((url,) + tuple(values))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["url"] + col_names)
        w.writerows(rows)

    print(f"[webcrawl] extract done — {len(rows)} rows → {out_path}")
    return rows


def _mode_pages(start_url, max_depth, out_dir, url_filter=None, filter_fn=None, max_pages=0):
    _os.makedirs(out_dir, exist_ok=True)
    count = skipped = 0
    for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter, max_pages):
        if filter_fn:
            text = _to_text(tree)
            if not filter_fn(url, text):
                print("[webcrawl] [skip]")
                skipped += 1
                continue
        rel = _url_to_relpath(url, ".txt")
        dest = _os.path.join(out_dir, rel)
        _os.makedirs(_os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(f"URL: {url}\n\n")
            f.write(_to_text(tree) if not filter_fn else text)
        count += 1
    label = f" ({skipped} skipped by filter)" if skipped else ""
    print(f"[webcrawl] pages done — {count} files → {out_dir}{label}")
    return count


def _mode_combine(start_url, max_depth, out_path, url_filter=None, filter_fn=None, max_pages=0):
    parts = []
    skipped = 0
    for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter, max_pages):
        text = _to_text(tree)
        if filter_fn:
            if not filter_fn(url, text):
                print("[webcrawl] [skip]")
                skipped += 1
                continue
        parts.append(f"=== {url} ===\n\n{text}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n" + ("─" * 60) + "\n\n".join(parts))
    label = f" ({skipped} skipped by filter)" if skipped else ""
    print(f"[webcrawl] combine done — {len(parts)} pages → {out_path}{label}")
    return parts


def _mode_mirror(start_url, max_depth, out_dir, url_filter=None, max_pages=0):
    from lxml import etree as _et

    _os.makedirs(out_dir, exist_ok=True)

    # pass 1 — crawl, collect pages, build url→local_file index
    pages: list[tuple[str, bytes, object]] = []
    url_to_file: dict[str, str] = {}
    for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter, max_pages):
        rel = _url_to_relpath(url, ".html")
        dest = _os.path.join(out_dir, rel)
        pages.append((url, raw, tree))
        url_to_file[url] = dest

    # pass 2 — rewrite links, save
    for url, raw, tree in pages:
        dest = url_to_file[url]
        from_dir = _os.path.dirname(dest)

        # remove <base href> — it overrides all relative links and points back to original site
        for base in tree.xpath("//base"):
            p = base.getparent()
            if p is not None:
                p.remove(base)

        for a in tree.xpath("//a[@href]"):
            href = a.get("href", "")
            abs_href = _urljoin(url, href).split("#")[0]
            if abs_href in url_to_file:
                to_file = url_to_file[abs_href]
                rel_path = _os.path.relpath(to_file, from_dir).replace("\\", "/")
                a.set("href", rel_path)

        _os.makedirs(from_dir, exist_ok=True)
        html_bytes = _et.tostring(tree, method="html", encoding="unicode").encode("utf-8")
        with open(dest, "wb") as f:
            f.write(html_bytes)

    print(f"[webcrawl] mirror done — {len(pages)} html files → {out_dir}")
    return len(pages)


# ---------------------------------------------------------------------------
# LLM mode
# ---------------------------------------------------------------------------

def _mode_llm(chat, start_url: str, max_depth: int, task: str,
              out_path: str, fmt: str, fields: list, url_filter=None, max_pages=0):
    """Crawl with LLM filter (call 1) + LLM reduce (call 2) per page."""

    # schema inference for structured format
    if fmt == "structured" and not fields:
        print("[webcrawl] inferring schema from task...")
        chat._sep("AI")
        fields = _llm_infer_schema(chat, task)
        if fields:
            print(f"[webcrawl] schema: {', '.join(fields)}")
        else:
            print("[webcrawl] schema inference failed — falling back to log format")
            fmt = "log"

    rows: list      = []   # structured: [[url, v1, v2, ...], ...]
    log_parts: list = []   # log: ["URL: ...\n<text>", ...]
    filtered = skipped = 0

    for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter, max_pages):
        text = _to_text(tree)

        # call 1 — filter
        print(f"[webcrawl] [filter] {url}")
        chat._sep("AI")
        if not _llm_filter_page(chat, task, url, text):
            print("[webcrawl] [skip] not relevant")
            skipped += 1
            continue
        filtered += 1

        # call 2 — reduce
        print(f"[webcrawl] [reduce] {url}")
        chat._sep("AI")
        if fmt == "structured":
            vals = _llm_reduce_structured(chat, task, url, text, fields)
            if vals:
                rows.append([url] + vals)
                print(f"[webcrawl] [got] {' | '.join(vals[:4])}")
        else:
            summary = _llm_reduce_log(chat, task, url, text)
            if summary:
                log_parts.append(f"URL: {url}\n{summary}")
                print(f"[webcrawl] [got] {summary[:80]}{'...' if len(summary) > 80 else ''}")

    # write output
    if fmt == "structured" and fields:
        if out_path.endswith(".md"):
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(f"# Crawl results: {start_url}\n\nTask: {task}\n\n")
                header = "| url | " + " | ".join(fields) + " |"
                sep    = "|-----|" + "---|" * len(fields)
                f.write(header + "\n" + sep + "\n")
                for row in rows:
                    f.write("| " + " | ".join(str(v) for v in row) + " |\n")
        else:
            if not out_path.endswith(".csv"):
                out_path += ".csv"
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                w.writerow(["url"] + fields)
                w.writerows(rows)
        result = rows
    else:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(log_parts) if log_parts else "(no relevant pages found)")
        result = log_parts

    total = filtered + skipped
    print(f"[webcrawl] llm done — {filtered}/{total} relevant → {out_path}")
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(chat, args: str):
    mode_m    = _re.search(r"--mode\s+(\S+)", args)
    depth_m   = _re.search(r"--depth\s+(\d+)", args)
    out_m     = _re.search(r"--output\s+(\S+)", args) or _re.search(r"--out\s+(\S+)", args)
    columns_m = _re.search(r"--columns\s+(\S+)", args)
    filter_m  = _re.search(r"--filter\s+(\S+)", args)
    task_m    = _re.search(r'--task\s+"([^"]*)"', args) or _re.search(r"--task\s+(\S+)", args)
    format_m  = _re.search(r"--format\s+(\S+)", args)
    limit_m   = _re.search(r"(?:--limit|-N)\s+(\d+)", args)
    ask       = "--ask" in args

    mode      = mode_m.group(1) if mode_m else "extract"
    max_depth = int(depth_m.group(1)) if depth_m else 2
    max_pages = int(limit_m.group(1)) if limit_m else 0
    ask_summary = ask
    task      = task_m.group(1) if task_m else ""

    # --filter llm/smart → LLM filter; url-prefix → resolved after start_url; else → literal prefix
    filter_val = filter_m.group(1) if filter_m else None
    llm_filter = filter_val in ("llm", "smart") if filter_val else False
    use_start_as_prefix = filter_val == "url-prefix"
    url_filter = None if (llm_filter or use_start_as_prefix) else filter_val

    if llm_filter and mode != "llm":
        if not task:
            print("[webcrawl] --filter llm requires --task \"...\""); return
        print(f"[webcrawl] filter: LLM relevance filter active")

    start_url = _strip_flags(args)
    if not start_url.startswith("http"):
        print(
            "usage:\n"
            "  /flow webcrawl <url> name:\"//h1/text()\" price:\"//span/text()\" [--out result.csv]\n"
            "  /flow webcrawl <url> --columns cols.yaml [--out result.csv]\n"
            "  /flow webcrawl <url> --mode pages   --out ./pages/\n"
            "  /flow webcrawl <url> --mode combine --out all.txt\n"
            "  /flow webcrawl <url> --mode mirror  --out ./mirror/\n"
            "  /flow webcrawl <url> --mode llm --task \"your goal\" [--format log|structured] [--out result.csv]"
        )
        return

    if use_start_as_prefix:
        url_filter = start_url.rstrip("/")
    if url_filter:
        print(f"[webcrawl] filter: only pages under {_normalize_filter(url_filter)}")

    try:
        from lxml import etree  # noqa: F401
    except ImportError:
        print("[webcrawl] lxml not installed — run: pip install lxml")
        return

    def _mk_llm_filter_fn():
        def _fn(url, text):
            print(f"[webcrawl] [filter] {url}")
            chat._sep("AI")
            return _llm_filter_page(chat, task, url, text)
        return _fn

    def _mk_keyword_filter_fn():
        def _fn(url, text):
            if not _keyword_match(task, text):
                print("[webcrawl] [prefilter] no keywords")
                return False
            return True
        return _fn

    def _pick_filter_fn():
        """Return appropriate filter_fn based on flags."""
        if llm_filter:
            return _mk_llm_filter_fn()
        if task:
            return _mk_keyword_filter_fn()
        return None

    # ---- dispatch ----

    if mode == "llm":
        if not task:
            print("[webcrawl] --mode llm requires --task \"your search goal\""); return
        out_path = out_m.group(1) if out_m else "crawl_llm.txt"
        # auto-detect format from extension if not explicit
        if format_m:
            fmt = format_m.group(1)
        elif out_path.endswith(".csv"):
            fmt = "structured"
        else:
            fmt = "log"
        # fields from --columns or empty (LLM will infer for structured)
        fields = list(_load_yaml_columns(columns_m.group(1)).keys()) if columns_m else []
        result = _mode_llm(chat, start_url, max_depth, task, out_path, fmt, fields, url_filter, max_pages)
        summary_hint = f"{len(result)} relevant items → {out_path}"
        sample = str(result[:3])

    elif mode == "extract":
        filter_fn = _pick_filter_fn()
        if columns_m:
            columns = _load_yaml_columns(columns_m.group(1))
        else:
            columns = _parse_inline_columns(args)
        if not columns:
            print("[webcrawl] extract mode requires columns — inline or --columns file.yaml")
            return
        out_path = out_m.group(1) if out_m else "crawl_result.csv"
        # extract mode: filter_fn pre-checks each page before XPath extraction
        if filter_fn:
            rows = []
            col_names = list(columns.keys())
            from itertools import zip_longest as _zl
            for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter, max_pages):
                text = _to_text(tree)
                if not filter_fn(url, text):
                    print("[webcrawl] [skip]")
                    continue
                xpaths = list(columns.values())
                results = []
                for xp in xpaths:
                    nodes = tree.xpath(xp)
                    vals = [n.text_content().strip() if hasattr(n, "text_content") else str(n).strip() for n in nodes]
                    results.append([v for v in vals if v])
                for values in _zl(*results, fillvalue=""):
                    rows.append((url,) + tuple(values))
            import csv as _c
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = _c.writer(f)
                w.writerow(["url"] + col_names)
                w.writerows(rows)
            result = rows
        else:
            result = _mode_extract(start_url, max_depth, columns, out_path, url_filter, max_pages)
        summary_hint = f"{len(result)} rows extracted to {out_path}"
        sample = "\n".join(",".join(str(v) for v in r) for r in result[:40])

    elif mode == "pages":
        out_dir   = out_m.group(1) if out_m else "crawl_pages"
        filter_fn = _pick_filter_fn()
        count = _mode_pages(start_url, max_depth, out_dir, url_filter, filter_fn, max_pages)
        summary_hint = f"{count} pages saved to {out_dir}"
        sample = f"Directory: {out_dir}"

    elif mode == "combine":
        out_path  = out_m.group(1) if out_m else "crawl_combined.txt"
        filter_fn = _pick_filter_fn()
        parts = _mode_combine(start_url, max_depth, out_path, url_filter, filter_fn, max_pages)
        summary_hint = f"{len(parts)} pages combined to {out_path}"
        sample = "\n\n---\n\n".join(p[:500] for p in parts[:3])

    elif mode == "mirror":
        out_dir = out_m.group(1) if out_m else "crawl_mirror"
        count = _mode_mirror(start_url, max_depth, out_dir, url_filter, max_pages)
        summary_hint = f"{count} html files mirrored to {out_dir}"
        sample = f"Directory: {out_dir}"

    else:
        print(f"[webcrawl] unknown mode '{mode}' — use: extract | pages | combine | mirror | llm")
        return

    if not ask_summary:
        return

    prompt = (
        f"I crawled {start_url} in '{mode}' mode (depth={max_depth}).\n"
        f"Result: {summary_hint}\n\n"
        f"Sample output:\n{sample}\n\n"
        f"Summarize what was found and note anything interesting or unexpected."
    )
    temp_msgs = [{"role": "system", "content": chat._role},
                 {"role": "user",   "content": prompt}]
    chat._sep("AI")
    reply = chat._stream_chat(temp_msgs)
    if reply:
        chat.last_reply = reply
        chat._last_output = reply
        chat.messages.append({"role": "user",      "content": f"[webcrawl: {start_url} mode={mode}]"})
        chat.messages.append({"role": "assistant", "content": reply})
