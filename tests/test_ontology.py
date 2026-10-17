"""Tests for _bcoder_data/flows/ontology.py — parser, contradiction checker,
journal and subcommands. No LLM or Ollama: chat._stream_chat is faked."""
import builtins
import importlib.util
import json
import os

import pytest

_P = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_bcoder_data", "flows", "ontology.py")
_spec = importlib.util.spec_from_file_location("ontology_flow", _P)
onto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(onto)


def T(line, src=""):
    ts, err = onto.parse_triple(line, src)
    assert not err, err
    return ts


def kb(*lines):
    out = []
    for ln in lines:
        out += T(ln)
    return onto.KB(out)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return "p"


class FakeChat:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.messages = []
        self._last_output = ""

    def _stream_chat(self, msgs):
        return self.replies.pop(0) if self.replies else ""


def answers(monkeypatch, *seq):
    it = iter(seq)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parse_aliases_negation_and_normalization():
    t = T("TRIPLE: Жінка :: може :: Взяти декретну відпустку.")[0]
    assert (t.s, t.p, t.o) == ("жінка", "can", "взяти декретну відпустку")
    assert T("man :: can not :: x")[0].p == "cannot"
    assert T("bank :: not issues :: loans")[0].p == "not_issues"
    assert T("bank :: не видає :: кредити")[0].p == "not_видає"
    assert T("a :: is a :: b")[0].p == "is_a"


def test_parse_joins_split_verb_phrase():
    assert T("intern :: cannot :: sign :: the contract")[0].o == "sign the contract"


def test_ask_both_answers_only_for_the_same_object(proj, capsys):
    for ln in ("man :: cannot :: take maternity leave", "man :: can :: take paternity leave"):
        onto.cmd_add(None, f'"{ln}" --project {proj}')
    capsys.readouterr()
    onto.cmd_ask(FakeChat(), f'"man :: can :: ?" --project {proj}')
    assert "both answers" not in capsys.readouterr().out


def test_parse_rejects_bad_lines():
    for line in ("a :: b", "a :: is_a :: a", "a :: not can :: b", " :: can :: b"):
        ts, err = onto.parse_triple(line)
        assert err and not ts, line


def test_condition_becomes_subclass():
    g = T("user :: can :: take leave | woman")
    assert [x.text() for x in g] == ["woman :: is_a :: user", "woman :: can :: take leave"]
    assert g[0].auto
    g = T("user :: can :: take leave | if the user is a woman")
    assert g[-1].s == "woman"
    g = T("user :: can :: take leave | gender = female")
    assert g[0].text() == "user[gender=female] :: is_a :: user"


def test_llm_output_only_triple_lines():
    raw = "Sure, here you go:\nTRIPLE: a :: is_a :: b\nnoise :: x :: y\n- TRIPLE: c :: can :: d\nTRIPLE: bad"
    ts, errs = onto.parse_llm_output(raw, "file: x.md:1")
    assert [t.text() for t in ts] == ["a :: is_a :: b", "c :: can :: d"]
    assert len(errs) == 1 and ts[0].src == "file: x.md:1"


# ── the checker: scenarios from concepts/ONTOLOGY.md ─────────────────────────

BASE = ("user :: is_a :: person", "woman :: is_a :: user", "man :: is_a :: user",
        "woman :: disjoint_with :: man")


def test_maternity_example():
    k = kb(*BASE, "woman :: can :: take maternity leave", "man :: cannot :: take maternity leave")
    assert k.conflicts() == []                       # disjoint siblings: fine
    new = k.check(T("user :: can :: take maternity leave")[0])
    assert new and new[0].level == "red"
    texts = {t.text() for t in new[0].triples}
    assert "man :: is_a :: user" in texts and "man :: cannot :: take maternity leave" in texts


def test_direct_contradiction_same_subject():
    k = kb("woman :: does :: give birth")
    assert k.check(T("woman :: does_not :: give birth")[0])[0].level == "red"


def test_must_vs_cannot_conflicts_must_vs_can_not():
    k = kb("employee :: must :: sign the contract")
    assert k.check(T("employee :: cannot :: sign the contract")[0])[0].level == "red"
    assert k.check(T("employee :: can :: sign the contract")[0]) == []


