"""creator_plan — creator, plus an upfront plan and real /read + /run.

Sibling of flows/creator.py — same named-fence auto-save write mechanism
(see that file's docstring for the rationale and its limits), but structured
in two phases instead of one open-ended "write / anything else?" loop:

  1. PLAN — one call, asking the model to break the task into a short
     numbered plan (##1. ..., ##2. ..., ...), explaining WHY: each numbered
     item becomes its own separate call later, with no memory of the plan
     text itself (only of what happened in earlier steps) — so a step needs
     to be small enough to finish in one reply, and if the model needs to
     verify something (read a file back, run it, check the output), that
     has to be its own explicit step, not something folded silently into a
     write step.

  2. EXECUTE — one call PER plan item, in order. The loop is bounded by the
     number of plan items, not by an arbitrary turn cap — a 30-item plan
     just runs 30 steps, a 1-item plan runs one. Each step's reply may:
       - write a named fence -> auto-saved, same as creator.py (the path
         on the opening fence line is MANDATORY — an unnamed fence is not
         a valid write, see _STEP_SYSTEM)
       - and/or put /read <file> and/or /run <cmd> lines inside a ```bash
         fence -> executed for real (see _dispatch_isolated below), result
         fed back as context for the NEXT step

This is deliberately not the full /agent machinery (chat.py's
_run_agent_loop, with its gates/tool-allowlist/plan-step engine) — that
was judged too much machinery for what this needs; see agents/harness.txt
(still just an idea, not built) for where that heavier approach would go
if it's ever needed. Here the command surface is intentionally tiny:
write-by-fence (always available) plus exactly two commands recognized
inside a ```bash fence, /read and /run — nothing else. (The old ACTION:
line convention is still recognized as a silent fallback, see the comment
above _SAVE_VERBS, but no longer taught — a fenced code block turned out
to be a much more natural shape for a coding model to reliably produce.)
Both /read and /run are dispatched through chat._route so this reuses the
real _cmd_read/_cmd_run implementations (not reimplemented here), isolated
from the user's live session the same way deepagent_architect.py's
_run_research_agent and _run_agent_loop's own "before proc" ACTION
execution already do it: snapshot chat.messages length, call _route, pull
out whatever got appended, then roll chat.messages back so none of this
leaks into the real session.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow creator_plan <task>
  /flow creator_plan <task> file: <path>
  /flow creator_plan <task> files: <path1>,<path2>
  /flow creator_plan <task> --model <name> --max-tokens N

  file:/files:  seed context with existing file content (optional — same
                shallow-tree fallback as creator.py if omitted)
  --model       temporarily switch model for this run only (restored after)
  --max-tokens  default: unbounded (provider's own default) — pass a
                number to cap num_predict per turn if a model rambles

No --max-turns here — the plan's own length IS the turn budget, by design
(see PLAN phase above). No test: flag either — verification is something
the model puts IN the plan (as its own step) rather than something this
flow bolts on from outside.

Examples:
  /flow creator_plan build a Flask blog app with posts and comments
  /flow creator_plan add auth to app.py, then verify it still starts file: app.py
  /flow creator_plan port utils.py to TypeScript, run tsc to check it compiles file: utils.py

Safety: same as creator.py — no confirmation prompt on writes, first
overwrite of an existing file backs it up to <path>.bkup for the run.
Ctrl+C mid-step retries that step with an optional feedback comment, same
convention as creator.py / chat.py's _run_agent_loop / glossary.py.
"""
import os as _os
import re as _re
import shutil as _shutil

_SEED_FILE_CHAR_CAP = 8000
_TREE_IGNORE_DIRS   = {".git", "node_modules", "venv", ".venv", "__pycache__",
                        "dist", "build", ".idea", ".vscode", ".1bcoder"}
_TREE_MAX_ENTRIES   = 200

_PLAN_SYSTEM = """\
You are a coding assistant. Before writing any code, break the task into a
short numbered plan, one line per step, formatted EXACTLY like this:

##1. <what this step does>
##2. <what this step does>

Why: each numbered step will be given to you separately, in its own later
turn — you will NOT see this plan text again, only the results of earlier
steps. So:
- Keep each step small enough to fully finish in ONE reply.
- If the whole task is small enough to do in one reply, write just ##1.
- If you need to verify something — read a file back, run it, check
  output — make that its own explicit step (e.g. ##3. run app.py and
  confirm it starts without errors).

Write ONLY the numbered plan right now. No code yet, no other text.
"""

