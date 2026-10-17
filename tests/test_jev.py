"""Tests for jev.py — decision-model adapter. No Ollama needed: requests.post is
monkeypatched with responses recorded from tev1:0.8b (Ollama 0.35)."""
import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import jev  # noqa: E402


# ── recorded replies ──────────────────────────────────────────────────────────

NOUL_ANS = {"type": "noul", "noul": 0.9541}
CHOICE_ANS = {"type": "choice", "choice": "py",
              "probabilities": {"py": 0.8299, "js": 0.1233, "go": 0.0468}, "confidence": 0.4938}
SCORE_ANS = {"type": "score", "score": 1.0917, "legend": {"0": "bad", "1": "ok", "2": "good"},
             "probabilities": {"0": 0.1281, "1": 0.6520, "2": 0.2199}, "confidence": 0.2034}


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self.content = json.dumps(body, ensure_ascii=False).encode("utf-8")


class Recorder:
    """Fake requests.post: answers every question with answers_by_type[type]."""

    def __init__(self, answers_by_type=None, status=200, body=None, exc=None):
        self.calls = []
        self.answers_by_type = answers_by_type or {"noul": NOUL_ANS, "choice": CHOICE_ANS,
                                                   "score": SCORE_ANS}
        self.status, self.body, self.exc = status, body, exc

    def __call__(self, url, json=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc:
            raise self.exc
        if self.body is not None:
            return FakeResp(self.status, self.body)
        answers = {qid: self.answers_by_type[q["type"]] for qid, q in json["questions"].items()}
        return FakeResp(200, {"model": json["model"], "answers": answers, "usage": {}})


@pytest.fixture
def settings():
    s = dict(jev.DEFAULTS)
    s.update(model="tev1:0.8b", host="http://localhost:11434")
    return s


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    home = tmp_path / "home_rubrics"
    work = tmp_path / "work"
    local = work / ".1bcoder" / "rubrics"
    for d in (home, local):
        d.mkdir(parents=True)
    monkeypatch.setattr(jev, "HOME_RUBRICS_DIR", str(home))
    monkeypatch.setattr(jev, "SETTINGS_FILE", str(tmp_path / "jev.json"))
    return {"home": home, "work": work, "local": local, "tmp": tmp_path}


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def rubric(criteria, name="r", **top):
    data = dict(top)
    data["criteria"] = criteria
    return jev.normalize_rubric(data, name)


# ── settings ──────────────────────────────────────────────────────────────────

def test_settings_defaults_when_missing(dirs):
    assert jev.load_settings() == jev.DEFAULTS


def test_settings_roundtrip_cyrillic_and_bom(dirs):
    s = jev.load_settings()
    s["model"] = "тев1:0.8b"
    jev.save_settings(s)
    raw = open(jev.SETTINGS_FILE, encoding="utf-8").read()
    assert "тев1" in raw  # ensure_ascii=False
    assert jev.load_settings()["model"] == "тев1:0.8b"
    # utf-8-sig: a BOM written by another editor must not break loading
    with open(jev.SETTINGS_FILE, "w", encoding="utf-8-sig") as f:
        json.dump({"threshold": 0.7}, f)
    s2 = jev.load_settings()
    assert s2["threshold"] == 0.7 and s2["batch"] == 64


def test_settings_corrupt_raises_config_error(dirs):
    write(dirs["tmp"] / "jev.json", "{not json")
    with pytest.raises(jev.JevConfigError):
        jev.load_settings()


@pytest.mark.parametrize("key,raw,expected", [
    ("timeout", "45", 45), ("timeout", "2.5", 2.5), ("threshold", "0.7", 0.7),
    ("batch", "8", 8), ("on_error", "PASS", "pass"), ("keep_alive", "10m", "10m"),
    ("keep_alive", "-1", -1), ("host", "-", ""), ("model", "tev1:4b", "tev1:4b"),
    ("host", "192.168.0.5:11434", "192.168.0.5:11434"),
])
def test_coerce_ok(key, raw, expected):
    assert jev.coerce_setting(key, raw) == expected


@pytest.mark.parametrize("key,raw", [
    ("threshold", "1.5"), ("threshold", "abc"), ("batch", "0"), ("batch", "65"),
    ("timeout", "0"), ("on_error", "maybe"), ("nope", "1"), ("host", "openai://x:1234"),
])
def test_coerce_bad(key, raw):
    with pytest.raises(jev.JevConfigError):
        jev.coerce_setting(key, raw)


def test_resolve_host():
    assert jev.resolve_host({"host": ""}, "http://localhost:11434") == "http://localhost:11434"
    assert jev.resolve_host({"host": "10.0.0.2:11434"}, "x") == "http://10.0.0.2:11434"
    assert jev.resolve_host({"host": "ollama://h:1/"}, "x") == "http://h:1"
    with pytest.raises(jev.JevConfigError):
        jev.resolve_host({"host": "openai://h:1"}, "x")
    with pytest.raises(jev.JevConfigError):
        jev.resolve_host({"host": ""}, "")


# ── rubric discovery ──────────────────────────────────────────────────────────

MINI = "criteria:\n  - id: a\n    check: regex\n    pattern: x\n"


def test_list_rubrics_order_dedupe_and_local_override(dirs):
    write(dirs["home"] / "zeta.yaml", MINI)
    write(dirs["home"] / "alpha.yaml", "description: home\n" + MINI)
    write(dirs["home"] / "beta.json", json.dumps({"criteria": [{"id": "a", "check": "regex", "pattern": "x"}]}))
    write(dirs["home"] / "notes.txt", "ignored")
    write(dirs["local"] / "zeta.yml", MINI)                              # hides global zeta
    write(dirs["local"] / "mine.yaml", MINI)
    got = jev.list_rubrics(str(dirs["work"]))
    assert [(s, n) for s, n, _ in got] == [("g", "alpha"), ("g", "beta"), ("l", "mine"), ("l", "zeta")]
    assert got[0][2].startswith(str(dirs["home"]))
    # index vs name
    assert jev.load_rubric("1", str(dirs["work"]))["description"] == "home"
    assert jev.load_rubric(4, str(dirs["work"]))["scope"] == "l"
    assert jev.load_rubric("mine", str(dirs["work"]))["name"] == "mine"
    with pytest.raises(jev.JevConfigError):
        jev.load_rubric("9", str(dirs["work"]))
    with pytest.raises(jev.JevConfigError):
        jev.load_rubric("missing", str(dirs["work"]))


def test_reserved_rubric_name(dirs):
    write(dirs["local"] / "list.yaml", MINI)
    with pytest.raises(jev.JevConfigError, match="reserved"):
        jev.load_rubric("list", str(dirs["work"]))


def test_load_yaml_cyrillic_bom(dirs):
    p = dirs["local"] / "uk.yaml"
    p.write_text("name: Стиль\ncriteria:\n  - id: no_dash\n    check: regex\n    pattern: \"—\"\n"
                 "    message: \"використано довге тире\"\n", encoding="utf-8-sig")
    r = jev.load_rubric("uk", str(dirs["work"]))
    assert r["title"] == "Стиль" and r["criteria"][0]["message"] == "використано довге тире"
    res = jev.run_rubric("текст — з тире", r, dict(jev.DEFAULTS))
    assert res["passed"] is False


def test_shipped_rubrics_valid():
    inst = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_bcoder_data", "rubrics")
    for stem in ("relevance", "tree_rules"):
        r = jev.normalize_rubric(jev._read_rubric_file(os.path.join(inst, stem + ".yaml")), stem)
        assert r["criteria"]
    tr = jev.normalize_rubric(jev._read_rubric_file(os.path.join(inst, "tree_rules.yaml")), "tree_rules")
    bare = next(c for c in tr["criteria"] if c["id"] == "no_bare_headings")["_re"]
    assert bare.search("## Title\n\n### Sub\ntext")
    assert not bare.search("## Title\nA real sentence.\n### Sub\ntext")
    pre = next(c for c in tr["criteria"] if c["id"] == "no_meta_preamble")["_re"]
    assert pre.search("  Okay, here's a breakdown")
    assert not pre.search("## Title\nOkay later")


# ── validation ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("criteria,msg", [
    ([{"id": "a", "type": "noul", "instructions": "q"}, {"id": "a", "type": "noul", "instructions": "q"}], "duplicate"),
    ([{"id": "a", "type": "score", "instructions": "q", "levels": ["one"]}], "at least 2"),
    ([{"id": "a", "type": "choice", "instructions": "q", "levels": ["x", "y"]}], "mapping"),
    ([{"id": "a", "type": "choice", "instructions": "q", "levels": {"x": None}, "pass": ["z"]}], "not in levels"),
    ([{"id": "bad id", "type": "noul", "instructions": "q"}], "id"),
    ([{"id": "a", "type": "vote", "instructions": "q"}], "type"),
    ([{"id": "a", "type": "noul"}], "instructions"),
    ([{"id": "a", "check": "regex", "pattern": "("}], "bad regex"),
    ([{"id": "a", "check": "regex", "pattern": "x", "fail_if": "maybe"}], "fail_if"),
    ([{"id": "a", "check": "regex", "pattern": "x", "flags": "q"}], "flag"),
    ([{"id": "a", "check": "llm", "pattern": "x"}], "check"),
    ([{"id": "a", "type": "noul", "instructions": "q", "threshold": 2}], "threshold"),
    ([], "non-empty"),
])
def test_validation_errors(criteria, msg):
    with pytest.raises(jev.JevConfigError, match=msg):
        rubric(criteria)