def test_unknown_overlap_is_yellow_and_disjoint_settles_it():
    k = kb("resident :: can :: vote", "woman :: is_a :: person")
    c = k.check(T("woman :: cannot :: vote")[0])
    assert c and c[0].level == "yellow"
    k2 = kb("resident :: can :: vote", "woman :: disjoint_with :: resident")
    assert k2.check(T("woman :: cannot :: vote")[0]) == []


def test_shared_subclass_is_red():
    k = kb("resident :: can :: vote", "pensioner :: is_a :: resident", "pensioner :: is_a :: woman")
    assert k.check(T("woman :: cannot :: vote")[0])[0].level == "red"


def test_attribute_classes_are_disjoint():
    k = onto.KB(T("user :: can :: take leave | gender = female"))
    assert k.check(T("user :: cannot :: take leave | gender = male")[-1]) == []


def test_structural_conflicts():
    k = kb("a :: is_a :: b")
    assert k.check(T("b :: is_a :: a")[0])[0].kind == "cycle"
    k = kb(*BASE)
    c = k.check(T("anna :: instance_of :: woman")[0])
    assert c == []
    c = kb(*BASE, "anna :: instance_of :: woman").check(T("anna :: instance_of :: man")[0])
    assert c[0].kind == "disjoint"


def test_distinct_instances_never_overlap():
    k = kb("anna :: instance_of :: user", "anna :: can :: vote", "bob :: instance_of :: user")
    assert k.check(T("bob :: cannot :: vote")[0]) == []


def test_same_as_merges_entities():
    k = kb("employee :: same_as :: worker", "employee :: can :: rest")
    assert k.check(T("worker :: cannot :: rest")[0])[0].level == "red"


def test_ask_inherits_and_flags_both_answers():
    k = kb(*BASE, "woman :: can :: take maternity leave", "man :: cannot :: take maternity leave",
           "user :: must :: sign the contract")
    res = k.ask("man", "can", "?")
    got = {(t.p, t.o) for t, _ in res}
    assert ("cannot", "take maternity leave") in got and ("must", "sign the contract") in got
    inh = [path for t, path in res if t.o == "sign the contract"][0]
    assert inh[0].text() == "man :: is_a :: user"
    assert {t.o for t, _ in k.ask("woman", "is_a", "?")} == {"user", "person"}


def test_statement_reading():
    assert onto.statement(T("user[gender=female] :: can :: x")[0]) == "user whose gender is female can x"
    assert onto.statement(T("bank :: not issues :: loans")[0]) == "bank does not issues loans"


# ── journal ──────────────────────────────────────────────────────────────────

def _base(proj):
    for ln in BASE + ("man :: cannot :: take maternity leave",):
        onto.cmd_add(None, f'"{ln}" --project {proj}')


def test_refused_fact_goes_to_journal_with_sources(proj, capsys):
    _base(proj)
    onto.cmd_add(None, f'"user :: can :: take maternity leave" --project {proj}')
    rows = onto.load_jsonl(proj, onto.JOURNAL)
    assert len(rows) == 1 and rows[0]["status"] == "open" and rows[0]["level"] == "red"
    assert rows[0]["found_in"] == ["manual"]
    withs = {onto.Triple.from_list(x).text() for x in rows[0]["with"]}
    assert "man :: cannot :: take maternity leave" in withs
    md = open(onto._path(proj, "conflicts.md"), encoding="utf-8").read()
    assert "NOT RECORDED: `user :: can :: take maternity leave`" in md
    assert "contradicts recorded facts" in md
    # met again: same open row, no duplicate
    s = onto.Session(proj)
    s.offer(T("user :: can :: take maternity leave", "file: b.md:3"), quiet=True)
    rows = onto.load_jsonl(proj, onto.JOURNAL)
    assert len(rows) == 1 and rows[0]["found_in"] == ["file: b.md:3", "manual"]


