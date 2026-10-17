"""/flow glossary index --terms-only: one LLM call per chunk (the TERMS: list), no definitions,
no facts, no think-step for code — and every mention in the term's SEEN: list. No Ollama:
chat._stream_chat is faked."""
import importlib.util
import os

_P = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_bcoder_data", "flows", "glossary.py")
_spec = importlib.util.spec_from_file_location("glossary_flow", _P)
gl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gl)


class _Chat:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def _stream_chat(self, msgs):
        self.calls.append(msgs[0]["content"][:30])
        return self.answer


def test_terms_only_is_one_call_per_chunk_and_counts_mentions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("A quality gate checks a project.", encoding="utf-8")
    (docs / "b.md").write_text("Every project has one quality gate.", encoding="utf-8")
    (docs / "c.py").write_text("class QualityGate:\n    pass\n", encoding="utf-8")
    chat = _Chat("TERMS: quality gate, project")
    gl._cmd_index(chat, f"{docs} --project t --terms-only")
    assert len(chat.calls) == 3                                   # 3 chunks, no think / definition / facts
    assert all(c.startswith("You extract glossary terms") for c in chat.calls)
    gate = gl._read_term_file("t", "quality gate")
    assert gate["definitions"] == [] and gate["facts"] == []
    assert len(gate["seen"]) == 3 and any("a.md" in s for s in gate["seen"])
    assert sorted(gl._load_terms("t")) == ["project", "quality-gate"]
    text = open(gl._term_path("t", "quality gate"), encoding="utf-8").read()
    assert "\nSEEN:\n" in text and text.index("SEEN:") < text.index("HISTORY:")


def test_a_full_run_keeps_definitions_and_also_counts_mentions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.md").write_text("A quality gate checks a project.", encoding="utf-8")

    class Full(_Chat):
        def _stream_chat(self, msgs):
            system = msgs[0]["content"]
            self.calls.append(system[:30])
            if system.startswith("You extract glossary terms"):
                return "TERMS: quality gate"
            if system.startswith("You write a concise"):
                return "DEFINITION: a set of conditions a project must meet."
            return "- it checks a project"
    chat = Full("")
    gl._cmd_index(chat, f"{tmp_path / 'a.md'} --project t")
    gate = gl._read_term_file("t", "quality gate")
    assert len(chat.calls) == 3 and gate["definitions"] and gate["facts"] and len(gate["seen"]) == 1
    # an older term file without SEEN: reads as before and keeps its shape on a rewrite
    gl._write_term_file("t", "old term", {"definitions": ["d"], "facts": ["f"], "links": [], "synonyms": [], "history": []})
    assert "SEEN:" not in open(gl._term_path("t", "old term"), encoding="utf-8").read()
    assert gl._read_term_file("t", "old term")["seen"] == []