def test_yaml_bool_keys_normalized():
    r = rubric([{"id": "a", "type": "noul", "instructions": "q",
                 "criteria": {True: "yes", False: "no"}, "pass_when": "false"}])
    c = r["criteria"][0]
    assert c["criteria"] == {"true": "yes", "false": "no"} and c["pass_when"] is False


# ── compile ───────────────────────────────────────────────────────────────────

def test_compile_questions_shapes():
    r = rubric([
        {"id": "re1", "check": "regex", "pattern": "x"},
        {"id": "n1", "type": "noul", "instructions": "Is it?"},
        {"id": "n2", "type": "noul", "instructions": "Is it?", "criteria": {"true": "T", "false": "F"}},
        {"id": "c1", "type": "choice", "instructions": "Which?", "levels": {"py": "Python", "js": None}},
        {"id": "s1", "type": "score", "instructions": "How?", "levels": ["bad", "ok", "good"]},
    ])
    qs = jev.compile_questions(r["criteria"])
    assert list(qs) == ["n1", "n2", "c1", "s1"]
    assert qs["n1"] == {"type": "noul", "instructions": "Is it?"}
    assert qs["n2"]["criteria"] == {"true": "T", "false": "F"}
    assert qs["c1"]["criteria"] == {"py": "Python", "js": None}
    assert qs["s1"]["criteria"] == ["bad", "ok", "good"]


