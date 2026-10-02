"""creator — fast multi-file code generator, no confirmation prompts.

NOT a supervisor/harness (see flows/creator_plan.py and the still-pending
agents/harness.txt idea for that). This is closer to /fix or /patch: you
describe what you want, it generates named fenced code blocks, and every
named block gets auto-saved to its own file. Good for quickly scaffolding
a project or bulk-generating several files from one description — and, per
feedback on the first version of this file (when it was still called
`harness`), that auto-save-on-any-named-fence convention is exactly what
makes this fast but NOT something to build careful supervision on top of:
nothing stops a model from second-guessing itself and writing several draft
attempts at the same file in one reply, and this loop has no way to tell
"final answer" from "draft" other than "last one in the reply wins" (write
order = file write order, so a later fence for the same path simply
overwrites the earlier one).

Loop shape: write named fence(s) -> auto-save -> "anything else? (no code
block = done)" -> repeat. Structural "done" signal, not self-report: a
reply with NO named code block in it IS "done" — nothing to remember,
nothing for the model to get wrong (unlike agents that ask the model to
literally write "task is done").

No default turn or token cap (see feedback: "чому обмеження в 6 кроків
якщо харнес сам має вирішувати коли зупинятись" / "приберемо обмеження на
700 токенів"). --max-turns/--max-tokens still exist for anyone who wants a
budget, but by default this runs until the model itself stops emitting
named fences, same "-1 = unbounded" convention chat.py's own
_run_agent_loop already uses for /agent.

Reuses the /save_code named-fence convention (```lang path/to/file.ext or
bare ```path/to/file.ext) built into chat.py's core (_fence_filename /
_extract_named_code_blocks). Duplicated here, not imported, so this flow
stays self-contained/stdlib-only — same policy as flows/glossary.py. If you
just want that behavior applied once, standalone, to whatever the model
last said (not a generation loop at all): that's bare `/save_code` in the
main REPL, already built, no separate tool needed for that.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow creator <task>
  /flow creator <task> file: <path>
  /flow creator <task> files: <path1>,<path2>
  /flow creator <task> test: <shell command>
  /flow creator <task> --model <name> --max-turns N --max-tokens N

  file:/files:  seed context with existing file content (optional — without
                it, falls back to a shallow directory listing so the model
                at least knows what filenames exist to reference)
  test:         shell command run after every write round. Exit 0 = pass.
                On failure, stdout+stderr (tail) is fed back for a fix
                round. On pass, asks once more whether anything else is
                needed, then stops on the first no-code-block reply.
  --model       temporarily switch model for this run only (restored after)
  --max-turns   default: unbounded (-1) — pass a number to cap it
  --max-tokens  default: unbounded (provider's own default) — pass a
                number to cap num_predict per turn if a model rambles

Examples:
  /flow creator write fizzbuzz.py that prints 1-100 with fizzbuzz rules
  /flow creator add input validation to login() file: auth.py test: pytest auth_test.py
  /flow creator scaffold 6 files for a Flask blog app files: app.py
  /flow creator fix the bug --model ministral_3:3b test: python calc.py

Safety: files are written without a confirmation prompt (that's the point —
this is meant to run unattended). The first time a run overwrites an
EXISTING file, a pre-change copy is saved as <path>.bkup (never clobbered
again within the same run, so it always reflects the state before this run
started).

Ctrl+C mid-turn does not kill the run: it prompts "Retry this turn? [Y/n]",
and on retry, an optional feedback comment (e.g. "use a dict, not a list")
is injected before resending — same convention as /agent's own interrupt
handling (chat.py's _run_agent_loop) and glossary.py's _on_ctrl_c. Answering
n stops the run cleanly, reporting whatever was already saved.

A reply that stops mid-file (odd number of ``` in the reply — the closing
fence never arrived) is NOT the same failure as "fence with no filename",
and is nudged differently: told to rewrite the file top to bottom rather
than "add a filename" (which was already there and isn't the problem). This
is what happens if --max-tokens is set too low for the file being written;
with no cap by default this should be rare, but a provider can still impose
its own hard output limit regardless of num_predict.

An unnamed but otherwise-complete fence (some small models write correct
code but reliably ignore the naming instruction no matter how it's worded)
is salvaged before falling back to a full corrective retry: one small side
question — "what should this file be called?" — via _ask_filename(), cheap
and isolated, so working code doesn't get thrown away over a formatting
slip. Only if that also fails to produce a usable filename does the old
one-shot _MALFORMED_NUDGE retry kick in.

Not yet wired: automatic project-context assembly via svitovyd/simargl
(deterministic "push relevant files into context" instead of file:/files:
being required) — the shallow-tree fallback below is the placeholder for
that. See concepts/AUTOMATICAL_AGENTS_LOGICAL_EXTERNAL_APPROACH.md.
"""
import itertools as _itertools
import os as _os
import re as _re
import shutil as _shutil
import subprocess as _subprocess

