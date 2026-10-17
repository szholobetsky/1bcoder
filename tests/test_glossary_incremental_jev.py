"""/flow glossary index: incremental (_index.json — unchanged files skipped, a changed one forgotten
and read again, a vanished one pruned, a run stopped inside a file resumed without double counting)
and --jev (KIND: of new terms; unsupported definitions and facts dropped; facts the article already
has dropped). No Ollama: chat._stream_chat and jev.call_systemone are faked."""
import importlib.util
import os

_P = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_bcoder_data", "flows", "glossary.py")
_spec = importlib.util.spec_from_file_location("glossary_flow_incr", _P)
gl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gl)


class _Chat:
    """TERMS: the capitalised words of the chunk; a definition and two facts per term."""
    base_url = "http://localhost:11434"

    def __init__(self):
        self.calls = 0

    def _stream_chat(self, msgs):
        self.calls += 1
        system, text = msgs[0]["content"], msgs[1]["content"]
        if system.startswith("You extract glossary terms"):
            words = sorted({w.strip(".,") for w in text.split() if w[:1].isupper()})
            return "TERMS: " + ", ".join(words)
        if system.startswith("You write a concise"):
            return "DEFINITION: a thing of the text."
        return "- it is in the text\n- it is invented"


def _seen(project, term):
    return gl._read_term_file(project, term)["seen"]


def _write(path, text, mtime):
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_unchanged_skipped_changed_reread_vanished_pruned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "docs"
    d.mkdir()
    _write(d / "a.md", "Gate and Project.", 1_000_000)
    _write(d / "b.md", "Project only.", 1_000_000)
    chat = _Chat()
    gl._cmd_index(chat, "docs --project t --terms-only")
    assert chat.calls == 2 and len(_seen("t", "project")) == 2
    assert set(gl._load_manifest("t")["files"]) == {"docs/a.md", "docs/b.md"}

    gl._cmd_index(chat, "docs --project t --terms-only")             # nothing changed: no call at all
    assert chat.calls == 2 and len(_seen("t", "project")) == 2

    _write(d / "a.md", "Rule and Project.", 2_000_000)                # a.md changed: Gate is gone, Rule is new
    gl._cmd_index(chat, "docs --project t --terms-only")
    assert chat.calls == 3
    assert len(_seen("t", "project")) == 2                           # forgotten, then counted again — not 3
    assert "gate" not in gl._load_terms("t") and not os.path.exists(gl._term_path("t", "gate"))
    assert "rule" in gl._load_terms("t")

    (d / "b.md").unlink()                                            # b.md gone: --prune takes it out
    gl._cmd_index(chat, "docs --project t --terms-only --prune")
    assert [s for s in _seen("t", "project")] == ["file: docs/a.md:1"]
    assert "docs/b.md" not in gl._load_manifest("t")["files"]

    gl._cmd_index(chat, "docs --project t --terms-only --all")       # --all reads it again, still no doubles
    assert chat.calls == 4 and len(_seen("t", "project")) == 1


def test_a_run_stopped_inside_a_file_resumes_without_doubles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "docs"
    d.mkdir()
    _write(d / "a.md", "Gate and Project.", 1_000_000)
    chat = _Chat()
    gl._cmd_index(chat, "docs --project t --terms-only")
    os.remove(gl._manifest_path("t"))                                 # as if the run died before a.md was recorded
    gl._cmd_index(chat, "docs --project t --terms-only")
    assert len(_seen("t", "gate")) == 1 and len(_seen("t", "project")) == 1


class _Jev:
    """A fake jev module: every term is domain except 'Noise'; facts 'invented' are not in the text;
    'it is in the text' is already in an article that has it."""
    class JevError(Exception):
        pass

    class JevConfigError(JevError):
        pass

    def __init__(self):
        self.requests = []

    def load_settings(self):
        return {"model": "tev1:0.8b", "host": "", "threshold": 0.5}

    def resolve_host(self, settings, fallback):                       # as jev.resolve_host
        return settings.get("host") or fallback

    def call_systemone(self, state, questions, settings):
        self.requests.append(questions)
        out = {}
        for qid, q in questions.items():
            ins = q["instructions"]
            if q["type"] == "choice":
                k = "noise" if '"Noise"' in ins else "domain"
                out[qid] = {"choice": k, "probabilities": {k: 0.9}}
            elif ins.startswith("Does the article already state"):
                out[qid] = {"noul": 0.95 if "in the text" in ins and "in the text" in state else 0.05}
            else:
                out[qid] = {"noul": 0.1 if "invented" in ins else 0.9}
        return out


