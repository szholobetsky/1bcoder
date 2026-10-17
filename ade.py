"""1bcoder in an ADE (agent workspace app): herdr, orca.

herdr (https://herdr.dev) puts HERDR_ENV=1, HERDR_PANE_ID and HERDR_BIN_PATH into every pane it
opens and lets any program report itself as the pane's agent (`herdr pane report-agent`). 1bcoder
does that on its own when it starts in a herdr pane — nothing to install. The state follows the
prompt: waiting at `> ` is idle, waiting at any other question (y/n, pick) is blocked, anything
else is working. On exit the pane is released.

orca has a fixed list of agents and no way to register another; 1bcoder is started there by a
Quick command (Settings -> Terminal -> Quick commands). `/ade` only reads orca's settings.
"""
import atexit
import json
import os
import subprocess
import time

AGENT = "1bcoder"
SOURCE = "1bcoder"


class Herdr:
    """The herdr pane this 1bcoder runs in; every call is quiet and never stops 1bcoder."""

    def __init__(self, exe: str, pane: str, resume: list):
        self.exe, self.pane, self.resume = exe, pane, resume
        self.resume_ok = True              # herdr < 0.9.2 refuses `-- <resume command>`
        self.state = None
        self.error = ""

    @classmethod
    def here(cls, resume: list):
        exe, pane = os.environ.get("HERDR_BIN_PATH"), os.environ.get("HERDR_PANE_ID")
        if os.environ.get("HERDR_ENV") != "1" or not exe or not pane:
            return None
        return cls(exe, pane, resume)

    def _seq(self) -> str:
        return str(time.time_ns() // 1000)    # herdr keeps the report with the highest seq

    def _report(self, state: str) -> list:
        cmd = [self.exe, "pane", "report-agent", self.pane, "--source", SOURCE, "--agent", AGENT,
               "--state", state, "--seq", self._seq()]
        return cmd + ["--", *self.resume] if self.resume_ok and self.resume else cmd

    def _run(self, cmd: list) -> int:
        try:
            r = subprocess.run(cmd, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               text=True, errors="replace")
            if r.returncode:
                lines = (r.stderr or "").strip().splitlines()
                self.error = lines[-1] if lines else f"exit code {r.returncode}"
            return r.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            self.error = str(e)
            return -1

    def start(self) -> bool:
        """Register the pane as agent 1bcoder (idle); release it when 1bcoder exits."""
        ok = self._run(self._report("idle")) == 0
        if not ok and self.resume:
            self.resume_ok = False
            ok = self._run(self._report("idle")) == 0
        if ok:
            self.state = "idle"
            self.error = ""
            atexit.register(self.release)
        return ok

    def set(self, state: str):
        """idle | working | blocked — only a change is sent, without waiting for herdr."""
        if self.state is None or state == self.state:
            return
        self.state = state
        try:
            subprocess.Popen(self._report(state), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def release(self):
        if self.state is None:
            return
        self.state = None
        self._run([self.exe, "pane", "release-agent", self.pane, "--source", SOURCE, "--agent", AGENT,
                   "--seq", self._seq()])


# ── /ade ─────────────────────────────────────────────────────────────────────

def _orca_settings():
    """orca's settings as the app last exported them (read only); None when there is no orca."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    path = os.path.join(base, "orca", "profiles", "local-default", "orca-data.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("settings", {})
    except (OSError, ValueError):
        return None


def status(herdr) -> str:
    out = []
    if herdr is not None:
        if herdr.state is not None:
            resume = "" if herdr.resume_ok else "  (this herdr is older than 0.9.2: no restart of 1bcoder after a herdr restart)"
            out.append(f"herdr: pane {herdr.pane} reported as agent '{AGENT}', state {herdr.state}{resume}")
        else:
            out.append(f"herdr: pane {herdr.pane}, but herdr refused the report: {herdr.error or '?'}")
    else:
        out.append("herdr: not in a herdr pane — start 1bcoder in one and it registers itself")
    s = _orca_settings()
    if s is None:
        out.append("orca:  no orca settings found")
    else:
        mine = [q for q in s.get("terminalQuickCommands", []) if AGENT in q.get("command", "")]
        for q in mine:
            out.append(f"orca:  quick command {q.get('label')!r}: {q.get('command')}  "
                       f"(scope: {q.get('scope', {}).get('type', '?')})")
        if not mine:
            out.append("orca:  no quick command runs 1bcoder — add one: Settings -> Terminal -> "
                       "Quick commands -> command `1bcoder`")
    return "\n".join(out)
