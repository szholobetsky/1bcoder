"""radogast-gate — live context-drift check via Radogast (per-turn, not per-task).

Different question from the existing `autocheck radogast` preset (which asks
"is the task DONE" via POSITIVE/NEGATIVE milestone completion, spawning the
full `radogast analyze` CLI each call): this asks "is the CURRENT message
drifting off target RIGHT NOW", every turn, reading Radogast's drift_status
directly via an in-process import — same Python interpreter 1bcoder runs
under (sys.executable), no subprocess spawn, no click overhead. Also skips
silently when there's nothing to check yet instead of FAILing (no
.radogast/task.yaml, no agent context, radogast not installed).

Reads the live agent context from BCODER_AGENT_CTX_FILE (dumped by 1bcoder
automatically on every turn inside /agent runs) — this is the in-flight
message list, not a saved yasna session, so it sees the current turn before
it is ever written to disk. Only active inside /agent runs (same scope as
ladder.py's --gate mode, which reads the same env var).

Requires: pip install radogast   (radogast[embed] for the embedding-based
drift metric — without it, drift falls back to "no_embeddings" and only
markers/coverage/verification/balance still run, lexically).

Modes:
  (no args)   gate mode — silent on pass. Prints "FAIL: ..." only when
              drift_status is "critical" or a mandatory verification term
              is absent from context.
                Usage: /proc gate on radogast-gate
  --report    verbose one-line summary, always printed, never blocks —
              for a live dashboard readout instead of enforcement.
                Usage: /proc on radogast-gate --report
                   or: /proc run radogast-gate --report

Target resolution (first match wins):
  1. .radogast/task.yaml in BCODER_WORKDIR       (create with `radogast init`)
  2. no task.yaml → auto-derive a Target from the first real user message in
     the live context (radogast.target.derive_target — no LLM call, same
     no-LLM approach ladder.py uses for its own term auto-extraction)
  3. no usable message yet → skip silently

Cost note: with hybrid drift enabled (the default), the first call in a
session loads a sentence-transformer embedding model, which can be slow on a
cold cache. If that matters for gate latency, set `hybrid: false` in
.radogast/config.yaml to fall back to lexical-only metrics (no embeddings).
"""
import sys
import os
import json
import re

_reply = sys.stdin.read()  # consumed, unused — the signal here is the live
                            # context as a whole, not this single reply

_report_mode = "--report" in sys.argv[1:]

workdir  = os.environ.get("BCODER_WORKDIR", os.getcwd())
ctx_file = os.environ.get("BCODER_AGENT_CTX_FILE", "")

if not ctx_file or not os.path.isfile(ctx_file):
    sys.exit(0)  # not inside an /agent run yet — nothing to evaluate

try:
    with open(ctx_file, encoding="utf-8") as f:
        messages = json.load(f)
except Exception:
    sys.exit(0)

if not messages:
    sys.exit(0)

try:
    os.chdir(workdir)  # radogast's config/target lookup is Path.cwd()-relative
    from radogast import config as _rcfg
    from radogast.target import load_target, derive_target
    from radogast.analyzer import analyze
except ImportError:
    if _report_mode:
        print("[radogast] not installed — pip install radogast")
    sys.exit(0)

_TOOL_HDR_RE = re.compile(r'^\[(run|file|read|tool|web|webask|webfetch|search|fetch)[:\s]', re.IGNORECASE)


def _resolve_target():
    found = _rcfg.find_target()
    if found:
        return load_target(str(found))
    for m in messages:
        content = m.get("content", "")
        if m.get("role") == "user" and len(content) > 30 and not _TOOL_HDR_RE.match(content.lstrip()):
            return derive_target(content)
    return None


target = _resolve_target()
if target is None:
    sys.exit(0)  # nothing to compare against yet

try:
    cfg = _rcfg.load()
    report = analyze(messages, target, cfg, verbose=False)
except Exception as e:
    if _report_mode:
        print(f"[radogast] analysis failed: {e}")
    sys.exit(0)

if _report_mode:
    angle = f"{report.drift_angle}deg" if report.drift_angle is not None else "n/a"
    stage = report.active_milestone or "none"
    defined = sum(1 for s in report.term_coverage.values() if s == "defined")
    total = len(report.term_coverage)
    verif = ("FAIL - " + ", ".join(report.verification_fails)) if report.verification_fails else "OK"
    print(f"[radogast] drift: {angle} ({report.drift_status})  stage: {stage}  "
          f"coverage: {defined}/{total} defined  verification: {verif}")
    for s in report.suggestions[:3]:
        print(f"  -> {s}")
    sys.exit(0)

# ── gate mode: silent on pass, FAIL only on critical drift or missing verification ──
reasons = []
if report.drift_status == "critical":
    angle = f"{report.drift_angle}deg" if report.drift_angle is not None else "?"
    reasons.append(f"context drifted {angle} from the task goal")
if report.verification_fails:
    reasons.append(f"required terms missing from context: {', '.join(report.verification_fails[:5])}")

if reasons:
    print(f"FAIL: [radogast] {'; '.join(reasons)}")
    if report.suggestions:
        print(f"  suggestion: {report.suggestions[0]}")
