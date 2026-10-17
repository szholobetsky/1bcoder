"""/grep: grep -rn as models write it, in pure Python."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chat  # noqa: E402


def test_parse_combined_flags_quotes_and_pipe_tail():
    o = chat._grep_parse('-Rni "Sonar-Version" server/web src | head -20')
    assert o["i"] and o["patterns"] == ["Sonar-Version"] and o["paths"] == ["server/web", "src"]


def test_parse_include_e_and_max_count():
    o = chat._grep_parse('-r --include=*.tsx -e foo -e bar -m 3 .')
    assert o["include"] == ["*.tsx"] and o["patterns"] == ["foo", "bar"] and o["max_count"] == 3
    assert o["paths"] == ["."]


def test_parse_unbalanced_quote_falls_back_to_split():
    o = chat._grep_parse("it's here")
    assert o["patterns"] == ["it's"] and o["paths"] == ["here"]


def test_bre_operators_and_literals():
    assert chat._bre_to_python(r"foo\|bar") == "foo|bar"
    assert chat._bre_to_python(r"f(x)") == r"f\(x\)"
    assert chat._bre_to_python(r"\(ab\)\+") == "(ab)+"


def test_uncompilable_pattern_is_searched_as_text():
    rx, literal = chat._grep_regex(chat._grep_parse('-E \'setHeader("Sonar-Version"\''))
    assert literal and rx.search('wsResponse.setHeader("Sonar-Version", v)')


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "WORKDIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Filter.java").write_text(
        'class F {\n  void f() { r.setHeader("Sonar-Version", v); }\n  @Override\n}\n', encoding="utf-8")
    (tmp_path / "src" / "legacy.ts").write_text("const v = h.get('Sonar-Version');\n", encoding="utf-8")
    (tmp_path / "src" / "other.ts").write_text("nothing here$\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.txt").write_text("Sonar-Version\n", encoding="utf-8")
    c = object.__new__(chat.CoderCLI)
    c.messages, c._last_output, c._vars = [], "", {}
    c._in_agent, c._auto_apply, c._capturing = False, True, False
    c._aliases, c._stats, c.cmd_history, c.num_ctx = {}, {"cmds": {}}, [], 8192
    c._confirm = lambda prompt, ctx_add="", **kw: True
    return c


def test_grep_lines_files_and_context(cli, capsys):
    cli._cmd_grep('/grep -rn "Sonar-Version" . | head -5')
    out = capsys.readouterr().out
    assert 'src/Filter.java:2:  void f() { r.setHeader("Sonar-Version", v); }' in out
    assert "src/legacy.ts:1:" in out and ".git" not in out
    assert cli._vars["grep_files"] == "src/Filter.java,src/legacy.ts"
    assert cli.messages and "[grep" in cli.messages[0]["content"]


def test_grep_l_include_and_missing_path(cli, capsys):
    cli._cmd_grep("/grep -il --include=*.java sonar-version src nope")
    out = capsys.readouterr().out
    assert "src/Filter.java" in out and "legacy.ts" not in out
    assert "grep: nope: No such file or directory" in out


def test_line_anchors_inside_a_file(cli, capsys):
    cli._cmd_grep('/grep -rn "^  @Override" src')
    assert "src/Filter.java:3:  @Override" in capsys.readouterr().out


def test_dollar_and_at_reach_grep_unexpanded(cli, capsys):
    cli._last_output = "SHOULD-NOT-APPEAR"
    cli._route('/grep -rn "here\\$" src')
    cli._route('/grep -rn "@Override" src')
    out = capsys.readouterr().out
    assert "src/other.ts:1:nothing here$" in out and "src/Filter.java:3:" in out
    assert "SHOULD-NOT-APPEAR" not in out
