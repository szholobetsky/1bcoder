"""ANSI colors only reach a terminal that shows them; files, pipes, NO_COLOR and
TERM=dumb get plain text."""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chat  # noqa: E402


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_strip_removes_codes_and_keeps_text():
    out = io.StringIO()
    s = chat._AnsiStrip(out)
    s.write("\x1b[90mOkay\x1b[0m\x1b[90m,\x1b[0m\x1b[90m let's\x1b[0m")
    assert out.getvalue() == "Okay, let's"


def test_strip_holds_a_sequence_split_across_writes():
    out = io.StringIO()
    s = chat._AnsiStrip(out)
    s.write("word\x1b[9")
    s.write("0mnext\x1b[0m")
    assert out.getvalue() == "wordnext"


def test_file_gets_no_colors(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    assert chat._colors_supported(io.StringIO()) is False


def test_no_color_wins_over_terminal(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert chat._colors_supported(_Tty()) is False


def test_force_color_wins_over_file(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert chat._colors_supported(io.StringIO()) is True


def test_dumb_terminal_gets_no_colors(monkeypatch):
    for k in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert chat._colors_supported(_Tty()) is False


def test_setup_wraps_redirected_stdout(monkeypatch):
    for k in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE"):
        monkeypatch.delenv(k, raising=False)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    chat._setup_colors()
    print("\x1b[32mok\x1b[0m")
    assert buf.getvalue() == "ok\n"