_DEFAULT_MAX_TURNS  = -1     # -1 = unbounded, same convention as _run_agent_loop
_DEFAULT_MAX_TOKENS = None   # None = don't touch num_predict, use provider's own default
_SEED_FILE_CHAR_CAP = 8000     # per seeded file, avoid blowing small-model context
_TEST_OUTPUT_TAIL   = 2000     # chars of test output fed back on failure
_TREE_IGNORE_DIRS   = {".git", "node_modules", "venv", ".venv", "__pycache__",
                        "dist", "build", ".idea", ".vscode", ".1bcoder"}
_TREE_MAX_ENTRIES   = 200

_SYSTEM_PROMPT = """\
You are a coding assistant. You have exactly one way to act: write a file
by putting its full content in a fenced code block with the file's path on
the opening fence line, like this:

```path/to/file.py
<full content of the file>
```

Write one such block per file. The file path and name are MANDATORY on the
opening ``` line — a block with no path is ignored. You may write several
blocks in one reply to touch several files.

When the task is completely finished, reply with plain text and write NO
code block at all. That is the only way to signal you are done — an empty
reply body with no fenced block IS "done", nothing else counts.
"""

_MALFORMED_NUDGE = (
    "You wrote a code block but did not put a file path on the opening "
    "``` line, so it was ignored. Rewrite it as:\n"
    "```path/to/file.ext\n<content>\n```"
)

_TRUNCATED_NUDGE = (
    "Your last reply stopped in the middle of a code block (it never got "
    "closed) — probably ran out of room, not a filename problem. Rewrite "
    "that file's block completely, from the top, in this reply."
)


# ── fence parsing — duplicated from chat.py's _fence_filename/            ──
# ── _extract_named_code_blocks; see module docstring for why             ──

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
    """[(filename_or_None, content), ...] for every CLOSED fence, in order."""
    return [(_fence_filename(m.group(1)), m.group(2))
            for m in _re.finditer(r'```([^\n]*)\n(.*?)```', text, _re.DOTALL)]


def _looks_truncated(text) -> bool:
    """An odd number of ``` markers means the last fence was never closed —
    the reply was cut off mid-file, not a 'forgot the filename' mistake.
    Distinguishing these two matters: they need different corrective nudges
    (see module docstring)."""
    return text.count("```") % 2 == 1


# ── filename salvage — ask a cheap side question instead of discarding    ──
# ── otherwise-correct code (some small models reliably write working code ──
# ── but ignore the "name the fence" instruction no matter how it's worded)──

_FILENAME_ASK_SYSTEM = (
    "You will be shown a code snippet. Reply with ONLY a filename for it — "
    "including a correct extension for its language — and nothing else. "
    "No explanation, no code fence, no quotes, no path unless the code "
    "itself makes a directory obvious."
)
_FILENAME_PATTERN = _re.compile(r'[\w][\w./\\-]*\.[A-Za-z0-9_]{1,10}')
_FILENAME_ASK_SNIPPET_CAP = 1500


