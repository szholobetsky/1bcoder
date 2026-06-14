"""autocheck — gate that runs a test command and checks output against a criterion.

Stops the agent when tests fail; lets it continue when tests pass.
Works as a gate: run AFTER action-required so expensive commands only run when
the agent has actually taken an action (gates are chained — first FAIL stops chain).

Single-token presets (no spaces needed):
  maven     → mvn test              ok: BUILD SUCCESS or exit 0
  gradle    → gradle test           ok: BUILD SUCCESSFUL
  pytest    → pytest                ok: exit 0
  npm       → npm test              ok: exit 0
  jest      → npx jest              ok: exit 0 + no failures line
  go        → go test ./...         ok: exit 0 + no FAIL line
  cargo     → cargo test            ok: exit 0 + "test result: ok"
  rspec     → bundle exec rspec     ok: exit 0 + "0 failures"
  dotnet    → dotnet test           ok: "Test Run Successful"
  phpunit   → vendor/bin/phpunit    ok: exit 0 + "OK ("
  radogast  → radogast analyze      ok: "POSITIVE" in output

Keyword check (checks agent reply, not a command):
  keyword:WORD   → checks if WORD is in the agent's last reply

Config file .autocheck.yaml (for custom commands with spaces):
  cmd: "mvn -pl core test"
  ok: maven              # or: regexp:BUILD, keyword:COMPLETE, exit0
  keyword: COMPLETE      # also check agent reply
  msg: "Fix failing tests before completing the task"

Usage:
  /proc gate on autocheck pytest
  /proc gate on autocheck maven
  /proc gate on autocheck keyword:COMPLETE
  /proc gate on autocheck cmd:"python calc.py" ok:exit0
  /proc gate on autocheck cmd:"mvn test" regexp:"BUILD SUCCESS"
  /proc gate on autocheck cmd:"python calc.py" keyword:passed
  /proc gate on autocheck              (reads .autocheck.yaml from project dir)

Typical setup (action-required chains to autocheck):
  /proc gate on action-required
  /proc gate on autocheck pytest

Output:
  FAIL: [maven] Maven build/test failed — please fix...
    <relevant error lines>
    [criterion: ok:maven]
  (no output) = PASS
"""
import sys
import os
import subprocess
import re

PRESETS = {
    "maven":    {"cmd": "mvn test",             "ok": "maven"},
    "gradle":   {"cmd": "gradle test",          "ok": "gradle"},
    "pytest":   {"cmd": "pytest",               "ok": "pytest"},
    "npm":      {"cmd": "npm test",             "ok": "npm"},
    "jest":     {"cmd": "npx jest",             "ok": "jest"},
    "go":       {"cmd": "go test ./...",        "ok": "go"},
    "cargo":    {"cmd": "cargo test",           "ok": "cargo"},
    "rspec":    {"cmd": "bundle exec rspec",    "ok": "rspec"},
    "dotnet":   {"cmd": "dotnet test",          "ok": "dotnet"},
    "phpunit":  {"cmd": "vendor/bin/phpunit",   "ok": "phpunit"},
    "radogast": {"cmd": "radogast analyze --input {BCODER_AGENT_CTX_FILE}", "ok": "radogast"},
}

FRIENDLY = {
    "maven":    "Maven build/test failed — please fix the following errors:",
    "gradle":   "Gradle build/test failed — please fix the following errors:",
    "pytest":   "pytest failed — please fix the failing tests:",
    "npm":      "npm test failed — please fix the following errors:",
    "jest":     "Jest tests failed — please fix the failing tests:",
    "go":       "Go tests failed — please fix the following errors:",
    "cargo":    "Cargo tests failed — please fix the following errors:",
    "rspec":    "RSpec tests failed — please fix the failing tests:",
    "dotnet":   ".NET tests failed — please fix the following errors:",
    "phpunit":  "PHPUnit tests failed — please fix the failing tests:",
    "radogast": "Radogast did not confirm task completion. Context is missing required milestones:",
    "exit0":    "Command exited with non-zero code — please fix the following errors:",
}


def _extract_errors(lines, keywords, max_lines=25):
    """Return relevant lines from output: lines matching keywords + context."""
    relevant = []
    for i, line in enumerate(lines):
        if any(kw.lower() in line.lower() for kw in keywords):
            relevant.extend(lines[max(0, i - 1):min(len(lines), i + 4)])
            if len(relevant) >= max_lines:
                break
    if not relevant:
        relevant = lines[-15:]
    return "\n".join(relevant[:max_lines])