# ── verdicts ──────────────────────────────────────────────────────────────────

def test_score_normalization(monkeypatch, settings):
    rec = Recorder()
    monkeypatch.setattr(jev.requests, "post", rec)
    r = rubric([{"id": "q", "type": "score", "instructions": "How?", "levels": ["bad", "ok", "good"],
                 "threshold": 0.5}])
    res = jev.run_rubric("x", r, settings)
    c = res["criteria"][0]
    assert c["value"] == pytest.approx(0.546, abs=1e-3)
    assert c["raw"]["score"] == 1.0917
    assert c["passed"] is True and res["passed"] is True
    payload = rec.calls[0]["json"]
    assert payload["model"] == "tev1:0.8b" and payload["keep_alive"] == "5m"
    assert rec.calls[0]["url"] == "http://localhost:11434/v1/systemone"
    assert rec.calls[0]["timeout"] == 30


def test_noul_pass_when_false(monkeypatch, settings):
    monkeypatch.setattr(jev.requests, "post", Recorder())
    r = rubric([{"id": "no_silent_except", "type": "noul", "instructions": "Silent except?",
                 "pass_when": False},
                {"id": "single", "type": "noul", "instructions": "One purpose?"}], name="CODE_REVIEW")
    res = jev.run_rubric("x", r, settings)
    by = {c["id"]: c for c in res["criteria"]}
    assert by["no_silent_except"]["passed"] is False      # p(true)=0.95 -> problem present
    assert by["single"]["passed"] is True
    assert res["passed"] is False
    assert jev.fail_lines(res) == [
        "FAIL: CODE_REVIEW.no_silent_except — p(true)=0.95, pass_when=false (threshold 0.5)"]


