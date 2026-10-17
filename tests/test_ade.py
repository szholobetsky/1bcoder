"""1bcoder reports itself to herdr: registered at start, a state sent only when it changes,
an older herdr (no `-- <resume command>`) asked again without it, released at exit."""
import subprocess

import ade


def _herdr(monkeypatch, refuse_resume=False):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        bad = refuse_resume and "--" in cmd
        return subprocess.CompletedProcess(cmd, 2 if bad else 0, "", "unknown option: --" if bad else "")

    monkeypatch.setattr(ade.subprocess, "run", run)
    monkeypatch.setattr(ade.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(ade.atexit, "register", lambda f: None)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p2")
    monkeypatch.setenv("HERDR_BIN_PATH", "herdr.exe")
    return calls


def test_outside_herdr_nothing(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    assert ade.Herdr.here(["1bcoder"]) is None


def test_registered_and_only_changes_sent(monkeypatch):
    calls = _herdr(monkeypatch)
    h = ade.Herdr.here(["1bcoder", "--model", "m"])
    assert h.start() and h.state == "idle"
    assert calls[0][:4] == ["herdr.exe", "pane", "report-agent", "w1:p2"]
    assert calls[0][-4:] == ["--", "1bcoder", "--model", "m"]
    h.set("working"); h.set("working"); h.set("blocked")
    states = [c[c.index("--state") + 1] for c in calls[1:]]
    assert states == ["working", "blocked"]
    h.release()
    assert calls[-1][2] == "release-agent" and h.state is None
    h.set("idle")                                     # released: nothing more is sent
    assert calls[-1][2] == "release-agent"


def test_an_older_herdr_without_resume(monkeypatch):
    calls = _herdr(monkeypatch, refuse_resume=True)
    h = ade.Herdr.here(["1bcoder"])
    assert h.start() and not h.resume_ok
    assert "--" in calls[0] and "--" not in calls[1]
    assert "herdr: pane w1:p2 reported as agent '1bcoder'" in ade.status(h)