def check_ok(ok_key, output, returncode):
    """Returns (passed: bool, error_text: str)."""
    lines = output.splitlines()

    table = {
        "maven":    (returncode == 0 or "BUILD SUCCESS" in output,
                     _extract_errors(lines, ["BUILD FAILURE", "ERROR", "Tests run", "FAIL"])),
        "gradle":   ("BUILD SUCCESSFUL" in output,
                     _extract_errors(lines, ["BUILD FAILED", "FAILURE", "error"])),
        "pytest":   (returncode == 0,
                     _extract_errors(lines, ["FAILED", "ERROR", "error"])),
        "npm":      (returncode == 0,
                     _extract_errors(lines, ["npm ERR!", "Error", "failed"])),
        "jest":     (returncode == 0 and not re.search(r"Tests:.*\d+ failed", output),
                     _extract_errors(lines, ["FAIL", "●"])),
        "go":       (returncode == 0 and not re.search(r"^FAIL\b", output, re.MULTILINE),
                     _extract_errors(lines, ["FAIL", "panic:"])),
        "cargo":    (returncode == 0 and "test result: ok" in output,
                     _extract_errors(lines, ["FAILED", "error[E"])),
        "rspec":    (returncode == 0 and "0 failures" in output,
                     _extract_errors(lines, ["failure", "Failure", "error"])),
        "dotnet":   ("Test Run Successful" in output,
                     _extract_errors(lines, ["Test Run Failed", "Error", "Failed"])),
        "phpunit":  (returncode == 0 and ("OK (" in output or "OK!" in output),
                     _extract_errors(lines, ["FAILURES", "ERRORS", "FAILED"])),
        "radogast": ("POSITIVE" in output,
                     _extract_errors(lines, ["NEGATIVE", "not reached", "missing", "absent"])),
        "exit0":    (returncode == 0,
                     "\n".join(lines[-15:])),
    }

    if ok_key in table:
        return table[ok_key]

    if ok_key.startswith("regexp:"):
        pattern = ok_key[7:]
        if re.search(pattern, output, re.MULTILINE):
            return True, ""
        return False, f"Pattern not found: `{pattern}`\n" + "\n".join(lines[:20])

    if ok_key.startswith("keyword:"):
        word = ok_key[8:]
        if word.lower() in output.lower():
            return True, ""
        return False, f"Keyword `{word}` not found in output.\n" + "\n".join(lines[:20])

    return returncode == 0, "\n".join(lines[-15:])


# ── entry point ────────────────────────────────────────────────────────────────
workdir = os.environ.get("BCODER_WORKDIR", ".")
reply = sys.stdin.read()


def _parse_kv_args(argv):
    """Parse sys.argv[1:] as key:value pairs. Values may be bare or quoted (already split by shlex)."""
    result = {}
    for tok in argv:
        if ":" in tok:
            k, v = tok.split(":", 1)
            result[k.strip().lower()] = v.strip()
        else:
            result["_bare"] = tok.strip()
    return result


kv = _parse_kv_args(sys.argv[1:])

# ── mode 1: keyword-only (check agent reply, no command) ──────────────────────
# Usage: /proc gate on autocheck keyword:DONE
# (bare "keyword:X" arg with no cmd: key)
if "keyword" in kv and "cmd" not in kv:
    kw = kv["keyword"]
    if kw.lower() not in reply.lower():
        print(f"FAIL: Completion keyword '{kw}' not found in your reply.")
        print(f"Please write '{kw}' to signal that the task is complete.")
    sys.exit(0)

# ── mode 2: key:value format ───────────────────────────────────────────────────
# Usage: /proc gate on autocheck cmd:"python calc.py" ok:exit0
#        /proc gate on autocheck cmd:"pytest" keyword:passed regexp:"passed"
if "cmd" in kv:
    cfg        = {}
    cmd        = kv["cmd"]
    if "regexp" in kv:
        ok_key = f"regexp:{kv['regexp']}"
    elif "keyword" in kv:
        ok_key = f"keyword:{kv['keyword']}"
    else:
        ok_key = kv.get("ok", "exit0")
    custom_msg = kv.get("msg", "")

# ── mode 3: legacy single-arg (preset name or .autocheck.yaml) ────────────────
else:
    arg = kv.get("_bare", "")

    if not arg and sys.argv[1:]:
        arg = sys.argv[1].strip()

    cfg = {}
    if arg in PRESETS:
        cfg = dict(PRESETS[arg])
    else:
        config_path = os.path.join(workdir, ".autocheck.yaml")
        if os.path.isfile(config_path):
            try:
                import yaml
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            except ImportError:
                print("FAIL: [autocheck] PyYAML not installed — cannot read .autocheck.yaml")
                sys.exit(0)
            except Exception as e:
                print(f"FAIL: [autocheck] cannot read .autocheck.yaml: {e}")
                sys.exit(0)
        elif arg:
            known = ", ".join(PRESETS)
            print(f"FAIL: [autocheck] unknown preset '{arg}'. Known presets: {known}")
            sys.exit(0)
        else:
            sys.exit(0)  # no arg, no config → pass silently

    cmd        = cfg.get("cmd", "")
    ok_key     = str(cfg.get("ok", "exit0"))
    custom_msg = cfg.get("msg", "")

# optional keyword check on agent reply (from yaml config)
cfg_keyword = str(cfg.get("keyword", ""))
if cfg_keyword and cfg_keyword.lower() not in reply.lower():
    print(f"FAIL: Completion keyword '{cfg_keyword}' not found in your reply.")
    print(f"Please write '{cfg_keyword}' to signal that the task is complete.")
    sys.exit(0)

if not cmd:
    sys.exit(0)

# expand {ENV_VAR} placeholders in cmd (e.g. {BCODER_AGENT_CTX_FILE})
cmd = re.sub(r'\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), cmd)

# ── run command ────────────────────────────────────────────────────────────────
try:
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        cwd=workdir, timeout=300,
        encoding="utf-8", errors="replace",
    )
except subprocess.TimeoutExpired:
    print(f"FAIL: [autocheck] command timed out after 300s: {cmd}")
    sys.exit(0)
except Exception as e:
    print(f"FAIL: [autocheck] failed to run '{cmd}': {e}")
    sys.exit(0)

output = (result.stdout or "") + (result.stderr or "")
passed, errors = check_ok(ok_key, output, result.returncode)

if passed:
    print(f"SUCCESS: [{ok_key}] test passed. Task is complete — no further action needed.")
    sys.exit(0)

# ── FAIL output ────────────────────────────────────────────────────────────────
header = FRIENDLY.get(ok_key, f"Check failed ({ok_key}):")
print(f"FAIL: {header}")
if custom_msg:
    print(f"  {custom_msg}")
for line in errors.splitlines():
    print(f"  {line}")
print(f"  [criterion: ok:{ok_key}]")
