"""websearch — one web search for 1bcoder and vyrii, several engines.

search(term, n, settings) → [(title, url, snippet), ...] from the engine the
person chose. Settings are a plain dict (1bcoder keeps it in
~/.1bcoder/search.json, vyrii in ~/.vyrii/config.json under "search"):

    {"default": "ddg",
     "engines": {"brave":   {"key": "..."},
                 "tavily":  {"key": "..."},
                 "searxng": {"url": "http://localhost:8888"}}}

Engines: ddg (default), wikipedia, openalex, crossref, arxiv — no key;
brave, tavily — a key; searxng — the URL of an instance (own or public).

A blocked request (202 "anomaly", 403, 429, captcha, "Just a moment") raises
SearchBlocked instead of looking like "no results". Only requests + stdlib,
so it installs on a phone. vyrii keeps an identical copy (vyrii/websearch.py).
"""
from __future__ import annotations

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import requests

DEFAULT_ENGINE = "ddg"
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".1bcoder", "search.json")
TIMEOUT = 15
_BROWSER_UA = "Mozilla/5.0"
# Wikipedia (and polite API use in general) wants a descriptive User-Agent, else 403
_API_UA = "websearch/1.0 (1bcoder / vyrii; python-requests)"

# name → (what it searches, what it needs: "" | "key" | "url")
ENGINES = {
    "ddg":       ("DuckDuckGo — the whole web",                  ""),
    "wikipedia": ("Wikipedia articles",                          ""),
    "openalex":  ("OpenAlex — scientific papers",                ""),
    "crossref":  ("Crossref — papers and DOIs",                  ""),
    "arxiv":     ("arXiv — preprints",                           ""),
    "brave":     ("Brave Search API — the whole web",            "key"),
    "tavily":    ("Tavily — web search for agents",              "key"),
    "searxng":   ("SearXNG — your own or a public instance",     "url"),
}


class SearchError(Exception):
    """The engine could not answer (network, bad reply)."""


class SearchConfigError(SearchError):
    """The engine needs a key or a URL that is not set."""


class SearchBlocked(SearchError):
    """The engine refused the request as automated."""


# ── settings ──────────────────────────────────────────────────────────────────

