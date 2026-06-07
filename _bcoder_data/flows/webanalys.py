"""webanalys — analyse web sources for relevance to a query.

For each DDG result, fetches the page and asks the LLM to rate it 0-5
with a one-line explanation. Useful before running a long deepagent_md
to understand what quality of sources the query will yield.

Usage:
  /flow webanalys <query> [-n N] [--fix top:N,mid:N,last:N] [--scan N]

Examples:
  /flow webanalys "Cauchy problem heat equation Java implementation"
  /flow webanalys "REST API design best practices" -n 8
  /flow webanalys "quantum field theory simulation Python" -n 5 --fix top:1500,mid:500
"""
import re as _re


_RATING_PROMPT = """\
Rate how well the following web page content addresses this query:
Query: "{query}"

Rate from 0 to 5:
  5 = Directly answers the query with specific details, examples, or code
  4 = Mostly relevant, covers the topic well
  3 = Partially relevant, touches the topic but superficially
  2 = Weakly related, mostly different topic
  1 = Almost unrelated
  0 = Completely unrelated

Reply in this exact format (two lines only):
Rating: N
Why: one sentence explanation

Page content:
{content}"""


def _stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def run(chat, args: str):
    args = args.strip()

    n = 5
    nm = _re.search(r'-n\s+(\d+)', args)
    if nm:
        n = int(nm.group(1))
        args = (args[:nm.start()] + args[nm.end():]).strip()

    fix_spec = None
    fm = _re.search(r'--fix\s+(\S+)', args)
    if fm:
        fix_spec = fm.group(1)
        args = (args[:fm.start()] + args[fm.end():]).strip()

    scan_to = None
    sm = _re.search(r'--scan\s+(\d+)', args)
    if sm:
        scan_to = int(sm.group(1))
        args = (args[:sm.start()] + args[sm.end():]).strip()

    query = args.strip().strip("\"'")
    if not query:
        print("usage: /flow webanalys <query> [-n N] [--fix top:N,mid:N] [--scan N]")
        return

    print(f"[webanalys] query: {query}")
    print(f"[webanalys] fetching top {n} results from DDG...")

    try:
        results = chat._web_ddg_search(query, n=n + 3)
    except Exception as e:
        print(f"[webanalys] search failed: {e}")
        return
    if not results:
        print("[webanalys] no results")
        return

    import requests as _r

    # import helpers from deepagent_md
    try:
        import importlib as _il, sys as _sys, os as _os
        _flow_dir = _os.path.dirname(_os.path.abspath(__file__))
        if _flow_dir not in _sys.path:
            _sys.path.insert(0, _flow_dir)
        _dam = _il.import_module("deepagent_md")
        _apply = _dam._apply_extract
        _fetch = _dam._fetch_page
    except Exception:
        def _apply(chat, text, fix_spec, scan_to):
            return text[:2000]
        def _fetch(url, timeout=12):
            import requests as _rr
            try:
                r = _rr.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
                return r.content
            except Exception:
                return None

    ratings = []
    fetched = 0
    for title, url, snippet in results:
        if fetched >= n:
            break
        if not url.startswith("http"):
            continue
        print(f"  [{fetched + 1}/{n}] {url[:70]}")
        page_bytes = _fetch(url)
        source_label = "full page"
        if page_bytes:
            try:
                raw = chat._web_strip_html(page_bytes)
                content = _apply(chat, raw, fix_spec, scan_to)
            except Exception:
                content = snippet or ""
                source_label = "DDG snippet (parse error)"
        else:
            content = snippet or ""
            source_label = "DDG snippet (site blocked)"
            print(f"         (blocked — using DDG snippet)")

        if not content.strip():
            ratings.append((0, title, url, f"(no content — {source_label})"))
            fetched += 1
            continue

        source_note = f"[Source: {source_label}]\n\n" if source_label != "full page" else ""
        prompt = _RATING_PROMPT.format(query=query, content=source_note + content[:3000])
        reply = chat._stream_chat([{"role": "user", "content": prompt}]) or ""

        # parse rating
        score = 0
        why = "—"
        rm = _re.search(r'[Rr]ating:\s*(\d)', reply)
        wm = _re.search(r'[Ww]hy:\s*(.+)', reply)
        if rm:
            score = max(0, min(5, int(rm.group(1))))
        if wm:
            why = wm.group(1).strip()

        why_full = why if source_label == "full page" else f"{why}  [{source_label}]"
        ratings.append((score, title, url, why_full))
        fetched += 1

    if not ratings:
        print("[webanalys] no pages could be evaluated")
        return

    # sort by score descending
    ratings.sort(key=lambda x: x[0], reverse=True)

    lines = [f"# webanalys: {query}\n"]
    for i, (score, title, url, why) in enumerate(ratings, 1):
        lines.append(f"{i}. [{score}/5] {_stars(score)}  {title}")
        lines.append(f"   {url}")
        lines.append(f"   {why}")
        lines.append("")
    output = "\n".join(lines)

    print(f"\n[webanalys] results for: {query}\n")
    print(output)

    # make result saveable via /save
    chat.last_reply = output
    chat._last_output = output