def test_resolve_by_condition(proj, monkeypatch):
    _base(proj)
    onto.cmd_add(None, f'"user :: can :: take maternity leave" --project {proj}')
    answers(monkeypatch, "c", "woman")
    onto.cmd_conflicts(None, f"--project {proj}")
    row = onto.load_jsonl(proj, onto.JOURNAL)[0]
    assert row["status"] == "refined" and row["note"] == "woman"
    assert "woman :: can :: take maternity leave" in {t.text() for t in onto.load_triples(proj)}


def test_resolve_as_known_keeps_row_and_records_nothing(proj, monkeypatch):
    _base(proj)
    onto.cmd_add(None, f'"user :: can :: take maternity leave" --project {proj}')
    n = len(onto.load_triples(proj))
    answers(monkeypatch, "k", "policy.md and faq.md really disagree")
    onto.cmd_conflicts(None, f"--project {proj}")
    row = onto.load_jsonl(proj, onto.JOURNAL)[0]
    assert row["status"] == "known" and "really disagree" in row["note"]
    assert len(onto.load_triples(proj)) == n
    assert "— known" in open(onto._path(proj, "conflicts.md"), encoding="utf-8").read()


def test_resolve_reject_and_override(proj, monkeypatch):
    _base(proj)
    onto.cmd_add(None, f'"man :: can :: take maternity leave" --project {proj}')
    answers(monkeypatch, "o")
    onto.cmd_conflicts(None, f"--project {proj}")
    texts = {t.text() for t in onto.load_triples(proj)}
    assert "man :: can :: take maternity leave" in texts
    assert "man :: cannot :: take maternity leave" not in texts
    assert "man :: cannot :: take maternity leave" in onto.load_nogood(proj)
    onto.cmd_add(None, f'"man :: does_not :: x" --project {proj}')
    onto.cmd_add(None, f'"man :: does :: x" --project {proj}')
    answers(monkeypatch, "r")
    onto.cmd_conflicts(None, f"--project {proj}")
    assert onto.load_jsonl(proj, onto.JOURNAL)[-1]["status"] == "rejected"
    onto.cmd_add(None, f'"man :: does :: x" --project {proj}')       # nogood: silently refused
    assert len(onto.load_jsonl(proj, onto.JOURNAL)) == 2


def test_strict_holds_yellow_and_disjoint_resolves(proj, monkeypatch):
    onto.cmd_add(None, f'"resident :: can :: vote" --project {proj}')
    onto.cmd_add(None, f'"child :: cannot :: vote" --project {proj} --strict')
    row = onto.load_jsonl(proj, onto.JOURNAL)[0]
    assert row["level"] == "yellow" and row["status"] == "open"
    answers(monkeypatch, "d")
    onto.cmd_conflicts(None, f"--project {proj}")
    texts = {t.text() for t in onto.load_triples(proj)}
    assert {"child :: disjoint_with :: resident", "child :: cannot :: vote"} <= texts
    assert onto.load_jsonl(proj, onto.JOURNAL)[0]["status"] == "disjoint"


def test_quit_leaves_rows_open(proj, monkeypatch):
    _base(proj)
    onto.cmd_add(None, f'"user :: can :: take maternity leave" --project {proj}')
    answers(monkeypatch, "q")
    onto.cmd_conflicts(None, f"--project {proj}")
    assert onto.load_jsonl(proj, onto.JOURNAL)[0]["status"] == "open"


# ── index, files, export ─────────────────────────────────────────────────────

def test_index_records_and_journals(proj, tmp_path, capsys):
    doc = tmp_path / "docs" / "hr.md"
    doc.parent.mkdir()
    doc.write_text("Users can take leave.\n\nMen cannot take maternity leave.\n", encoding="utf-8")
    onto.cmd_add(None, f'"man :: is_a :: user" --project {proj}')
    chat = FakeChat(["TRIPLE: man :: cannot :: take maternity leave\n"
                     "TRIPLE: user :: can :: take maternity leave\n"
                     "TRIPLE: broken line"])
    onto.cmd_index(chat, f"docs --project {proj} --no-jev")
    out = capsys.readouterr().out
    assert "accepted 1, RED 1" in out and "invalid 1" in out
    row = onto.load_jsonl(proj, onto.JOURNAL)[0]
    assert row["found_in"][0].startswith("file: ") and "hr.md:1" in row["found_in"][0]