def test_choice_sum_of_pass_probabilities(monkeypatch, settings):
    monkeypatch.setattr(jev.requests, "post", Recorder())
    lv = {"py": None, "js": None, "go": None}
    r = rubric([
        {"id": "a", "type": "choice", "instructions": "?", "levels": lv, "pass": ["js", "go"], "threshold": 0.15},
        {"id": "b", "type": "choice", "instructions": "?", "levels": lv, "pass": ["js", "go"], "threshold": 0.2},
        {"id": "c", "type": "choice", "instructions": "?", "levels": lv},   # informational
    ])
    by = {c["id"]: c for c in jev.run_rubric("x", r, settings)["criteria"]}
    assert by["a"]["raw"]["p_pass"] == pytest.approx(0.1701)
    assert by["a"]["passed"] is True       # 0.1701 >= 0.15 although the chosen key is py
    assert by["b"]["passed"] is False
    assert by["c"]["passed"] is True and by["c"]["value"] == "py"


def test_regex_first_and_short_circuit(monkeypatch, settings):
    rec = Recorder()
    monkeypatch.setattr(jev.requests, "post", rec)
    r = rubric([
        {"id": "sem", "type": "noul", "instructions": "?"},
        {"id": "no_bare_headings", "check": "regex", "pattern": r"(?m)^#{1,6} .+\n\s*(?=^#{1,6} )"},
    ], name="tree_rules")
    state = "## A\n\n## B\n\n## C\ntext"
    res = jev.run_rubric(state, r, settings, short_circuit=True)
    assert rec.calls == []
    by = {c["id"]: c for c in res["criteria"]}
    assert by["sem"]["passed"] is None and by["sem"]["message"] == "skipped"
    assert by["no_bare_headings"]["value"] == 2
    assert res["passed"] is False
    assert jev.fail_lines(res) == ["FAIL: tree_rules.no_bare_headings — matched 2× (fail_if: match)"]
    # without short_circuit the model is still called
    res2 = jev.run_rubric(state, r, settings)
    assert len(rec.calls) == 1 and res2["criteria"][0]["passed"] is True


def test_regex_nomatch_and_flags():
    r = rubric([{"id": "has_title", "check": "regex", "pattern": "^title", "flags": "im", "fail_if": "nomatch"}])
    assert jev.run_rubric("x\nTITLE here", r, dict(jev.DEFAULTS))["passed"] is True
    res = jev.run_rubric("nothing", r, dict(jev.DEFAULTS))
    assert res["passed"] is False
    assert "no match (fail_if: nomatch)" in jev.fail_lines(res)[0]


def test_regex_only_rubric_needs_no_model():
    r = rubric([{"id": "a", "check": "regex", "pattern": "x"}])
    assert jev.run_rubric("abc", r, dict(jev.DEFAULTS))["passed"] is True


