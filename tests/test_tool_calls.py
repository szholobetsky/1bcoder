"""Native tool calls (message.tool_calls / delta.tool_calls) reach the agent as ACTION lines."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chat  # noqa: E402


def test_aliases_namespace_and_argument_values():
    calls = [{"function": {"name": "repo_browser.print_tree", "arguments": {"path": "server/sonar-web", "depth": 2}}},
             {"function": {"name": "read_file", "arguments": '{"path": "src/App.tsx"}'}},
             {"function": {"name": "grep", "arguments": {"pattern": "ManualProjectCreate"}}},
             {"function": {"name": "bash", "arguments": {"command": "ls -la\npwd"}}},
             {"function": {"name": "map_find", "arguments": {}}}]
    assert chat._tool_calls_to_actions(calls) == [
        "ACTION: /tree server/sonar-web 2",
        "ACTION: /read src/App.tsx",
        "ACTION: /grep ManualProjectCreate",
        "ACTION: /run ls -la pwd",
        "ACTION: /map_find",
    ]


def test_search_tool_drops_limits_and_quotes_pattern():
    calls = [{"function": {"name": "search", "arguments": {"query": "setHeader(\"Sonar-Version\"", "max_results": 20}}},
             {"function": {"name": "grep", "arguments": {"pattern": "create project", "path": "server",
                                                          "include": "*.tsx", "case_insensitive": True}}},
             {"function": {"name": "find", "arguments": {"pattern": "CreateProject", "max_results": 20}}}]
    assert chat._tool_calls_to_actions(calls) == [
        "ACTION: /grep 'setHeader(\"Sonar-Version\"'",
        "ACTION: /grep --include=*.tsx -i 'create project' server",
        "ACTION: /find CreateProject",
    ]


def test_bash_grep_becomes_grep_and_wrapper_is_unwrapped():
    calls = [{"function": {"name": "bash", "arguments": {"command": "bash -lc 'grep -R \"Validation\" server/web | head'"}}},
             {"function": {"name": "shell", "arguments": {"command": ["bash", "-lc", "grep -rn key core.properties"]}}},
             {"function": {"name": "exec", "arguments": {"command": "python -V"}}}]
    assert chat._tool_calls_to_actions(calls) == [
        'ACTION: /grep -R "Validation" server/web | head',
        "ACTION: /grep -rn key core.properties",
        "ACTION: /run python -V",
    ]


def test_broken_argument_json_is_kept_as_text():
    calls = [{"function": {"name": "read", "arguments": "{path: oops"}}]
    assert chat._tool_calls_to_actions(calls) == ["ACTION: /read {path: oops"]


class _Resp:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self._lines)


@pytest.fixture
def cli():
    c = object.__new__(chat.CoderCLI)
    c._role, c.params, c.num_ctx, c.timeout = "", {}, 8192, 30
    c.model, c.base_url, c.log_requests = "m", "http://h", False
    c.think_show, c.think_in_ctx = True, False
    c._stats = chat.CoderCLI._new_stats()
    return c


def test_ollama_stream_with_only_a_tool_call_gives_an_action(cli, monkeypatch):
    cli.provider = "ollama"
    lines = [json.dumps({"message": {"thinking": "Let's confirm by exploring the tree."}}),
             json.dumps({"message": {"content": "", "tool_calls": [
                 {"function": {"name": "repo_browser.print_tree", "arguments": {"path": "server"}}}]}}),
             json.dumps({"message": {"content": ""}, "done": True, "eval_count": 40, "prompt_eval_count": 900,
                         "eval_duration": 10 ** 9, "prompt_eval_duration": 10 ** 9})]
    monkeypatch.setattr(chat.requests, "post", lambda *a, **k: _Resp(lines))
    reply = cli._stream_chat([{"role": "user", "content": "list files"}])
    assert reply == "ACTION: /tree server"
    assert cli._stats["requests"] == 1 and cli._stats["tokens_prompt"] == 900


def test_openai_stream_assembles_fragments(cli, monkeypatch):
    cli.provider = "openai"
    cli._to_openai_messages = lambda m: m
    frag = lambda **d: "data: " + json.dumps({"choices": [{"delta": d}]})
    lines = [frag(content="Looking."),
             frag(tool_calls=[{"index": 0, "function": {"name": "read_file", "arguments": '{"pa'}}]),
             frag(tool_calls=[{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]),
             "data: [DONE]"]
    monkeypatch.setattr(chat.requests, "post", lambda *a, **k: _Resp(lines))
    reply = cli._stream_chat([{"role": "user", "content": "read it"}])
    assert reply == "Looking.\nACTION: /read a.py"


def test_thinking_only_reply_still_counts_its_tokens(cli, monkeypatch):
    cli.provider = "ollama"
    lines = [json.dumps({"message": {"thinking": "hmm"}}),
             json.dumps({"message": {"content": ""}, "done": True, "eval_count": 300, "prompt_eval_count": 1500})]
    monkeypatch.setattr(chat.requests, "post", lambda *a, **k: _Resp(lines))
    assert cli._stream_chat([{"role": "user", "content": "x"}]) == ""
    assert cli._stats["requests"] == 1 and cli._stats["tokens_gen"] == 300


def test_shell_reads_become_read():
    calls = [{"function": {"name": "exec", "arguments": {"cmd": ["bash", "-lc", "sed -n '1,200p' src/Input.tsx"]}}},
             {"function": {"name": "bash", "arguments": {"command": "bash -lc sed -n '10,40p' a/B.java"}}},
             {"function": {"name": "shell", "arguments": {"command": "head -n 50 README.md"}}},
             {"function": {"name": "shell", "arguments": {"command": "cat pom.xml"}}}]
    assert chat._tool_calls_to_actions(calls) == [
        "ACTION: /read src/Input.tsx 1-200",
        "ACTION: /read a/B.java 10-40",
        "ACTION: /read README.md 1-50",
        "ACTION: /read pom.xml",
    ]


def test_run_shell_timeout_kills_the_grandchild_too():
    import subprocess
    import time
    # the shell starts python, which starts a sleeping python: a grandchild holding the pipe
    cmd = f'"{sys.executable}" -c "import subprocess, sys; subprocess.run([sys.executable, \'-c\', \'import time; time.sleep(60)\'])"'
    t = time.time()
    rc, out, timed_out = chat._run_shell(cmd, 2, stdin=subprocess.DEVNULL)
    assert timed_out and time.time() - t < 20


def test_unattended_run_gets_no_stdin(tmp_path, monkeypatch):
    c = object.__new__(chat.CoderCLI)
    c.messages, c._last_output, c._in_agent, c._auto_apply = [], "", True, True
    c._stats, c.params = {"run_calls": 0}, {"run_timeout": 20}
    c._cmd_run(f'"{sys.executable}" -c "import sys; print(\'read\', len(sys.stdin.read()))"')
    assert "read 0" in c._last_output