def test_jev_kinds_and_checks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _Jev()
    monkeypatch.setattr(gl, "_jev_module", lambda: fake)
    (tmp_path / "a.md").write_text("Gate and Noise.", encoding="utf-8")
    (tmp_path / "b.md").write_text("Gate again.", encoding="utf-8")
    chat = _Chat()
    gl._cmd_index(chat, f"{tmp_path / 'a.md'} --project t --jev")
    gate = gl._read_term_file("t", "gate")
    assert gl.term_kind(gate) == "domain" and gl.term_kind(gl._read_term_file("t", "noise")) == "noise"
    assert [gl._strip_source_tag(f) for f in gate["facts"]] == ["it is in the text"]     # the invented one dropped
    kinds = [q for q in fake.requests if any(v["type"] == "choice" for v in q.values())]
    assert len(kinds[0]) == 2                                         # both new terms judged in one call
    gl._cmd_index(chat, f"{tmp_path / 'b.md'} --project t --jev")
    gate = gl._read_term_file("t", "gate")
    assert len(gate["facts"]) == 1                                    # "it is in the text" again: a duplicate
    assert len(gate["kind"]) == 1                                     # judged once, not again in b.md
    assert gl._JEV_STATS["facts_duplicate"] == 1 and gl._JEV_STATS["facts_unsupported"] == 1