def test_lines_without_separators_repaired_only_when_unambiguous():
    ts, errs = onto.parse_llm_output(
        "TRIPLE: entity map is_a ontological knowledge graph\n"
        "TRIPLE: two-phase reflective agent instance_of cognitive agent\n"
        "TRIPLE: codeXport builds Word2Vec model trained on code identifiers\n"   # free verb
        "TRIPLE: a is_a b part_of c\n")                                            # two predicates
    assert [t.key for t in ts] == [("entity map", "is_a", "ontological knowledge graph"),
                                   ("two-phase reflective agent", "instance_of", "cognitive agent")]
    assert len(errs) == 2


def test_grounder_uses_chat_host_when_jev_host_empty(monkeypatch, tmp_path):
    """vyrii's adapter has no _jev_settings: empty jev host -> the chat's host."""
    import sys, types
    fake = types.SimpleNamespace(
        load_settings=lambda: {"model": "tev1:0.8b", "host": ""},
        resolve_host=lambda s, fb: "http://" + fb if fb else (_ for _ in ()).throw(ValueError("no host")),
        read_rubric=lambda name, wd: (None, name, "", {"criteria": []}))
    monkeypatch.setitem(sys.modules, "jev", fake)
    chat = types.SimpleNamespace(_base_url="localhost:11434")
    g = onto.Grounder.setup(chat)
    assert g is not None and g.settings["host"] == "http://localhost:11434"


def test_prompt_echo_is_dropped(proj, tmp_path, capsys):
    """lfm2.5:8b copied the format line; gemma copies the examples."""
    doc = tmp_path / "d.md"
    doc.write_text("Interns cannot sign contracts. A woman is a user here.\n", encoding="utf-8")
    chat = FakeChat(["TRIPLE: subject :: predicate :: object\n"
                     "TRIPLE: wheel :: part_of :: car\n"          # example, not in the text
                     "TRIPLE: woman :: is_a :: user\n"            # example, but the text says it
                     "TRIPLE: intern :: cannot :: sign contracts"])
    onto.cmd_index(chat, f'"{doc}" --project {proj} --no-jev')
    keys = {t.key for t in onto.load_triples(proj)}
    assert ("subject", "predicate", "object") not in keys and ("wheel", "part_of", "car") not in keys
    assert ("woman", "is_a", "user") in keys and ("intern", "cannot", "sign contracts") in keys
    assert "copied from the instruction" in capsys.readouterr().out


def test_thinking_block_and_doubts_are_not_triples():
    """gpt-oss drafts triples in its reasoning ('... ? actually ... so');
    vyrii's engine returns the reasoning wrapped in <think>."""
    raw = ("<think>maybe TRIPLE: 1b model :: is_a :: small language model? or turing machine. so\n"
           "TRIPLE: woman :: must :: be_woman? not present</think>\n"
           "TRIPLE: 1b model :: is_a :: turing machine\n"
           "TRIPLE: sqlite :: is_selected_by :: classic_fail? not sure\n"
           "<think>cut off TRIPLE: a :: is :: b")
    ts, errors = onto.parse_llm_output(raw)
    assert [t.key for t in ts] == [("1b model", "is_a", "turing machine")]
    assert len(errors) == 1 and "question" in errors[0][1]


def test_host_hooks_root_src_base_and_cancel(tmp_path, monkeypatch):
    """vyrii moves the storage, rebases source paths and stops via a flag."""
    root, docs = tmp_path / "store", tmp_path / "files" / "docs"
    docs.mkdir(parents=True)
    (docs / "a.md").write_text("Men cannot fly.\n\nSecond paragraph.\n", encoding="utf-8")
    monkeypatch.setattr(onto, "ROOT", str(root))
    monkeypatch.setattr(onto, "SRC_BASE", str(tmp_path / "files"))
    chat = FakeChat(["TRIPLE: man :: cannot :: fly"])
    calls = []
    chat._ontology_should_cancel = lambda: len(calls) > 0
    chat._stream_chat = lambda msgs: calls.append(1) or "TRIPLE: man :: cannot :: fly"
    onto.cmd_index(chat, f'"{docs}" --project hr --no-jev --chunk 4')
    assert len(calls) == 1                               # stopped before chunk 2
    assert (root / "hr" / "triples.txt").is_file()
    assert "[src: file: " + os.path.join("docs", "a.md") + ":1]" in (root / "hr" / "triples.txt").read_text(encoding="utf-8")
    assert onto.list_projects() == [{"project": "hr", "triples": 1, "open": 0}]