def _ask_filename(chat, content: str):
    """One small isolated LLM call: 'what should this be called?' Cheaper
    and safer than throwing away a working code block and re-prompting the
    whole turn. Returns a filename string, or None if the model's reply
    doesn't contain anything filename-shaped."""
    snippet = content[:_FILENAME_ASK_SNIPPET_CAP]
    msgs = [
        {"role": "system", "content": _FILENAME_ASK_SYSTEM},
        {"role": "user", "content": f"```\n{snippet}\n```"},
    ]
    reply = chat._stream_chat(msgs) or ""
    m = _FILENAME_PATTERN.search(reply)
    return m.group(0) if m else None


# ── context assembly — pushed, not pulled (see module docstring) ────────

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
    """Fallback context when no file:/files: is given — a depth-2 filename
    listing so the model at least knows what exists to reference. Not a
    substitute for real relevant-file selection (svitovyd/simargl) — see
    module docstring."""
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


# ── writing — deterministic, no confirmation prompt (see docstring) ─────

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
            print(f"[creator] saved -> {path}")
        except OSError as e:
            print(f"[creator] error writing {path}: {e}")
    return saved


def _run_test(cmd: str):
    """Return (passed: bool, output_tail: str)."""
    try:
        proc = _subprocess.run(cmd, shell=True, capture_output=True,
                               encoding="utf-8", errors="replace", timeout=120)
    except _subprocess.TimeoutExpired:
        return False, "(test command timed out after 120s)"
    output = (proc.stdout + proc.stderr).strip()
    if len(output) > _TEST_OUTPUT_TAIL:
        output = "...\n" + output[-_TEST_OUTPUT_TAIL:]
    return proc.returncode == 0, output


# ── arg parsing ───────────────────────────────────────────────────────────

def _pop_flag(rest: str, pattern: str):
    m = _re.search(pattern, rest)
    if not m:
        return None, rest
    return m.group(1), (rest[:m.start()] + rest[m.end():]).strip()


def _parse_args(args: str):
    model, args      = _pop_flag(args, r'--model\s+(\S+)')
    max_turns, args  = _pop_flag(args, r'--max-turns\s+(\d+)')
    max_tokens, args = _pop_flag(args, r'--max-tokens\s+(\d+)')
    max_turns  = int(max_turns) if max_turns else _DEFAULT_MAX_TURNS
    max_tokens = int(max_tokens) if max_tokens else _DEFAULT_MAX_TOKENS

    test_cmd = None
    m = _re.search(r'\btest:\s*(.+)$', args, _re.IGNORECASE | _re.DOTALL)
    if m:
        test_cmd = m.group(1).strip()
        args = args[:m.start()].strip()

    files = []
    m = _re.search(r'\bfiles?:\s*(.+)$', args, _re.IGNORECASE | _re.DOTALL)
    if m:
        files = [f.strip() for f in _re.split(r'[,\s]+', m.group(1).strip()) if f.strip()]
        args = args[:m.start()].strip()

    return args.strip(), files, test_cmd, model, max_turns, max_tokens


# ── Ctrl+C handling — same convention as glossary.py's _on_ctrl_c and     ──
# ── chat.py's _run_agent_loop: retry the current turn, optionally with a  ──
# ── feedback comment injected first, or quit outright.                   ──