_STEP_SYSTEM = """\
You are executing ONE step of a plan you already wrote. Do only what this
step asks — do not redo earlier steps. You have two ways to act, and may
use either or both in this reply:

1. Write a file: put its full content in a fenced code block. The file
   path and name are MANDATORY on the opening fence line — a fence with no
   path is not a valid file write:
   ```path/to/file.py
   <full content>
   ```

2. Read or run something, to check it: put the command(s) in a ```bash
   fence, one per line:
   ```bash
   /read path/to/file.py
   /run pytest
   ```
   Each line is executed for real and its result is shown to you before
   the next step. Only /read and /run are recognized inside a ```bash
   fence — nothing else.
"""

# Not taught in _STEP_SYSTEM above (kept only as a silent safety net for a
# model that ignores the instructions anyway):
# - ACTION: /read|/run lines — the old convention, before it was replaced
#   with a ```bash fence on the theory that a fenced code block is a much
#   more natural shape for a coding model to reliably produce than a
#   custom text-line convention.
# - ACTION: /write or /save on an unnamed fence (confirmed live,
#   2026-08-21, AnimalAlert/log.txt: qwen2.5-coder:7b-instruct wrote
#   "ACTION: /write /path/to/database.py" followed by an unnamed ```python
#   fence — with only /read + /run recognized at the time, that whole
#   reply's code silently saved nothing).
_SAVE_VERBS = {"/save_code", "/save", "/write"}
_ACTION_RE = _re.compile(r'ACTION:\s*(/\S+(?:[ \t]+[^\n]+)?)', _re.MULTILINE)
_PLAN_ITEM_RE = _re.compile(r'^\s*##\s*(\d+)\.\s*(.+)$', _re.MULTILINE)


# ── fence parsing — duplicated from chat.py's _fence_filename/            ──
# ── _extract_named_code_blocks; see creator.py's docstring for why        ──

def _fence_filename(info: str):
    info = info.strip()
    if not info:
        return None
    parts = [p for p in _re.split(r'[\s:]+', info) if p]
    if not parts:
        return None
    candidate = parts[-1]
    if candidate.startswith('.'):
        return None
    if _re.match(r'^[\w./\\-]+\.[A-Za-z0-9_]{1,10}$', candidate):
        return candidate
    return None


def _extract_named_code_blocks(text):
    """[(filename_or_None, content, info), ...] for every CLOSED fence, in
    order. info is the raw opening-fence-line text (e.g. "bash", "python",
    "path/to/file.py") — needed to tell a ```bash action fence apart from a
    genuinely unnamed file fence (see _STEP_SYSTEM: exactly two kinds of
    fence are taught — a named file fence, and a ```bash action fence)."""
    return [(_fence_filename(m.group(1)), m.group(2), m.group(1))
            for m in _re.finditer(r'```([^\n]*)\n(.*?)```', text, _re.DOTALL)]


def _extract_bash_commands(blocks) -> list:
    """/read and /run lines pulled out of every ```bash-tagged block, in
    order. Anything else inside a ```bash fence is ignored with a warning —
    matches _STEP_SYSTEM's "only /read and /run, nothing else" rule."""
    cmds = []
    for fn, content, info in blocks:
        if fn or info.strip().lower() != "bash":
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            verb = line.split(None, 1)[0]
            if verb in ("/read", "/run"):
                cmds.append(line)
            else:
                print(f"[creator_plan] ignored line in ```bash fence: {line}  (only /read, /run allowed)")
    return cmds


# ── filename salvage — same rationale as creator.py's _ask_filename: some  ──
# ── small models write correct code but reliably ignore the naming         ──
# ── instruction, so ask a cheap side question instead of discarding it     ──

_FILENAME_ASK_SYSTEM = (
    "You will be shown a code snippet. Reply with ONLY a filename for it — "
    "including a correct extension for its language — and nothing else. "
    "No explanation, no code fence, no quotes, no path unless the code "
    "itself makes a directory obvious."
)
_FILENAME_PATTERN = _re.compile(r'[\w][\w./\\-]*\.[A-Za-z0-9_]{1,10}')
_FILENAME_ASK_SNIPPET_CAP = 1500


def _ask_filename(chat, content: str):
    snippet = content[:_FILENAME_ASK_SNIPPET_CAP]
    msgs = [
        {"role": "system", "content": _FILENAME_ASK_SYSTEM},
        {"role": "user", "content": f"```\n{snippet}\n```"},
    ]
    reply = chat._stream_chat(msgs) or ""
    m = _FILENAME_PATTERN.search(reply)
    return m.group(0) if m else None