def test_triples_file_roundtrip_and_manual_edit(proj):
    onto.cmd_add(None, f'"жінка :: може :: взяти відпустку" --project {proj}')
    p = onto._path(proj, "triples.txt")
    with open(p, "a", encoding="utf-8") as f:
        f.write("чоловік :: is_a :: працівник\n# comment\n")
    texts = [t.text() for t in onto.load_triples(proj)]
    assert texts == ["жінка :: can :: взяти відпустку", "чоловік :: is_a :: працівник"]
    assert onto.load_triples(proj)[0].src == "manual"


def test_remove(proj):
    onto.cmd_add(None, f'"a :: can :: b" --project {proj}')
    onto.cmd_remove(None, f'"a :: can :: b" --project {proj}')
    assert onto.load_triples(proj) == []


def test_export_turtle_parses_if_rdflib(proj):
    onto.cmd_add(None, f'"жінка :: is_a :: користувач" --project {proj}')
    onto.cmd_add(None, f'"жінка :: може :: взяти відпустку" --project {proj}')
    onto.cmd_export(None, f"--project {proj}")
    ttl = open(onto._path(proj, "ontology.ttl"), encoding="utf-8").read()
    assert "rdfs:subClassOf" in ttl and '"жінка"' in ttl
    rdflib = pytest.importorskip("rdflib")
    g = rdflib.Graph()
    g.parse(data=ttl, format="turtle")
    assert len(g) == 5


def test_sparql_cyrillic_and_spaces(proj, capsys):
    pytest.importorskip("rdflib")
    for ln in ("жінка :: is_a :: користувач", "користувач :: може :: взяти відпустку",
               "чоловік :: cannot :: take maternity leave"):
        onto.cmd_add(None, f'"{ln}" --project {proj}')
    capsys.readouterr()
    onto.cmd_sparql(None, '"SELECT ?o WHERE { e:жінка rdfs:subClassOf* ?c . ?c p:can ?o }" '
                          f'--project {proj}')
    out = capsys.readouterr().out
    assert "взяти відпустку" in out and "1 row(s)" in out
    onto.cmd_sparql(None, f'"SELECT ?s WHERE {{ ?s p:cannot e:take_maternity_leave }}" --project {proj}')
    assert "чоловік" in capsys.readouterr().out


def test_chunks_keep_paragraphs_and_lines():
    text = "a\nb\n\nc\n\n" + "x" * 50
    ch = onto.chunks_of(text, 10)
    assert ch[0] == ("a\nb\n\nc", 1)
    assert ch[1][1] == 6


def test_grounder_batches_one_request(monkeypatch):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import jev
    raw = jev._read_rubric_file(os.path.join(os.path.dirname(_P), "..", "rubrics", "triple_grounded.yaml"))
    calls = []

    def fake(state, qs, settings):
        calls.append(qs)
        return {qid: {"noul": 0.9 if "can vote" in q["instructions"] else 0.1} for qid, q in qs.items()}
    monkeypatch.setattr(jev, "call_systemone", fake)
    g = onto.Grounder(jev, dict(jev.DEFAULTS, model="m", host="h"), raw)
    ok = g.judge("Residents can vote.", [T("resident :: can :: vote")[0], T("resident :: can :: fly")[0]])
    assert ok == [True, False] and len(calls) == 1 and len(calls[0]) == 2


def test_run_dispatch_and_help(proj, capsys):
    onto.run(None, "")
    assert "Usage:" in capsys.readouterr().out
    onto.run(None, "nonsense")
    assert "unknown subcommand" in capsys.readouterr().out
