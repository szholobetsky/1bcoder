"""webindex — crawl a site and index it with simargl for RAG use.

Crawls a URL (BFS), saves pages as .txt files, then runs simargl index
so the content becomes searchable via --rag in deepagent_md.

Usage:
  /flow webindex <url> --project <name> --path <dir> [--depth N] [--pages N]

Examples:
  /flow webindex https://kafka.apache.org/documentation/ --project kafka --path C:\\MyProject\\
  /flow webindex https://docs.python.org/3/library/ --project pydocs --path . --pages 30

After indexing:
  /flow deepagent_md "my topic" --rag kafka --rag-store C:\\MyProject --web 3
"""
import os as _os
import re as _re


def run(chat, args: str):
    args = args.strip()

    depth = 2
    dm = _re.search(r'--depth\s+(\d+)', args)
    if dm:
        depth = int(dm.group(1))
        args = (args[:dm.start()] + args[dm.end():]).strip()

    max_pages = 20
    pm = _re.search(r'--pages\s+(\d+)', args)
    if pm:
        max_pages = int(pm.group(1))
        args = (args[:pm.start()] + args[pm.end():]).strip()

    project = None
    prm = _re.search(r'--project\s+(\S+)', args)
    if prm:
        project = prm.group(1)
        args = (args[:prm.start()] + args[prm.end():]).strip()

    path = _os.getcwd()
    pam = _re.search(r'--path\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))', args)
    if pam:
        path = (pam.group(1) or pam.group(2) or pam.group(3)).rstrip("\\").rstrip("/")
        args = (args[:pam.start()] + args[pam.end():]).strip()

    url = args.strip().strip("\"'")
    if not url or not url.startswith("http"):
        print("usage: /flow webindex <url> --project <name> --path <dir> [--depth N] [--pages N]")
        return
    if not project:
        # derive project name from domain
        import urllib.parse as _up
        project = _up.urlparse(url).netloc.replace(".", "_").replace("www_", "")

    out_dir = _os.path.join(path, ".simargl_web", project)
    _os.makedirs(out_dir, exist_ok=True)

    print(f"[webindex] url     : {url}")
    print(f"[webindex] project : {project}")
    print(f"[webindex] output  : {out_dir}")
    print(f"[webindex] depth   : {depth}  pages: {max_pages}")

    # ── BFS crawl ──────────────────────────────────────────────────────────────
    from urllib.parse import urlparse as _up, urljoin as _uj

    # use browser fetch from deepagent_md if available
    try:
        import importlib as _il, sys as _sys
        _flow_dir = _os.path.dirname(_os.path.abspath(__file__))
        if _flow_dir not in _sys.path:
            _sys.path.insert(0, _flow_dir)
        _fetch_page = _il.import_module("deepagent_md")._fetch_page
    except Exception:
        def _fetch_page(url, timeout=12):
            import requests as _rr
            try:
                return _rr.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout).content
            except Exception:
                return None

    def _strip_html(content: bytes) -> str:
        try:
            text = chat._web_strip_html(content)
        except Exception:
            try:
                from lxml import html as lhtml
                import re
                tree = lhtml.fromstring(content)
                for tag in tree.xpath("//script|//style|//nav|//footer"):
                    p = tag.getparent()
                    if p is not None: p.remove(tag)
                try:
                    text = tree.text_content()
                except AttributeError:
                    text = "".join(tree.itertext())
                text = re.sub(r'\s+', ' ', text).strip()
            except Exception:
                text = content.decode("utf-8", errors="ignore")
        return text

    def _get_links(html_bytes: bytes, base: str, origin: str) -> list:
        try:
            from lxml import html as lhtml
            tree = lhtml.fromstring(html_bytes, base_url=base)
            tree.make_links_absolute()
            parsed_origin = _up(origin)
            prefix = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
            links = []
            seen = set()
            for el in tree.xpath("//a[@href]"):
                href = el.get("href", "").split("#")[0].rstrip("/")
                if href and href not in seen and href.startswith(prefix):
                    seen.add(href)
                    links.append(href)
            return links[:60]
        except Exception:
            return []

    visited = set()
    queue = [(url, 0)]
    saved = 0

    while queue and saved < max_pages:
        cur_url, cur_depth = queue.pop(0)
        if cur_url in visited:
            continue
        visited.add(cur_url)

        page_bytes = _fetch_page(cur_url)
        if not page_bytes:
            print(f"  [skip] {cur_url}")
            continue

        text = _strip_html(page_bytes)
        if not text.strip():
            continue

        # safe filename from URL
        safe = _re.sub(r'[^\w\-.]', '_', cur_url[7:])[:120]
        fname = _os.path.join(out_dir, f"{safe}.txt")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(f"URL: {cur_url}\n\n{text}")
        saved += 1
        print(f"  [{saved}/{max_pages}] {cur_url[:70]}")

        if cur_depth < depth:
            for link in _get_links(page_bytes, cur_url, url):
                if link not in visited:
                    queue.append((link, cur_depth + 1))

    print(f"\n[webindex] crawled {saved} pages -> {out_dir}")

    # ── simargl index ─────────────────────────────────────────────────────────
    print(f"[webindex] running simargl index...")
    try:
        import subprocess as _sp
        # run from `path` dir and pass relative out_dir so simargl stores
        # paths like ".simargl_web/hotel/file.txt" — retrieve can then find
        # them when run from the same `path` directory
        rel_out = _os.path.relpath(out_dir, path)
        result = _sp.run(
            ["simargl", "index", "files", rel_out,
             "--project", project,
             "--store", ".simargl"],
            capture_output=True, text=True, timeout=300,
            cwd=path,
        )
        if result.returncode == 0:
            print(f"[webindex] index OK — project '{project}' ready")
            print(f"[webindex] use: /flow deepagent_md <task> --rag {project} --rag-store {path}")
            print(f"[webindex]  or: /flow deepagent_md <task> --rag {project}  (if cwd={path})")
            print(f"[webindex] CLI: cd {path} && simargl retrieve \"query\" --mode file --project {project} --source .simargl_web\\{project}")
        else:
            print(f"[webindex] index error:\n{result.stderr[:500]}")
    except FileNotFoundError:
        print("[webindex] simargl not found in PATH — index step skipped")
        print(f"[webindex] run manually: cd {path} && simargl index files {_os.path.relpath(out_dir, path)} --project {project}")
        print(f"[webindex] then use: /flow deepagent_md <task> --rag {project} --rag-store {path}")
    except Exception as e:
        print(f"[webindex] index failed: {e}")