def test_batch_splitting(monkeypatch, settings):
    rec = Recorder()
    monkeypatch.setattr(jev.requests, "post", rec)
    settings["batch"] = 2
    r = rubric([{"id": f"q{i}", "type": "noul", "instructions": "?"} for i in range(5)])
    res = jev.run_rubric("x", r, settings)
    assert len(rec.calls) == 3
    assert [len(c["json"]["questions"]) for c in rec.calls] == [2, 2, 1]
    assert all(c["value"] == 0.9541 for c in res["criteria"])


def test_threshold_precedence(monkeypatch, settings):
    monkeypatch.setattr(jev.requests, "post", Recorder())
    settings["threshold"] = 0.99
    r = rubric([{"id": "a", "type": "noul", "instructions": "?"},
                {"id": "b", "type": "noul", "instructions": "?", "threshold": 0.9}], threshold=0.97)
    by = {c["id"]: c for c in jev.run_rubric("x", r, settings)["criteria"]}
    assert by["a"]["threshold"] == 0.97 and by["a"]["passed"] is False
    assert by["b"]["threshold"] == 0.9 and by["b"]["passed"] is True


# ── errors ────────────────────────────────────────────────────────────────────

def _mixed():
    return rubric([{"id": "r", "check": "regex", "pattern": "zzz"},
                   {"id": "s", "type": "noul", "instructions": "?"}], name="mix")


def test_on_error_fail(monkeypatch, settings):
    monkeypatch.setattr(jev.requests, "post", Recorder(exc=requests.exceptions.ConnectionError("refused")))
    res = jev.run_rubric("x", _mixed(), settings)
    assert "error" in res and res["passed"] is False
    assert res["criteria"][1]["passed"] is None
    lines = jev.fail_lines(res)
    assert len(lines) == 1 and lines[0].startswith("FAIL: mix — jev unavailable: cannot connect")


def test_on_error_pass(monkeypatch, settings):
    monkeypatch.setattr(jev.requests, "post", Recorder(exc=requests.exceptions.ConnectionError("refused")))
    settings["on_error"] = "pass"
    res = jev.run_rubric("x", _mixed(), settings)
    assert "error" in res and res["passed"] is True and jev.fail_lines(res) == []
    res2 = jev.run_rubric("zzz", _mixed(), settings)   # regex still decides
    assert res2["passed"] is False


def test_timeout_and_5xx_are_infra(monkeypatch, settings):
    monkeypatch.setattr(jev.requests, "post", Recorder(exc=requests.exceptions.ReadTimeout("slow")))
    with pytest.raises(jev.JevError) as ei:
        jev.call_systemone("x", {"a": {"type": "noul", "instructions": "?"}}, settings)
    assert not isinstance(ei.value, jev.JevConfigError)
    monkeypatch.setattr(jev.requests, "post", Recorder(status=500, body={"error": "boom"}))
    with pytest.raises(jev.JevError) as ei:
        jev.call_systemone("x", {"a": {"type": "noul", "instructions": "?"}}, settings)
    assert not isinstance(ei.value, jev.JevConfigError)


def test_400_is_config_error_even_with_on_error_pass(monkeypatch, settings):
    err = {"error": 'model "qwen3:1.7b" is not supported by System One; use a local Nimble or Tev GGUF model'}
    monkeypatch.setattr(jev.requests, "post", Recorder(status=400, body=err))
    settings["on_error"] = "pass"
    with pytest.raises(jev.JevConfigError, match="not supported by System One"):
        jev.run_rubric("x", _mixed(), settings)
    monkeypatch.setattr(jev.requests, "post",
                        Recorder(status=404, body={"error": 'model "nope" not found, try pulling it first'}))
    with pytest.raises(jev.JevConfigError, match="not found"):
        jev.run_rubric("x", _mixed(), settings)


