"""Command output reaches the context in a script run with -y (only an agent's own
loop keeps it out), and "/read f -> var" puts the file text into the variable."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chat  # noqa: E402


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
    c._capturing = False
    c._aliases = {}
    c._vars = {}
    c._stats = {"cmds": {}, "run_calls": 0, "files_read": 0}
    c.cmd_history = []
    c.num_ctx = 8192
    c._role = ""
    c._proc_active = []
    c._proc_gates = []
    c.params = {}
    c._hooks = {}
    return c


def test_run_reaches_context_in_auto_apply_script(cli):
    cli._auto_apply = True                       # what /script apply -y sets
    cli._cmd_run('python -c "print(42)"')
    assert len(cli.messages) == 1
    assert "42" in cli.messages[0]["content"]


def test_run_stays_out_of_main_context_inside_agent(cli):
    cli._auto_apply = True
    cli._in_agent = True
    cli._cmd_run('python -c "print(42)"')
    assert cli.messages == []


def test_script_apply_y_run_then_read_both_in_context(cli, tmp_path):
    (tmp_path / "note.txt").write_text("candidate files here\n", encoding="utf-8")
    script = tmp_path / "s.txt"
    script.write_text('/run python -c "print(7)"\n/read note.txt\n', encoding="utf-8")
    cli._cmd_script(f"/script apply -y {script.as_posix()}")
    text = "\n".join(m["content"] for m in cli.messages)
    assert "7" in text and "candidate files here" in text
    assert cli._auto_apply is False


def test_script_with_bom_runs_first_line_as_command(cli, tmp_path):
    script = tmp_path / "bom.txt"
    script.write_bytes('/run python -c "print(5)"\n'.encode("utf-8-sig"))
    cli._cmd_script(f"/script apply -y {script.as_posix()}")
    assert len(cli.messages) == 1 and cli.messages[0]["content"].startswith("[run:")


def test_script_does_not_ask_for_a_key_it_captures_itself(cli, tmp_path, monkeypatch):
    (tmp_path / "t.txt").write_text("the task\n", encoding="utf-8")
    script = tmp_path / "s.txt"
    script.write_text("/read t.txt -> task\n/run python -c \"print('{{task}}')\"\n", encoding="utf-8")
    asked = []
    monkeypatch.setattr(cli, "_prompt_input", lambda p: asked.append(p) or "", raising=False)
    cli._cmd_script(f"/script apply -y {script.as_posix()}")
    assert asked == []
    assert "the task" in cli.messages[-1]["content"]


def test_stats_save_json_with_host_bucket(cli, tmp_path):
    import json
    import time
    cli._stats = chat.CoderCLI._new_stats()
    cli._stats["requests"], cli._stats["tokens_prompt"] = 2, 900
    cli._stats["by_model"]["m"] = {"requests": 2, "tokens_gen": 50, "tokens_prompt": 900, "tokens_cached": 0}
    cli._stats["by_host"]["http://h"] = {"requests": 2, "tokens_gen": 50}
    cli.model, cli.base_url, cli._session_start = "m", "http://h", time.time()
    out = tmp_path / "st.json"
    cli._cmd_stats(f"/stats save json {out.as_posix()}")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["tokens_prompt"] == 900 and data["requests"] == 2


def test_agent_result_choice_does_not_wait_for_keyboard_in_script(cli, monkeypatch):
    def no_keyboard(*a, **k):
        raise AssertionError("asked the keyboard")
    monkeypatch.setattr(chat, "_input", no_keyboard)
    cli._script_auto = True
    assert cli._agent_result_choice() == "s"


def test_script_y_sets_and_restores_script_auto(cli, tmp_path):
    seen = []
    cli._route = lambda cmd, auto=False: seen.append(cli._script_auto)
    script = tmp_path / "s.txt"
    script.write_text("/ls\n", encoding="utf-8")
    cli._cmd_script(f"/script apply -y {script.as_posix()}")
    assert seen == [True] and cli._script_auto is False


def test_headless_param_with_spaces_and_quotes_stays_one_value():
    import shlex
    params = {"task": 'message "Project is not analyzed yet." is confusing', "k": "8"}
    line = "/script apply -y s.txt " + " ".join(shlex.quote(f"{k}={v}") for k, v in params.items())
    _, _, parsed = chat._parse_script_apply_args(line[len("/script apply "):])
    assert parsed == params


def test_question_into_variable_holds_the_answer_without_reasoning(cli):
    def fake_chat(text, auto=False):
        print("─── AI ───\n<think>long reasoning about the task</think>\nForm, validation, projectKey")
        cli.last_reply = "<think>long reasoning about the task</think>\nForm, validation, projectKey"
        cli._stats["requests"] = cli._stats.get("requests", 0) + 1
    route = cli._route
    cli._route = lambda text, auto=False: fake_chat(text) if not text.startswith("/") else None
    route.__func__(cli, "list the keywords -> keywords")
    assert cli._vars["keywords"] == "Form, validation, projectKey"


def test_read_into_variable_holds_file_text_not_notice(cli, tmp_path):
    (tmp_path / "my.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    cli._route("/read my.txt -> myvar")
    assert cli._vars["myvar"] == "alpha\nbeta"
    assert cli.messages == []
    assert cli._capturing is False


def test_read_without_capture_still_injects(cli, tmp_path):
    (tmp_path / "my.txt").write_text("alpha\n", encoding="utf-8")
    cli._route("/read my.txt")
    assert len(cli.messages) == 1 and "alpha" in cli.messages[0]["content"]
