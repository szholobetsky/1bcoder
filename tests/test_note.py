"""/note: titled notes in .1bcoder/note/<term>.md — note_store.py helpers and chat.py's
_cmd_note (no Ollama: the "last reply" is set by hand)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chat  # noqa: E402
import note_store as ns  # noqa: E402


# ── note_store ────────────────────────────────────────────────────────────────

def test_norm_keeps_any_script():
    assert ns.norm("BOOKLIFY") == "booklify"
    assert ns.norm("Order Service") == "order-service"
    assert ns.norm("Тип користувача") == "тип-користувача"


def test_add_render_parse_round_trip(tmp_path):
    d = str(tmp_path)
    assert ns.add(d, "Booklify", "def", "a flask app\n- with a list\n  indented", "[llm: m · 2026-10-16]")
    assert ns.add(d, "booklify", "fact", "uses sqlite", "[user · 2026-10-16]")
    assert not ns.add(d, "booklify", "fact", "uses sqlite", "[user · 2026-10-17]")      # same text: kept once
    ns.add_keywords(d, "booklify", ["books", " Books", "exchange"])
    note = ns.read(d, "BOOKLIFY")
    assert note["definitions"] == ["a flask app\n- with a list\n  indented [llm: m · 2026-10-16]"]
    assert note["facts"] == ["uses sqlite [user · 2026-10-16]"]
    assert note["keywords"] == ["books", "exchange"]
    text = (tmp_path / "booklify.md").read_text(encoding="utf-8")
    assert "\n- a flask app\n  - with a list\n    indented [llm: m · 2026-10-16]\n" in text


def test_search_ranks_and_shows_only_matching_facts(tmp_path):
    d = str(tmp_path)
    ns.add(d, "booklify", "def", "a flask app for book exchange", "[user · 2026-10-16]")
    ns.add(d, "booklify", "def", "a second definition", "[user · 2026-10-16]")
    ns.add(d, "booklify", "fact", "booklify uses sqlite as its database engine", "[user · 2026-10-16]")
    ns.add(d, "booklify", "fact", "booklify has 4 types of users", "[user · 2026-10-16]")
    ns.add(d, "sqlite", "def", "an embedded database", "[user · 2026-10-16]")
    ns.add(d, "ledger", "def", "accounts", "[user · 2026-10-16]")
    ns.add_keywords(d, "ledger", ["books"])

    hits = ns.search(d, "sqlite")
    assert [h["note"]["term"] for h in hits] == ["sqlite", "booklify"]   # the name first, a fact last
    out = ns.format_hits(hits)
    assert out.splitlines()[0] == "--[ 1 ]" + "-" * 18
    assert "booklify: a flask app for book exchange [user · 2026-10-16]" in out
    assert "          a second definition [user · 2026-10-16]" in out      # every definition, aligned
    assert "fact: booklify uses sqlite as its database engine" in out
    assert "4 types" not in out                                            # only the facts with the word

    assert [h["note"]["term"] for h in ns.search(d, "books")] == ["ledger"]  # keyword; "book exchange" != "books"
    assert "keywords: books" in ns.format_hits(ns.search(d, "books"))
    assert ns.search(d, "booklify users")[0]["facts"] == [
        "booklify uses sqlite as its database engine [user · 2026-10-16]",
        "booklify has 4 types of users [user · 2026-10-16]"]
    assert ns.search(d, '"book exchange"')[0]["note"]["term"] == "booklify"
    assert ns.search(d, "nothing-like-this") == []


def test_parse_numbers():
    assert ns.parse_numbers("2,5") == [2, 5]
    assert ns.parse_numbers("2 5") == [2, 5]
    assert ns.parse_numbers("2-4") == [2, 3, 4]
    with pytest.raises(ValueError):
        ns.parse_numbers("two")


# ── /note in chat.py ──────────────────────────────────────────────────────────

@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "NOTES_DIR", str(tmp_path / "note"))
    c = object.__new__(chat.CoderCLI)
    c.messages = []
    c.last_reply = ""
    c._last_output = ""
    c._in_agent = False
    c.model = "qwen3:4b"
    return c


def test_dollar_takes_the_last_reply_and_says_whose(cli, tmp_path, capsys):
    cli.last_reply = cli._last_output = "booklify uses sqlite ~ and @app.route stay as they are"
    cli._cmd_note("/note fact booklify $")
    cli._cmd_note('/note fact booklify "booklify has 4 types of users"')
    facts = ns.read(str(tmp_path / "note"), "booklify")["facts"]
    assert facts[0].startswith("booklify uses sqlite ~ and @app.route stay as they are [llm: qwen3:4b · ")
    assert facts[1].startswith("booklify has 4 types of users [user · ")
    cli._last_output = "a.py:3: x = 1"                                   # /find output, not a reply
    cli._cmd_note("/note fact booklify $")
    assert "[output · " in ns.read(str(tmp_path / "note"), "booklify")["facts"][2]


def test_note_bypasses_the_global_expansion():
    assert "/note" in chat.CoderCLI._NO_EXPAND_CMDS


def test_search_load_and_ctx_in_the_main_session(cli, capsys):
    cli._cmd_note("/note def booklify a flask app")
    cli._cmd_note("/note fact booklify uses sqlite")
    cli._cmd_note("/note def sqlite an embedded database")
    cli._cmd_note("/note search sqlite")
    out = capsys.readouterr().out
    assert "--[ 1 ]" in out and "--[ 2 ]" in out and "/note load N,M" in out
    assert cli.messages == []                                            # search alone: nothing in the context
    cli._cmd_note("/note load 2")
    assert cli.messages[-1]["content"].startswith("[notes]\nbooklify: a flask app")
    cli._cmd_note("/note search sqlite ctx")
    assert cli.messages[-1]["content"].startswith("[note search: sqlite]\n--[ 1 ]")
    cli._cmd_note("/note load 7")
    assert "the last search found 2" in capsys.readouterr().out


def test_no_match_says_how_many_notes_there_are(cli, capsys):
    cli._cmd_note("/note search sqlite")
    assert "there are no notes yet" in capsys.readouterr().out
    cli._cmd_note("/note def booklify a flask app")
    cli._cmd_note("/note def ledger accounts")
    capsys.readouterr()
    cli._cmd_note("/note search sqlite")
    assert "2 note(s) in total: booklify, ledger" in capsys.readouterr().out


def test_in_an_agent_the_text_goes_to_the_agent_not_the_main_context(cli, capsys):
    cli._cmd_note("/note def booklify a flask app")
    cli._cmd_note("/note fact booklify uses sqlite")
    cli._in_agent = True
    capsys.readouterr()
    cli._cmd_note("/note search sqlite ctx")
    cli._cmd_note("/note term booklify ctx")
    cli._cmd_note("/note load 1")
    cli._cmd_note("/note list")
    out = capsys.readouterr().out
    assert cli.messages == []                                            # the main context stays clean
    assert out.count("booklify: a flask app") == 3                       # search, term, load all printed
    assert "booklify  (1 def, 1 facts)" in out


def test_agents_are_offered_the_read_commands():
    for tools in (chat.DEFAULT_AGENT_TOOLS, chat.DEFAULT_AGENT_TOOLS_ADVANCED, chat.DEFAULT_AGENT_TOOLS_ASK):
        assert {"note list", "note term", "note search"} <= set(tools)
    help_list = chat.get_help_list(["note list", "note term", "note search"])
    assert "/note search <words>" in help_list and "/note def" not in help_list
