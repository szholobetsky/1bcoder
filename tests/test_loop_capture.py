"""Tests for the chat.py side of /loop: cmd: capture, _note_offered, the /visual
confirm fix, and _cmd_loop end-to-end with a fake model (no Ollama)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chat  # noqa: E402
import loop_stream as ls  # noqa: E402


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "WORKDIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    c = object.__new__(chat.CoderCLI)
    c.messages = []
    c.last_reply = ""
    c._last_output = ""
    c._auto_apply = False
    c._in_agent = False
    c._aliases = {}
    c._vars = {}
    c._stats = {"cmds": {}}
    c.cmd_history = []
    c.num_ctx = 8192
    c._role = ""
    c._proc_active = []
    c._proc_gates = []
    return c


def write(path, text):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ── cmd: capture ──────────────────────────────────────────────────────────────

def test_capture_find_returns_offered_text_and_restores_state(cli, tmp_path):
    write(tmp_path / "a.py", "x = 1\nneedle_one = 2\n")
    write(tmp_path / "sub" / "b.py", "# needle_two тут\n")
    cli.messages.append({"role": "user", "content": "before"})
    orig_confirm = cli._confirm
    orig_stdin = sys.stdin
    out = cli._loop_capture_cmd("/find needle -c")
    assert "needle_one = 2" in out and "needle_two тут" in out
    assert "\x1b[" not in out
    assert cli.messages == [{"role": "user", "content": "before"}]
    assert cli._confirm == orig_confirm
    assert cli._auto_apply is False
    assert sys.stdin is orig_stdin


@pytest.mark.parametrize("cmd", [
    "/edit a.py 1", "/edit_code a.py", "/save x.md", "/loop last", "/agent do it",
    "/ctx clear", "/script apply x", "/patch a.py", "/clear", "/exit", "find x",
])
def test_capture_blocked(cli, cmd):
    with pytest.raises(ls.LoopError):
        cli._loop_capture_cmd(cmd)


def test_capture_blocked_via_alias(cli):
    cli._aliases = {"/ed": "/edit"}
    with pytest.raises(ls.LoopError):
        cli._loop_capture_cmd("/ed a.py")


def test_capture_interactive_command_refused(cli, monkeypatch):
    def needs_input(self, user_input):
        chat._input("pick one: ")
    monkeypatch.setattr(chat.CoderCLI, "_cmd_tree", needs_input)
    with pytest.raises(ls.LoopError, match="interactive"):
        cli._loop_capture_cmd("/tree")


def test_capture_falls_back_to_printed_output(cli, monkeypatch):
    def prints(self, user_input):
        print(f"{chat._GREEN}plain output{chat._R}")
        self.messages.append({"role": "user", "content": "leak"})
    monkeypatch.setattr(chat.CoderCLI, "_cmd_tree", prints)
    assert cli._loop_capture_cmd("/tree") == "plain output"
    assert cli.messages == []


# ── _note_offered / _confirm ──────────────────────────────────────────────────

def test_confirm_notes_offered_text_even_on_no(cli, monkeypatch):
    monkeypatch.setattr(chat, "_input", lambda prompt="": "n")
    assert cli._confirm("add? [Y/n]", ctx_add="знайдено") is False
    assert cli._last_output == "знайдено"
    cli._confirm("add? [Y/n]")                  # no ctx_add -> $ unchanged
    assert cli._last_output == "знайдено"


# ── /visual confirm fix ───────────────────────────────────────────────────────

@pytest.mark.parametrize("answer, appended", [(True, 1), (False, 0)])
def test_visual_injects_only_on_yes(cli, tmp_path, monkeypatch, answer, appended):
    img = tmp_path / "s.png"
    img.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(chat.CoderCLI, "_visual_run", lambda self, b64, prompt: "опис")
    monkeypatch.setattr(chat.CoderCLI, "_visual_load_cfg", lambda self: {"model": "m"})
    cli._confirm = lambda prompt, ctx_add="", **kw: answer
    cli._cmd_visual(f"/visual explain {img}")
    assert len(cli.messages) == appended
    assert cli._last_output == "опис"


# ── _cmd_loop end-to-end (fake model) ─────────────────────────────────────────

def test_loop_llm_out_resume_sig_and_fresh(cli, tmp_path, monkeypatch, capsys):
    write(tmp_path / "d" / "a.md", "перший файл")
    write(tmp_path / "d" / "b.md", "другий файл")
    write(tmp_path / "d" / "c.txt", "не md")
    calls = []

    def fake_stream(self, msgs):
        calls.append(msgs)
        return "summary of " + msgs[-1]["content"].split("---\n", 1)[1]
    monkeypatch.setattr(chat.CoderCLI, "_stream_chat", fake_stream)

    cmd = '/loop dir:d file:*.md --llm "summarize" --out ans/r.md -y'
    cli._cmd_loop(cmd)
    assert len(calls) == 2
    assert calls[0][-1]["content"].startswith("summarize\n[element 1 of 2 — d/a.md]\n---\nперший файл")
    assert calls[0][0]["role"] == "user"        # no role set, no history (--ctx 0)
    md = (tmp_path / "ans" / "r.md").read_text(encoding="utf-8")
    assert "## d/a.md" in md and "summary of другий файл" in md
    recs = ls.read_log(str(tmp_path / "ans" / "r.md.jsonl"))
    assert [r["id"] for r in recs] == ["d/a.md", "d/b.md"]
    assert cli._last_output == md
    assert len(cli.messages) == 1 and "summary of перший файл" in cli.messages[0]["content"]

    cli._cmd_loop(cmd)                           # resume: nothing to do
    assert len(calls) == 2
    assert "resuming: 2 already" in capsys.readouterr().out

    cli._cmd_loop('/loop dir:d file:*.md --llm "other prompt" --out ans/r.md -y')
    assert len(calls) == 2
    assert "--fresh" in capsys.readouterr().out

    cli._cmd_loop('/loop dir:d file:*.md --llm "other prompt" --out ans/r.md --fresh -y')
    assert len(calls) == 4
    recs = ls.read_log(str(tmp_path / "ans" / "r.md.jsonl"))
    assert len(recs) == 2 and len({r["sig"] for r in recs}) == 1


def test_loop_ctrl_c_stops_without_logging(cli, tmp_path, monkeypatch):
    cli._last_output = "## 1. A\nа\n## 2. B\nб\n## 3. C\nв"
    n = {"i": 0}

    def fake_stream(self, msgs):
        n["i"] += 1
        return None if n["i"] == 2 else "ok"      # Ctrl+C on the 2nd element
    monkeypatch.setattr(chat.CoderCLI, "_stream_chat", fake_stream)
    cli._cmd_loop('/loop last split:"## " --llm "x" --out o.txt -y')
    recs = ls.read_log(str(tmp_path / "o.txt.jsonl"))
    assert len(recs) == 1 and recs[0]["id"].endswith("#1") and recs[0]["id"].startswith("last@")


def test_loop_jev_question_filter_keep_fail(cli, tmp_path, monkeypatch):
    jev = chat._jev()
    cli._last_output = "line one\nline two question?\nline three"
    monkeypatch.setattr(chat.CoderCLI, "_jev_settings",
                        lambda self: dict(jev.DEFAULTS, model="tev", host="http://x"))

    def fake_ask(state, question, options, settings):
        v = 0.9 if "?" in state else 0.1
        return {"question": question, "kind": "noul", "value": v, "passed": v >= 0.5}
    monkeypatch.setattr(jev, "ask", fake_ask)
    cli._cmd_loop('/loop last split:line --jev "Is it a question?" --keep fail --out q.csv -y')
    recs = ls.read_log(str(tmp_path / "q.csv.jsonl"))
    assert [r["kept"] for r in recs] == [True, False, True]
    text = (tmp_path / "q.csv").read_text(encoding="utf-8-sig")
    assert "line one" in text and "line two" not in text and "p_true" in text


def test_loop_jev_infra_error_recorded_and_retried(cli, tmp_path, monkeypatch):
    jev = chat._jev()
    cli._last_output = "a\nb"
    monkeypatch.setattr(chat.CoderCLI, "_jev_settings",
                        lambda self: dict(jev.DEFAULTS, model="tev", host="http://x"))

    def down(*a, **k):
        raise jev.JevError("connection refused")
    monkeypatch.setattr(jev, "ask", down)
    cli._cmd_loop('/loop last split:line --jev "q?" --out e.md -y')
    recs = ls.read_log(str(tmp_path / "e.md.jsonl"))
    assert len(recs) == 2 and all("connection refused" in r["error"] for r in recs)
    assert ls.read_done_ids(str(tmp_path / "e.md.jsonl")) == set()


def test_loop_no_model_aborts(cli, monkeypatch, capsys):
    jev = chat._jev()
    cli._last_output = "a"
    monkeypatch.setattr(chat.CoderCLI, "_jev_settings", lambda self: dict(jev.DEFAULTS, host="http://x"))
    cli._cmd_loop('/loop last --jev "q?" -y')
    assert "no decision model" in capsys.readouterr().out


def test_loop_parse_error_is_reported(cli, capsys):
    cli._cmd_loop("/loop last dir:x")
    assert "exactly one source" in capsys.readouterr().out


def test_loop_routes_without_expansion(cli, monkeypatch):
    seen = []
    monkeypatch.setattr(chat.CoderCLI, "_cmd_loop", lambda self, s: seen.append(s))
    cli._last_output = "SHOULD-NOT-APPEAR"
    cli._route('/loop last --llm "cost in $ and ~"', auto=True)
    assert seen == ['/loop last --llm "cost in $ and ~"']