def _write_named_blocks(named: list, backed_up: set) -> list:
    saved = []
    for path, content in named:
        try:
            if _os.path.exists(path) and path not in backed_up:
                _shutil.copy2(path, path + ".bkup")
                backed_up.add(path)
            dirpart = _os.path.dirname(path)
            if dirpart:
                _os.makedirs(dirpart, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            saved.append(path)
            print(f"[creator_plan] saved -> {path}")
        except OSError as e:
            print(f"[creator_plan] error writing {path}: {e}")
    return saved


# ── plan parsing ──────────────────────────────────────────────────────────

def _parse_plan(text: str) -> list:
    """["<step 1 text>", "<step 2 text>", ...] in the order they appear
    (not re-sorted by their ##N — a model that numbers out of order is
    still followed in the order it wrote them, since that's what it meant
    to communicate)."""
    return [m.group(2).strip() for m in _PLAN_ITEM_RE.finditer(text)]


# ── context assembly — same as creator.py ────────────────────────────────

def _read_seed_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError as e:
        return f"(could not read {path}: {e})"
    if len(content) > _SEED_FILE_CHAR_CAP:
        content = content[:_SEED_FILE_CHAR_CAP] + f"\n... (truncated, {len(content)} chars total)"
    return content


def _shallow_tree(root: str = ".") -> str:
    lines = []
    for dirpath, dirnames, filenames in _os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _TREE_IGNORE_DIRS and not d.startswith(".")]
        depth = dirpath[len(root):].count(_os.sep)
        if depth > 1:
            dirnames[:] = []
            continue
        rel = _os.path.relpath(dirpath, root)
        for fn in sorted(filenames):
            lines.append(fn if rel == "." else _os.path.join(rel, fn))
            if len(lines) >= _TREE_MAX_ENTRIES:
                return "\n".join(lines) + "\n... (truncated)"
    return "\n".join(lines) if lines else "(empty directory)"


def _build_context(files: list) -> str:
    if not files:
        tree = _shallow_tree()
        return f"Project files (depth-2 listing, no content shown):\n{tree}"
    parts = []
    for path in files:
        parts.append(f"--- {path} ---\n{_read_seed_file(path)}")
    return "\n\n".join(parts)


# ── /read + /run — dispatched through the real chat._route, isolated ────

def _dispatch_isolated(chat, cmd: str) -> str:
    """Run cmd (/read ... or /run ...) through chat._route so this reuses
    the real _cmd_read/_cmd_run instead of reimplementing file-reading or
    subprocess handling here. _cmd_read/_cmd_run only append to
    chat.messages when chat._auto_apply is False, so that's forced for the
    call; whatever gets appended is captured and chat.messages is rolled
    back to its original length afterward, so none of this leaks into the
    user's real session — same isolation pattern as _run_agent_loop's
    'before proc' ACTION execution and deepagent_architect.py's
    _run_research_agent."""
    saved_auto_apply = chat._auto_apply
    chat._auto_apply = False
    n = len(chat.messages)
    try:
        chat._route(cmd, auto=True)
    finally:
        chat._auto_apply = saved_auto_apply
    new_msgs = chat.messages[n:]
    chat.messages = chat.messages[:n]
    return "\n".join(m["content"] for m in new_msgs if m.get("role") in ("user", "assistant"))


# ── arg parsing ───────────────────────────────────────────────────────────

def _pop_flag(rest: str, pattern: str):
    m = _re.search(pattern, rest)
    if not m:
        return None, rest
    return m.group(1), (rest[:m.start()] + rest[m.end():]).strip()


def _parse_args(args: str):
    model, args      = _pop_flag(args, r'--model\s+(\S+)')
    max_tokens, args = _pop_flag(args, r'--max-tokens\s+(\d+)')
    max_tokens = int(max_tokens) if max_tokens else None

    files = []
    m = _re.search(r'\bfiles?:\s*(.+)$', args, _re.IGNORECASE | _re.DOTALL)
    if m:
        files = [f.strip() for f in _re.split(r'[,\s]+', m.group(1).strip()) if f.strip()]
        args = args[:m.start()].strip()

    return args.strip(), files, model, max_tokens


# ── Ctrl+C handling — same convention as creator.py / glossary.py /       ──
# ── chat.py's _run_agent_loop                                             ──

