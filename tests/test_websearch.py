"""Tests for websearch.py — engine choice, settings, DDG parsing, block detection.
No network: requests.get/post are monkeypatched."""
import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import websearch as ws  # noqa: E402


class FakeResp:
    def __init__(self, status, text, ctype="text/html"):
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"Content-Type": ctype}

    def json(self):
        return json.loads(self.text)


DDG_PAGE = """<html><body>
<div class="result results_links web-result"><div class="links_main result__body">
<h2 class="result__title"><a rel="nofollow" class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fg2&amp;rut=x">The <b>G2</b> test</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fg2">
Log-likelihood <b>ratio</b> for two corpora.</a>
</div></div>
<div class="result"><h2 class="result__title"><a class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fmi">Mutual information</a></h2>
<a class="result__snippet" href="#">Measured in nats.</a></div>
</body></html>"""


def test_ddg_title_url_and_snippet(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, DDG_PAGE))
    r = ws.search("g2", 5, {})
    assert r == [("The G2 test", "https://example.org/g2", "Log-likelihood ratio for two corpora."),
                 ("Mutual information", "https://example.com/mi", "Measured in nats.")]


def test_ddg_202_anomaly_is_blocked_not_empty(monkeypatch):
    page = '<html><div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div></html>'
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(202, page))
    with pytest.raises(ws.SearchBlocked, match="HTTP 202"):
        ws.search("g2", 5, {})


def test_captcha_page_with_200_is_blocked(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, "<html>Please solve the captcha</html>"))
    with pytest.raises(ws.SearchBlocked):
        ws.search("g2", 5, {})


def test_default_engine_from_settings_and_fallback():
    assert ws.default_engine({}) == "ddg"
    assert ws.default_engine({"default": "openalex"}) == "openalex"
    assert ws.default_engine({"default": "no-such"}) == "ddg"


def test_resolve_engine_by_number_and_name():
    assert ws.resolve_engine("1") == "ddg"
    assert ws.resolve_engine(str(len(ws.ENGINES))) == list(ws.ENGINES)[-1]
    assert ws.resolve_engine("ArXiv") == "arxiv"
    assert ws.resolve_engine("0") is None
    assert ws.resolve_engine("google") is None


def test_key_engine_needs_key():
    assert ws.missing({}, "brave") == "key"
    assert ws.missing({"engines": {"brave": {"key": "x"}}}, "brave") == ""
    assert ws.missing({}, "searxng") == "url"
    assert ws.missing({}, "ddg") == ""
    with pytest.raises(ws.SearchConfigError, match="needs a key"):
        ws.search("q", 3, {"default": "tavily"})


def test_openalex_abstract_rebuilt(monkeypatch):
    body = {"results": [{"display_name": "Corpora", "doi": "https://doi.org/10.1/x",
                         "publication_year": 2011,
                         "abstract_inverted_index": {"two": [1], "Comparing": [0], "corpora": [2]}}]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, json.dumps(body), "application/json"))
    assert ws.search("q", 3, {"default": "openalex"}) == [
        ("Corpora", "https://doi.org/10.1/x", "2011. Comparing two corpora")]


def test_searxng_url_and_403_hint(monkeypatch):
    seen = {}

    def fake_get(url, params=None, **k):
        seen["url"] = url
        return FakeResp(403, "Forbidden")
    monkeypatch.setattr(requests, "get", fake_get)
    s = {"default": "searxng", "engines": {"searxng": {"url": "http://pi:8888/search/"}}}
    with pytest.raises(ws.SearchBlocked, match="json format"):
        ws.search("q", 3, s)
    assert seen["url"] == "http://pi:8888/search"


def test_settings_roundtrip(tmp_path):
    p = str(tmp_path / "sub" / "search.json")
    assert ws.load_settings(p) == {}
    ws.save_settings({"default": "arxiv"}, p)
    assert ws.load_settings(p) == {"default": "arxiv"}