def load_settings(path: str = SETTINGS_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict, path: str = SETTINGS_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def default_engine(settings: dict | None) -> str:
    name = ((settings or {}).get("default") or DEFAULT_ENGINE).strip().lower()
    return name if name in ENGINES else DEFAULT_ENGINE


def engine_options(settings: dict | None, name: str) -> dict:
    opts = ((settings or {}).get("engines") or {}).get(name) or {}
    return opts if isinstance(opts, dict) else {}


def missing(settings: dict | None, name: str) -> str:
    """What the engine still needs ("key" / "url"), or "" when it is ready."""
    need = ENGINES.get(name, ("", ""))[1]
    if need and not str(engine_options(settings, name).get(need) or "").strip():
        return need
    return ""


def resolve_engine(key: str) -> str | None:
    """'3' or 'openalex' → engine name; None if unknown."""
    key = (key or "").strip().lower()
    names = list(ENGINES)
    if key.isdigit():
        i = int(key)
        return names[i - 1] if 1 <= i <= len(names) else None
    return key if key in ENGINES else None


# ── search ────────────────────────────────────────────────────────────────────

def search(term: str, n: int = 8, settings: dict | None = None,
           engine: str | None = None) -> list[tuple[str, str, str]]:
    """Search with `engine` (or the default from settings). Raises SearchError."""
    name = (engine or default_engine(settings)).strip().lower()
    if name not in ENGINES:
        raise SearchConfigError(f"unknown search engine '{name}' — known: {', '.join(ENGINES)}")
    need = missing(settings, name)
    if need:
        raise SearchConfigError(f"{name} needs a {need} — set it first")
    opts = engine_options(settings, name)
    try:
        results = _BACKENDS[name](term, n, opts)
    except SearchError:
        raise
    except requests.RequestException as e:
        raise SearchError(f"{name}: {e}") from e
    except (ValueError, KeyError, TypeError, ET.ParseError) as e:
        raise SearchError(f"{name}: unexpected reply ({e})") from e
    return [(_clean(t), u, _clean(s)) for t, u, s in results if u][:n]


def _clean(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", s or ""))
    return re.sub(r"\s+", " ", s).strip()


_BLOCK_MARKS = ("anomaly-modal", "anomaly.js", "captcha", "just a moment", "unusual traffic",
                "are you a robot", "enablejs")


def _check_blocked(name: str, resp: requests.Response) -> None:
    code = resp.status_code
    if code in (202, 403, 429):
        raise SearchBlocked(f"{name} blocked the request as automated (HTTP {code})")
    if code >= 400:
        raise SearchError(f"{name}: HTTP {code}")
    head = resp.text[:20000].lower() if "html" in resp.headers.get("Content-Type", "") else ""
    for mark in _BLOCK_MARKS:
        if mark in head:
            raise SearchBlocked(f"{name} blocked the request as automated ({mark!r} page)")


def _get(name, url, params=None, headers=None):
    resp = requests.get(url, params=params, timeout=TIMEOUT,
                        headers={"User-Agent": _API_UA, **(headers or {})})
    _check_blocked(name, resp)
    return resp


# ── engines ───────────────────────────────────────────────────────────────────

class _DDGParser(HTMLParser):
    """html.duckduckgo.com: a result = title link (result__a), then its snippet."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._in = None

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "") or ""
        if tag == "a" and "result__a" in cls:
            href = d.get("href", "") or ""
            try:
                url = parse_qs(urlparse(href).query).get("uddg", [""])[0] or unquote(href)
            except Exception:
                url = href
            if url.startswith("//"):
                url = "https:" + url
            self.results.append(["", url, ""])
            self._in = 0
        elif "result__snippet" in cls and self.results:
            self._in = 2

    def handle_endtag(self, tag):
        if self._in is not None and tag in ("a", "td", "div"):
            self._in = None

    def handle_data(self, data):
        if self._in is not None:
            self.results[-1][self._in] += data


def _ddg(term, n, opts):
    resp = requests.post("https://html.duckduckgo.com/html/", data={"q": term},
                         headers={"User-Agent": _BROWSER_UA}, timeout=TIMEOUT)
    _check_blocked("ddg", resp)
    p = _DDGParser()
    p.feed(resp.text)
    return [tuple(r) for r in p.results]


def _wikipedia(term, n, opts):
    lang = (opts.get("lang") or "en").strip()
    data = _get("wikipedia", f"https://{lang}.wikipedia.org/w/api.php", {
        "action": "query", "list": "search", "srsearch": term, "srlimit": n,
        "format": "json", "utf8": 1}).json()
    return [(h["title"], f"https://{lang}.wikipedia.org/wiki/" + h["title"].replace(" ", "_"),
             h.get("snippet", ""))
            for h in data.get("query", {}).get("search", [])]


def _abstract(inv: dict | None) -> str:
    """OpenAlex keeps abstracts as an inverted index {word: [positions]}."""
    if not inv:
        return ""
    pos = {}
    for word, places in inv.items():
        for p in places:
            pos[p] = word
    return " ".join(pos[i] for i in sorted(pos))[:400]


def _openalex(term, n, opts):
    data = _get("openalex", "https://api.openalex.org/works",
                {"search": term, "per-page": n}).json()
    out = []
    for w in data.get("results", []):
        url = w.get("doi") or (w.get("primary_location") or {}).get("landing_page_url") or w.get("id", "")
        year = w.get("publication_year")
        snippet = (f"{year}. " if year else "") + _abstract(w.get("abstract_inverted_index"))
        out.append((w.get("display_name") or "", url, snippet))
    return out


def _crossref(term, n, opts):
    data = _get("crossref", "https://api.crossref.org/works", {
        "query": term, "rows": n,
        "select": "DOI,title,container-title,issued,URL"}).json()
    out = []
    for it in data.get("message", {}).get("items", []):
        title = (it.get("title") or [""])[0]
        venue = (it.get("container-title") or [""])[0]
        year = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        snippet = ", ".join(str(x) for x in (venue, year) if x)
        out.append((title, it.get("URL") or f"https://doi.org/{it.get('DOI', '')}", snippet))
    return out


_ATOM = "{http://www.w3.org/2005/Atom}"


def _arxiv(term, n, opts):
    resp = _get("arxiv", "https://export.arxiv.org/api/query",
                {"search_query": f"all:{term}", "start": 0, "max_results": n})
    root = ET.fromstring(resp.content)
    return [(e.findtext(_ATOM + "title", ""), e.findtext(_ATOM + "id", ""),
             e.findtext(_ATOM + "summary", "")[:400])
            for e in root.findall(_ATOM + "entry")]


def _brave(term, n, opts):
    data = _get("brave", "https://api.search.brave.com/res/v1/web/search",
                {"q": term, "count": min(n, 20)},
                {"Accept": "application/json", "X-Subscription-Token": opts["key"].strip()}).json()
    return [(r.get("title", ""), r.get("url", ""), r.get("description", ""))
            for r in (data.get("web") or {}).get("results", [])]


def _tavily(term, n, opts):
    resp = requests.post("https://api.tavily.com/search", timeout=TIMEOUT * 2,
                         headers={"Authorization": f"Bearer {opts['key'].strip()}",
                                  "User-Agent": _API_UA},
                         json={"query": term, "max_results": min(n, 20)})
    if resp.status_code in (401, 403):
        raise SearchConfigError(f"tavily rejected the key (HTTP {resp.status_code})")
    _check_blocked("tavily", resp)
    return [(r.get("title", ""), r.get("url", ""), r.get("content", ""))
            for r in resp.json().get("results", [])]


def _searxng(term, n, opts):
    base = opts["url"].strip().rstrip("/")
    if base.endswith("/search"):
        base = base[: -len("/search")]
    try:
        data = _get("searxng", base + "/search", {"q": term, "format": "json"}).json()
    except SearchBlocked as e:
        raise SearchBlocked(f"{e} — or the instance has the json format switched off "
                            f"(settings.yml: search.formats: [html, json])") from e
    return [(r.get("title", ""), r.get("url", ""), r.get("content", ""))
            for r in data.get("results", [])]


_BACKENDS = {
    "ddg": _ddg, "wikipedia": _wikipedia, "openalex": _openalex, "crossref": _crossref,
    "arxiv": _arxiv, "brave": _brave, "tavily": _tavily, "searxng": _searxng,
}