def test_unset_model(settings):
    settings["model"] = ""
    with pytest.raises(jev.JevConfigError, match="no decision model set"):
        jev.run_rubric("x", _mixed(), settings)
    with pytest.raises(jev.JevConfigError, match="no decision model set"):
        jev.ask("x", "q?", None, settings)


def test_bad_question_ids(settings):
    with pytest.raises(jev.JevConfigError):
        jev.call_systemone("x", {"bad id": {"type": "noul", "instructions": "?"}}, settings)


# ── ask ───────────────────────────────────────────────────────────────────────

def test_ask_noul(monkeypatch, settings):
    rec = Recorder()
    monkeypatch.setattr(jev.requests, "post", rec)
    res = jev.ask("def add(a, b): return a + b", "Is this Python code?", None, settings)
    assert res["kind"] == "noul" and res["value"] == 0.9541 and res["passed"] is True
    assert rec.calls[0]["json"]["questions"] == {"q": {"type": "noul", "instructions": "Is this Python code?"}}
    settings["threshold"] = 0.96
    assert jev.ask("x", "q?", [], settings)["passed"] is False


def test_ask_choice(monkeypatch, settings):
    rec = Recorder()
    monkeypatch.setattr(jev.requests, "post", rec)
    res = jev.ask("x", "Яка це мова?", ["py", "js", "go", "py"], settings)
    assert res["kind"] == "choice" and res["value"] == "py" and res["passed"] is None
    assert res["probabilities"]["js"] == 0.1233 and res["confidence"] == 0.4938
    q = rec.calls[0]["json"]["questions"]["q"]
    assert q == {"type": "choice", "instructions": "Яка це мова?",
                 "criteria": {"py": None, "js": None, "go": None}}


# ── formatting / models ───────────────────────────────────────────────────────

def test_format_table(monkeypatch, settings):
    monkeypatch.setattr(jev.requests, "post", Recorder())
    r = rubric([{"id": "r", "check": "regex", "pattern": "x", "message": "has x"},
                {"id": "s", "type": "score", "instructions": "?", "levels": ["a", "b", "c"]}], name="t")
    lines = jev.format_table(jev.run_rubric("x", r, settings))
    assert lines[0] == "[jev] t: FAIL"
    assert any("FAIL" in ln and "matched 1" in ln for ln in lines)
    assert any("has x" in ln for ln in lines)
    assert any("PASS" in ln and "score=0.55" in ln for ln in lines)


def test_list_models_capabilities(monkeypatch):
    tags = {"models": [
        {"name": "tev1:0.8b", "capabilities": ["decision", "completion"], "details": {"parameter_size": "752M"}},
        {"name": "qwen3:1.7b", "capabilities": ["completion"]},
        {"name": "nimble:9b"},              # old server: no capabilities
        {"name": "llama3:8b"},
    ]}

    class R:
        status_code = 200
        content = json.dumps(tags).encode()

        def raise_for_status(self):
            pass

    monkeypatch.setattr(jev.requests, "get", lambda url, timeout=None: R())
    models = jev.list_models("http://h")
    assert [m["decision"] for m in models] == [True, False, None, None]
    assert [m["name"] for m in jev.decision_models(models)] == ["tev1:0.8b", "nimble:9b"]


# ── request size limit (Ollama rejects /v1/systemone bodies over 64 KiB) ─────

def test_oversized_state_is_a_config_error_before_posting(settings, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(requests, "post", rec)
    big = "Привіт " * 3000          # ~18k Cyrillic chars -> ~100 KiB as \uXXXX
    with pytest.raises(jev.JevConfigError, match="64 KiB"):
        jev.ask(big, "Is this a greeting?", None, settings)
    assert rec.calls == []


def test_http_413_is_a_config_error(settings, monkeypatch):
    rec = Recorder(status=413, body={"error": "request body must not exceed 64 KiB"})
    monkeypatch.setattr(requests, "post", rec)
    with pytest.raises(jev.JevConfigError, match="64 KiB"):
        jev.ask("short", "Is this short?", None, settings)