def _prompt_interrupt():
    """On a Ctrl+C sentinel (reply is None) from chat._stream_chat: ask
    whether to retry this turn, optionally with a feedback comment.
    Returns None to mean 'quit', "" to mean 'retry, no comment', or a
    non-empty string to mean 'retry, inject this comment first'."""
    try:
        choice = input("  [creator] interrupted. Retry this turn? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice.startswith("n"):
        return None
    try:
        note = input("  Add feedback for the retry (blank = none): ").strip()
    except (EOFError, KeyboardInterrupt):
        note = ""
    return note


# ── main loop ─────────────────────────────────────────────────────────────

def run(chat, args: str) -> None:
    args = args.strip()
    if not args:
        print(__doc__)
        return

    task, files, test_cmd, model_override, max_turns, max_tokens = _parse_args(args)
    if not task:
        print("usage: /flow creator <task> [file: <path> | files: <p1>,<p2>] "
              "[test: <cmd>] [--model <name>] [--max-turns N] [--max-tokens N]")
        return

    context = _build_context(files)
    user_msg = f"Task: {task}\n\nContext:\n{context}"
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    saved_model  = chat.model
    saved_params = dict(chat.params)
    if model_override:
        chat.model = model_override
    if max_tokens is not None:
        chat.params["num_predict"] = max_tokens

    backed_up: set = set()
    all_saved: set = set()
    malformed_retry_used  = False
    truncated_retry_used  = False

    turns_label = "unbounded" if max_turns == -1 else str(max_turns)
    tokens_label = "unbounded" if max_tokens is None else str(max_tokens)
    print(f"[creator] model: {chat.model}  max_turns: {turns_label}  max_tokens: {tokens_label}")
    if test_cmd:
        print(f"[creator] test: {test_cmd}")
    print(f"[creator] task: {task}\n")

    turn_iter = _itertools.count(1) if max_turns == -1 else range(1, max_turns + 1)

    try:
        for turn in turn_iter:
            print(f"[creator] turn {turn}/{turns_label}")
            reply = chat._stream_chat(messages)
            if reply is None:   # Ctrl+C sentinel — same convention as _run_agent_loop
                action = _prompt_interrupt()
                if action is None:
                    print("[creator] stopped: interrupted by user")
                    if all_saved:
                        print(f"[creator] files touched: {', '.join(sorted(all_saved))}")
                    return
                if action:   # non-empty comment to inject before retrying this turn
                    messages.append({"role": "user", "content": action})
                continue   # retry: messages already reflects pre-turn state, just resend
            messages.append({"role": "assistant", "content": reply})

            if _looks_truncated(reply) and not truncated_retry_used:
                truncated_retry_used = True
                malformed_retry_used = False
                print("[creator] truncated: reply cut off mid-file — one corrective retry")
                messages.append({"role": "user", "content": _TRUNCATED_NUDGE})
                continue
            truncated_retry_used = False

            blocks  = _extract_named_code_blocks(reply)
            named   = [(fn, c) for fn, c in blocks if fn]
            unnamed = [c for fn, c in blocks if not fn]

            if unnamed:
                print(f"[creator] {len(unnamed)} unnamed fence(s) — asking model for filename...")
                still_unnamed = []
                for content in unnamed:
                    fn = _ask_filename(chat, content)
                    if fn:
                        print(f"[creator]   -> {fn}")
                        named.append((fn, content))
                    else:
                        still_unnamed.append(content)
                unnamed = still_unnamed

            if not named:
                if unnamed and not malformed_retry_used:
                    # filename-asking couldn't resolve it either -> unambiguous
                    # formatting slip, not an intentional "done" signal
                    malformed_retry_used = True
                    print("[creator] malformed: fence with no filename — one corrective retry")
                    messages.append({"role": "user", "content": _MALFORMED_NUDGE})
                    continue
                print(f"[creator] done — no code block in reply (turn {turn})")
                if all_saved:
                    print(f"[creator] files touched: {', '.join(sorted(all_saved))}")
                else:
                    print("[creator] no files were ever written")
                return

            saved = _write_named_blocks(named, backed_up)
            all_saved.update(saved)
            malformed_retry_used = False

            if test_cmd:
                passed, output = _run_test(test_cmd)
                if passed:
                    print("[creator] test: PASS")
                    messages.append({"role": "user", "content":
                        f"Saved: {', '.join(saved)}\nTest passed.\n"
                        f"Anything else needed? If the task is complete, reply with no code block."})
                else:
                    print("[creator] test: FAIL")
                    messages.append({"role": "user", "content":
                        f"Saved: {', '.join(saved)}\nTest failed:\n{output}\n"
                        f"Fix the problem. Write only the file(s) that need to change."})
            else:
                messages.append({"role": "user", "content":
                    f"Saved: {', '.join(saved)}\n"
                    f"Anything else needed? If the task is complete, reply with no code block."})

        print(f"[creator] stopped: max turns ({max_turns}) reached without an explicit completion signal")
        if all_saved:
            print(f"[creator] files touched: {', '.join(sorted(all_saved))}")
    finally:
        chat.model  = saved_model
        chat.params = saved_params