def _prompt_interrupt():
    try:
        choice = input("  [creator_plan] interrupted. Retry this step? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice.startswith("n"):
        return None
    try:
        note = input("  Add feedback for the retry (blank = none): ").strip()
    except (EOFError, KeyboardInterrupt):
        note = ""
    return note


def _stream_with_retry(chat, messages):
    """chat._stream_chat, but a Ctrl+C sentinel (None) prompts to retry
    (optionally with a feedback comment appended first) instead of killing
    the run. Returns the reply string, or None only if the user chose to
    quit outright."""
    while True:
        reply = chat._stream_chat(messages)
        if reply is not None:
            return reply
        action = _prompt_interrupt()
        if action is None:
            return None
        if action:
            messages.append({"role": "user", "content": action})
        # loop again -> retry, messages already reflects pre-attempt state


# ── main ──────────────────────────────────────────────────────────────────

def run(chat, args: str) -> None:
    args = args.strip()
    if not args:
        print(__doc__)
        return

    task, files, model_override, max_tokens = _parse_args(args)
    if not task:
        print("usage: /flow creator_plan <task> [file: <path> | files: <p1>,<p2>] "
              "[--model <name>] [--max-tokens N]")
        return

    context = _build_context(files)

    saved_model  = chat.model
    saved_params = dict(chat.params)
    if model_override:
        chat.model = model_override
    if max_tokens is not None:
        chat.params["num_predict"] = max_tokens

    messages = [
        {"role": "system", "content": _PLAN_SYSTEM},
        {"role": "user", "content": f"Task: {task}\n\nContext:\n{context}"},
    ]

    print(f"[creator_plan] model: {chat.model}")
    print(f"[creator_plan] task: {task}\n")
    print("[creator_plan] asking for a plan...")

    backed_up: set = set()
    all_saved: set = set()

    try:
        plan_reply = _stream_with_retry(chat, messages)
        if plan_reply is None:
            print("[creator_plan] stopped: interrupted by user")
            return
        messages.append({"role": "assistant", "content": plan_reply})

        steps = _parse_plan(plan_reply)
        if not steps:
            print("[creator_plan] could not parse a numbered plan (##1. ...) from the reply:")
            print(plan_reply)
            return

        print(f"[creator_plan] plan ({len(steps)} step(s)):")
        for i, s in enumerate(steps, 1):
            print(f"  {i}. {s}")
        print()

        messages[0] = {"role": "system", "content": _STEP_SYSTEM}

        for idx, step_text in enumerate(steps, 1):
            print(f"[creator_plan] step {idx}/{len(steps)}: {step_text}")
            messages.append({"role": "user", "content": f"Step {idx}/{len(steps)}: {step_text}"})

            reply = _stream_with_retry(chat, messages)
            if reply is None:
                print("[creator_plan] stopped: interrupted by user")
                if all_saved:
                    print(f"[creator_plan] files touched: {', '.join(sorted(all_saved))}")
                return
            messages.append({"role": "assistant", "content": reply})

            blocks  = _extract_named_code_blocks(reply)
            named   = [(fn, c) for fn, c, info in blocks if fn]
            unnamed = [c for fn, c, info in blocks if not fn and info.strip().lower() != "bash"]

            # primary convention: ```bash fence, one /read or /run per line
            action_cmds = _extract_bash_commands(blocks)
            save_paths  = []
            # fallback: old ACTION: line convention, still recognized in
            # case a model reaches for it out of habit (see comment above
            # _SAVE_VERBS)
            for m in _ACTION_RE.finditer(reply):
                cmd = m.group(1).strip()
                verb = cmd.split()[0]
                if verb in ("/read", "/run"):
                    action_cmds.append(cmd)
                elif verb in _SAVE_VERBS:
                    arg = cmd.split(None, 1)
                    if len(arg) > 1 and arg[1].strip():
                        save_paths.append(arg[1].strip())
                    else:
                        print(f"[creator_plan] ignored {verb}: missing file path")
                else:
                    print(f"[creator_plan] ignored unsupported action: {cmd}  (only /read, /run, /save_code allowed)")

            # pair save-actions with unnamed fences in the order both appear —
            # matches the observed pattern of "ACTION: /save_code <path>"
            # immediately preceded by an unnamed fence in the same reply.
            for path, content in zip(save_paths, unnamed):
                named.append((path, content))
            if len(save_paths) > len(unnamed):
                print(f"[creator_plan] {len(save_paths) - len(unnamed)} save action(s) had no fence to pair with")
            leftover_unnamed = unnamed[len(save_paths):]

            if leftover_unnamed:
                print(f"[creator_plan] {len(leftover_unnamed)} unnamed fence(s) left — asking model for filename...")
                for content in leftover_unnamed:
                    fn = _ask_filename(chat, content)
                    if fn:
                        print(f"[creator_plan]   -> {fn}")
                        named.append((fn, content))
                    else:
                        print("[creator_plan]   could not determine a filename — content discarded")

            saved = _write_named_blocks(named, backed_up) if named else []
            all_saved.update(saved)

            action_results = []
            for cmd in action_cmds:
                print(f"[creator_plan] action: {cmd}")
                out = _dispatch_isolated(chat, cmd)
                if out:
                    action_results.append(out)

            feedback_parts = []
            if saved:
                feedback_parts.append(f"Saved: {', '.join(saved)}")
            feedback_parts.extend(action_results)
            if feedback_parts:
                messages.append({"role": "user", "content": "\n\n".join(feedback_parts)})

        print(f"\n[creator_plan] done — {len(steps)} step(s) completed")
        if all_saved:
            print(f"[creator_plan] files touched: {', '.join(sorted(all_saved))}")
        else:
            print("[creator_plan] no files were written")
    finally:
        chat.model  = saved_model
        chat.params = saved_params
