"""Crawl URLs, save pages or extract structured data.

Modes:
  extract (default) — XPath columns → CSV
  pages             — each page as .txt file (URL structure preserved)
  combine           — all pages as one .txt file
  mirror            — save .html files with relative links

Usage:
  /flow webcrawl <url> name:"//h1/text()" price:"//span/text()" [--out result.csv]
  /flow webcrawl <url> --columns cols.yaml [--out result.csv]
  /flow webcrawl <url> --mode pages   --out ./pages/  [--depth 2]
  /flow webcrawl <url> --mode combine --out all.txt   [--depth 2]
  /flow webcrawl <url> --mode mirror  --out ./mirror/ [--depth 2]
  /flow webcrawl <url> --mode pages   --filter mysite.com/docs --out ./docs/

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
    raw = (body if body is not None else tree).text_content()
    # collapse whitespace
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
                r"--depth\s+\d+", r"--out\s+\S+", r"--filter\s+\S+", r"--ask"):
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


def _crawl(start_url: str, max_depth: int, url_filter: str | None = None):
    """Yield (url, depth, raw_bytes, lxml_tree) for each reachable page."""
    try:
        from lxml import etree as _et
    except ImportError:
        raise ImportError("lxml not installed — run: pip install lxml")

    prefix = _normalize_filter(url_filter) if url_filter else None
    domain = _urlparse(start_url).netloc
    queue = _deque([(start_url, 0)])
    visited: set[str] = set()

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

        if depth < max_depth:
            for link in _extract_links(tree, url, domain):
                if link not in visited:
                    queue.append((link, depth + 1))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def _mode_extract(start_url, max_depth, columns, out_path, url_filter=None):
    rows = []
    col_names = list(columns.keys())
    xpaths = list(columns.values())

    for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter):
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


def _mode_pages(start_url, max_depth, out_dir, url_filter=None):
    _os.makedirs(out_dir, exist_ok=True)
    count = 0
    for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter):
        rel = _url_to_relpath(url, ".txt")
        dest = _os.path.join(out_dir, rel)
        _os.makedirs(_os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(f"URL: {url}\n\n")
            f.write(_to_text(tree))
        count += 1
    print(f"[webcrawl] pages done — {count} files → {out_dir}")
    return count


def _mode_combine(start_url, max_depth, out_path, url_filter=None):
    parts = []
    for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter):
        text = _to_text(tree)
        parts.append(f"=== {url} ===\n\n{text}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n" + ("─" * 60) + "\n\n".join(parts))
    print(f"[webcrawl] combine done — {len(parts)} pages → {out_path}")
    return parts


def _mode_mirror(start_url, max_depth, out_dir, url_filter=None):
    from lxml import etree as _et

    _os.makedirs(out_dir, exist_ok=True)

    # pass 1 — crawl, collect pages, build url→local_file index
    pages: list[tuple[str, bytes, object]] = []
    url_to_file: dict[str, str] = {}
    for url, depth, raw, tree in _crawl(start_url, max_depth, url_filter):
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
# Entry point
# ---------------------------------------------------------------------------

def run(chat, args: str):
    mode_m    = _re.search(r"--mode\s+(\S+)", args)
    depth_m   = _re.search(r"--depth\s+(\d+)", args)
    out_m     = _re.search(r"--out\s+(\S+)", args)
    columns_m = _re.search(r"--columns\s+(\S+)", args)
    filter_m  = _re.search(r"--filter\s+(\S+)", args)
    ask       = "--ask" in args

    mode       = mode_m.group(1) if mode_m else "extract"
    max_depth  = int(depth_m.group(1)) if depth_m else 2
    url_filter = filter_m.group(1) if filter_m else None
    ask_summary = ask

    if url_filter:
        print(f"[webcrawl] filter: only pages under {_normalize_filter(url_filter)}")

    start_url = _strip_flags(args)
    if not start_url.startswith("http"):
        print(
            "usage:\n"
            "  /flow webcrawl <url> name:\"//h1/text()\" price:\"//span/text()\" [--out result.csv]\n"
            "  /flow webcrawl <url> --columns cols.yaml [--out result.csv]\n"
            "  /flow webcrawl <url> --mode pages   --out ./pages/\n"
            "  /flow webcrawl <url> --mode combine --out all.txt\n"
            "  /flow webcrawl <url> --mode mirror  --out ./mirror/"
        )
        return

    try:
        from lxml import etree  # noqa: F401
    except ImportError:
        print("[webcrawl] lxml not installed — run: pip install lxml")
        return

    # ---- dispatch ----

    if mode == "extract":
        if columns_m:
            columns = _load_yaml_columns(columns_m.group(1))
        else:
            columns = _parse_inline_columns(args)
        if not columns:
            print("[webcrawl] extract mode requires columns — inline or --columns file.yaml")
            return
        out_path = out_m.group(1) if out_m else "crawl_result.csv"
        result = _mode_extract(start_url, max_depth, columns, out_path, url_filter)
        summary_hint = f"{len(result)} rows extracted to {out_path}"
        sample = "\n".join(",".join(str(v) for v in r) for r in result[:40])

    elif mode == "pages":
        out_dir = out_m.group(1) if out_m else "crawl_pages"
        count = _mode_pages(start_url, max_depth, out_dir, url_filter)
        summary_hint = f"{count} pages saved to {out_dir}"
        sample = f"Directory: {out_dir}"

    elif mode == "combine":
        out_path = out_m.group(1) if out_m else "crawl_combined.txt"
        parts = _mode_combine(start_url, max_depth, out_path, url_filter)
        summary_hint = f"{len(parts)} pages combined to {out_path}"
        sample = "\n\n---\n\n".join(p[:500] for p in parts[:3])

    elif mode == "mirror":
        out_dir = out_m.group(1) if out_m else "crawl_mirror"
        count = _mode_mirror(start_url, max_depth, out_dir, url_filter)
        summary_hint = f"{count} html files mirrored to {out_dir}"
        sample = f"Directory: {out_dir}"

    else:
        print(f"[webcrawl] unknown mode '{mode}' — use: extract | pages | combine | mirror")
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