def test_jev_needs_a_decision_model(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    fake = _Jev()
    fake.load_settings = lambda: {"model": ""}
    monkeypatch.setattr(gl, "_jev_module", lambda: fake)
    (tmp_path / "a.md").write_text("Gate.", encoding="utf-8")
    chat = _Chat()
    gl._cmd_index(chat, f"{tmp_path / 'a.md'} --project t --jev")
    assert chat.calls == 0 and "no decision model set" in capsys.readouterr().out


def test_jev_model_for_one_run(tmp_path, monkeypatch):
    """--jev-model: this run's decision model, asked on the glossary model's host; jev.json untouched."""
    monkeypatch.chdir(tmp_path)
    fake = _Jev()
    fake.load_settings = lambda: {"model": "tev1:0.8b", "host": "http://elsewhere:11434", "threshold": 0.5}
    seen = []
    fake.call_systemone = lambda state, qs, settings: seen.append((settings["model"], settings["host"])) or {}
    monkeypatch.setattr(gl, "_jev_module", lambda: fake)
    (tmp_path / "a.md").write_text("Gate.", encoding="utf-8")
    chat = _Chat()
    chat.base_url = "http://panteon.local:11434"
    gl._cmd_index(chat, f"{tmp_path / 'a.md'} --project t --terms-only --jev-model tev1:4b")
    assert seen and set(seen) == {("tev1:4b", "http://panteon.local:11434")}   # the probe, then the kinds


def test_jev_text_too_long_is_asked_shorter_and_never_stops_the_run(tmp_path, monkeypatch):
    """tev1 reads at most ~2 050 tokens and answers a longer text with HTTP 400 (JevConfigError
    "... tokens ..."): asked again with a shorter text; if nothing fits, the term is kept unjudged."""
    monkeypatch.chdir(tmp_path)
    fake = _Jev()
    sizes = []

    def call(state, qs, settings):
        sizes.append(len(state))
        if "probe" not in qs and len(state) > 2000:
            raise fake.JevConfigError("prompt 0 has 2090 tokens; expected 1-2050 (input is never truncated)")
        return {k: {"choice": "domain", "probabilities": {"domain": 0.9}} for k in qs}
    fake.call_systemone = call
    monkeypatch.setattr(gl, "_jev_module", lambda: fake)
    (tmp_path / "a.md").write_text("Gate " + "x " * 2500, encoding="utf-8")
    chat = _Chat()
    gl._cmd_index(chat, f"{tmp_path / 'a.md'} --project t --terms-only --jev")
    assert sizes[1:] == [4000, 3000, 2000]                            # full chunk, then shorter until it fits
    assert gl.term_kind(gl._read_term_file("t", "gate")) == "domain"


def test_jev_model_missing_on_the_host_is_said_before_the_run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    fake = _Jev()

    def call(state, qs, settings):
        raise fake.JevConfigError("model 'tev1:9b' not found")
    fake.call_systemone = call
    monkeypatch.setattr(gl, "_jev_module", lambda: fake)
    (tmp_path / "a.md").write_text("Gate.", encoding="utf-8")
    chat = _Chat()
    gl._cmd_index(chat, f"{tmp_path / 'a.md'} --project t --terms-only --jev-model tev1:9b")
    assert chat.calls == 0 and "does not answer" in capsys.readouterr().out


def test_jev_questions_go_in_groups_and_halves(tmp_path, monkeypatch):
    """The text and all questions of a request are one prompt: many terms are asked in groups
    (_JEV_GROUP), a group still too long is halved — every term gets its KIND."""
    monkeypatch.chdir(tmp_path)
    fake = _Jev()
    groups = []

    def call(state, qs, settings):
        if "probe" in qs:
            return {}
        groups.append(len(qs))
        if len(qs) > 5:
            raise fake.JevConfigError("prompt 0 has 2260 tokens; expected 1-2050 (input is never truncated)")
        return {k: {"choice": "domain", "probabilities": {"domain": 0.9}} for k in qs}
    fake.call_systemone = call
    monkeypatch.setattr(gl, "_jev_module", lambda: fake)
    words = " ".join(f"Term{i}" for i in range(12))
    (tmp_path / "a.md").write_text(words, encoding="utf-8")
    chat = _Chat()
    gl._cmd_index(chat, f"{tmp_path / 'a.md'} --project t --terms-only --jev")
    assert all(gl.term_kind(gl._read_term_file("t", f"term{i}")) == "domain" for i in range(12))
    assert max(groups) <= gl._JEV_GROUP and groups[0] == gl._JEV_GROUP   # 8 tried, refused, halved to 4 + 4, then 4


def test_jev_host_apart_from_the_glossary_model(tmp_path, monkeypatch):
    """--jev-host: the decision model on another machine than the glossary model."""
    monkeypatch.chdir(tmp_path)
    fake = _Jev()
    seen = []
    fake.call_systemone = lambda state, qs, settings: seen.append((settings["model"], settings["host"])) or {}
    monkeypatch.setattr(gl, "_jev_module", lambda: fake)
    (tmp_path / "a.md").write_text("Gate.", encoding="utf-8")
    chat = _Chat()
    chat.base_url = "http://panteon.local:11434"
    gl._cmd_index(chat, f"{tmp_path / 'a.md'} --project t --terms-only --jev-model tev1:4b --jev-host localhost:11434")
    assert set(seen) == {("tev1:4b", "http://localhost:11434")}


def _slow_setup(tmp_path, monkeypatch, n_files, delay):
    import time
    monkeypatch.chdir(tmp_path)
    fake = _Jev()

    def call(state, qs, settings):
        if "probe" in qs:
            return {}
        time.sleep(delay)
        return {k: {"choice": "domain", "probabilities": {"domain": 0.9}} for k in qs}
    fake.call_systemone = call
    monkeypatch.setattr(gl, "_jev_module", lambda: fake)
    d = tmp_path / "docs"
    d.mkdir()
    for i in range(n_files):
        (d / f"f{i}.md").write_text(f"Term{i} and Shared.", encoding="utf-8")

    class Slow(_Chat):
        def _stream_chat(self, msgs):
            time.sleep(delay)
            return super()._stream_chat(msgs)
    return Slow()


def test_pipeline_on_two_hosts(tmp_path, monkeypatch, capsys):
    """jev on another host: KIND: is judged on a second thread while the next chunk's terms are
    listed — a chunk costs the longer call, not the sum; every term judged, every file recorded."""
    import time
    chat = _slow_setup(tmp_path, monkeypatch, 6, 0.15)
    chat.base_url = "http://localhost:11434"                         # the same host: sequential, for comparison
    t0 = time.time()
    gl._cmd_index(chat, "docs --project seq --terms-only --jev-model tev1:4b --jev-host localhost:11434")
    sequential = time.time() - t0
    chat.base_url = "http://panteon.local:11434"
    t0 = time.time()
    gl._cmd_index(chat, "docs --project t --terms-only --jev-model tev1:4b --jev-host localhost:11434")
    pipeline = time.time() - t0
    assert "two threads" in capsys.readouterr().out
    assert pipeline < 0.8 * sequential, (pipeline, sequential)      # ~0.55 when the two calls overlap
    assert all(gl.term_kind(gl._read_term_file("t", f"term{i}")) == "domain" for i in range(6))
    assert len(gl._read_term_file("t", "shared")["kind"]) == 1        # judged once, though in every chunk
    assert len(gl._load_manifest("t")["files"]) == 6


def test_ctrl_c_in_the_pipeline_does_not_wait_for_jev(tmp_path, monkeypatch, capsys):
    """Ctrl+C while the main thread waits on a full queue: the run ends at once — the chunks still
    queued are dropped unjudged, and their files stay unrecorded (read again next run)."""
    import time
    chat = _slow_setup(tmp_path, monkeypatch, 6, 0.0)
    fake = gl._jev_module()
    slow = fake.call_systemone
    fake.call_systemone = lambda state, qs, settings: (time.sleep(1.0) if "probe" not in qs else None) \
        or slow(state, qs, settings)
    real_put, n = gl._put, [0]

    def put(q, item):
        n[0] += 1
        if n[0] == 5:
            raise KeyboardInterrupt
        real_put(q, item)
    monkeypatch.setattr(gl, "_put", put)
    chat.base_url = "http://panteon.local:11434"
    t0 = time.time()
    gl._cmd_index(chat, "docs --project t --terms-only --jev-model tev1:4b --jev-host localhost:11434")
    assert time.time() - t0 < 2.0                                    # not 4+ queued jev calls of 1 s
    assert "interrupted" in capsys.readouterr().out
    assert len(gl._load_manifest("t")["files"]) < 6


def test_one_host_stays_sequential(tmp_path, monkeypatch, capsys):
    chat = _slow_setup(tmp_path, monkeypatch, 2, 0.0)
    chat.base_url = "http://localhost:11434"
    gl._cmd_index(chat, "docs --project t --terms-only --jev-model tev1:4b --jev-host localhost:11434")
    assert "two threads" not in capsys.readouterr().out
    assert gl.term_kind(gl._read_term_file("t", "term0")) == "domain"


def test_saving_a_term_keeps_the_kind_the_other_thread_wrote(tmp_path, monkeypatch):
    """A full run reads a term, waits for its definition and facts, then saves: the KIND: written
    meanwhile by the judging thread must survive."""
    monkeypatch.chdir(tmp_path)
    gl._write_term_file("t", "gate", {"definitions": [], "facts": [], "links": [], "synonyms": [],
                                      "seen": ["file: a.md:1"], "history": []})
    gl._add_term("t", "gate")
    stale = gl._read_term_file("t", "gate")                          # the main thread's copy
    fresh = gl._read_term_file("t", "gate")
    fresh["kind"] = ["domain 0.90 [file: a.md:1]"]
    gl._write_term_file("t", "gate", fresh)                         # the judging thread
    stale["seen"].append("file: b.md:1")
    gl._save_term("t", "gate", stale, is_new=False)
    after = gl._read_term_file("t", "gate")
    assert after["kind"] == ["domain 0.90 [file: a.md:1]"] and len(after["seen"]) == 2


def test_collect_skips_dot_folders_and_caches(tmp_path):
    """Source = the project folder itself: its own .1bcoder/glossary output, .git, node_modules
    are never read; the source folder named explicitly is read even when it starts with a dot."""
    (tmp_path / "a.md").write_text("Alpha", encoding="utf-8")
    for d in (".1bcoder/glossary/x", ".git", "node_modules/p", "sub"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / ".1bcoder" / "glossary" / "x" / "alpha.md").write_text("Beta", encoding="utf-8")
    (tmp_path / ".git" / "notes.md").write_text("Gamma", encoding="utf-8")
    (tmp_path / "node_modules" / "p" / "readme.md").write_text("Delta", encoding="utf-8")
    (tmp_path / "sub" / "b.md").write_text("Epsilon", encoding="utf-8")
    got = sorted(os.path.relpath(fp, tmp_path) for fp, *_ in gl._collect(str(tmp_path), False))
    assert got == ["a.md", os.path.join("sub", "b.md")]
    hidden = tmp_path / ".1bcoder" / "glossary" / "x"
    assert [os.path.basename(fp) for fp, *_ in gl._collect(str(hidden), False)] == ["alpha.md"]


def test_a_term_repeated_in_one_terms_line_is_taken_once(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "docs"
    d.mkdir()
    _write(d / "a.md", "search.py and ragmcp.", 1_000_000)

    class _Repeats(_Chat):
        def _stream_chat(self, msgs):
            self.calls += 1
            return "TERMS: search.py, ragmcp, search.py, Search.py"

    gl._cmd_index(_Repeats(), "docs --project t --terms-only")
    assert len(_seen("t", "search-py")) == 1
    assert "term: 2/2  ragmcp" in capsys.readouterr().out


def test_contents_md_and_html_link_terms_and_sources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "docs"
    d.mkdir()
    _write(d / "a.md", "Gate and Project.", 1_000_000)
    gl._cmd_index(_Chat(), "docs --project t")
    contents = (tmp_path / ".1bcoder" / "glossary" / "t" / "_contents.md").read_text(encoding="utf-8")
    assert "- [gate](gate.md) — a thing of the text." in contents      # the source tag is cut off
    assert "_contents" not in gl._load_terms("t")

    gl._cmd_html(None, "--project t")
    page = (tmp_path / ".1bcoder" / "glossary" / "t" / "gate.html").read_text(encoding="utf-8")
    assert '<a class="src" href="../../../docs/a.md">docs/a.md:1</a>' in page   # definition source and SEEN
    assert "<h2>Seen in</h2>" in page
    assert "a thing of the text." in (tmp_path / ".1bcoder" / "glossary" / "t" / "glossary.html").read_text(encoding="utf-8")
