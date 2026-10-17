"""Multi-line input (" \\" goes on, " \\t" opens the editor, <<< … >>> blocks),
/script help, and /command: a named script that puts back every setting it changed."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chat  # noqa: E402

BS = "\\"


# ── line endings ──────────────────────────────────────────────────────────────

def test_continues():
    assert chat._continues("some text " + BS) == ("some text", "cont")
    assert chat._continues(BS) == ("", "cont")
    assert chat._continues("/agent ask why " + BS + "t") == ("/agent ask why", "edit")
    assert chat._continues("a literal " + BS + BS) == ("a literal " + BS, None)
    assert chat._continues("split on " + BS + BS + "t") == ("split on " + BS + "t", None)
    assert chat._continues("/cd C:" + BS + "Projects" + BS) == ("/cd C:" + BS + "Projects" + BS, None)
    assert chat._continues("plain") == ("plain", None)


def test_script_step_joins_continued_lines():
    lines = ["/var set p = first " + BS + "\n", "      second " + BS + "\n", "  third\n", "/ls\n"]
    text, nxt, literal, var = chat._script_step(lines, 0)
    assert text == "/var set p = first\nsecond\nthird"
    assert (nxt, literal, var) == (3, False, None)
    assert chat._script_step(lines, 3)[:2] == ("/ls", 4)


def test_script_step_block_is_literal_and_captures():
    lines = ["<<<\n", "  indented $HOME ~ @app\n", ":NOT_A_LABEL\n", "# a heading, not a comment\n",
             ">>> -> answer\n", "/var set p =<<<\n", "x\n", ">>>\n"]
    text, nxt, literal, var = chat._script_step(lines, 0)
    assert text == "  indented $HOME ~ @app\n:NOT_A_LABEL\n# a heading, not a comment"
    assert (nxt, literal, var) == (5, True, "answer")
    assert chat._script_step(lines, 5)[:4] == ("/var set p =\nx", 8, True, None)
    assert "NOT_A_LABEL" not in chat._build_label_map(lines)


def test_unclosed_block_is_found_before_running():
    assert chat._unclosed_block(["/ls\n", "<<<\n", "text\n"]) == 2
    assert chat._unclosed_block(["<<<\n", ">>>\n"]) is None
    with pytest.raises(ValueError):
        chat._script_step(["<<<\n", "text\n"], 0)


def test_fix_command_keeps_line_breaks(monkeypatch):
    fixed = chat.fix_command("/var sett p = one\n  two", auto=True)
    assert fixed == "/var set p = one\n  two"


# ── scripts ───────────────────────────────────────────────────────────────────

@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name, sub in (("SCRIPTS_DIR", "scripts"), ("COMMANDS_DIR", "command")):
        monkeypatch.setattr(chat, name, str(tmp_path / ".1bcoder" / sub))
    monkeypatch.setattr(chat, "GLOBAL_SCRIPTS_DIR", str(tmp_path / "home" / "scripts"))
    monkeypatch.setattr(chat, "GLOBAL_COMMANDS_DIR", str(tmp_path / "home" / "command"))
    c = object.__new__(chat.CoderCLI)
    c.messages, c.last_reply, c._last_output = [], "", ""
    c._auto_apply = c._in_agent = c._capturing = False
    c._aliases, c._vars, c._hooks, c.params = {}, {}, {}, {}
    c._stats = {"cmds": {}, "run_calls": 0, "files_read": 0}
    c.cmd_history = []
    c.num_ctx, c.timeout = 8192, 120
    c.model, c.base_url, c.provider = "m", "http://h", "ollama"
    c._role = "You are a developer."
    c._proc_active, c._proc_before, c._proc_gates, c._proc_gates_post = [], [], [], []
    c._script_file = None
    c.think_in_ctx, c.think_show = False, True
    sent = []
    c._sent = sent

    real_route = chat.CoderCLI._route

    def route(text, auto=False, literal=False, capture=None):
        if text.startswith("/"):
            return real_route(c, text, auto=auto, literal=literal, capture=capture)
        sent.append(text)                       # a prompt to the model
        c.last_reply = c._last_output = f"reply to: {text}"
        if capture:
            c._vars[capture] = c.last_reply
    c._route = route
    return c


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_script_runs_blocks_and_continued_lines(cli, tmp_path):
    cli._last_output = "LAST"
    p = _write(str(tmp_path / ".1bcoder" / "scripts" / "s.txt"),
               "/var set rules =<<<\nKeep $ as it is.\n  Indented.\n>>>\n"
               "<<<\n{{rules}}\nList the tasks.\n>>> -> tasks\n"
               "one long " + BS + "\n   question $\n")
    cli._cmd_script(f"/script run {p.replace(os.sep, '/')}")
    assert cli._sent[0] == "Keep $ as it is.\n  Indented.\nList the tasks."   # literal: no $ expansion
    assert cli._vars["tasks"].startswith("reply to: Keep $")
    assert cli._sent[1] == "one long\nquestion $"            # " \" joined (the $ expands in _route)
    assert "#POS" not in open(p, encoding="utf-8").read()


def test_unclosed_block_runs_nothing(cli, tmp_path, capsys):
    p = _write(str(tmp_path / ".1bcoder" / "scripts" / "bad.txt"), "/var set a =1\n<<<\nno end\n")
    cli._cmd_script(f"/script run {p.replace(os.sep, '/')}")
    assert "never closed" in capsys.readouterr().out
    assert cli._vars == {} and cli._sent == []


def test_script_help_and_list_show_the_header(cli, tmp_path, capsys):
    _write(str(tmp_path / ".1bcoder" / "scripts" / "fix.txt"),
           "# fix — repairs a thing\n# Usage:\n#   /script run fix\n/ls\n")
    cli._cmd_script("/script help 1")
    out = capsys.readouterr().out
    assert "fix — repairs a thing\nUsage:\n  /script run fix" in out
    cli._cmd_script("/script help fix")
    assert "repairs a thing" in capsys.readouterr().out
    cli._cmd_script("/script list")
    assert "fix.txt" in (out := capsys.readouterr().out) and "repairs a thing" in out


# ── /command ──────────────────────────────────────────────────────────────────

def test_command_restores_every_setting_and_keeps_the_reply(cli, tmp_path, capsys):
    _write(str(tmp_path / "home" / "command" / "sum.txt"),
           "# sum — sums a text up\n/param temperature 0.1\n/role You are terse.\n"
           "/var set scratch =x\n<<<\nSum up {{args}} ({{1}}).\n>>>\n")
    cli._vars["keep"] = "me"
    cli._cmd_command("/command list")
    assert "sum" in capsys.readouterr().out
    cli._cmd_command("/command sum notes.md please")
    assert cli._sent == ["Sum up notes.md please (notes.md)."]
    assert cli.params == {} and cli._role == "You are a developer."
    assert cli._vars == {"keep": "me"}                       # args, 1, 2, scratch are gone
    assert cli.last_reply.startswith("reply to: Sum up")     # the result stays
    assert cli._in_command is False and cli._script_file is None


def test_project_command_wins_and_help(cli, tmp_path, capsys):
    _write(str(tmp_path / "home" / "command" / "c.txt"), "# c — global\n/ls\n")
    _write(str(tmp_path / ".1bcoder" / "command" / "c.txt"), "# c — project\n/ls\n")
    cli._cmd_command("/command help c")
    assert "c — project" in capsys.readouterr().out


def test_a_command_inside_a_command_is_refused(cli, tmp_path, capsys):
    _write(str(tmp_path / "home" / "command" / "inner.txt"), "<<<\ninner\n>>>\n")
    _write(str(tmp_path / "home" / "command" / "outer.txt"), "/command inner\n<<<\nouter\n>>>\n")
    _write(str(tmp_path / ".1bcoder" / "scripts" / "s.txt"), "/command inner\n")
    cli._cmd_command("/command outer")
    assert "cannot run another command" in capsys.readouterr().out
    assert cli._sent == ["outer"]
    cli._cmd_script("/script run s.txt")                    # a script may run a command
    assert cli._sent == ["outer", "inner"]


def test_command_restores_settings_after_an_error(cli, tmp_path):
    _write(str(tmp_path / "home" / "command" / "boom.txt"), "/param temperature 0.1\n<<<\nx\n>>>\n")

    def boom(text, auto=False, literal=False, capture=None):
        if text.startswith("/"):
            return chat.CoderCLI._route(cli, text, auto=auto)
        raise RuntimeError("model down")
    cli._route = boom
    with pytest.raises(RuntimeError):
        cli._cmd_command("/command boom")
    assert cli.params == {} and cli._in_command is False


def test_default_commands_have_help_and_closed_blocks():
    d = os.path.join(os.path.dirname(chat.__file__), "_bcoder_data", "command")
    names = sorted(f for f in os.listdir(d) if f.endswith(".txt"))
    assert {"summary.txt", "action-items.txt", "commit-msg.txt"} <= set(names)
    for f in names:
        path = os.path.join(d, f)
        head = chat._script_header(path)
        assert head and head[0].startswith(f[:-4] + " — "), f
        assert chat._unclosed_block(chat._load_script(path)) is None, f


# ── the prompt ────────────────────────────────────────────────────────────────

def test_prompt_goes_on_and_moves_into_the_editor(cli, monkeypatch):
    typed = iter(["   second line " + BS, "third " + BS + "t"])
    monkeypatch.setattr(chat, "_input", lambda prompt="", none_on_eof=False: next(typed))
    opened = []

    def editor(default=""):
        opened.append(default)
        return default + "\nedited in the editor", None
    monkeypatch.setattr(chat, "_multiline_input", editor)
    assert cli._read_more("plain line") == "plain line"
    out = cli._read_more("/agent ask first " + BS)
    assert opened == ["/agent ask first\nsecond line\nthird"]
    assert out == "/agent ask first\nsecond line\nthird\nedited in the editor"


def test_text_and_note_are_separate_methods():
    import inspect
    assert list(inspect.signature(chat.CoderCLI._cmd_text).parameters) == ["self"]
    assert list(inspect.signature(chat.CoderCLI._cmd_note).parameters) == ["self", "user_input"]


def test_command_without_its_argument_shows_help_and_runs_nothing(cli, tmp_path, capsys, monkeypatch):
    _write(str(tmp_path / "home" / "command" / "summary.txt"),
           "# summary — the main points of a file\n# Usage:\n#   /command summary notes.md\n"
           "/read {{1}}\n<<<\nSum it up.\n>>>\n")
    monkeypatch.setattr(chat, "_input", lambda *a, **k: pytest.fail("asked the keyboard"))
    cli._cmd_command("/command summary")
    out = capsys.readouterr().out
    assert "needs 1 argument(s), got 0" in out and "/command summary notes.md" in out
    assert cli._sent == [] and cli._vars == {}


def test_ctrl_c_in_a_continuation_goes_back_to_the_prompt(cli, monkeypatch, capsys):
    # Windows: readline gives "" (None here) on Ctrl+C; the loop must go on, not end
    typed = iter(["first part " + BS, None, "/ls", EOFError])

    def fake_input(prompt="", none_on_eof=False):
        v = next(typed)
        if v is EOFError:
            raise EOFError
        return v
    monkeypatch.setattr(chat, "_input", fake_input)
    monkeypatch.setattr(chat, "_swallow_late_interrupt", lambda: None)
    routed = []
    cli._route = lambda text, auto=False: routed.append(text)
    cli._print_status = lambda: None
    cli._do_autosave = lambda: None
    cli._history = []
    cli.run(_resumed=True)
    assert routed == ["/ls"]                    # the cancelled text was never sent
    assert "[cancelled]" in capsys.readouterr().out


def test_ctrl_c_raised_in_a_continuation_also_cancels(cli, monkeypatch):
    def interrupted(prompt="", none_on_eof=False):
        raise KeyboardInterrupt
    monkeypatch.setattr(chat, "_input", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli._read_more("first part " + BS)
