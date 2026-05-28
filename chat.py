#!/usr/bin/env python3
"""1bcoder — AI coder for 1B models

(c) 2026 Stanislav Zholobetskyi
Institute for Information Recording, National Academy of Sciences of Ukraine, Kyiv
Створено в рамках аспірантського дослідження на тему:
"Інтелектуальна технологія підтримки розробки та супроводу програмних продуктів"
"""

import re
import os
import sys
import io
import json
import argparse
import threading
import subprocess
import difflib
import warnings
warnings.filterwarnings("ignore", message="urllib3", category=Warning)
import requests

# ── terminal colors ────────────────────────────────────────────────────────────

if sys.platform == "win32":
    os.system("")  # enable ANSI in Windows console

_R = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_RED   = "\033[31m"
_GREEN = "\033[32m"
_YELL  = "\033[33m"
_CYAN  = "\033[36m"
_GRAY  = "\033[90m"
_LBLUE = "\033[94m"


def _ok(msg: str):   print(f"{_GREEN}{msg}{_R}")
def _err(msg: str):  print(f"{_RED}error: {msg}{_R}")
def _info(msg: str): print(f"{_CYAN}{msg}{_R}")
def _warn(msg: str): print(f"{_YELL}{msg}{_R}")


def _multiline_input() -> tuple:
    """Open a multi-line text editor in the terminal.

    Uses prompt_toolkit if available (Alt+Enter / Esc→Enter to submit).
    Falls back to a line-by-line collector terminated by '/end' on its own line.
    Returns (text, prompt_name_or_None):
      prompt_name is None  — do not save as /prompt
      prompt_name is ""    — save as /prompt, name not yet known (caller should ask)
      prompt_name is <str> — save as /prompt with this name
    """
    try:
        from prompt_toolkit import prompt as _pt_prompt
        from prompt_toolkit.key_binding import KeyBindings as _KB

        kb = _KB()
        _QUICKSAVE_FILE = os.path.join('.1bcoder', 'task.txt')
        _prompt_flag = [False]  # set by Ctrl+T

        @kb.add('c-d')
        def _submit(event):
            event.current_buffer.validate_and_handle()

        @kb.add('c-t')
        def _save_as_prompt(event):
            _prompt_flag[0] = True
            event.current_buffer.validate_and_handle()

        @kb.add('enter')
        def _enter_or_end(event):
            buf = event.current_buffer
            current_line = buf.document.current_line.strip()
            if (current_line in ('/end', '/save') or current_line.startswith('/save ')
                    or current_line == '/prompt' or current_line.startswith('/prompt ')):
                buf.validate_and_handle()
            else:
                buf.newline()

        print("[multiline — Enter = new line · Ctrl+D or /end to submit"
              " · /save [file] to save · Ctrl+T or /prompt [name] to save as /prompt"
              " · Ctrl+C to cancel]")
        try:
            text = _pt_prompt('··· ', multiline=True, key_bindings=kb)
        except KeyboardInterrupt:
            print()
            return "", None

        # strip trailing empty lines, check for /end, /save, /prompt commands
        lines = text.split('\n')
        while lines and lines[-1].strip() == '':
            lines.pop()

        save_filename = None
        prompt_name = None

        if _prompt_flag[0]:
            prompt_name = ""  # Ctrl+T: save as prompt, name unknown

        if lines:
            last = lines[-1].strip()
            if last == '/end':
                lines.pop()
            elif last == '/save':
                save_filename = ''
                lines.pop()
            elif last.startswith('/save '):
                save_filename = last[6:].strip()
                lines.pop()
            elif last == '/prompt':
                prompt_name = ""
                lines.pop()
            elif last.startswith('/prompt '):
                prompt_name = last[8:].strip()
                lines.pop()

        text = '\n'.join(lines)

        if save_filename == '' and text.strip():
            os.makedirs('.1bcoder', exist_ok=True)
            with open(_QUICKSAVE_FILE, 'w', encoding='utf-8') as _f:
                _f.write(text)
            print(f"  [text] saved → {_QUICKSAVE_FILE}")
        elif save_filename and text.strip():
            with open(save_filename, 'w', encoding='utf-8') as _f:
                _f.write(text)
            print(f"  [text] saved → {save_filename}")
        return text, prompt_name

    except ImportError:
        print("[multiline — type /end to submit · /prompt [name] to save as /prompt · Ctrl+C to cancel]")
        lines = []
        prompt_name = None
        while True:
            try:
                line = input("··· ")
            except (EOFError, KeyboardInterrupt):
                print()
                return "", None
            stripped = line.strip()
            if stripped == "/end":
                break
            if stripped == "/prompt":
                prompt_name = ""
                break
            if stripped.startswith("/prompt "):
                prompt_name = stripped[8:].strip()
                break
            lines.append(line)
        return "\n".join(lines), prompt_name


def _fts_rank(terms: list, file_contents: dict, top_k: int = 10) -> list:
    """Rank files by BM25 using in-memory FTS5.

    Args:
        terms: list of search terms
        file_contents: {rel_path: text} — only pre-filtered candidates
        top_k: max results to return

    Returns:
        list of (rel_path, score) sorted best-first (score is raw FTS5 rank, negative)
    """
    import sqlite3 as _sqlite3
    db = _sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE t USING fts5(path UNINDEXED, content)")
    db.executemany("INSERT INTO t VALUES (?, ?)", file_contents.items())
    fts_query = " OR ".join(f'"{t}"' for t in terms)
    rows = db.execute(
        "SELECT path, rank FROM t WHERE t MATCH ? ORDER BY rank LIMIT ?",
        (fts_query, top_k)
    ).fetchall()
    db.close()
    return rows


class _Tee:
    """Tee stdout to both terminal and an internal buffer."""
    def __init__(self):
        self._orig = sys.stdout
        self._buf  = io.StringIO()

    def write(self, s: str):
        self._orig.write(s)
        self._buf.write(s)

    def flush(self):
        self._orig.flush()

    def getvalue(self) -> str:
        return self._buf.getvalue()

    def __enter__(self):
        sys.stdout = self
        return self

    def __exit__(self, *_):
        sys.stdout = self._orig


def _cdiff(line: str) -> str:
    """Colorize a single unified-diff or map-diff line for terminal display."""
    if line.startswith(("--- ", "+++ ")):
        return f"{_DIM}{line}{_R}"
    if line.startswith("@@"):
        return f"{_CYAN}{line}{_R}"
    if line.startswith("+"):
        return f"{_GREEN}{line}{_R}"
    if line.startswith("-"):
        return f"{_RED}{line}{_R}"
    if line.startswith("!"):
        return f"{_YELL}{line}{_R}"
    return line


# ── constants ──────────────────────────────────────────────────────────────────

BANNER = """\
 ██╗██████╗        ██████╗ ██████╗ ██████╗ ███████╗██████╗
███║██╔══██╗      ██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗
╚██║██████╔╝█████╗██║     ██║   ██║██║  ██║█████╗  ██████╔╝
 ██║██╔══██╗╚════╝██║     ██║   ██║██║  ██║██╔══╝  ██╔══██╗
 ██║██████╔╝      ╚██████╗╚██████╔╝██████╔╝███████╗██║  ██║
 ╚═╝╚═════╝        ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝\
"""

WORKDIR   = os.getcwd()
BCODER_DIR = os.path.join(WORKDIR, ".1bcoder")           # project-local
HOME_BCODER_DIR   = os.path.join(os.path.expanduser("~"), ".1bcoder")  # user home global
INSTALL_BCODER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bcoder_data")  # wheel defaults

SCRIPTS_DIR        = os.path.join(BCODER_DIR, "scripts")
CTX_DIR            = os.path.join(BCODER_DIR, "ctx")
PROJECTS_DIR       = os.path.join(BCODER_DIR, "projects")
GLOBAL_SCRIPTS_DIR = os.path.join(HOME_BCODER_DIR, "scripts")
PROMPTS_FILE       = os.path.join(HOME_BCODER_DIR, "prompts.txt")
PROC_DIR           = os.path.join(HOME_BCODER_DIR, "proc")
LOCAL_PROC_DIR     = os.path.join(BCODER_DIR, "proc")
TEAMS_DIR          = os.path.join(HOME_BCODER_DIR, "teams")
LOCAL_TEAMS_DIR    = os.path.join(BCODER_DIR, "teams")
NUM_CTX    = 8192        # default Ollama context window (tokens)
TIMEOUT    = 120         # default HTTP read timeout in seconds

# ── /agent settings ─────────────────────────────────────────────────────────────

AGENT_CONFIG_FILE   = os.path.join(BCODER_DIR, "agent.txt")
ALIASES_FILE        = os.path.join(BCODER_DIR, "aliases.txt")
CONFIG_FILE         = os.path.join(BCODER_DIR, "config.yml")
GLOBAL_CONFIG_FILE  = os.path.join(HOME_BCODER_DIR, "config.yml")
GLOBAL_ALIASES_FILE  = os.path.join(HOME_BCODER_DIR, "aliases.txt")
AGENTS_DIR           = os.path.join(BCODER_DIR, "agents")
GLOBAL_AGENTS_DIR    = os.path.join(HOME_BCODER_DIR, "agents")
PROFILES_FILE        = os.path.join(BCODER_DIR, "profiles.txt")
GLOBAL_PROFILES_FILE = os.path.join(HOME_BCODER_DIR, "profiles.txt")

DEFAULT_AGENT_TOOLS = [
    "read", "readln", "tree", "find", "insert", "save", "fim",
]

DEFAULT_AGENT_TOOLS_ADVANCED = [
    "read", "run", "insert", "save", "bkup", "diff", "patch",
    "tree", "find",
    "map index", "map find", "map idiff", "map diff", "map trace", "map keyword",
    "help",
]

DEFAULT_AGENT_TOOLS_ASK = [
    "read", "readln", "tree", "find",
    "map index", "map find", "map trace", "map keyword",
]

ASK_RESULT_LIMIT_CHARS = 4000   # ~1K tokens — truncate if result exceeds this
ASK_RESULT_SHOW_CHARS  = 2000   # ~500 tokens shown when truncated

AGENT_SYSTEM_BASIC = """\
You are a coding assistant. Complete the task using the available tools.

To call a tool, write ACTION: on its own line. Wait for [tool result].
Run one action at a time until the task is complete.
When done, write a short summary with no ACTION and end with: Task is complete.

To fix or edit a file:
  ACTION: /fim <file> <description of what to fix>
  Example: ACTION: /fim calc.py fix the division by zero bug
  Example: ACTION: /fim main.py fix the indentation on line 5

To read a file:
  ACTION: /read <file>

To create or fully replace a file: write the full code block, then:
  ACTION: /save <file> code

Available tools:
{tool_list}
"""

AGENT_SYSTEM_ADVANCED = """\
You are an autonomous coding assistant. Complete the task using the available tools.

To call a tool, write ACTION: followed by the command. Stop and wait for [tool result].
When the task is complete, write a short summary and end with: Task is complete.

How to write files (the keyword "code" means: use the last ``` ``` block written above):
- To MODIFY an existing file: write a SEARCH/REPLACE block, then ACTION: /patch <file> code
  Or use FIM for targeted fixes: ACTION: /fim <file> <line or start-end> <hint>
- To INSERT new code at a line: write the code block, then ACTION: /insert <file> <line> code
- To CREATE or fully REPLACE a file: write the full code block, then ACTION: /save <file> code

SEARCH/REPLACE format:
<<<<<<< SEARCH
exact lines to replace
=======
new lines
>>>>>>> REPLACE

Rules:
- /readln a file before editing it
- /bkup save <file> before modifying important files
- /run to test after applying a fix

Available tools:
{tool_list}
"""

AGENT_SYSTEM_ASK = """\
You are a code research assistant. Explore the project to answer the question.

To call a tool, write ACTION: on its own line followed by the command.
Wait for [tool result] before calling the next tool.
Build understanding from broad to narrow. One ACTION per turn.
When done, write a plain text answer or plan. Do not write any ACTION when done.

Strategy:
1. If project structure is not yet in context use ACTION: /tree
2. To locate relevant identifiers use ACTION: /map find \keyword  (single word, no quotes)
3. To find real code identifiers from a phrase use ACTION: /map keyword extract phrase -f -c  (no quotes)
4. To search file content use a single keyword e.g. ACTION: /find keyword -c
5. To read specific sections use ACTION: /read file start-end
6. Stop when you have enough to answer

Never edit files. Output a clear report or plan when done.

Available tools:
{tool_list}
"""


# ── /map settings ───────────────────────────────────────────────────────────────

import map_index
import map_query


FIX_SYSTEM = (
    "You are a code repair tool. "
    "Respond with ONLY the single most important fix in this exact format:\n"
    "LINE <number>: <corrected line content>\n"
    "One fix only. No explanation. No other text."
)

PATCH_SYSTEM = (
    "You are a code editor. Output ONLY a single SEARCH/REPLACE block.\n"
    "SEARCH must be an exact copy of consecutive lines from the file — "
    "whitespace and indentation matter.\n"
    "Use this exact format:\n"
    "<<<<<<< SEARCH\n"
    "exact lines to replace\n"
    "=======\n"
    "replacement lines\n"
    ">>>>>>> REPLACE\n"
    "Follow the SEARCH/REPLACE format. Do not forget the SEARCH and REPLACE keywords. "
    "Place the word SEARCH after <<<<<<< and ======= separates the two blocks. "
    "Place REPLACE after >>>>>>>.\n"
    "No explanation. No other text. One block only."
)

HELP_TEXT = """\
Commands

/tree [path] [-d <depth>] [ctx]
    Show directory tree rooted at path (default: current directory).
    Depth defaults to 4. Pass ctx to inject into AI context (or answer Y/n prompt).
    e.g.  /tree
          /tree src
          /tree src/java/com -d 6
          /tree static ctx

/find <pattern> [-f] [-c] [-i] [--ext <ext>] [ctx]
    Search filenames and file content for <pattern> (regex supported).
    After showing results, asks "Add results to context?" (Y/n).
    Pass ctx to skip the prompt and inject automatically.
    Sets {{find_files}} to a comma-separated list of matched file paths.
    Flags: -f filenames only · -c content only · -i case-insensitive
           --ext py  filter by file extension (no dot needed)
    Directories starting with . are excluded automatically.
    e.g.  /find MyClass
          /find user_id -c -i
          /find config --ext py ctx
          /find \.connect\( -c
/find <term1> [term2 ...] -r [--ext <ext>]
    Ranked search using BM25 (in-memory FTS5). Returns top-10 files by relevance.
    Terms are space- or comma-separated. No regex — plain keyword matching.
    Sets {{find_files}} to a comma-separated list of matched file paths.
    e.g.  /find login auth -r
          /find UserService,register -r
          /find migration schema -r --ext py

/read <file> [file2 ...] [start-end]
    Inject file(s) into AI context without line numbers (clean text).
    Range (start-end) only applies when reading a single file.
    Files can be space- or comma-separated (e.g. /read {{find_files}} or /read {{map_files}}).
    e.g.  /read main.py
          /read main.py 10-30
          /read instruction.txt README.md main.py
          /read {{find_files}}

/readln <file> [file2 ...] [start-end]
    Same as /read but includes line numbers (useful for /patch and /fix).
    Files can be space- or comma-separated.
    e.g.  /readln main.py
          /readln models.py 40-60
          /readln {{find_files}}

/view [--keep] <image> [image2 ...] [prompt]
    Inject image(s) for multimodal models (qwen2-vl, llava, moondream, etc.).
    Supported formats: png jpg jpeg gif webp bmp.
    With prompt  — injects image and immediately calls the model.
    Without prompt — injects silently; ask your question in the next message.
    --keep  retain raw image bytes in context after the reply (default: strip).
    Default (no --keep): after the model replies, the base64 image is removed
    from context — only the text prompt and reply remain. Use this for agents
    that process many images without overflowing the context window.
    Works with @ file picker. Multiple images share one model call.
    e.g.  /view screenshot.png what do you see?
          /view diagram.png explain this architecture
          /view @ is there a dog in this image? YES or NO
          /view --keep ref.png before.png after.png what changed?

/text
    Open a multi-line text editor and send the composed text to the AI.
    Enter = new line. To submit, use one of:
      Ctrl+D                — submit without saving
      /end                  — submit without saving (type on its own line, then Enter)
      /save                 — save to .1bcoder/task.txt and submit
      /save <filename>      — save to custom file and submit
      Ctrl+T                — save as /prompt (asks for name after editor closes) and submit
      /prompt <name>        — save as named /prompt and submit (type on its own line)
      /prompt               — save as /prompt, name will be asked after editor closes
    Ctrl+C cancels without sending.
    If prompt_toolkit is installed: full editing with arrow keys, Home/End, scroll.
    Fallback (no prompt_toolkit): same commands work line by line.
    Useful for pasting multi-paragraph task descriptions, code snippets, or long instructions.

/edit <file> <line>
    Manually replace a line. Type new content when prompted.
    e.g.  /edit main.py 15
/edit <file> code
    Apply last AI reply (first code block) to the whole file.
    Creates the file if it does not exist. Shows unified diff before applying.
    e.g.  /edit main.py code
/edit <file> <line> code
    Apply last AI reply code block starting at <line>.
    Replaces as many lines as the new code has. Creates file if missing. Shows diff.
    e.g.  /edit main.py 312 code
/edit <file> <start>-<end> code
    Apply last AI reply code block replacing exactly lines start–end.
    Most precise form — use when you know the exact line range.
    e.g.  /edit main.py 1-4 code

/insert <file> <line>
    Insert last AI reply before line N (full text, no code extraction).
    e.g.  /insert notes.txt 5
/insert <file> <line> code
    Insert extracted code block from last AI reply before line N.
    e.g.  /insert main.py 14 code
/insert <file> <line> <inline text>
    Insert literal text directly (anything that is not the keyword "code").
    e.g.  /insert main.py 14 SET_SLEEP_DELAY = 10
          /insert config.py 1 import os

/fix <file> [start-end] [hint]
    AI proposes one-line fix. Shows diff before applying.
    e.g.  /fix main.py
          /fix main.py 2-2
          /fix main.py 2-2 wrong operator

/fim <file> [line or start-end] [-w N] [hint]
    FIM-based fix. Marks the line(s), passes file to model, diffs the result.
    Without a line number: passes the whole file with hint only (agent fallback mode).
    -w N  pass only N lines of context around the marked section (for large files).
    e.g.  /fim main.py 3
          /fim main.py 3 replace != with ==
          /fim main.py 3-5 fix the logic
          /fim huge.py 14567 -w 20 fix indentation
          /fim main.py fix the division by zero bug

/patch <file> [start-end] [hint]
    AI proposes a multi-line SEARCH/REPLACE edit. Shows unified diff before applying.
    Better for 7B+ models. Use /fix for 1B models.
    e.g.  /patch main.py
          /patch main.py 10-40
          /patch main.py 10-40 fix the loop logic
/patch <file> code
    Apply SEARCH/REPLACE block from the last AI reply directly (no new LLM call).
    Use in agent mode: write the block in the reply, then ACTION: /patch <file> code
    e.g.  /patch main.py code

/run <command>
    Run shell command, inject output into context.
    e.g.  /run python main.py

/save <file> [file2 ...] [code] [mode]
    Save last AI reply to file(s). Keywords can appear in any order.
    Files can be space- or comma-separated.
    /save <file>                     — full reply, overwrite
    /save <file> code                — extract first ```...``` block
    /save f1 f2 code                 — extract block 1 → f1, block 2 → f2
    /save f1 f2 f3 code              — block 1 → f1, block 2 → f2 & f3
    Modes (apply to all files):
      overwrite (default), append-above / -aa, append-below / -ab, add-suffix
    e.g.  /save out.txt
          /save main.py code
          /save index.html style.css code
          /save main.py code -ab     -> appends extracted code below
          /save out.txt add-suffix   -> out_1.txt, out_2.txt ...
          /save @/result.txt code    -> pick dir with @/, save chosen_dir/result.txt
          /save @/r1.txt @/r2.txt code  -> pick dir once, save two files there

/script list              List all scripts (* = current). Shows global scripts (g:) and project scripts.
/script open              Select and load a script (type number). Includes global and project scripts.
    Script format: one command per line. Lines starting with [v] are done (skipped).
                 Lines starting with # are comments (skipped).
/script create [path]          Create a new empty script.
/script create ctx [path]      Create script from this session's command history.
    Records all /read /edit /fix /patch /run /save /bkup /map /model /host commands typed so far.
    Session-only commands (/ctx /clear /script /help /init /exit) are excluded.
    e.g.  /script create
          /script create fix-bug.txt
          /script create ctx
          /script create ctx my-workflow.txt
/script show [N]          Display steps of the current script. If N given, open script N from list first.
/script add <command>     Append a step to the current script.
    e.g.  /script add /fix main.py 2-2 fix indentation
/script clear             Wipe current script completely.
/script reset             Unmark all done steps.
/script reapply [key=value ...]   Reset all done steps then apply the plan automatically.
/script refresh           Reload script from disk and show contents.
/script run <file> [key=value ...]        Run all steps automatically (shorthand for apply -y).
/script apply [file] [key=value ...]     Run steps one by one (Y/n/q per step).
/script apply -y [file] [key=value ...]  Run all pending steps automatically.
    Parameters substitute {{key}} placeholders in script steps.
    Missing parameters are prompted interactively.
    e.g.  /script run simargl-find.txt query="add author field" mode=file
          /script apply -y collect.txt
          /script apply fix-fn.txt file=calc.py range=1-4

/prompt save <name>   Save the last user message as a reusable prompt template.
                      Name becomes the filename (no spaces, .txt added automatically).
/prompt load [N]      Show numbered list of saved prompts; select by number inline or interactively.
                      {{param}} placeholders are prompted interactively before injecting.
    e.g.  /prompt save ConvertJavaToPy
          /prompt load
          /prompt load 2

/proc list              List available post-processors with one-line descriptions.
/proc help <name>       Show full docstring / usage for a processor.
/proc run <name> [-f <file>]  Run processor against last LLM reply (or file with -f).
/proc run <name> translated   Run processor against last translated reply (from /translate).
/proc on <name>         Persistent mode: run processor after every LLM reply automatically.
/proc off               Stop persistent processor.
/proc before on <name>  Before-proc: run processor BEFORE every LLM call to enrich agent context.
                        Plain text output is injected as [context]. ACTION: lines are executed
                        and the resulting LLM reply is injected as [context] (not added to main ctx).
/proc before off        Stop before processor.
/proc gate on <name> [args ...]  Gate: run processor AFTER reply; FAIL retries current plan step.
/proc gate off          Stop gate processor.
/proc new <name>        Create a new processor from template.
    Processor protocol: stdin = last LLM reply. stdout = result (injected to context).
    Output lines "key=value" are extracted as params.
    Output line "ACTION: /command" is confirmed with user then executed (run mode only).
    Output line "ALERT: message"   prints a warning but continues.
    Output line "BLOCK: reason"    cancels the triggering command (hooks only).
    Output line "FAIL: reason"     gate mode: retries plan step; proc mode: prints warning.
    Output line "CAPTURE: text"   sets $ to text; use with ACTION: /parallel $ to avoid quoting issues.
    Exit code non-zero = show stderr as warning, skip ACTION.
    Env vars available in all proc subprocesses:
      BCODER_WORKDIR      current working directory
      BCODER_TASK         current agent task string (set when inside /agent or /ask)
      BCODER_CTX_USED     estimated tokens used in main context
      BCODER_CTX_MAX      configured context limit
      BCODER_CTX_PCT      usage percentage
    e.g.  /proc run extract-files
          /proc on grounding-check
          /proc off
          /proc before on assist /short
          /proc gate on action-required
          /proc gate on pattern-gate "@Query" "use CriteriaBuilder only"
    Guard processors (run via /proc on or /hook before <cmd>):
          /proc on ctx_cut            auto /ctx cut when context exceeds 90%
          /proc on ctx_cut 80         custom threshold
          /proc on rude_words         alert if LLM reply contains profanity
          /proc on rude_words ua      + Ukrainian word list
          /proc on secret_check       alert if reply contains sensitive names (google, anthropic...)
          /proc on secret_check client=acme,invoice   add custom keywords
          /hook before run   sql_readonly_guard.py      block /run if SQL contains DELETE/DROP/TRUNCATE/UPDATE
          /hook before patch auto-bkup.txt              backup file to <file>.bkup before every /patch
          /hook before edit  auto-bkup.txt              backup file to <file>.bkup before every /edit
          /hook after  patch edit-control.txt           /diff + /map idiff after every /patch
          /hook after  edit  edit-control.txt           /diff + /map idiff after every /edit
    regexp-extract <pattern> [-i] [-u] [-g N]  extract regex matches from last reply
          # find all 3-digit numbers
          /proc run regexp-extract \b[0-9]{3}\b
          # extract function names (unique, capture group 1)
          /proc run regexp-extract "def (\w+)\(" -g 1 -u
          # find class names case-insensitive, no duplicates
          /proc run regexp-extract \b[A-Z]\w+Service\b -u -i
          # collect all .py file paths mentioned
          /proc run regexp-extract [\w./\\-]+\.py -u
          # render last reply as Markdown in terminal (requires: pip install rich)
          /proc run md
          # render last reply as Markdown + LaTeX + Mermaid in browser
          /proc run mdx
          # render translated reply in browser (after /translate last or with auto-translate on)
          /proc run mdx translated

/translate setup [lang:<code>] [mode:online|mini|offline|lm] [host:<url>] [model:<name>] [profile:<name>]
/translate on | off | status
/translate last [mode:<m>] [lang:<code>]  Retranslate last reply with optional override.
    online  = Google Translate — WARNING: sends text to Google, not for confidential projects
    mini    = argostranslate (local, ~100MB packages, fully private)
    offline = NLLB-200 ctranslate2 (local, ~2.4GB download, ~600MB after conversion, better quality, no model switch)
    lm      = local/remote Ollama model (fully private, configurable host + model)
    Language codes: uk (Ukrainian), de (German), fr (French), pl (Polish), es (Spanish), hi (Hindi)
    Full list: /doc translate
    Config stored globally in ~/.1bcoder/translate.json — persists across sessions.
    e.g.  /translate setup lang:uk mode:lm host:192.168.0.5:11434 model:translategemma:4b
          /translate setup host:openai://localhost:1234 model:translategemma:4b
          /translate setup lang:uk mode:offline
          /translate last
          /translate last mode:mini lang:de
          /translate off

/visual setup host:<url> model:<name> [timeout:<s>] [profile:<name>]
    Configure a dedicated vision model (separate from the main model).
    Config stored globally in ~/.1bcoder/visual.json — persists across sessions.
/visual status
    Show current vision model configuration.
/visual explain [<image>] [prompt] [-y]
    Send image to the vision model in an isolated context (no conversation history shared).
    If no image file given, uses the last image injected via /view.
    Result is shown, sets $, then asks "Inject into context? [Y/n]".
    -y or agent mode: auto-injects without asking.
    Use this to get a text description from a vision model (moondream, qwen2-vl, llava)
    and pass it to a non-vision main model (nemotron, deepseek-coder, etc.).
    e.g.  /visual setup host:localhost:11434 model:moondream:v2
          /visual setup host:localhost:11434 model:qwen3-vl:4b timeout:300
          /visual setup profile:moondream
          /visual explain screenshot.png
          /visual explain diagram.png describe the architecture
          /visual explain -y
          /view diagram.png
          /visual explain what technologies are shown?

/web search <term> [-> varname]   Search DuckDuckGo; results shown + URLs saved to variable.
/web fetch <url> [-n 4000]        Fetch URL, strip HTML, inject cleaned text into context.
    e.g.  /web search python asyncio tutorial -> urls
          /web fetch https://docs.python.org/3/library/asyncio.html

/flow list                List available flows (built-in + global + local).
/flow <name> [args]       Run a flow — deterministic Python pipeline with loops and lists.
    Flows live in _bcoder_data/flows/ (built-in) or ~/.1bcoder/flows/ (global/local).
    Built-in flows: webask, grounding, simargl_files, py_error_trace, commit_message
    e.g.  /flow webask what is asyncio -d 5
          /flow grounding dependency injection
          /flow simargl_files add user authentication
          /flow py_error_trace -f error.txt
          /flow commit_message

/var save <file>            Save all session variables to a key=value file (auto adds .var if no ext).
                             First line is always "# <description>" — set with /var set description=...
                             Agents use /read file.var 1-1 to read only the description and decide relevance.
/var load <file>            Load variables from a key=value file (.var or any text). Skips # comments.
/var set <name> [<key>]     Capture a variable from the last proc output.
                             With <key>: reads the "key=value" line from proc stdout.
                             Without <key>: takes the first non-param line of proc output
                             (or first line of last LLM reply if no proc ran).
/var set <name> =<value>    Set a literal value directly.
/var get                    List all active session variables.
/var extract                Show all {{placeholders}} in the current script with set/NaN status.
/var extract <file>         Same, but scan any .txt or .md file instead of the open script.
/var def <name> [<name2> ...]  Declare variable(s) with NaN value (skips if already set).
/var del <name>             Remove a variable.
    Variables expand as {{name}} in any command, script step, or agent prompt.
    Plan apply merges session vars as defaults; explicit key=value params override them.
    e.g.  /proc run regexp-extract [\w./\\-]+\.py -u
          /var set file_list matches
          /script apply -y fix-fn.txt
          /read {{file_list}}
          /agent explain {{topic}} -y
          /var set project_name =MyService
          /var get
          /var extract
Output capture operators (work with any command — LLM reply, tool, proc):
  <command> -> <varname>   Capture all output of <command> into session variable <varname>.
  $                        Expand to the last captured output (last AI reply or tool output).
  ~                        Expand to the last user input (last message or command typed).
  @[dir]                   File picker: show numbered tree (rooted at dir or . by default),
                           type a number to select; the @[dir] token is replaced by the chosen path.
  @/[suffix]               Directory picker: show numbered tree of directories,
                           type a number; @/suffix → chosen_dir/suffix.
                           Multiple @/suffix tokens in one command share the same pick.
    e.g.  /map keyword extract auth.py -> keywords
          /ask find files related to {{keywords}}
          summarize this for me -> myplan
          /agent planning $
          /ask ~
          /small ~
          поясни: $
          /readln @
          /readln @src
          /fim @src
          /save @/result.txt code
          /save @/result1.txt @/result2.txt code

/role <persona>             Set a system role prepended to every chat request (survives /ctx clear).
/role show                  Show the current role.
/role clear                 Remove the role.
    Default role: "You are a software developer assistant."
    Note: words like "senior", "expert", "professor" push the model to rely on its own knowledge
    and skip cautious steps (read-before-edit, describe-before-change). Use them intentionally.

/team list                        List all team definitions (.yaml files in teams dir).
/team show <name>                 Show workers defined in a team.
/team run <name> [--param k=v]    Spawn one 1bcoder process per worker, each runs its script.
                                  Waits until all finish, then notifies.
/team new <name>                  Create a new team yaml from template.
    Team yaml format (.1bcoder/teams/<name>.yaml):
      workers:
        - name: worker1
          host: localhost:11434
          model: qwen2.5-coder:1.5b
          script: my-script.txt
        - name: worker2
          host: openai://localhost:1234
          model: qwen2.5-coder:1.5b
          script: other-script.txt
          depends_on: worker1
    name: optional worker label (auto-assigned 1,2,3... if omitted).
    depends_on: comma-separated names — worker waits until all listed workers finish.
    Parameters passed via --param are forwarded to every worker script.
    e.g.  /team run auth-analysis --param task="fix login" --param filename=auth.py
          /team show auth-analysis
          /team new my-team

/config save [file]            Save current state (host, model, ctx, params, vars, procs) to .1bcoder/config.yml.
/config save global            Save current state to ~/.1bcoder/config.yml (global).
/config save host              Save only the current host to local config.
/config save global host       Save only the current host to global config.
/config save model             Save only the current model to local config.
/config save global model      Save only the current model to global config.
/config save ctx               Save only the current ctx to config.
/config save params            Save only the current params to config.
/config save vars              Save only the current vars to config.
/config save procs             Save only the current procs to config.
/config load [file|global]     Restore state from local config, file, or global config.
/config show [file|global]     Print local config, file, or global config contents.
/config auto on|off [global]   Enable/disable auto-load at startup (local or global config).
/config del model|host|ctx              Remove top-level key from config.
/config del var <name>    Remove specific variable from config.
/config del vars          Remove entire vars section from config.
/config del param <name>  Remove specific param from config.
/config del params        Remove entire params section from config.
/config del proc <name>   Remove specific proc from config.
/config del procs         Remove entire procs section from config.
    e.g.  /config save
          /config save global
          /config save global host
          /config auto on
          /config auto on global
          /config del var project
          /config del procs

/ctx <n>                  Set context window size in tokens (default 8192).
/ctx clear               Clear all conversation messages (keeps /param, num_ctx, and /var variables).
/ctx clear <n>           Remove last N messages from context.
/ctx clear view [N|all]        Strip base64 from oldest N (default 1) image messages.
/ctx clear view last [N|all]   Strip base64 from newest N image messages.
/ctx cut                 Remove oldest messages until context fits within the limit.
/ctx compact             Ask AI to summarize the conversation, then replace context with the summary.
/ctx compact <N>         Summarize last N messages in place, replace them with one compact block.
/ctx compact savepoint   Summarize only messages since the savepoint, replace them with summary.
/ctx compact profile: <name>            Use external model (from profile) for summarization.
/ctx compact savepoint profile: <name>  External model + savepoint scope.
/ctx save <name>         Save full conversation context to .1bcoder/ctx/<name>.txt
                         If <name> has a path separator, saves to that exact location.
/ctx load <file>         Restore context from a saved file (appends to current context).
                         If <file> has no path, looks in .1bcoder/ctx/ automatically.
/ctx list                List files in .1bcoder/ctx/ (project context library).
/ctx autosave on|off     Enable/disable silent autosave after every LLM response (default: off).
/ctx autosave show       Show autosave state and current file path.
/ctx savepoint set       Mark current context position as a savepoint.
/ctx savepoint rollback  Remove all messages added since the savepoint.
/ctx savepoint show      Show savepoint position and how many messages have been added since.

/ctx compose add <file>       Add ctx file to compose queue (bare name resolved from ctx/ or projects/<key>/).
/ctx compose add <N,M>        Add by number from last /proj find results.
/ctx compose add all          Add all /proj find results to queue.
/ctx compose list                Show compose queue with sizes and accumulated total.
/ctx compose clear               Clear the compose queue.
/ctx compose run [output]        Merge queue into one file (or load into context if no output given).
                                 Bare name → .1bcoder/ctx/<name>.txt  |  path with dir → as-is.
                                 Deduplication: identical message blocks appear only once in output.
/ctx compose <f1> <f2> ...       Direct compose without queue — merge files immediately.
    Files are resolved: bare name → .1bcoder/ctx/ → .1bcoder/projects/<key>/
    Workflow:
      /proj find isbn              search projects, results are numbered [1], [2], ...
      /ctx compose add 1,3      add result #1 and #3 to queue
      /ctx compose list            review queue
      /ctx compose run mytask      merge → .1bcoder/ctx/mytask.txt
      /ctx load mytask             load into context — LLM wakes up knowing all three files
    e.g.  /ctx compact 1
          /ctx compact 3
          /ctx compose add book-html.txt
          /ctx compose run mytask

/tempctx <N>               Set agent context limit to N tokens (overrides num_ctx for this agent run).
/tempctx show              Show agent context size (messages + token estimate).
/tempctx cut               Remove oldest messages from agent context until it fits within the limit.
/tempctx clear             Reset agent context to system prompt + task only.
    Only available inside an agent loop. Use these instead of /ctx when writing agent actions.
    Agents should list "tempctx" in their tools — this hides /ctx from the model entirely.
    /tempctx N can also be set via params = agent_ctx = N in the agent file.
    e.g.  /tempctx 4000
          ACTION: /tempctx show
          ACTION: /tempctx cut
          ACTION: /tempctx clear

/think include      Keep <think>...</think> blocks in context (pass reasoning to next model).
/think exclude      Strip <think> from context (default).
/think show         Show <think> blocks in terminal (default).
/think hide         Hide <think> blocks in terminal.
    include/exclude and show/hide are independent — any combination works.
    To persist across sessions use /param: /param think_exclude false (include) | true (exclude)
    then /config save — params are saved and restored automatically.

/param <key> <value>    Set a model parameter sent with every request. Overwrites if already set.
/param                  Show current params (includes timeout).
/param clear            Remove all params and reset timeout to default (120s).
    Model params: temperature (0.0–2.0), top_p (0.0–1.0), top_k, num_predict, seed, stop, enable_thinking
    Connection:   timeout <seconds>  — HTTP read timeout (increase for slow/large-context models)
    e.g.  /param temperature 0.2
          /param enable_thinking false
          /param seed 42
          /param timeout 300
          /param clear

/format <description>
    Inject a strict output format constraint into context.
    Affects all following replies until /format clear.
    e.g.  /format JSON array of strings
          /format one word answer
          /format comma separated list
/format clear
    Remove the format constraint from context.

/clear          Clear conversation context, reset params, clear /var variables, and reload model metadata.
                Use this to fully reset session state when the model starts behaving oddly.
/clear kv       Evict the Ollama KV cache by unloading the model (keep_alive=0). Ollama-only.
                Use before benchmarks or tests that require a completely clean inference state.
                The model will be reloaded on the next request.

/model [-sc] [-cc]      Switch AI model interactively (type number from list).
/model <name> [-sc] [-cc]  Switch directly by model name (e.g. /model gemma3:1b).
                        -sc / save-context: keep context when switching.
                        -cc / clear-cache:  evict KV cache of the old model before switching (Ollama only).

/host <url> [-sc]   Switch host and provider on the fly.
                    -sc / save-context: keep context when switching.
                    Provider is set by URL scheme: ollama:// (default) or openai://.
                    Plain host without scheme defaults to ollama.
    e.g.  /host localhost:11434                   (Ollama, default)
          /host openai://localhost:1234            (LMStudio)
          /host openai://localhost:4000            (LiteLLM)
          /host openai://localhost:1234 -sc

/mcp connect <name> <command> [--cwd <dir>]
    Start an MCP server and connect to it. --cwd sets the working directory for the subprocess.
    e.g.  /mcp connect fs npx -y @modelcontextprotocol/server-filesystem .
          /mcp connect simargl simargl-mcp --cwd C:/Project/my-app --project-id default
/mcp tools [name]
    List tools from all connected servers (or one named server).
/mcp call <server/tool> [json_args]
    Call a tool and inject the result into context.
    e.g.  /mcp call fs/read_file {"path": "main.py"}
          /mcp call read_file        (if only one server connected)
/mcp disconnect <name>
    Shut down a connected MCP server.
    See /doc MCP for ready-to-use servers (filesystem, web, git, db, browser...).

/parallel [main question]  [list: a1, a2, a3]  profile: name
          [ctx: full|last|none]  [file: path [-n N]]
          [collect: compact [profile: name]]  [--seq]
    Send prompts to multiple models. No quotes required.
    $ expands to last output, ~ expands to last input, @ triggers file picker, @/ triggers dir picker.
    Context sent to workers (default: ctx: last):
      ctx: full    send full conversation context
      ctx: last    send only the last message (default)
      ctx: none    send no context (prompt only)
    Prompt modes:
      plain text only  → same prompt sent to all workers
      list: a1, a2     → aspects distributed one per worker; combined with main question as:
                          "{main question} Aspect: {aspect_i}"
                          Use list: to ask one question from multiple angles simultaneously.
      comma list only  → matched 1:1 to workers (last reused for remaining)
    file: path [-n N]  split file into N chunks (one per worker); -n auto = chunks = workers.
      === separator in file is used if present; otherwise equal line splits.
    collect: compact [profile: name]
      after all workers finish, run /ctx compact savepoint [profile: name].
      savepoint is set automatically before workers run.
    --seq  run workers sequentially instead of in parallel.
    Profiles stored in .1bcoder/profiles.txt, one per line:
      small1: localhost:11434|gemma3:1b|ans/gem.txt localhost:11435|llama3:1b|ans/lam.txt
    e.g.  /parallel review for bugs  profile: small1
          /parallel Hegel philosophy  list: axiology, epistemology, ethics  profile: small1
          /parallel $  profile: short
          /parallel {{q1}}, {{q2}}, {{q3}}  profile: three-machines
          /parallel file: bigfile.txt -n auto  profile: cluster  collect: compact profile: small
          /parallel what is this  list: pros, cons  profile: small1  collect: compact
          /parallel what does this do  localhost:11434|llama3.2:1b|ans/a.txt
/parallel profile create <name> [host|model|file ...]
    Inline: workers supplied as space-separated host|model|file specs.
    Interactive: omit workers — wizard prompts host/model/file one by one.
/parallel profile list
    Show all saved profiles (local + global). Local overrides global for same name.
/parallel profile show <name>
    Print the raw profile string (source file indicated).
/parallel profile add <name>
    Append current host+model to an existing profile (prompts for output file).

/map index [path] [depth]
    Scan project and extract definitions, cross-references into a searchable map.
    Saves to .1bcoder/map.txt. Does NOT inject into context. Run once per session (or after big changes).
    Directories starting with . are excluded automatically.
    depth 2 (default) — classes, functions, endpoints, tables
    depth 3           — also variables, function parameters, module assignments
    Partial indexing: if path is a subfolder, saves a segment file
    (.1bcoder/map_<slug>.txt) and patches map.txt in-place — only that
    subtree is replaced. Use for large codebases where full scan is slow.
    e.g.  /map index .
          /map index src/ 3
          /map index sonar_core/src/main/java/org/sonar/core/util
/map find [query] [-d N] [-y]
    Search map.txt and inject matching file blocks into context.
    No query → inject full map (asks confirmation).
    -d 1  filenames only   -d 2  filenames + defines/vars   -d 3  full (default)
    -y skips the "add to context?" prompt (useful in scripts).
    Sets {{map_files}} to a comma-separated list of matched file paths.
    Token syntax:
      term    filename contains term
      !term   exclude if filename contains term
      \\term  include block if any child line contains term
      \\!term exclude entire block if any child contains term
      -term   show ONLY child lines containing term
      -!term  hide child lines containing term
      +term   like -term but also hides files with no matching children
    e.g.  /map find register
          /map find \\register !mock
          /map find auth \\UserService -!deprecated -y
          /map find register|email     (OR: either term)
/map trace <identifier> [-d N] [-y]
    Follow the call chain backwards from a defined identifier.
    Shows which files reference it, then which files reference those, etc. (BFS).
    -d N  max depth (default 8)
    -y    skips the "add to context?" prompt.
    e.g.  /map trace insertEmail
          /map trace register -d 2
          /map trace UserService -d 3 -y
/map trace deps <identifier> [-d N] [-leaf] [-y]
    Forward dependency tree: what does this identifier's file depend on?
    -d N    max depth (default 8)
    -leaf   show only leaf files (deepest dependencies, no further outgoing links)
    e.g.  /map trace deps UserService
          /map trace deps UserService -d 3
          /map trace deps UserService -leaf
/map trace <start> <end> [-y]
    Find the shortest dependency path between two identifiers or file substrings.
    Each argument can be an identifier name (resolved to its defining file) or
    a substring of a file path.  Tries both directions (forward and reverse graph).
    After each path: [Y] add + next  [s] skip + next  [l] loop N (auto-collect N paths)  [n] stop.
    -y adds the first path and stops (non-interactive).
    e.g.  /map trace AccountNumber UserController
          /map trace firstName /users
          /map trace UserEntity.java UserController.java
/map diff
    Compare map.txt vs map.prev.txt without re-indexing.
    Safe to run multiple times — does not overwrite the snapshot.
/map idiff [path] [depth]
    Re-index the project, then diff vs the previous snapshot. One step.
    Use this after making code changes. Tell the agent to use idiff.
    e.g.  /map idiff
          /map idiff src/ 3
/map keyword index
    Build a keyword vocabulary index from the project map.
    Reads .1bcoder/map.txt, saves result to .1bcoder/keyword.txt (CSV format).
    CSV format: word, count, semicolon-separated list of line numbers in map.txt.
    Sorted alphabetically. Run once after /map index (or whenever map changes).
    e.g.  /map keyword index
/map keyword extract <text or file> [-a] [-s] [-f] [-l] [-n] [-c] [-o]
    Extract real identifiers from keyword.txt matching words in the given text or file.
    Output is always real identifiers from keyword.txt — never synthetic splits.
    Default (exact): query word must exactly match a keyword.txt entry.
        "rule" matches "rule" only — does NOT match "RuleIndex".
    -f  fuzzy subword match: splits both query and keyword into subwords,
        matches if ALL query subwords (≥5 chars) are present in the keyword's subwords.
        Short words (<5 chars: is, in, for, main, pull...) are skipped as stopwords.
        "rule"      → skipped (4 chars) — use exact match instead
        "RuleIndex" → matches RuleIndex only (needs both 'rule' AND 'index')
        "coverage"  → matches CoverageMetric, LineCoverage, BranchCoverage
        "RuleIndex" → does NOT match Rule (missing 'index') or Index (missing 'rule')
    -l  like match (%token%): any keyword containing the query token as substring
        "rule" → RuleIndex, rule_validator, parseRule, getRuleSet
    -a  alphabetical order
    -s  sort by codebase count descending (most frequent first)
    -n  show codebase count next to each word: RuleIndex(25) RuleName(12)
        (-n implies -s)
    -c  comma-separated output instead of one per line
    -o  show origin: which files each keyword appears in: keyword -> file1, file2
    e.g.  /map keyword extract notes.txt
          /map keyword extract notes.txt -f
          /map keyword extract notes.txt -l
          /map keyword extract notes.txt -f -n -c
          /map keyword extract "add isbn field to the Book class" -f -a
          /map keyword extract "fix rule search" -l -o

/hook before|after <cmd> <script>
    Run a script before or after a command (edit, patch, fix, insert).
    Script runs from the scripts dir. {{file}} and {{range}} are injected automatically.
    before hook: if script not found, the command is cancelled.
    after hook:  runs unconditionally after the command completes.
/hook list
    Show all active hooks.
/hook clear
    Remove all hooks.
/hook clear before|after [<cmd>]
    Remove specific hook(s).
    Hooks are saved in config.yml and loaded automatically when auto: true.
    e.g.  /hook before edit   pre-edit.txt
          /hook after patch   post-patch.txt
          /hook list
          /hook clear before edit

/bkup save <file>
    Save a backup copy as <file>.bkup (overwrites existing).
    e.g.  /bkup save calc.py
/bkup restore <file>
    Delete <file> and replace it with <file>.bkup.
    e.g.  /bkup restore calc.py

/diff <file_a> <file_b> [-y]
    Show colored unified diff between two files.
    -y: skip confirmation and inject diff into context automatically.
    e.g.  /diff main.py main.py.bkup
          /diff v1/calc.py v2/calc.py -y

/alias                        List all active aliases.
/alias /name = expansion      Define an alias. {{args}} in expansion is replaced by everything after the name.
/alias clear /name            Remove an alias (session only).
/alias save /name             Persist an alias to .1bcoder/aliases.txt.
    Aliases are loaded from global then local aliases.txt at startup and survive /clear.
    Agents can also define aliases in their .txt file (agent-scoped, applied during that run).
    e.g.  /alias /sql = /run python db.py "{{args}}"
          /alias /ask = /agent ask
          /alias save /sql

/agent list
    List all named agents from .1bcoder/agents/ (* = local project) and ~/.1bcoder/agents/ (g = global).
    Shows name and description for each agent.
/agent <name> [-t N] [-y] <task> [plan: step1, step2, ...] [file: steps.md]
    Run a named agent defined in .1bcoder/agents/<name>.txt (local overrides global).
    Agent file defines: system prompt, tools, max_turns, auto_exec, aliases, params, before, gates.
    e.g.  /agent list
          /agent ask what does this project do
          /agent advance refactor the auth module
          /agent dbsearcher find all users created this week
    Direct command syntax also works: /dbsearcher find all users created this week
/agent [-t N] [-y] <task> [plan: step1, step2, ...] [file: steps.md]
    Run the default agent. The model uses tools to complete the task.
    The agent prompt instructs the model to emit one ACTION per turn.
    If the model emits multiple ACTION lines, all are executed in order.
    Stops when the model outputs plain text with no ACTION.
    Configure via .1bcoder/agent.txt (max_turns, auto_apply, tools, advanced_tools).
    -t N  override max_turns for this run only.
    -y    skip per-action confirmation (execute all actions automatically).
    Without -y: each action shows [Y/n/e/f/q]:
      Y / Enter  execute the action
      n          skip this action
      e          edit the command before executing (copies to clipboard on Windows)
      f          send feedback to the AI and skip the action (redirect the model)
      q          stop the agent
    Ctrl+C interrupts at any turn.
    plan: comma-separated list of hints, one injected per turn.
    file: path to a .md/.txt file — steps extracted from numbered/bulleted list or === sections.
          For .md: numbered/bulleted items → steps; === separator → one step per section.
                   ### Example/Summary → injected as context before step 1.
          For .txt: each non-comment, non-[v] line → one step.
          If a turn returns empty or no ACTION, the agent continues to the next step.
          Gate FAIL on a plan step retries that step (re-injects hint, removes old one).
    e.g.  /agent find and fix the divide by zero bug in calc.py
          /agent -t 1 read models.py and explain the User class
          /agent -y -t 5 refactor utils.py
          /agent read files plan: models.py, views.py, urls.py
          /agent fix the book model file: steps.md
          /agent implement sharepoint file: plan.md

/proj set <key>              Set active project — creates .1bcoder/projects/<key>/ with project.txt.
/proj status                 Show active project name and project.txt contents.
/proj list                   List all projects (newest first). Active project marked with *.
/proj save <file>            Save current ctx to .1bcoder/projects/<key>/<file>.
/proj load <file>            Load ctx file from active project (resolves path automatically).
/proj show                   List ctx files in active project (newest first).
/proj find <term> [-f|-c]    Search all projects. -f fast: project.txt + filenames (default).
                             -c content: also grep inside ctx files with line numbers.
/proj keyword add <k1,k2>    Add keywords to project.txt of active project.
/proj file add <f1,f2>       Add file paths to project.txt of active project.
/proj index                  Extract file paths from /read /edit /patch etc. in ctx files → project.txt.
    project.txt format:
      Description: <text>
      Keywords: k1, k2, k3
      Files:
      path/to/file.py
      path/to/other.py
    Active project is saved in config via /config save and auto-restored on next startup.
    e.g.  /proj set ABC-123
          /proj keyword add ppcon, payment, legacy
          /proj file add models.py, views.py
          /proj save session1.txt
          /proj find payment -f
          /proj find payment -c
          /config save

/init           Create .1bcoder/ scaffold in current directory (safe to re-run).

/help                   Show full help.
/help <command>         Show help for one command (e.g. /help map, /help fix).
/help <command> ctx     Same but also inject the text into AI context.
/help all ctx           Inject the entire help reference into AI context — then ask the model anything about available commands.

/doc list               List documentation articles in doc/.
/doc <name>             Show article rendered as Markdown.
/doc <name> raw         Show article as plain text.
/doc <name> ctx         Add article to AI context.

/exit           Quit.

/help all ctx   Inject the entire help reference into AI context — then ask the model anything about available commands.

Ctrl+C      - interrupt AI response mid-stream.
Enter       - submit message.
"""


def get_help_list(tools_list: list) -> str:
    """Return 2-line summaries for each tool, extracted from HELP_TEXT.

    For each line in HELP_TEXT that starts with /<tool>, outputs:
      - that line  (the command signature, may include inline description)
      - next indented line if present  (first description line)

    Compound commands (map, script, mcp, ctx) produce one entry per subcommand.
    Always in sync with HELP_TEXT — no separate maintenance needed.

    Example:
        get_help_list(["read", "fix", "bkup"])  →

        /read <file> [start-end]
            Inject file into AI context.

        /fix <file> [start-end] [hint]
            AI proposes one-line fix. Shows diff before applying.

        /bkup save <file>
            Save a backup copy as <file>.bkup (overwrites existing).

        /bkup restore <file>
            Delete <file> and replace it with <file>.bkup.
    """
    all_lines = HELP_TEXT.splitlines()
    result    = []
    seen      = set()

    for tool in tools_list:
        pat = re.compile(r'^/' + re.escape(tool.lstrip('/')) + r'(\s|$)')
        for i, line in enumerate(all_lines):
            if not pat.match(line) or line in seen:
                continue
            seen.add(line)
            result.append(line)
            # grab next non-empty indented line as description
            j = i + 1
            while j < len(all_lines) and not all_lines[j].strip():
                j += 1
            if j < len(all_lines) and all_lines[j].startswith('    '):
                result.append(all_lines[j])
            result.append("")

    return '\n'.join(result).strip()


def get_cmd_list(tools_list: list) -> str:
    """Like get_help_list but returns only the command signature line — no descriptions."""
    all_lines = HELP_TEXT.splitlines()
    result    = []
    seen      = set()
    for tool in tools_list:
        pat = re.compile(r'^/' + re.escape(tool.lstrip('/')) + r'(\s|$)')
        for line in all_lines:
            if pat.match(line) and line not in seen:
                seen.add(line)
                result.append(line)
    return '\n'.join(result)


# ── core helpers ───────────────────────────────────────────────────────────────

def parse_host(host_str):
    """Parse 'ollama://host:port' or 'openai://host:port' or plain 'host:port'.
    Returns (http_url, provider).  Default provider is 'ollama'."""
    s = host_str.rstrip("/")
    if s.startswith("ollama://"):
        return "http://" + s[len("ollama://"):], "ollama"
    if s.startswith("openai://"):
        return "http://" + s[len("openai://"):], "openai"
    if not s.startswith(("http://", "https://")):
        s = "http://" + s
    return s, "ollama"


def list_models(base_url, provider="ollama"):
    if provider == "openai":
        resp = requests.get(f"{base_url}/v1/models", timeout=5)
        resp.raise_for_status()
        return [m["id"] for m in resp.json().get("data", [])]
    resp = requests.get(f"{base_url}/api/tags", timeout=5)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])]


# ── model metadata helpers ──────────────────────────────────────────────────

# Known context limits for OpenAI models (tokens).  Matched by prefix.
_OPENAI_CTX = {
    "gpt-4.1":       1_047_576,
    "gpt-4o":          128_000,
    "gpt-4-turbo":     128_000,
    "gpt-4":             8_192,
    "gpt-3.5-turbo":    16_385,
    "o1":              200_000,
    "o3":              200_000,
    "o4-mini":         200_000,
}


def _fmt_size(n_bytes: int) -> str:
    """Convert bytes → compact string: 815000000 → '815M', 3800000000 → '3.8G'."""
    if n_bytes >= 1_000_000_000:
        return f"{n_bytes / 1e9:.1f}G"
    return f"{n_bytes // 1_000_000}M"


def _fmt_ctx(n: int) -> str:
    """Convert token count → compact string: 32768 → '32K', 512 → '512'."""
    if n >= 1000:
        return f"{n // 1024}K"
    return str(n)


def read_file(path, start=None, end=None, line_numbers=True):
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        raise ValueError(f"[read] {path}: binary file, cannot read as text")
    total = len(lines)
    if start is not None:
        start = max(1, start)
        end = min(end or total, total)
        lines = lines[start - 1:end]
        offset = start
    else:
        offset = 1
    if line_numbers:
        return "".join(f"{offset + i:4}: {line}" for i, line in enumerate(lines)), total
    else:
        return "".join(lines), total


def edit_line(path, lineno, new_content):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if not 1 <= lineno <= len(lines):
        raise ValueError(f"line {lineno} out of range (file has {len(lines)} lines)")
    lines[lineno - 1] = new_content if new_content.endswith("\n") else new_content + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _parse_openai_stream(resp, on_chunk, chunks):
    """Parse SSE stream from OpenAI-compatible endpoint."""
    for line in resp.iter_lines():
        if not line:
            continue
        text = line.decode() if isinstance(line, bytes) else line
        if text.startswith("data: "):
            text = text[6:]
        if text == "[DONE]":
            break
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        chunk = (data.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
        if chunk:
            if on_chunk:
                on_chunk(chunk)
            chunks.append(chunk)


def ai_fix(base_url, model, content, label, hint="", on_chunk=None, provider="ollama"):
    user_msg = f"Fix the bug in this code ({label}):\n```\n{content}```"
    if hint:
        user_msg = f"{hint}\n\n{user_msg}"
    msgs = [
        {"role": "system", "content": FIX_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    chunks = []
    if provider == "openai":
        with requests.post(
            f"{base_url}/v1/chat/completions",
            json={"model": model, "messages": msgs, "stream": True},
            stream=True, timeout=120,
        ) as resp:
            resp.raise_for_status()
            _parse_openai_stream(resp, on_chunk, chunks)
    else:
        with requests.post(
            f"{base_url}/api/chat",
            json={"model": model, "messages": msgs, "stream": True},
            stream=True, timeout=120,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    if on_chunk:
                        on_chunk(chunk)
                    chunks.append(chunk)
                if data.get("done"):
                    break
    raw = "".join(chunks)
    m = re.search(r'LINE\s+(\d+)\s*: ?(.*)', raw, re.IGNORECASE)
    if m:
        return int(m.group(1)), m.group(2).rstrip()
    return None, raw


def ai_fill(base_url, model, file_lines, start, end, hint="", on_chunk=None, provider="ollama"):
    """FIM-based fix: marks line range in the whole file, asks model for corrected file.
    If start is None: no marker — whole file passed with hint only (agent fallback mode)."""
    if start is None:
        file_content = "".join(file_lines)
        user_msg = file_content + "\nFix the issue. Output the complete corrected file."
    else:
        marked = list(file_lines)
        if start == end:
            marked[start - 1] = f"<<<{file_lines[start - 1].rstrip()}>>>\n"
        else:
            marked[start - 1] = "<<<\n" + file_lines[start - 1]
            marked[end - 1]   = file_lines[end - 1].rstrip() + "\n>>>\n"
        file_content = "".join(marked)
        user_msg = file_content + "\nFix the <<<...>>> section. Output the complete corrected file."
    if hint:
        user_msg = f"{hint}\n\n{user_msg}"
    msgs = [{"role": "user", "content": user_msg}]
    chunks = []
    if provider == "openai":
        with requests.post(
            f"{base_url}/v1/chat/completions",
            json={"model": model, "messages": msgs, "stream": True},
            stream=True, timeout=120,
        ) as resp:
            resp.raise_for_status()
            _parse_openai_stream(resp, on_chunk, chunks)
    else:
        with requests.post(
            f"{base_url}/api/chat",
            json={"model": model, "messages": msgs, "stream": True},
            stream=True, timeout=120,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    if on_chunk:
                        on_chunk(chunk)
                    chunks.append(chunk)
                if data.get("done"):
                    break
    return "".join(chunks)


def _parse_patch(text):
    """Extract (search_text, replace_text) from a SEARCH/REPLACE block, or (None, None)."""
    m = re.search(
        r'<{6,}\s*SEARCH\s*\n(.*?)\n={6,}[^\n]*\n(.*?)\n>{6,}\s*REPLACE',
        text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1), m.group(2)
    return None, None


def _strip_line_numbers(lines):
    """Remove /read line-number prefixes like '   1: ' from a list of strings."""
    stripped = []
    for l in lines:
        m = re.match(r'^\s*\d+: ?', l)
        stripped.append(l[m.end():] if m else l)
    return stripped


def _find_in_lines(lines, search_text):
    """Return (start_idx, end_idx) 0-based exclusive end, or (None, None).
    Tries three strategies: exact → indent-tolerant → strip /read line numbers."""
    slines = [l.rstrip('\n') for l in search_text.splitlines()]
    while slines and not slines[0].strip():
        slines.pop(0)
    while slines and not slines[-1].strip():
        slines.pop()
    n = len(slines)
    if not n:
        return None, None
    flines = [l.rstrip('\n') for l in lines]
    # 1. exact
    for i in range(len(flines) - n + 1):
        if flines[i:i + n] == slines:
            return i, i + n
    # 2. fuzzy: ignore leading whitespace differences
    sls = [l.lstrip() for l in slines]
    for i in range(len(flines) - n + 1):
        if [l.lstrip() for l in flines[i:i + n]] == sls:
            return i, i + n
    # 3. model echoed /read line numbers (e.g. "   1: import random")
    sls_no_num = [l.lstrip() for l in _strip_line_numbers(slines)]
    for i in range(len(flines) - n + 1):
        if [l.lstrip() for l in flines[i:i + n]] == sls_no_num:
            return i, i + n
    return None, None


def _extract_code_block(text):
    """Return content of the first ```...``` block, or the full text if none found."""
    m = re.search(r'```[^\n]*\n(.*?)```', text, re.DOTALL)
    return m.group(1) if m else text


def _extract_all_code_blocks(text):
    """Return list of all ```...``` block contents found in text."""
    return re.findall(r'```[^\n]*\n(.*?)```', text, re.DOTALL)


def _copy_to_clipboard(text: str) -> None:
    """Copy text to system clipboard (Windows / macOS / Linux)."""
    import subprocess, sys
    try:
        if sys.platform == "win32":
            subprocess.Popen(["clip"], stdin=subprocess.PIPE,
                             close_fds=True).communicate(input=text.encode("utf-16"))
        elif sys.platform == "darwin":
            subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE).communicate(input=text.encode("utf-8"))
        else:
            subprocess.Popen(["xclip", "-selection", "clipboard"],
                             stdin=subprocess.PIPE).communicate(input=text.encode("utf-8"))
    except Exception:
        pass


def _next_suffix_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    m = re.match(r'^(.*?)_(\d+)$', base)
    stem = m.group(1) if m else base
    n = int(m.group(2)) + 1 if m else 1
    while True:
        candidate = f"{stem}_{n}{ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _get_package_version() -> str:
    """Return installed package version string, or '0' if not installed via pip."""
    try:
        from importlib.metadata import version
        return version("1bcoder")
    except Exception:
        return "0"


def _merge_text_file(src: str, dst: str) -> list[str]:
    """Append lines from src to dst that are not already present (by key prefix).
    For aliases.txt: key = '/name'. For profiles.txt: key = 'name:'.
    Returns list of added keys."""
    with open(src, encoding="utf-8") as f:
        src_lines = f.readlines()
    with open(dst, encoding="utf-8") as f:
        dst_content = f.read()

    added = []
    pending = []
    for line in src_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            pending.append(line)
            continue
        # extract key: '/name' for aliases, 'name:' for profiles
        key = stripped.split("=")[0].strip() if "=" in stripped else stripped.split(":")[0].strip()
        if key and key not in dst_content:
            pending.append(line)
            added.append(key)
        else:
            pending = []  # reset comment buffer if key exists

    if added:
        with open(dst, "a", encoding="utf-8") as f:
            if not dst_content.endswith("\n"):
                f.write("\n")
            f.write("# ── new in this version (uncomment to enable) ──\n")
            for line in pending:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    f.write("# " + line if not line.startswith("#") else line)
                else:
                    f.write(line)
    return added


def _bootstrap_global_dir():
    """Copy missing items from wheel's _bcoder_data/ to ~/.1bcoder/.
    On upgrade: merges new entries in aliases.txt and profiles.txt without overwriting user data.
    Tracks last bootstrapped version in ~/.1bcoder/.version to detect upgrades."""
    import shutil
    if not os.path.isdir(INSTALL_BCODER_DIR):
        return  # running from source without bundled defaults

    os.makedirs(HOME_BCODER_DIR, exist_ok=True)
    current_ver = _get_package_version()
    version_file = os.path.join(HOME_BCODER_DIR, ".version")
    last_ver = open(version_file, encoding="utf-8").read().strip() if os.path.isfile(version_file) else ""

    # ── first run: copy everything missing ───────────────────────────────────
    bootstrapped = []
    for item in os.listdir(INSTALL_BCODER_DIR):
        src = os.path.join(INSTALL_BCODER_DIR, item)
        dst = os.path.join(HOME_BCODER_DIR, item)
        if not os.path.exists(dst):
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            bootstrapped.append(item)
    if bootstrapped:
        print(f"[1bcoder] initialized ~/.1bcoder/ — copied: {', '.join(sorted(bootstrapped))}")

    # ── upgrade: merge new entries in text files ──────────────────────────────
    if current_ver and current_ver != last_ver and last_ver:
        merge_files = ["aliases.txt", "profiles.txt"]
        merged = []
        for fname in merge_files:
            src = os.path.join(INSTALL_BCODER_DIR, fname)
            dst = os.path.join(HOME_BCODER_DIR, fname)
            if os.path.isfile(src) and os.path.isfile(dst):
                added = _merge_text_file(src, dst)
                if added:
                    merged.extend(f"{fname}:{k}" for k in added)
        if merged:
            print(f"[1bcoder] updated ~/.1bcoder/ — new entries: {', '.join(merged)}")

    # ── save current version ──────────────────────────────────────────────────
    if current_ver and current_ver != last_ver:
        with open(version_file, "w", encoding="utf-8") as f:
            f.write(current_ver)


def _load_profile(name):
    """Return list of (host, model, filename) for the named profile, or None if not found.
    Local .1bcoder/profiles.txt takes precedence over global."""
    for profiles_file in (PROFILES_FILE, GLOBAL_PROFILES_FILE):
        if not os.path.exists(profiles_file):
            continue
        with open(profiles_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pname, _, rest = line.partition(":")
                if pname.strip() != name:
                    continue
                rest = rest.split("#")[0]
                workers = []
                for spec in rest.split():
                    parts = spec.split("|", 2)
                    if len(parts) == 3:
                        workers.append(tuple(parts))
                if workers:
                    return workers
    return None


def _list_profiles():
    """Return list of (name, workers, comment, source) for all profiles.
    Local overrides global for same name. source is 'local' or 'global'."""
    seen = {}
    result = []
    for profiles_file, source in ((PROFILES_FILE, "local"), (GLOBAL_PROFILES_FILE, "global")):
        if not os.path.exists(profiles_file):
            continue
        with open(profiles_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pname, _, rest = line.partition(":")
                pname = pname.strip()
                comment = ""
                if "#" in rest:
                    rest, comment = rest.split("#", 1)
                    comment = comment.strip()
                workers = []
                for spec in rest.split():
                    parts = spec.split("|", 2)
                    if len(parts) == 3:
                        workers.append(tuple(parts))
                if pname not in seen:
                    seen[pname] = True
                    result.append((pname, workers, comment, source))
    return result


def _save_profile(name, workers, comment=""):
    """Append or replace a profile entry in local profiles.txt."""
    os.makedirs(BCODER_DIR, exist_ok=True)
    profiles_file = PROFILES_FILE
    # read existing lines, replace if name already exists
    lines = []
    replaced = False
    if os.path.exists(profiles_file):
        with open(profiles_file, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    pname, _, _ = stripped.partition(":")
                    if pname.strip() == name:
                        replaced = True
                        continue          # drop old entry
                lines.append(line)
    specs = " ".join(f"{h}|{m}|{fn}" for h, m, fn in workers)
    entry = f"{name}: {specs}"
    if comment:
        entry += f"  # {comment}"
    lines.append(entry + "\n")
    with open(profiles_file, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return replaced


def _load_script(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


def _save_script(lines, path):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _apply_params(cmd_str: str, params: dict) -> str:
    """Replace {{key}} placeholders in a script step with param values.
    Empty-string values (unset /var sentinels) are skipped.
    """
    for key, value in params.items():
        if value:
            cmd_str = cmd_str.replace(f"{{{{{key}}}}}", value)
    return cmd_str


def _find_template_keys(steps: list) -> list:
    """Return sorted list of unique {{key}} placeholders found in script steps."""
    keys = set()
    for _, cmd in steps:
        keys.update(re.findall(r'\{\{(\w+)\}\}', cmd))
    return sorted(keys)


def _parse_script_apply_args(rest: str):
    """Parse /script apply arguments: returns (auto_yes, filename, params dict)."""
    import shlex
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    auto_yes = False
    filename = None
    params = {}
    for token in tokens:
        if token == "-y":
            auto_yes = True
        elif "=" in token:
            key, _, value = token.partition("=")
            params[key.strip()] = value.strip()
        elif filename is None:
            filename = token
    return auto_yes, filename, params


def _list_script_files():
    """Return (global_plans, local_plans) — each a sorted list of (label, abs_path)."""
    def _scan(directory):
        if not os.path.isdir(directory):
            return []
        result = []
        for root, _, files in os.walk(directory):
            for f in sorted(files):
                if f.endswith(".txt"):
                    abs_path = os.path.join(root, f)
                    label = os.path.relpath(abs_path, directory)
                    result.append((label, abs_path))
        return result
    global_plans = _scan(GLOBAL_SCRIPTS_DIR)
    local_plans  = _scan(SCRIPTS_DIR)
    # hide global scripts that are overridden locally
    local_labels = {label for label, _ in local_plans}
    global_plans = [(l, p) for l, p in global_plans if l not in local_labels]
    return global_plans, local_plans


def _load_agent_script_file(path: str):
    """Parse .md or .txt file into (steps, context_text) for /agent plan <file>.

    === markers (alone on a line): text between consecutive === → one step each.
    .txt: each non-empty, non-comment, non-[v] line → one step; no context.
    .md:  numbered/bulleted list items → steps;
          ### Example / ### Summary sections (and other non-list ## headings) → context_text.

    Returns:
        steps        — list[str] of step strings
        context_text — str with Example/Summary sections (empty if none found)
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        return [], ""

    # === separator — takes priority over all other parsing
    _STEP_MARKER = re.compile(r'^===\s*$', re.MULTILINE)
    if _STEP_MARKER.search(raw):
        parts = _STEP_MARKER.split(raw)
        steps = [p.strip() for p in parts if p.strip()]
        return steps, ""

    ext = os.path.splitext(path)[1].lower()

    if ext == ".txt":
        steps = []
        for line in raw.splitlines():
            line = line.rstrip()
            if not line or line.strip().startswith("#") or line.startswith("[v]"):
                continue
            steps.append(line)
        return steps, ""

    # .md — separate list items from context sections
    _MD_BOLD  = re.compile(r'\*\*(.+?)\*\*')
    _MD_CODE  = re.compile(r'`([^`]+)`')
    _HEADING  = re.compile(r'^#{1,6}\s+(.*)')
    _LIST_NUM = re.compile(r'^\d+\.\s+(.*)')
    _LIST_BUL = re.compile(r'^[-*]\s+(.*)')

    def _strip_md(text: str) -> str:
        text = _MD_BOLD.sub(r'\1', text)
        text = _MD_CODE.sub(r'\1', text)
        return text.strip()

    steps: list[str] = []
    context_parts: list[str] = []

    lines = raw.splitlines()
    i = 0
    in_code_fence = False
    current_section: str | None = None   # "step" | "context" | None
    current_buf: list[str] = []

    def _flush():
        nonlocal current_buf
        if not current_buf:
            return
        if current_section == "step":
            text = " ".join(s for s in current_buf if s).strip()
            if text:
                steps.append(_strip_md(text))
        elif current_section == "context":
            text = "\n".join(current_buf).rstrip()
            if text.strip():
                context_parts.append(text)
        current_buf = []

    while i < len(lines):
        line = lines[i]

        # track fenced code blocks
        if line.strip().startswith("```"):
            in_code_fence = not in_code_fence
            if current_section == "context":
                current_buf.append(line)
            i += 1
            continue

        if in_code_fence:
            if current_section == "context":
                current_buf.append(line)
            i += 1
            continue

        # heading line
        hm = _HEADING.match(line)
        if hm:
            _flush()
            heading = hm.group(1).strip().lower()
            if any(kw in heading for kw in ("example", "summary", "note", "result")):
                current_section = "context"
                current_buf = [line]   # keep heading in context
            else:
                current_section = None   # other headings: neither step nor context
            i += 1
            continue

        # numbered list item — always starts a new step (even inside context heading)
        nm = _LIST_NUM.match(line)
        if nm:
            _flush()
            current_section = "step"
            current_buf = [nm.group(1)]
            i += 1
            continue

        # bulleted list item
        bm = _LIST_BUL.match(line)
        if bm:
            if current_section == "context":
                # bullets inside context section (e.g. Summary) stay in context
                current_buf.append(line)
            elif current_section == "step":
                # sub-bullet: append to current step
                current_buf.append(bm.group(1))
            else:
                _flush()
                current_section = "step"
                current_buf = [bm.group(1)]
            i += 1
            continue

        # blank line ends a step but keeps context sections open
        if not line.strip():
            if current_section == "step":
                _flush()
                current_section = None
            elif current_section == "context":
                current_buf.append("")
            i += 1
            continue

        # regular text line
        if current_section in ("step", "context"):
            current_buf.append(line.strip())

        i += 1

    _flush()

    context_text = "\n".join(context_parts).strip()
    return steps, context_text


def _parse_plan_arg(raw: str):
    """Parse 'plan:' argument — comma-separated inline step list."""
    return [s.strip() for s in raw.split(',') if s.strip()], ""


def _parse_path_arg(raw: str) -> list:
    """Parse 'path:' argument — expand to sorted list of matching file paths."""
    import glob as _glob
    pattern = raw.strip()
    if os.path.isdir(pattern):
        files = []
        for root, _, fnames in os.walk(pattern):
            for fn in sorted(fnames):
                files.append(os.path.join(root, fn).replace("\\", "/"))
        return files
    files = sorted(_glob.glob(pattern, recursive=True))
    return [f.replace("\\", "/") for f in files]


def _parse_file_arg(raw: str):
    """Parse 'file:' argument — load plan steps from a file (=== separator required).

    Steps must be explicitly separated with === markers (alone on a line).
    If none are found, the file is returned as a single step after a warning —
    the user should review, add === markers between steps, and re-run.
    """
    token = raw.strip()
    path = token if os.path.isabs(token) else os.path.join(WORKDIR, token)
    if not os.path.isfile(path):
        print(f"[agent] file not found: {token}")
        return [], ""
    try:
        content = open(path, encoding="utf-8").read()
    except OSError as e:
        print(f"[agent] could not read {token}: {e}")
        return [], ""
    if re.search(r'^===\s*$', content, re.MULTILINE):
        steps, ctx = _load_agent_script_file(path)
        if not steps:
            print(f"[agent] warning: no steps found in {token}")
        return steps, ctx
    # no === markers — warn and return whole file as one step
    print(f"[agent] warning: {token} has no === step separators.")
    print(f"        Plan files should be reviewed, corrected, and separated with ===")
    print(f"        before running. Proceeding with the whole file as a single step.")
    return [content.strip()], ""


# ── MCP client ─────────────────────────────────────────────────────────────────

class MCPClient:
    """Minimal MCP client over stdio using LSP-style Content-Length framing."""

    def __init__(self, cmd: str, cwd: str | None = None):
        self.proc = subprocess.Popen(
            cmd, shell=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )
        self._id = 0
        self._lock = threading.Lock()
        self._stderr_buf: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        # give the process a moment to start, then check it's alive
        import time; time.sleep(0.5)
        if self.proc.poll() is not None:
            raise RuntimeError(
                f"process exited immediately (code {self.proc.returncode}): "
                f"{self._last_err() or 'command not found or crashed'}"
            )
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "1bcoder", "version": "1.0"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self._stderr_buf.append(line.decode(errors="replace").rstrip())

    def _last_err(self) -> str:
        return "\n".join(self._stderr_buf[-5:]) if self._stderr_buf else ""

    def _send(self, msg: dict):
        line = json.dumps(msg) + "\n"
        self.proc.stdin.write(line.encode())
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        while True:
            raw = self.proc.stdout.readline()
            if not raw:
                raise RuntimeError(self._last_err() or "server process exited")
            line = raw.decode().strip()
            if line:
                return json.loads(line)

    def _rpc(self, method: str, params=None) -> dict:
        self._id += 1
        req_id = self._id
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            msg["params"] = params
        with self._lock:
            self._send(msg)
            while True:
                data = self._recv()
                if data.get("id") == req_id:
                    if "error" in data:
                        raise RuntimeError(data["error"].get("message", "MCP error"))
                    return data.get("result", {})

    def list_tools(self) -> list:
        return self._rpc("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict = None) -> str:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return "\n".join(
            c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
        )

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


# ── map partial-index helpers ──────────────────────────────────────────────────

def _split_identifier(name: str) -> list:
    """Split any identifier form into lowercase subwords.

    Handles: camelCase, PascalCase, snake_case, UPPER_SNAKE_CASE, kebab-case,
    and mixed forms like RuleINDEX or HTTP2Request.

    Examples:
        RuleIndex     → ['rule', 'index']
        rule_index    → ['rule', 'index']
        RULE_INDEX    → ['rule', 'index']
        HTTPRequest   → ['http', 'request']
        rule-index    → ['rule', 'index']
    Returns deduplicated list preserving order.
    """
    # split on _ and -
    parts = re.split(r'[_\-]+', name)
    result = []
    for part in parts:
        if not part:
            continue
        # insert boundary before a run of uppercase followed by uppercase+lowercase
        # e.g. HTTPRequest → HTTP_Request
        s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', part)
        # insert boundary between lowercase/digit and uppercase
        # e.g. ruleIndex → rule_Index
        s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
        result.extend(w.lower() for w in s.split('_') if len(w) >= 2)
    # deduplicate preserving order
    seen: dict = {}
    for w in result:
        seen.setdefault(w, None)
    return list(seen)


def _path_to_seg_name(rel_path: str) -> str:
    """Convert a relative path to a segment map filename.
    'sonar_core/src/it/java/org/sonar/core/util' → 'map_sonar_core_src_it_java_org_sonar_core_util.txt'
    """
    safe = rel_path.replace("\\", "/").strip("/")
    safe = re.sub(r"[/\\]+", "_", safe)
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", safe)
    return f"map_{safe}.txt"


def _adjust_map_paths(map_text: str, rel_prefix: str) -> str:
    """Prepend rel_prefix to all file paths in a partial map.

    Adjusts:
    - non-indented block header lines (the file paths)
    - 'links  → target' paths inside indented lines
    Leaves the comment header line unchanged.
    """
    prefix = rel_prefix.replace("\\", "/").rstrip("/")
    links_re = re.compile(r"^(  links\s+→\s+)(\S+)(.*)", re.DOTALL)
    result = []
    for line in map_text.splitlines(keepends=True):
        s = line.rstrip("\r\n")
        if not s or s.startswith("#"):
            result.append(line)
        elif not s[0].isspace():
            result.append(f"{prefix}/{s}\n")
        else:
            m = links_re.match(s)
            if m:
                result.append(f"{m.group(1)}{prefix}/{m.group(2)}{m.group(3)}\n")
            else:
                result.append(line)
    return "".join(result)


def _map_patch_remove(map_path: str, rel_prefix: str) -> int:
    """Remove all file blocks from map.txt whose path starts with rel_prefix.
    Returns the number of file blocks removed.
    """
    prefix = rel_prefix.replace("\\", "/").rstrip("/")
    with open(map_path, "r", encoding="utf-8") as f:
        content = f.read()
    # Split on the double-newline that separates file blocks.
    # Format: "# header\n\nfile1.py\n  defines...\n\nfile2.py\n..."
    sep = "\n\n"
    first_sep = content.find(sep)
    if first_sep == -1:
        return 0
    header = content[: first_sep + len(sep)]
    body   = content[first_sep + len(sep):]
    blocks = body.split(sep)
    kept, removed = [], 0
    for block in blocks:
        if not block.strip():
            continue
        first_line = block.split("\n")[0].replace("\\", "/").strip()
        if first_line == prefix or first_line.startswith(prefix + "/"):
            removed += 1
        else:
            kept.append(block)
    new_content = header + sep.join(kept)
    if kept and not new_content.endswith("\n"):
        new_content += "\n"
    with open(map_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return removed


# ── command fixer ──────────────────────────────────────────────────────────────

_KNOWN_CMDS = [
    "/read", "/readln", "/view", "/insert", "/edit", "/save", "/run", "/script", "/mcp",
    "/parallel", "/patch", "/fix", "/fim", "/bkup", "/diff", "/agent", "/tree",
    "/find", "/map", "/ctx", "/think", "/format", "/param", "/model",
    "/host", "/help", "/init", "/clear", "/exit",
    "/prompt", "/proc", "/team", "/var", "/config", "/alias",
    "/doc", "/tempctx", "/proj", "/text", "/role", "/ask", "/translate",
    "/web", "/flow", "/visual",
]

# file_idx : position of the file-path argument (None = no file arg)
# kw_idx   : position of the subcommand / keyword token (None = no keyword check)
# keywords : valid values for that token
_CMD_SPEC = {
    "/read":     dict(file_idx=1, kw_idx=None, keywords=[]),
    "/readln":   dict(file_idx=1, kw_idx=None, keywords=[]),
    "/view":     dict(file_idx=1, kw_idx=None, keywords=[]),
    "/insert":   dict(file_idx=1, kw_idx=3,    keywords=["code"]),
    "/edit":     dict(file_idx=1, kw_idx=None, keywords=[]),
    "/save":     dict(file_idx=1, kw_idx=None, keywords=["code", "overwrite",
                      "append-above", "append-below", "add-suffix"]),
    "/patch":    dict(file_idx=1, kw_idx=None, keywords=["code"]),
    "/fix":      dict(file_idx=1, kw_idx=None, keywords=[]),
    "/bkup":     dict(file_idx=2, kw_idx=1,    keywords=["save", "restore"]),
    "/diff":     dict(file_idx=1, kw_idx=None, keywords=[]),
    "/agent":    dict(file_idx=None, kw_idx=1, keywords=["advance", "ask", "fill", "planning"]),
    "/ctx":      dict(file_idx=None, kw_idx=1, keywords=["clear", "cut", "compact", "save", "load", "savepoint", "compose", "list"]),
    "/tempctx":      dict(file_idx=None, kw_idx=1, keywords=["clear", "cut", "compact"]),    
    "/think":    dict(file_idx=None, kw_idx=1, keywords=["include", "exclude", "show", "hide"]),
    "/script":     dict(file_idx=None, kw_idx=1, keywords=[
                      "list", "open", "create", "show", "run", "add",
                      "clear", "reset", "reapply", "refresh", "apply"]),
    "/map":      dict(file_idx=None, kw_idx=1, keywords=["index", "find", "trace", "deps", "diff", "idiff", "keyword"]),
    "/prompt":   dict(file_idx=None, kw_idx=1, keywords=["save", "load", "list", "delete"]),
    "/proc":     dict(file_idx=None, kw_idx=1, keywords=["list", "run", "help", "on", "off", "new"]),
    "/team":     dict(file_idx=None, kw_idx=1, keywords=["list", "show", "new", "run"]),
    "/var":      dict(file_idx=None, kw_idx=1, keywords=["set", "get", "del", "def", "extract", "save", "load"]),
    "/config":   dict(file_idx=None, kw_idx=1, keywords=["save", "load", "show", "auto", "del"]),
    "/proj":     dict(file_idx=None, kw_idx=1, keywords=["set", "status", "list", "save", "load", "show", "find", "keyword", "file", "index"]),
    "/alias":    dict(file_idx=None, kw_idx=1, keywords=["save", "clear"]),
    "/doc":      dict(file_idx=None, kw_idx=1, keywords=["list"]),
    "/translate": dict(file_idx=None, kw_idx=1, keywords=["setup", "on", "off", "status", "last"]),
    "/visual":    dict(file_idx=None, kw_idx=1, keywords=["setup", "status", "explain"]),
    "/web":       dict(file_idx=None, kw_idx=1, keywords=["search", "fetch"]),
    "/flow":      dict(file_idx=None, kw_idx=1, keywords=["list"]),
}


def _fuzzy_fix(token: str, candidates: list, cutoff: float = 0.65) -> str | None:
    """Return best match from candidates, or None if nothing close enough."""
    # 1. exact
    if token in candidates:
        return None
    # 2. prefix (unambiguous)
    prefix = [c for c in candidates if c.startswith(token)]
    if len(prefix) == 1:
        return prefix[0]
    # 3. edit distance
    matches = difflib.get_close_matches(token, candidates, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def _fix_path(path: str) -> str | None:
    """Fuzzy-match a missing file path against files in cwd (one level deep)."""
    if os.path.exists(path):
        return None
    candidates = []
    try:
        for entry in os.scandir("."):
            candidates.append(entry.name)
            if entry.is_dir() and not entry.name.startswith("."):
                try:
                    for sub in os.scandir(entry.path):
                        candidates.append(os.path.join(entry.name, sub.name).replace("\\", "/"))
                except OSError:
                    pass
    except OSError:
        return None
    return _fuzzy_fix(path, candidates, cutoff=0.65)


def fix_command(cmd: str, auto: bool = False, extra_cmds: list | None = None) -> str:
    """Check a 1bcoder command for common typos and fix them.

    Checks: command name, file path, subcommand/keyword.
    auto=True  — fix silently with a yellow warning (agent mode).
    auto=False — show the fix and ask Y/n (human mode).
    extra_cmds — additional command names to consider (e.g. active aliases).
    Returns the (possibly corrected) command string.
    """
    if not cmd.startswith("/"):
        return cmd

    tokens = cmd.split()
    fixes  = {}   # token_index → (original, corrected)

    # 1. command name
    cmd_name  = tokens[0]
    all_cmds  = _KNOWN_CMDS + (extra_cmds or [])
    fixed_name = _fuzzy_fix(cmd_name, all_cmds, cutoff=0.65)
    if fixed_name:
        fixes[0] = (tokens[0], fixed_name)
        tokens[0] = fixed_name
    cmd_root = tokens[0]

    spec = _CMD_SPEC.get(cmd_root)
    if spec:
        # 2. file path — skip for output commands where file need not exist yet
        #    also skip @ picker tokens (they are not real paths yet)
        fi = spec["file_idx"]
        if fi is not None and len(tokens) > fi and cmd_root not in ("/save",):
            if not tokens[fi].startswith("@"):
                fixed_path = _fix_path(tokens[fi])
                if fixed_path:
                    fixes[fi] = (tokens[fi], fixed_path)
                    tokens[fi] = fixed_path

        # 3. keyword / subcommand
        ki = spec["kw_idx"]
        kws = spec["keywords"]
        if ki is not None and kws and len(tokens) > ki:
            tok = tokens[ki].lower()
            if tokens[ki].startswith("@"):
                pass  # @ picker token — not a keyword
            # For /insert kw_idx=3: only fix if it looks like "code" (short word),
            # not if it's inline content like "SET_SLEEP_DELAY = 10"
            elif cmd_root == "/insert" and ki == 3 and len(tokens[ki]) > 6:
                pass  # long token → treat as inline text, not a keyword
            else:
                fixed_kw = _fuzzy_fix(tok, kws, cutoff=0.6)
                if fixed_kw and fixed_kw != tok:
                    fixes[ki] = (tokens[ki], fixed_kw)
                    tokens[ki] = fixed_kw

        # 4. for /save and /patch: also check LAST token for "code" keyword
        # Use prefix-only matching (no difflib) to avoid false positives on hint words
        # e.g. "model" would fuzzy-match "code" — prefix won't trigger on it
        if cmd_root in ("/save", "/patch") and len(tokens) > 2:
            last_i = len(tokens) - 1
            if last_i not in fixes and last_i != spec.get("file_idx"):
                if not tokens[last_i].startswith("@"):
                    tok = tokens[last_i].lower()
                    prefix_matches = [k for k in kws if k.startswith(tok)]
                    if len(prefix_matches) == 1 and prefix_matches[0] != tok:
                        fixes[last_i] = (tokens[last_i], prefix_matches[0])
                        tokens[last_i] = prefix_matches[0]

    if not fixes:
        return cmd

    # Rebuild command, preserving inline text after token[2] for /insert
    if cmd_root == "/insert":
        m = re.match(r"(\S+\s+\S+\s+\S+)(.*)", cmd, re.DOTALL)
        if m:
            prefix = " ".join(tokens[:3])
            fixed_cmd = prefix + m.group(2)
        else:
            fixed_cmd = " ".join(tokens)
    else:
        fixed_cmd = " ".join(tokens)

    # Report
    label = "[fix]" if auto else "[fix?]"
    summary = "  |  ".join(f"{o} → {n}" for _, (o, n) in sorted(fixes.items()))
    _warn(f"{label} {summary}")
    _info(f"       {fixed_cmd}")

    if not auto:
        try:
            ans = input("  apply? [Y/n]: ").strip().lower()
            if ans in ("n", "no"):
                return cmd
        except (EOFError, KeyboardInterrupt):
            return cmd

    return fixed_cmd


# ── CLI (--cli mode) ───────────────────────────────────────────────────────────


class CoderCLI:
    """Plain terminal REPL — no Textual, no widgets. Works in any shell or IDE terminal."""

    SEP = "─" * 40

    def _load_model_meta(self) -> None:
        """Fetch and cache model disk size, quantization, and native context window.

        Ollama  → /api/tags (size + quant) + /api/show (native num_ctx)
        OpenAI  → static lookup table for context; size stays None
        """
        self._meta_size: str | None = None
        self._meta_quant: str | None = None
        self._meta_ctx: int | None = None

        if self.provider == "ollama":
            try:
                resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
                resp.raise_for_status()
                for m in resp.json().get("models", []):
                    if m.get("name") == self.model:
                        if m.get("size"):
                            self._meta_size = _fmt_size(m["size"])
                        det = m.get("details", {})
                        q = det.get("quantization_level", "")
                        self._meta_quant = q[:6] or None
                        break
            except Exception:
                pass
            try:
                resp = requests.post(
                    f"{self.base_url}/api/show",
                    json={"model": self.model}, timeout=5,
                )
                resp.raise_for_status()
                data = resp.json()
                # model_info key varies by architecture; find any *context_length key
                ctx = None
                for key, val in data.get("model_info", {}).items():
                    if "context_length" in key:
                        ctx = int(val)
                        break
                # fallback: parse modelfile for PARAMETER num_ctx
                if ctx is None:
                    for line in data.get("modelfile", "").splitlines():
                        parts = line.split()
                        if len(parts) >= 3 and parts[0] == "PARAMETER" and parts[1] == "num_ctx":
                            ctx = int(parts[2])
                            break
                self._meta_ctx = ctx
            except Exception:
                pass

        elif self.provider == "openai":
            # static lookup for known OpenAI models (matched by prefix)
            for prefix, ctx in _OPENAI_CTX.items():
                if self.model.startswith(prefix):
                    self._meta_ctx = ctx
                    break
            # OpenAI-compatible local servers (LMStudio etc.) may expose extra fields
            if self._meta_ctx is None:
                try:
                    resp = requests.get(f"{self.base_url}/v1/models", timeout=5)
                    resp.raise_for_status()
                    for m in resp.json().get("data", []):
                        if m.get("id") == self.model:
                            ctx = m.get("context_length") or m.get("max_context_length")
                            if ctx:
                                self._meta_ctx = int(ctx)
                            break
                except Exception:
                    pass

        if self._meta_ctx:
            self.num_ctx = self._meta_ctx

    def _evict_kv_cache(self) -> None:
        """Unload the model from Ollama to evict the KV cache.

        Sends keep_alive=0 with no prompt — Ollama unloads the model immediately.
        The next request will reload the model with a completely clean KV cache.
        Ollama-only; prints a warning for other providers.
        """
        if self.provider != "ollama":
            print("[kv] KV eviction is Ollama-only (current provider: openai)")
            return
        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "keep_alive": 0},
                timeout=10,
            )
            resp.raise_for_status()
            print(f"[kv] model '{self.model}' unloaded — KV cache cleared")
        except Exception as e:
            print(f"[kv] failed to evict: {e}")

    def _short_model(self) -> str:
        """Truncate model name to fit a narrow terminal.

        lucasmg/deepseek-r1-8b:latest  → deepseek-r1:lates
        deepseek-coder:6.7b-instruct   → deepseek-c:6.7b-
        gemma3:1b                       → gemma3:1b
        """
        name = self.model
        if "/" in name:
            name = name.rsplit("/", 1)[1]
        if ":" in name:
            left, right = name.split(":", 1)
        else:
            left, right = name, ""
        left  = left[:10]
        right = right[:5]
        return f"{left}:{right}" if right else left

    def _file_picker(self, subdir: str = ".") -> dict:
        """Walk directory tree, number files only. Returns {n: path}."""
        mapping = {}
        counter = [1]
        SKIP = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.mypy_cache'}

        def walk(path: str, prefix: str = ""):
            try:
                entries = sorted(os.scandir(path), key=lambda e: (e.is_file(), e.name.lower()))
            except PermissionError:
                return
            entries = [e for e in entries if e.name not in SKIP]
            for i, entry in enumerate(entries):
                last = i == len(entries) - 1
                connector = "└── " if last else "├── "
                ext       = "    " if last else "│   "
                if entry.is_dir():
                    print(f"  {prefix}{connector}{_DIM}{entry.name}/{_R}")
                    walk(entry.path, prefix + ext)
                else:
                    n = counter[0]
                    mapping[n] = entry.path
                    counter[0] += 1
                    print(f"  {prefix}{connector}[{n}] {entry.name}")

        walk(subdir)
        return mapping

    def _resolve_at(self, user_input: str) -> str | None:
        """Replace the first @[dir] token (not @/) with a file path chosen from a tree picker."""
        m = re.search(r'@(?!/)(\S*)', user_input)
        if not m:
            return user_input
        subdir = m.group(1) or "."
        if not os.path.isdir(subdir):
            print(f"[picker] not a directory: {subdir}")
            return None
        mapping = self._file_picker(subdir)
        if not mapping:
            print("[picker] no files found")
            return None
        try:
            raw = input("pick: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            return None
        try:
            n = int(raw)
        except ValueError:
            print(f"[picker] expected a number, got: {raw}")
            return None
        path = mapping.get(n)
        if path is None:
            print(f"[picker] no file #{n}")
            return None
        return user_input[:m.start()] + path + user_input[m.end():]

    def _dir_picker(self, subdir: str = ".") -> dict:
        """Print a numbered tree of directories under subdir. Returns {n: dir_path}."""
        mapping: dict = {0: subdir}
        counter = [1]
        SKIP = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.mypy_cache'}
        print(f"  [0] {_DIM}./{_R}  (current directory)")

        def walk(path: str, prefix: str = ""):
            try:
                entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
            except PermissionError:
                return
            dirs = [e for e in entries if e.is_dir() and e.name not in SKIP]
            for i, entry in enumerate(dirs):
                last = i == len(dirs) - 1
                connector = "└── " if last else "├── "
                ext       = "    " if last else "│   "
                n = counter[0]
                mapping[n] = entry.path.replace("\\", "/")
                counter[0] += 1
                print(f"  {prefix}{connector}[{n}] {_DIM}{entry.name}/{_R}")
                walk(entry.path, prefix + ext)

        walk(subdir)
        return mapping

    def _resolve_at_dir(self, user_input: str) -> str | None:
        """Replace all @/suffix tokens with chosen_dir/suffix using a directory picker."""
        matches = list(re.finditer(r'@/(\S*)', user_input))
        if not matches:
            return user_input
        mapping = self._dir_picker(".")
        try:
            raw = input("pick dir: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            return None
        try:
            n = int(raw)
        except ValueError:
            print(f"[picker] expected a number, got: {raw}")
            return None
        chosen = mapping.get(n)
        if chosen is None:
            print(f"[picker] no directory #{n}")
            return None
        chosen = chosen.rstrip("/")
        result = user_input
        for m in reversed(matches):
            suffix = m.group(1)
            replacement = (chosen + "/" + suffix) if suffix else chosen
            result = result[:m.start()] + replacement + result[m.end():]
        return result

    def _autosave_prompt(self) -> None:
        """Ask user whether to save context; save to .1bcoder/autosave/ if confirmed."""
        if not self.messages:
            return
        try:
            ans = input("Save context before action? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ans == "n":
            return
        import datetime as _dt
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        save_dir = os.path.join(
            BCODER_DIR if os.path.isdir(BCODER_DIR) else HOME_BCODER_DIR,
            "autosave"
        )
        os.makedirs(save_dir, exist_ok=True)
        fpath = os.path.join(save_dir, f"{stamp}.txt")
        with open(fpath, "w", encoding="utf-8") as f:
            for msg in self.messages:
                f.write(f"=== {msg['role']} ===\n{msg['content']}\n\n")
        print(f"[autosave] context saved → {fpath}")

    def _do_autosave(self) -> None:
        """Silently save context after every response. Creates autosave file if no path set."""
        if not self._ctx_autosave_enabled or not self.messages:
            return
        if self._ctx_autosave_path is None:
            import datetime as _dt
            stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            save_dir = os.path.join(
                BCODER_DIR if os.path.isdir(BCODER_DIR) else HOME_BCODER_DIR,
                "autosave"
            )
            os.makedirs(save_dir, exist_ok=True)
            self._ctx_autosave_path = os.path.join(save_dir, f"{stamp}.txt")
        try:
            with open(self._ctx_autosave_path, "w", encoding="utf-8") as f:
                for msg in self.messages:
                    f.write(f"=== {msg['role']} ===\n{msg['content']}\n\n")
        except OSError:
            pass  # silent — don't interrupt the session

    def _print_status(self) -> None:
        """Print a single status line showing model, size, quant, native ctx, and usage."""
        est_tokens = sum(len(m["content"]) for m in self.messages) // 4
        pct = min(100, est_tokens * 100 // self.num_ctx)
        model_str = self._short_model()
        parts = [p for p in (self._meta_size, self._meta_quant) if p]
        if self._meta_ctx:
            parts.append(_fmt_ctx(self._meta_ctx))
        meta = f" [{' '.join(parts)}]" if parts else ""
        print(f"\033[2m {model_str}{meta}  │  ctx {est_tokens} / {self.num_ctx} ({pct}%)\033[0m")
        print()

    def __init__(self, base_url, model, models, provider="ollama"):
        self.base_url = base_url
        self.provider = provider
        self.model = model
        self.models = models
        self.messages = []
        self.last_reply = ""
        self.last_translated_reply = ""
        self.think_in_ctx = False  # False = strip <think> from context (default)
        self.think_show   = True   # True = show <think> blocks in terminal (default)
        self.num_ctx = NUM_CTX
        self.timeout = TIMEOUT     # HTTP read timeout in seconds (/param timeout N)
        self.params: dict = {}     # extra model params injected into every request
        self._meta_size: str | None = None
        self._meta_quant: str | None = None
        self._meta_ctx: int | None = None
        self._load_model_meta()
        self._auto_apply  = False   # True while agent is running with auto_apply
        self._in_agent    = False   # True while inside any agent loop (/agent, /ask)
        self._agent_msgs: list = [] # reference to active agent_msgs list (set in _run_agent_loop)
        self._agent_ctx:  int  = 0  # agent context limit (0 = use num_ctx); set via /tempctx N
        self._current_task: str = ""  # current agent task string — exposed as BCODER_TASK to procs
        self._proc_before: list = []  # procs that run BEFORE each LLM call
        self._proc_gates:  list = []  # procs that run after reply, PASS/FAIL gate
        self._savepoint  = None    # index into self.messages set by /ctx savepoint
        self._ctx_autosave_path: str | None = None   # set by /ctx load|save; auto on first message
        self._ctx_autosave_enabled: bool = False
        self._aliases    = self._load_aliases()  # global + local aliases.txt
        self._script_file = None
        self._proc_active: list[str] = []  # persistent procs (run after every reply)
        self._vars: dict = {}              # /var store — {{name}} placeholders
        self._role: str = "You are a software developer assistant."  # /role — system persona prepended to every chat request
        self._last_proc_stdout: str = ""   # saved last proc output for /var set
        self._last_output: str = ""        # universal last output (LLM, tool, proc) — $ and -> capture
        self._last_input:  str = ""        # last user message or command — ~ expansion
        self._hooks: dict = {}             # /hook — before/after command triggers
        self._mcp: dict = {}
        self._proj_key: str = ""       # active project key (/proj set)
        self._compose_queue: list = [] # /ctx compose queue (full resolved paths)
        self._last_find_results: list = []  # numbered results from last /proj find
        self._history: list[str] = []
        self.cmd_history: list[str] = []   # all /commands typed this session
        self.log_requests: bool = False    # /param log true — print request body + error detail
        # enable readline history if available
        try:
            import readline
            readline.set_history_length(200)
        except ImportError:
            pass

    # ── output / input helpers ─────────────────────────────────────────────────

    def _log(self, text: str = ""):
        print(text)

    def _sep(self, label: str = ""):
        if label:
            print(f"{_LBLUE}─── {label} " + "─" * (36 - len(label)) + _R)
        else:
            print(f"{_DIM}{self.SEP}{_R}")

    def _confirm(self, prompt: str, ctx_add: str = "") -> bool:
        if ctx_add:
            new_toks  = len(ctx_add) // 4
            cur_toks  = sum(len(m["content"]) for m in self.messages) // 4
            after_tok = cur_toks + new_toks
            pct       = min(100, after_tok * 100 // self.num_ctx)
            print(f"  {_DIM}+{_fmt_ctx(new_toks)} tok → {_fmt_ctx(after_tok)}/{_fmt_ctx(self.num_ctx)} ({pct}%){_R}")
        try:
            ans = input(prompt + " ").strip().lower()
            return ans in ("", "y", "yes")
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    def _prompt_input(self, prompt: str) -> str:
        try:
            return input(prompt + " ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""

    @staticmethod
    def _to_openai_messages(messages: list) -> list:
        """Convert internal message list to OpenAI format, expanding image messages."""
        result = []
        for m in messages:
            if "images" in m:
                content: list = []
                if m.get("content"):
                    content.append({"type": "text", "text": m["content"]})
                for b64 in m["images"]:
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    })
                result.append({"role": m["role"], "content": content})
            else:
                result.append(m)
        return result

    def _stream_chat(self, messages, hint: str = "") -> str:
        """POST to active provider, stream chunks to stdout. Returns full reply."""
        if self._role and not (messages and messages[0].get("role") == "system"):
            messages = [{"role": "system", "content": self._role}] + list(messages)
        chunks = []
        def _print(c):
            sys.stdout.write(c); sys.stdout.flush()
        try:
            if self.provider == "openai":
                body = {"model": self.model, "messages": self._to_openai_messages(messages), "stream": True}
                body.update(self.params)
                if self.log_requests:
                    print(f"  [log] POST {self.base_url}/v1/chat/completions")
                    print(f"  [log] model={self.model} messages={len(messages)} params={self.params}")
                with requests.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=body, stream=True, timeout=self.timeout,
                ) as resp:
                    resp.raise_for_status()
                    in_think = False
                    _had_thinking = False
                    _sep_done = False
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        text = line.decode() if isinstance(line, bytes) else line
                        if text.startswith("data: "):
                            text = text[6:]
                        if text == "[DONE]":
                            break
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        delta = (data.get("choices") or [{}])[0].get("delta", {})
                        # reasoning_content field (DeepSeek, LM Studio reasoning models)
                        reasoning = delta.get("reasoning_content") or ""
                        if reasoning and self.think_show:
                            _print(f"\033[90m{reasoning}\033[0m")
                            _had_thinking = True
                        chunk = delta.get("content") or ""
                        if chunk:
                            # apply <think>...</think> state machine (same as Ollama path)
                            while chunk:
                                if in_think:
                                    end = chunk.find('</think>')
                                    if end == -1:
                                        if self.think_show:
                                            _print(f"\033[90m{chunk}\033[0m")
                                        chunk = ""
                                    else:
                                        if self.think_show and end > 0:
                                            _print(f"\033[2m{chunk[:end]}\033[0m")
                                        in_think = False
                                        chunk = chunk[end + 8:]
                                else:
                                    start = chunk.find('<think>')
                                    if start == -1:
                                        if _had_thinking and not _sep_done:
                                            _print("\n")
                                            _sep_done = True
                                        _print(chunk)
                                        chunks.append(chunk)
                                        chunk = ""
                                    else:
                                        if start > 0:
                                            _print(chunk[:start])
                                            chunks.append(chunk[:start])
                                        in_think = True
                                        _had_thinking = True
                                        chunk = chunk[start + 7:]
            else:
                opts = {"num_ctx": self.num_ctx}
                opts.update(self.params)
                keep_alive = opts.pop("keep_alive", None)
                ollama_body = {"model": self.model, "messages": messages, "stream": True,
                               "options": opts}
                if keep_alive is not None:
                    ollama_body["keep_alive"] = keep_alive
                if self.log_requests:
                    print(f"  [log] POST {self.base_url}/api/chat")
                    print(f"  [log] model={self.model} messages={len(messages)} options={opts}"
                          + (f" keep_alive={keep_alive}" if keep_alive is not None else ""))
                with requests.post(
                    f"{self.base_url}/api/chat",
                    json=ollama_body,
                    stream=True, timeout=self.timeout,
                ) as resp:
                    resp.raise_for_status()
                    in_think = False
                    _had_thinking = False
                    _sep_done = False
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        data = json.loads(line)
                        msg = data.get("message", {})
                        # Ollama native thinking field (Qwen3, some models)
                        thinking = msg.get("thinking", "")
                        if thinking and self.think_show:
                            _print(f"\033[90m{thinking}\033[0m")
                            _had_thinking = True
                        chunk = msg.get("content", "")
                        if chunk:
                            # State machine: track <think>...</think> across tokens
                            while chunk:
                                if in_think:
                                    end = chunk.find('</think>')
                                    if end == -1:
                                        if self.think_show:
                                            _print(f"\033[90m{chunk}\033[0m")
                                        chunk = ""
                                    else:
                                        if self.think_show and end > 0:
                                            _print(f"\033[2m{chunk[:end]}\033[0m")
                                        in_think = False
                                        chunk = chunk[end + len('</think>'):]
                                else:
                                    start = chunk.find('<think>')
                                    if start == -1:
                                        if _had_thinking and not _sep_done:
                                            _print("\n")
                                            _sep_done = True
                                        _print(chunk)
                                        chunks.append(chunk)
                                        chunk = ""
                                    else:
                                        if start > 0:
                                            _print(chunk[:start])
                                            chunks.append(chunk[:start])
                                        in_think = True
                                        _had_thinking = True
                                        chunk = chunk[start + len('<think>'):]
                        if data.get("done"):
                            break
        except KeyboardInterrupt:
            print("\n[interrupted]")
            return None  # sentinel: interrupted (vs "" which means empty reply)
        except requests.exceptions.RequestException as e:
            print(f"\nerror: {e}")
            if hasattr(e, "response") and e.response is not None:
                body = e.response.text.strip()
                if body:
                    print(f"  detail: {body[:500]}")
                if self.log_requests:
                    print(f"  [log] status: {e.response.status_code}")
                    print(f"  [log] headers: {dict(e.response.headers)}")
            return ""
        print()
        reply = "".join(chunks)
        if not self.think_in_ctx:
            reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL).strip()
        return reply

    # ── REPL ──────────────────────────────────────────────────────────────────

    def run(self):
        print()
        print(BANNER)
        print()
        print(f"  model    : {self.model}")
        print(f"  host     : {self.base_url}")
        print(f"  provider : {self.provider}")
        print(f"  dir   : {os.getcwd()}")
        print()
        print("  /help for all commands   /init to create .1bcoder/ folder")
        print("  Ctrl+C interrupts stream   /exit to quit")
        print("  <cmd> -> var  capture output into variable   $ = last output   ~ = last input")
        print("  @ = file picker (type number)             @/ = dir prefix picker")
        print("  tip: /agent ask <question> to analyze code   /read <file> to load   /tree to explore")
        print()
        _auto_cfg = {}
        for _cfg_path in (CONFIG_FILE, GLOBAL_CONFIG_FILE):
            if os.path.isfile(_cfg_path):
                _c = self._load_config_file(_cfg_path)
                if _c.get("auto"):
                    _auto_cfg = _c
                    break
        if _auto_cfg:
            # host/model already applied at startup — only apply remaining settings
            _session_cfg = {k: v for k, v in _auto_cfg.items() if k not in ("host", "model")}
            self._apply_config(_session_cfg)
            print()
        self._translate_autoload()
        self._print_status()
        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                continue
            if user_input not in self._history or (self._history and self._history[-1] != user_input):
                self._history.append(user_input)
            _msg_count_before = len(self.messages)
            try:
                self._route(user_input)
            except Exception as _exc:
                import traceback as _tb
                import datetime as _dt
                print(f"\n[crash] {type(_exc).__name__}: {_exc}")
                _tb.print_exc()
                if self.messages:
                    _save_dir = os.path.join(
                        BCODER_DIR if os.path.isdir(BCODER_DIR) else HOME_BCODER_DIR,
                        "autosave"
                    )
                    _stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    _fpath = os.path.join(_save_dir, f"{_stamp}.txt")
                    try:
                        os.makedirs(_save_dir, exist_ok=True)
                        with open(_fpath, "w", encoding="utf-8") as _f:
                            for _m in self.messages:
                                _f.write(f"=== {_m['role']} ===\n{_m['content']}\n\n")
                        print(f"[crash] context auto-saved → {_fpath}")
                    except OSError as _e:
                        print(f"[crash] could not save context: {_e}")
            self._print_status()
            if len(self.messages) > _msg_count_before:
                self._do_autosave()
            if user_input.startswith("/"):
                import shlex as _shlex
                _args = user_input.split(None, 1)[1] if " " in user_input else ""
                try:
                    _parts = _shlex.split(_args)
                    self._last_input = _parts[0] if len(_parts) == 1 else _args
                except ValueError:
                    self._last_input = _args
            else:
                self._last_input = user_input

    # ── command routing ────────────────────────────────────────────────────────

    # commands excluded from cmd_history (session management, not reusable work)
    _HISTORY_SKIP = frozenset({
        "/exit", "/help", "/init", "/clear",
        "/model", "/host", "/ctx", "/script",
    })

    # shorthand aliases expanded before fix_command and routing
    _ALIASES = {
        }

    def _route(self, user_input: str, auto: bool = False):
        # ── /prompt save <name> suffix — works appended to any text ───────────
        _ps = re.search(r'\s+/prompt\s+save\s+(\S+)\s*$', user_input)
        if _ps:
            name = _ps.group(1).replace(" ", "-")
            text = user_input[:_ps.start()].strip()
            if text:
                self._save_prompt(name, text)
            else:
                print("[prompt] nothing to save — text was empty")
            return

        # ── capture suffix:  command -> varname ───────────────────────────────
        capture_var = None
        m = re.search(r'\s+->\s*(\w+)\s*$', user_input)
        if m:
            capture_var = m.group(1)
            user_input  = user_input[:m.start()].strip()

        if user_input.startswith("/"):
            user_input = self._ALIASES.get(user_input.split()[0], user_input)  # hardcoded shorthands
            user_input = self._expand_alias(user_input)                         # user-defined aliases
            user_input = fix_command(user_input, auto=auto, extra_cmds=list(self._aliases.keys()))
            if self._vars and "{{" in user_input:
                user_input = _apply_params(user_input, self._vars)
            # expand $ to last output, ~ to last input, @ to file picker
            if "$" in user_input:
                user_input = user_input.replace("$", self._last_output)
            if "~" in user_input:
                user_input = user_input.replace("~", self._last_input)
            if "@/" in user_input:
                resolved = self._resolve_at_dir(user_input)
                if resolved is None:
                    return
                user_input = resolved
            if "@" in user_input:
                resolved = self._resolve_at(user_input)
                if resolved is None:
                    return
                user_input = resolved
            cmd_root = user_input.split()[0]
            if cmd_root not in self._HISTORY_SKIP:
                self.cmd_history.append(user_input)
        else:
            # expand $ and ~ in plain chat messages too
            if "$" in user_input:
                user_input = user_input.replace("$", self._last_output)
            if "~" in user_input:
                user_input = user_input.replace("~", self._last_input)

        if capture_var:
            with _Tee() as tee:
                self._route(user_input, auto=auto)
            captured = tee.getvalue().strip()
            self._last_output = captured
            self._vars[capture_var] = captured
            _ok(f"[var] {capture_var} captured ({len(captured)} chars)")
            return

        if user_input == "/exit":
            self._autosave_prompt()
            sys.exit(0)
        elif user_input == "/about":
            self._cmd_about()
        elif user_input.startswith("/help"):
            self._cmd_help(user_input)
        elif user_input == "/init":
            self._cmd_init()
        elif user_input.startswith("/ctx"):
            self._cmd_ctx(user_input)
        elif user_input.startswith("/think"):
            parts = user_input.split()
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "include":
                self.think_in_ctx = True
                _ok("[think] <think> blocks kept in context")
            elif sub == "exclude":
                self.think_in_ctx = False
                _ok("[think] <think> blocks stripped from context")
            elif sub == "show":
                self.think_show = True
                _ok("[think] <think> blocks visible in terminal")
            elif sub == "hide":
                self.think_show = False
                _ok("[think] <think> blocks hidden in terminal")
            else:
                ctx_state  = "include" if self.think_in_ctx  else "exclude"
                show_state = "show"    if self.think_show     else "hide"
                print(f"[think] ctx:{ctx_state}  terminal:{show_state}  usage: /think include|exclude|show|hide")
        elif user_input.startswith("/format"):
            self._cmd_format(user_input)
        elif user_input.startswith("/param"):
            self._cmd_param(user_input)
        elif user_input in ("/clear", "/clear kv"):
            kv = user_input == "/clear kv"
            if kv:
                self._evict_kv_cache()
            else:
                self.messages.clear()
                self.last_reply = ""
                self.params.clear()
                self._vars.clear()
                self._last_proc_stdout = ""
                self._load_model_meta()   # re-detect num_ctx, forces Ollama model reload
                print("[context cleared]")
        elif user_input.startswith("/model"):
            self._cmd_model(user_input)
        elif user_input.startswith("/host"):
            self._cmd_host(user_input)
        elif user_input.startswith("/map"):
            self._cmd_map(user_input)
        elif user_input.startswith("/readln") or user_input.startswith("/read"):
            self._cmd_read(user_input)
        elif user_input.startswith("/view"):
            self._cmd_view(user_input)
        elif user_input.startswith("/insert"):
            _ha = self._extract_hook_args(user_input)
            if self._run_hook("before_insert", _ha):
                self._cmd_insert(user_input)
                self._run_hook("after_insert", _ha)
        elif user_input.startswith("/edit"):
            _ha = self._extract_hook_args(user_input)
            if self._run_hook("before_edit", _ha):
                self._cmd_edit(user_input)
                self._run_hook("after_edit", _ha)
        elif user_input.startswith("/save"):
            self._cmd_save(user_input)
        elif user_input.startswith("/run"):
            parts = user_input.split(None, 1)
            if len(parts) < 2:
                print("usage: /run <command>")
            else:
                if self._run_hook("before_run", {"content": parts[1]}):
                    self._cmd_run(parts[1])
                    self._run_hook("after_run", {"content": parts[1]})
        elif user_input.startswith("/script"):
            self._cmd_script(user_input)
        elif user_input.startswith("/prompt"):
            self._cmd_prompt(user_input)
        elif user_input.startswith("/translate"):
            self._cmd_translate(user_input)
        elif user_input.startswith("/visual"):
            self._cmd_visual(user_input)
        elif user_input.startswith("/web"):
            self._cmd_web(user_input)
        elif user_input.startswith("/flow"):
            self._cmd_flow(user_input)
        elif user_input.startswith("/proc"):
            self._cmd_proc(user_input)
        elif user_input.startswith("/mcp"):
            self._cmd_mcp(user_input)
        elif user_input.startswith("/parallel"):
            self._cmd_parallel(user_input)
        elif user_input.startswith("/patch"):
            _ha = self._extract_hook_args(user_input)
            if self._run_hook("before_patch", _ha):
                self._cmd_patch(user_input)
                self._run_hook("after_patch", _ha)
        elif user_input.startswith("/fim"):
            self._cmd_fill(user_input)
        elif user_input.startswith("/fix"):
            _ha = self._extract_hook_args(user_input)
            if self._run_hook("before_fix", _ha):
                self._cmd_fix(user_input)
                self._run_hook("after_fix", _ha)
        elif user_input.startswith("/hook"):
            self._cmd_hook(user_input)
        elif user_input.startswith("/bkup"):
            self._cmd_bkup(user_input)
        elif user_input.startswith("/diff"):
            self._cmd_diff(user_input)
        elif user_input.startswith("/alias"):
            self._cmd_alias(user_input)
        elif user_input.startswith("/tempctx"):
            self._cmd_tempctx(user_input)
        elif user_input.startswith("/ask"):  # keep /ask before generic named-agent fallthrough
            self._cmd_ask(user_input)
        elif user_input.startswith("/agent"):
            self._cmd_agent(user_input)
        elif user_input.startswith("/tree"):
            self._cmd_tree(user_input)
        elif user_input.startswith("/find"):
            self._cmd_find(user_input)
        elif user_input.startswith("/team"):
            self._cmd_team(user_input)
        elif user_input.startswith("/var"):
            self._cmd_var(user_input)
        elif user_input.startswith("/text"):
            self._cmd_note()
        elif user_input.startswith("/role"):
            self._cmd_role(user_input)
        elif user_input.startswith("/config"):
            self._cmd_config(user_input)
        elif user_input.startswith("/proj"):
            self._cmd_proj(user_input)
        elif user_input.startswith("/doc"):
            self._cmd_doc(user_input)
        elif user_input.startswith("/"):
            # check if it's a named agent (e.g. /dbsearcher task)
            name = user_input.split()[0][1:]   # strip leading /
            task = user_input[len(name) + 1:].strip()
            agent_path = self._find_agent_def(name)
            if agent_path:
                self._run_named_agent(name, task, agent_path)
            else:
                _err(f"unknown command: /{name}  (type /help for commands)")
        else:
            _tr_cfg = self._translate_load_cfg() if os.path.isfile(self._TRANSLATE_CFG) else {}
            _tr_on  = _tr_cfg.get("enabled") and _tr_cfg.get("lang") and _tr_cfg.get("lang") != "en"
            _tr_lang = _tr_cfg.get("lang", "en") if _tr_on else "en"
            msg_content = user_input
            if _tr_on:
                _translated_input = self._translate_run(user_input, _tr_lang, "en")
                if _translated_input:
                    msg_content = _translated_input
                    print(f"  [{_tr_lang}→en] {msg_content}")
                else:
                    print(f"  [{_tr_lang}→en] [translation failed — sending original]")
            self.messages.append({"role": "user", "content": msg_content})
            self._sep("AI")
            reply = self._stream_chat(self.messages)
            if reply:
                self.last_reply = reply
                self._last_output = reply
                self.messages.append({"role": "assistant", "content": reply})
                if _tr_on:
                    print(f"\n[translating en → {_tr_lang}...]", end="", flush=True)
                    translated_reply = self._translate_run(reply, "en", _tr_lang)
                    print(f"\r\033[K{_LBLUE}─── {_tr_lang.upper()} ───{_R}")
                    if translated_reply:
                        print(translated_reply)
                        self._last_output = translated_reply
                        self.last_translated_reply = translated_reply
                    else:
                        print("[translation failed]")
                else:
                    for _proc in self._proc_active:
                        self._run_proc(_proc, auto=True)
            elif self.messages:
                self.messages.pop()

    # ── commands ───────────────────────────────────────────────────────────────

    def _cmd_init(self):
        existed = os.path.isdir(BCODER_DIR)
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        os.makedirs(AGENTS_DIR, exist_ok=True)

        agent_path = os.path.join(BCODER_DIR, "agent.txt")
        if not os.path.exists(agent_path):
            with open(agent_path, "w", encoding="utf-8") as f:
                f.write("""\
# 1bcoder agent configuration
# max_turns     : max tool calls per /agent session
# auto_apply    : apply edits without confirmation prompts
# tools         : tools for /agent (one per line, indented) — minimal set for small models
# advanced_tools: tools for /agent advance — full set for larger models

max_turns = 10
auto_apply = true

tools =
    read
    insert
    save
    patch

advanced_tools =
    read
    run
    insert
    save
    bkup
    diff
    patch
    tree
    find
    map index
    map find
    map idiff
    map diff
    map trace
    map keyword
    help
""")
            print(f"  created  agent.txt")

        profiles_path = os.path.join(BCODER_DIR, "profiles.txt")
        if not os.path.exists(profiles_path):
            with open(profiles_path, "w", encoding="utf-8") as f:
                f.write("""\
# 1bcoder parallel profiles
# Format: name: host|model|outfile host|model|outfile  # optional comment
# Use /parallel profile create to add profiles interactively.
#
# Example:
# review: localhost:11434|ministral3:3b|ans/review.txt localhost:11435|cogito:3b|ans/tests.txt  # code review + unit tests
""")
            print(f"  created  profiles.txt")

        if existed:
            print(f"[init] .1bcoder already existed — missing files created if any")
        else:
            print(f"[init] created .1bcoder/ in {WORKDIR}")

    _FORMAT_MARKER = "Return ONLY text in the requested format."

    def _cmd_format(self, user_input: str):
        fmt = user_input[7:].strip()
        if not fmt:
            print("usage: /format <description>  |  /format clear")
            return
        if fmt == "clear":
            before = len(self.messages)
            self.messages = [m for m in self.messages
                             if not m.get("content", "").startswith(self._FORMAT_MARKER)]
            removed = before - len(self.messages)
            _ok(f"[format] cleared ({removed} message(s) removed)")
            return
        constraint = (
            f"{self._FORMAT_MARKER}\n"
            f"Format: {fmt}\n"
            f"No explanation. No preamble. No repetition of the task. "
            f"No markdown (no headers, no bold, no bullet points, no numbered lists). "
            f"No code fences. No emojis. No <think> blocks. Answer only."
        )
        self.messages.append({"role": "user", "content": constraint})
        _ok(f"[format] applied: {fmt}")

    def _cmd_param(self, user_input: str):
        tokens = user_input.split(None, 2)
        if len(tokens) == 1:
            print(f"  timeout        = {self.timeout}s")
            print(f"  num_ctx        = {self.num_ctx}")
            print(f"  think_exclude  = {not self.think_in_ctx}  (strip <think> blocks from context)")
            print(f"  think_show     = {self.think_show}  (show <think> blocks in terminal)")
            print(f"  log            = {self.log_requests}  (print request body + error detail)")
            print(f"  run_timeout    = {self.params.get('run_timeout', 30)}s  (0 = no timeout, for /run commands)")
            print(f"  ask_limit      = {self.params.get('ask_limit', ASK_RESULT_LIMIT_CHARS)}  (chars, /ask truncation limit)")
            print(f"  ask_show       = {self.params.get('ask_show',  ASK_RESULT_SHOW_CHARS)}  (chars shown when truncated)")
            model_params = {k: v for k, v in self.params.items() if k not in ("ask_limit", "ask_show")}
            if model_params:
                print("  ── model params ──")
                for k, v in model_params.items():
                    print(f"  {k} = {v}")
            return
        if tokens[1] == "clear":
            self.params.clear()
            self.timeout = TIMEOUT
            _ok(f"[params cleared — timeout reset to {TIMEOUT}s]")
            return
        if len(tokens) < 3:
            print("usage: /param <key> <value>  |  /param  |  /param clear")
            return
        key, raw_val = tokens[1], tokens[2]
        # auto-cast: bool → Python bool, number → float/int, else str
        if raw_val.lower() == "true":
            val = True
        elif raw_val.lower() == "false":
            val = False
        else:
            try:
                val = int(raw_val)
            except ValueError:
                try:
                    val = float(raw_val)
                except ValueError:
                    val = raw_val
        if key == "timeout":
            try:
                self.timeout = int(val)
                _ok(f"[param] timeout = {self.timeout}s")
            except (ValueError, TypeError):
                _err("timeout must be an integer number of seconds")
            return
        if key == "think_exclude":
            self.think_in_ctx = not bool(val)
            _ok(f"[param] think_exclude = {not self.think_in_ctx}")
            return
        if key == "think_show":
            self.think_show = bool(val)
            _ok(f"[param] think_show = {self.think_show}")
            return
        if key == "log":
            self.log_requests = bool(val)
            _ok(f"[param] log = {self.log_requests}")
            return
        self.params[key] = val
        _ok(f"[param] {key} = {val}")

    def _cmd_ctx(self, user_input: str):
        parts = user_input.split()
        if len(parts) < 2:
            est = sum(len(m["content"]) for m in self.messages) // 4
            print(f"[ctx limit: {self.num_ctx}  current: ~{est:,}]  usage: /ctx <n> | clear [N] | cut | compact | save <f> | load <f> | savepoint [set|rollback|compact|show]")
            return
        if parts[1] == "cut":
            est = sum(len(m["content"]) for m in self.messages) // 4
            if est <= self.num_ctx:
                print(f"[ctx: ~{est:,} / {self.num_ctx} — within limit, nothing to cut]")
                return
            removed = 0
            while self.messages and sum(len(m["content"]) for m in self.messages) // 4 > self.num_ctx:
                self.messages.pop(0)
                removed += 1
            print(f"[ctx cut: removed {removed} oldest message(s)]")
            return
        if parts[1] == "save":
            if len(parts) < 3:
                print("usage: /ctx save <name>  (saves to .1bcoder/ctx/<name>.txt)")
                return
            if not self.messages:
                print("[context is empty]")
                return
            save_path = parts[2]
            if not os.path.dirname(save_path):
                if not save_path.endswith(".txt") and not save_path.endswith(".md"):
                    save_path += ".txt"
                os.makedirs(CTX_DIR, exist_ok=True)
                save_path = os.path.join(CTX_DIR, save_path)
            try:
                with open(save_path, "w", encoding="utf-8") as f:
                    for msg in self.messages:
                        f.write(f"=== {msg['role']} ===\n{msg['content']}\n\n")
                self._ctx_autosave_path = save_path
                print(f"[context saved to {save_path} ({len(self.messages)} messages)]")
            except OSError as e:
                _err(e)
            return
        if parts[1] == "load":
            if len(parts) < 3:
                print("usage: /ctx load <file>")
                return
            load_path = parts[2]
            if not os.path.dirname(load_path) and not os.path.isfile(load_path):
                candidate = os.path.join(CTX_DIR, load_path)
                if os.path.isfile(candidate):
                    load_path = candidate
            try:
                with open(load_path, "r", encoding="utf-8") as f:
                    text = f.read()
                loaded = []
                current_role = "user"
                for block in re.split(r'=== (user|assistant|system) ===\n', text):
                    block = block.strip()
                    if not block:
                        continue
                    if block in ("user", "assistant", "system"):
                        current_role = block
                    else:
                        loaded.append({"role": current_role, "content": block})
                if not loaded:
                    print(f"[no messages found in {load_path}]")
                    return
                self.messages.extend(loaded)
                self._ctx_autosave_path = load_path
                last_a = next((m["content"] for m in reversed(loaded) if m["role"] == "assistant"), "")
                if last_a:
                    self.last_reply = last_a
                    self._last_output = last_a
                print(f"[loaded {len(loaded)} messages from {load_path}]")
            except FileNotFoundError:
                print(f"file not found: {load_path}")
            except OSError as e:
                _err(e)
            return
        if parts[1] == "savepoint":
            sub = parts[2] if len(parts) > 2 else "set"
            if sub == "set":
                self._savepoint = len(self.messages)
                est = sum(len(m["content"]) for m in self.messages) // 4
                _ok(f"[ctx savepoint] set at message {self._savepoint} (~{est:,} tokens)")
            elif sub == "rollback":
                if self._savepoint is None:
                    print("[ctx savepoint] no savepoint set — use /ctx savepoint set first")
                    return
                removed = len(self.messages) - self._savepoint
                del self.messages[self._savepoint:]
                self._savepoint = None
                _ok(f"[ctx savepoint] rolled back {removed} message(s)")
            elif sub == "compact":
                print("[ctx savepoint compact] use /ctx compact savepoint instead")
            elif sub == "show":
                if self._savepoint is None:
                    print("[ctx savepoint] not set")
                else:
                    since = len(self.messages) - self._savepoint
                    est   = sum(len(m["content"]) for m in self.messages[self._savepoint:]) // 4
                    print(f"[ctx savepoint] at message {self._savepoint}, {since} message(s) since (~{est:,} tokens)")
            else:
                print("usage: /ctx savepoint [set|rollback|compact|show]")
            return
        if parts[1] == "clear":
            if len(parts) > 2 and parts[2] == "view":
                # /ctx clear view [last] [N|all]
                rest = parts[3:]
                from_end = rest and rest[0] == "last"
                if from_end:
                    rest = rest[1:]
                if rest and rest[0] == "all":
                    n = None  # all
                elif rest and rest[0].isdigit():
                    n = int(rest[0])
                else:
                    n = 1
                img_indices = [i for i, m in enumerate(self.messages) if "images" in m]
                if not img_indices:
                    print("[ctx clear view] no images in context")
                    return
                targets = img_indices if n is None else (
                    img_indices[-n:] if from_end else img_indices[:n]
                )
                for i in targets:
                    del self.messages[i]["images"]
                direction = "last" if from_end else "oldest"
                count_label = "all" if n is None else str(len(targets))
                print(f"[ctx clear view] stripped images from {count_label} {direction} message(s)")
                return
            if len(parts) > 2 and parts[2].isdigit():
                n = int(parts[2])
                if n <= 0:
                    print(f"[ctx clear] n must be positive")
                    return
                actual = min(n, len(self.messages))
                del self.messages[-actual:]
                self.last_reply = ""
                print(f"[ctx clear] removed last {actual} message(s), {len(self.messages)} remaining")
                return
            self.messages.clear()
            self.last_reply = ""
            print(f"[context cleared — params and num_ctx ({self.num_ctx}) preserved]")
            return
        if parts[1] == "compose":
            self._ctx_compose(user_input)
            return
        if parts[1] == "compact":
            # /ctx compact N — compact last N messages in place
            if len(parts) > 2 and parts[2].isdigit():
                n = int(parts[2])
                if n <= 0 or n > len(self.messages):
                    print(f"[ctx compact] N must be 1..{len(self.messages)}")
                    return
                to_compact = self.messages[-n:]
                compact_prompt = (
                    f"Summarize these {n} message(s) into a concise context block. "
                    "Include: files read, changes made, decisions, key findings. "
                    "Plain text only. No code fences."
                )
                print(f"[ctx compact] summarizing last {n} message(s)...")
                summary = self._stream_chat(to_compact + [{"role": "user", "content": compact_prompt}])
                print()
                if not summary:
                    print("[ctx compact] failed — context unchanged")
                    return
                del self.messages[-n:]
                self.messages.append({"role": "assistant", "content": f"[compact of {n} message(s)]\n{summary}"})
                _ok(f"[ctx compact] {n} message(s) → 1 summary")
                self._last_output = summary
                return
            # parse: /ctx compact [savepoint] [profile: <name>]
            rest_parts  = parts[2:]
            use_savepoint = "savepoint" in rest_parts
            profile_name  = None
            for idx, p in enumerate(rest_parts):
                if p == "profile:" and idx + 1 < len(rest_parts):
                    profile_name = rest_parts[idx + 1]
                    break
                if p.startswith("profile:") and len(p) > 8:
                    profile_name = p[8:]
                    break

            if use_savepoint:
                if self._savepoint is None:
                    print("[ctx compact] no savepoint set — use /ctx savepoint set first")
                    return
                to_compact = self.messages[self._savepoint:]
                scope_label = "since savepoint"
            else:
                to_compact = list(self.messages)
                scope_label = "full context"

            if not to_compact:
                print(f"[ctx compact] nothing to compact ({scope_label})")
                return

            compact_prompt = (
                "Summarize the above into a concise but complete context block. "
                "Include: files read, changes made, decisions, key findings, current state. "
                "Plain text only. No code fences."
            ) if use_savepoint else (
                "Summarize this entire conversation into a concise but complete context block. "
                "Include: files read, changes made, decisions, key findings, current state of the code. "
                "Plain text only. No code fences. Be thorough — this summary replaces the full history."
            )
            summary_msgs = to_compact + [{"role": "user", "content": compact_prompt}]

            if profile_name:
                # use external model via profile
                workers = _load_profile(profile_name)
                if not workers:
                    print(f"[ctx compact] profile '{profile_name}' not found")
                    return
                host, model, _ = workers[0]
                url, prov = parse_host(host)
                print(f"[ctx compact] summarizing {scope_label} via {model}...")
                try:
                    if prov == "openai":
                        resp = requests.post(f"{url}/v1/chat/completions",
                                             json={"model": model, "messages": summary_msgs, "stream": False},
                                             timeout=300)
                        resp.raise_for_status()
                        summary = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
                    else:
                        resp = requests.post(f"{url}/api/chat",
                                             json={"model": model, "messages": summary_msgs,
                                                   "stream": False, "options": {"num_ctx": self.num_ctx}},
                                             timeout=300)
                        resp.raise_for_status()
                        summary = resp.json().get("message", {}).get("content", "")
                except Exception as e:
                    print(f"[ctx compact] error calling {model}: {e}")
                    return
            else:
                print(f"[ctx compact] summarizing {scope_label}...")
                self._sep("AI")
                summary = self._stream_chat(summary_msgs)

            if not summary:
                print("[ctx compact] failed — context unchanged")
                return

            self._autosave_prompt()
            if use_savepoint:
                del self.messages[self._savepoint:]
                self.messages.append({"role": "user", "content": f"[summary since savepoint]\n{summary}"})
                self._savepoint = None
                _ok(f"[ctx compact] {len(to_compact)} message(s) → summary ({scope_label})")
            else:
                self.messages.clear()
                self.messages.append({"role": "user", "content": f"[session summary]\n{summary}"})
                _ok(f"[ctx compact] context replaced with summary ({len(summary)} chars)")
            self._last_output = summary
            return
        if parts[1] == "list":
            if not os.path.isdir(CTX_DIR):
                print(f"[ctx] no ctx folder found — create .1bcoder/ctx/ and save context files there")
                return
            files = sorted(f for f in os.listdir(CTX_DIR)
                           if os.path.isfile(os.path.join(CTX_DIR, f)))
            if not files:
                print(f"[ctx] .1bcoder/ctx/ is empty")
                return
            print(f"[ctx] .1bcoder/ctx/  ({len(files)} file(s)):")
            for fname in files:
                size = os.path.getsize(os.path.join(CTX_DIR, fname))
                print(f"  {fname}  ({size:,} bytes)")
            return
        if parts[1] == "autosave":
            sub = parts[2] if len(parts) > 2 else "show"
            if sub == "on":
                self._ctx_autosave_enabled = True
                path_note = f" → {self._ctx_autosave_path}" if self._ctx_autosave_path else " (new file on next message)"
                _ok(f"[ctx autosave] enabled{path_note}")
            elif sub == "off":
                self._ctx_autosave_enabled = False
                _ok("[ctx autosave] disabled")
            else:
                state = "on" if self._ctx_autosave_enabled else "off"
                path_note = self._ctx_autosave_path or "(none)"
                print(f"[ctx autosave] {state}  file: {path_note}")
            return
        try:
            self.num_ctx = int(parts[1])
            print(f"[ctx set to {self.num_ctx} tokens]")
        except ValueError:
            print("usage: /ctx <number> | cut | compact [N] | save <f> | load <f> | compose ...")

    def _resolve_ctx_path(self, fname: str) -> str:
        """Resolve ctx file path: full path → as-is; bare name → ctx/ then projects/<key>/;
        prefix/name → projects/prefix/name."""
        if os.path.isfile(fname):
            return fname
        # bare filename — no directory component
        if not os.path.dirname(fname):
            candidate = os.path.join(CTX_DIR, fname)
            if os.path.isfile(candidate):
                return candidate
            if self._proj_key:
                candidate = os.path.join(PROJECTS_DIR, self._proj_key, fname)
                if os.path.isfile(candidate):
                    return candidate
            return fname  # not found — return as-is, caller handles error
        # has directory component like ABC-123/file.txt
        candidate = os.path.join(PROJECTS_DIR, fname)
        if os.path.isfile(candidate):
            return candidate
        return fname

    def _ctx_compose(self, user_input: str):
        """Compose ctx files with content-level dedup.

        /ctx compose add <file|N,M,N-M|all>   add to queue (N refs last /proj find)
        /ctx compose list                          show queue with sizes
        /ctx compose clear                         clear queue
        /ctx compose run [output.txt]              merge queue → file (dedup by content)
        /ctx compose <f1> <f2> ...                 direct compose → print or load
        """
        tokens = user_input.split(None, 2)  # ["/ctx", "compose", rest]
        rest   = tokens[2].strip() if len(tokens) > 2 else ""
        sub    = rest.split()[0] if rest else ""

        def _parse_ctx_file(path: str) -> list:
            """Parse ctx file → list of {role, content} dicts."""
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                _err(f"cannot read {path}: {e}")
                return []
            msgs = []
            current_role = "user"
            for block in re.split(r'=== (user|assistant|system) ===\n', text):
                block = block.strip()
                if not block:
                    continue
                if block in ("user", "assistant", "system"):
                    current_role = block
                else:
                    msgs.append({"role": current_role, "content": block})
            return msgs

        def _queue_size_info():
            total = sum(os.path.getsize(p) for p in self._compose_queue if os.path.isfile(p))
            return f"{len(self._compose_queue)} file(s)  /  {total:,} bytes total"

        def _do_append(path: str):
            resolved = self._resolve_ctx_path(path)
            if not os.path.isfile(resolved):
                _err(f"file not found: {path}")
                return
            size = os.path.getsize(resolved)
            self._compose_queue.append(resolved)
            _ok(f"[compose] + {os.path.basename(resolved)}  ({size:,} bytes)")
            print(f"[compose] queue: {_queue_size_info()}")

        if sub == "add":
            arg = rest.split(None, 1)[1].strip() if len(rest.split(None, 1)) > 1 else ""
            if not arg:
                print("usage: /ctx compose add <file|N,M|all>")
                return
            if arg == "all":
                if not self._last_find_results:
                    print("[compose] no /proj find results — run /proj find first")
                    return
                for p in self._last_find_results:
                    _do_append(p)
            elif re.match(r'^[\d,\s]+$', arg):
                # comma-separated numbers
                indices = [int(x.strip()) - 1 for x in arg.split(",") if x.strip().isdigit()]
                for idx in indices:
                    if 0 <= idx < len(self._last_find_results):
                        _do_append(self._last_find_results[idx])
                    else:
                        _warn(f"[compose] no result #{idx + 1}")
            else:
                _do_append(arg)

        elif sub == "list":
            if not self._compose_queue:
                print("[compose] queue is empty")
                return
            running = 0
            for i, p in enumerate(self._compose_queue, 1):
                size = os.path.getsize(p) if os.path.isfile(p) else 0
                running += size
                print(f"  {i}. {os.path.basename(p)}  ({size:,} bytes)  [total: {running:,}]")

        elif sub == "clear":
            self._compose_queue.clear()
            _ok("[compose] queue cleared")

        elif sub == "run":
            run_args = rest.split(None, 1)
            output = run_args[1].strip() if len(run_args) > 1 else ""
            if not self._compose_queue:
                print("[compose] queue is empty — use /ctx compose add first")
                return
            self._ctx_compose_merge(self._compose_queue, output)

        elif rest and not sub.startswith(".") and not os.path.sep in sub and sub not in ("add", "list", "clear", "run"):
            # treat whole rest as space-separated file list (direct mode)
            files = rest.split()
            if not files:
                print("usage: /ctx compose <f1> <f2> ... | append | list | clear | run")
                return
            self._ctx_compose_merge(files, "")

        else:
            print("usage: /ctx compose add <file|N,M|all> | list | clear | run [output.txt]")
            print("       /ctx compose <f1> <f2> ...   (direct mode)")

    def _ctx_compose_merge(self, file_list: list, output: str):
        """Merge ctx files with content-level dedup, write to output or load into context."""
        all_msgs = []
        seen = set()
        for path in file_list:
            resolved = self._resolve_ctx_path(path)
            if not os.path.isfile(resolved):
                _warn(f"[compose] skipping missing: {path}")
                continue
            msgs = []
            current_role = "user"
            try:
                with open(resolved, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                _err(f"cannot read {resolved}: {e}")
                continue
            for block in re.split(r'=== (user|assistant|system) ===\n', text):
                block = block.strip()
                if not block:
                    continue
                if block in ("user", "assistant", "system"):
                    current_role = block
                else:
                    msgs.append({"role": current_role, "content": block})
            added = 0
            for msg in msgs:
                key = msg["content"]
                if key not in seen:
                    seen.add(key)
                    all_msgs.append(msg)
                    added += 1
            skipped = len(msgs) - added
            print(f"  {os.path.basename(resolved)}: {added} messages added, {skipped} duplicates skipped")

        if not all_msgs:
            print("[compose] nothing to write")
            return

        total_chars = sum(len(m["content"]) for m in all_msgs)
        if output:
            if not os.path.dirname(output):
                if not output.endswith(".txt") and not output.endswith(".md"):
                    output += ".txt"
                os.makedirs(CTX_DIR, exist_ok=True)
                output = os.path.join(CTX_DIR, output)
            try:
                with open(output, "w", encoding="utf-8") as f:
                    for msg in all_msgs:
                        f.write(f"=== {msg['role']} ===\n{msg['content']}\n\n")
                _ok(f"[compose] {len(all_msgs)} messages → {output}  (~{total_chars // 4:,} tokens)")
                _info(f"  Load with: /ctx load {output}")
            except OSError as e:
                _err(f"cannot write {output}: {e}")
        else:
            # no output file — load directly into context
            self.messages.extend(all_msgs)
            _ok(f"[compose] {len(all_msgs)} messages loaded into context  (~{total_chars // 4:,} tokens)")

    def _cmd_tempctx(self, user_input: str):
        """Manage the agent's internal context limit and message history."""
        parts = user_input.split()
        sub   = parts[1] if len(parts) > 1 else ""

        # /tempctx N — set agent context limit (works outside agent too)
        if sub.isdigit():
            self._agent_ctx = int(sub)
            _ok(f"[tempctx] agent context limit set to {self._agent_ctx:,} tokens")
            return

        if not self._in_agent:
            limit = self._agent_ctx or self.num_ctx
            src   = f"set to {self._agent_ctx:,}" if self._agent_ctx else f"default ({self.num_ctx:,})"
            print(f"[tempctx] agent context limit: {src} tokens")
            print("usage: /tempctx N | cut | clear | show")
            return

        limit = self._agent_ctx or self.num_ctx
        if sub == "cut":
            before = len(self._agent_msgs)
            while (sum(len(m["content"]) for m in self._agent_msgs) // 4 > limit
                   and len(self._agent_msgs) > 2):
                self._agent_msgs.pop(1)   # keep index 0 (system msg)
            removed = before - len(self._agent_msgs)
            est = sum(len(m["content"]) for m in self._agent_msgs) // 4
            print(f"[tempctx cut] removed {removed} message(s) — ~{est:,}/{limit:,} tokens remaining")
        elif sub == "clear":
            del self._agent_msgs[2:]      # keep system msg + initial task
            est = sum(len(m["content"]) for m in self._agent_msgs) // 4
            print(f"[tempctx clear] reset to system+task — ~{est:,} tokens")
        elif sub == "show":
            est = sum(len(m["content"]) for m in self._agent_msgs) // 4
            pct = est * 100 // limit
            src = f"set" if self._agent_ctx else "= num_ctx"
            print(f"[tempctx] {len(self._agent_msgs)} messages  ~{est:,}/{limit:,} tokens ({pct}%)  limit {src}")
        else:
            print("usage: /tempctx N | cut | clear | show")
            print("  N     — set agent context limit to N tokens (persists across agents)")
            print("  cut   — remove oldest messages until context fits within limit")
            print("  clear — reset to system prompt + task only")
            print("  show  — display agent context usage vs limit")

    def _cmd_model(self, user_input: str = ""):
        tokens = user_input.split()
        save_ctx  = "-sc" in tokens or "save-context" in tokens
        clear_kv  = "-cc" in tokens or "clear-cache" in tokens
        flag_strs = {"-sc", "save-context", "-cc", "clear-cache"}
        args = [t for t in tokens[1:] if t not in flag_strs]

        # evict KV cache of the current model before switching
        if clear_kv:
            self._evict_kv_cache()

        # refresh model list from server
        try:
            refreshed = list_models(self.base_url, self.provider)
            if refreshed:
                self.models = refreshed
        except Exception:
            pass

        if args:
            # /model <name> — set directly by name
            name = args[0]
            if name not in self.models:
                print(f"[model] '{name}' not in available models — connecting anyway")
            self.model = name
            self._load_model_meta()
            if not save_ctx:
                self.messages.clear()
                print(f"[switched to {self.model}, context cleared]")
            else:
                print(f"[switched to {self.model}, context kept]")
            flags = (" -sc" if save_ctx else "") + (" -cc" if clear_kv else "")
            self.cmd_history.append(f"/model {name}{flags}")
            return

        # interactive selection by number
        print("Available models:")
        for i, m in enumerate(self.models, 1):
            print(f"  {i}. {m}")
        raw = self._prompt_input("  type number:")
        if not raw:
            print("[cancelled]")
            return
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(self.models):
                self.model = self.models[idx]
                self._load_model_meta()
                if not save_ctx:
                    self.messages.clear()
                    print(f"[switched to {self.model}, context cleared]")
                else:
                    print(f"[switched to {self.model}, context kept]")
                flags = (" -sc" if save_ctx else "") + (" -cc" if clear_kv else "")
                self.cmd_history.append(f"/model {self.model}{flags}")
            else:
                print("invalid choice")
        except ValueError:
            print("type a number")

    def _cmd_host(self, user_input: str):
        tokens = user_input.split()
        save_ctx = "-sc" in tokens or "save-context" in tokens
        args = [t for t in tokens[1:] if t not in ("-sc", "save-context")]
        raw = args[0] if args else ""
        if not raw:
            print(f"[current host: {self.base_url} ({self.provider})]  usage: /host <url> [-sc]")
            return
        new_url, new_provider = parse_host(raw)
        try:
            new_models = list_models(new_url, new_provider)
            self.base_url = new_url
            self.provider = new_provider
            self.models = new_models
            self.model = new_models[0]
            self._load_model_meta()
            if not save_ctx:
                self.messages.clear()
                print(f"[connected to {new_url} ({new_provider}), model: {self.model}, context cleared]")
            else:
                print(f"[connected to {new_url} ({new_provider}), model: {self.model}, context kept]")
            self.cmd_history.append(f"/host {raw}" + (" -sc" if save_ctx else ""))
        except requests.exceptions.ConnectionError:
            print(f"cannot connect to {new_url}")
        except requests.exceptions.HTTPError as e:
            _err(e)

    # ── /tree ──────────────────────────────────────────────────────────────────

    def _cmd_tree(self, user_input: str):
        tokens = user_input.split()[1:]  # drop "/tree"

        # parse flags
        depth      = 4
        inject_ctx = False
        path_arg   = None

        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t == "-d" and i + 1 < len(tokens):
                try:
                    depth = int(tokens[i + 1])
                except ValueError:
                    _err(f"invalid depth: {tokens[i+1]}")
                    return
                i += 2
            elif t == "ctx":
                inject_ctx = True
                i += 1
            elif not t.startswith("-"):
                path_arg = t
                i += 1
            else:
                i += 1

        root = os.path.join(WORKDIR, path_arg) if path_arg else WORKDIR
        root = os.path.normpath(root)

        if not os.path.isdir(root):
            _err(f"not a directory: {root}")
            return

        display_root = path_arg if path_arg else os.path.basename(root)

        # ── build tree ────────────────────────────────────────────────────
        term_lines  = [f"{_CYAN}{display_root}/{_R}"]   # colored for terminal
        plain_lines = [f"{display_root}/"]              # plain for context
        n_dirs = 0
        n_files = 0

        def _walk(dirpath: str, prefix: str, current_depth: int):
            nonlocal n_dirs, n_files
            if current_depth > depth:
                return
            try:
                entries = sorted(os.listdir(dirpath))
            except PermissionError:
                return

            # split into dirs and files, skip noisy dirs
            dirs  = [e for e in entries
                     if os.path.isdir(os.path.join(dirpath, e))
                     and e not in self._FIND_SKIP_DIRS]
            files = [e for e in entries
                     if os.path.isfile(os.path.join(dirpath, e))]

            children = [(e, True) for e in dirs] + [(e, False) for e in files]

            for idx, (name, is_dir) in enumerate(children):
                is_last    = idx == len(children) - 1
                connector  = "└── " if is_last else "├── "
                child_pref = prefix + ("    " if is_last else "│   ")

                if is_dir:
                    n_dirs += 1
                    term_lines.append(f"{prefix}{_DIM}{connector}{_R}{_CYAN}{name}/{_R}")
                    plain_lines.append(f"{prefix}{connector}{name}/")
                    if current_depth < depth:
                        _walk(os.path.join(dirpath, name), child_pref, current_depth + 1)
                    else:
                        # depth limit reached — hint there's more inside
                        try:
                            inner = os.listdir(os.path.join(dirpath, name))
                            inner_count = len(inner)
                        except PermissionError:
                            inner_count = 0
                        if inner_count:
                            term_lines.append(f"{child_pref}{_DIM}… ({inner_count} entries){_R}")
                            plain_lines.append(f"{child_pref}… ({inner_count} entries)")
                else:
                    n_files += 1
                    term_lines.append(f"{prefix}{_DIM}{connector}{name}{_R}")
                    plain_lines.append(f"{prefix}{connector}{name}")

        _walk(root, "", 1)

        summary_t = f"\n{_DIM}{n_dirs} director{'ies' if n_dirs != 1 else 'y'}, {n_files} file{'s' if n_files != 1 else ''}{_R}"
        summary_p = f"\n{n_dirs} director{'ies' if n_dirs != 1 else 'y'}, {n_files} file{'s' if n_files != 1 else ''}"

        for line in term_lines:
            print(line)
        print(summary_t)

        # ── inject into context ───────────────────────────────────────────
        if n_dirs + n_files > 0:
            ctx_text = "\n".join(plain_lines) + summary_p
            if not inject_ctx:
                inject_ctx = self._confirm("Add tree to context? [Y/n]", ctx_add=ctx_text)
            if inject_ctx and not self._auto_apply:
                self.messages.append({"role": "user", "content": ctx_text})
                _ok(f"[tree] injected into context ({len(plain_lines)} lines)")

    # ── /find ──────────────────────────────────────────────────────────────────

    _FIND_SKIP_DIRS = frozenset({
        ".git", ".hg", ".svn",
        "node_modules", "__pycache__", ".venv", "venv", "env",
        ".1bcoder", "dist", "build", ".mypy_cache", ".pytest_cache",
    })

    def _cmd_find(self, user_input: str):
        tokens = user_input.split()
        FLAGS = {"-f", "-c", "-i", "-r", "--ext", "ctx"}
        if len(tokens) < 2 or tokens[1] in FLAGS:
            print("usage: /find <pattern> [-f] [-c] [-i] [-r] [--ext <ext>] [ctx]")
            print("  -f   filenames only   -c   content only   -i  case-insensitive")
            print("  -r   ranked mode: BM25 relevance ranking (multiple terms, comma or space separated)")
            print("  --ext py  restrict to .py files")
            print("  ctx  inject results into AI context")
            return

        only_files   = "-f"  in tokens
        only_content = "-c"  in tokens
        case_insens  = "-i"  in tokens
        ranked       = "-r"  in tokens
        inject_ctx   = "ctx" in tokens
        ext_filter   = None
        if "--ext" in tokens:
            ei = tokens.index("--ext")
            if ei + 1 < len(tokens):
                ext_filter = "." + tokens[ei + 1].lstrip(".")

        # collect all non-flag, non-keyword tokens as search terms
        skip = {"--ext", ext_filter} if ext_filter else {"--ext"}
        term_tokens = [t for t in tokens[1:]
                       if t not in FLAGS and t not in skip and t != ext_filter]

        if ranked:
            # comma-separated terms in a single token (e.g. from {{kw}})
            terms = []
            for t in term_tokens:
                terms.extend(p.strip() for p in t.split(",") if p.strip())
            if not terms:
                print("[find] -r requires at least one search term")
                return
            self._cmd_find_ranked(terms, ext_filter, inject_ctx)
            return

        pattern_raw = tokens[1]

        try:
            rx_flags = re.IGNORECASE if case_insens else 0
            rx = re.compile(pattern_raw, rx_flags)
        except re.error as e:
            _err(f"invalid regex: {e}")
            return

        root = WORKDIR
        MAX_MATCHES = 60

        # ── walk once, collect filename hits and content hits ──────────────
        name_hits: list[str] = []
        content_hits: list[tuple[str, int, str]] = []  # (rel_path, lineno, line)
        total_content_matches = 0
        total_content_files   = 0
        _seen_files: set[str] = set()

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in self._FIND_SKIP_DIRS and not d.startswith('.')]
            rel_dir = os.path.relpath(dirpath, root)
            if rel_dir in (".", ""):
                rel_dir = ""

            for fname in filenames:
                if ext_filter and not fname.endswith(ext_filter):
                    continue
                rel_path = (rel_dir + "/" + fname) if rel_dir else fname

                if not only_content and rx.search(fname):
                    name_hits.append(rel_path)

                if not only_files:
                    full = os.path.join(dirpath, fname)
                    try:
                        with open(full, "rb") as fh:
                            if b"\x00" in fh.read(8192):
                                continue
                        with open(full, encoding="utf-8", errors="replace") as fh:
                            for lineno, line in enumerate(fh, 1):
                                if rx.search(line):
                                    total_content_matches += 1
                                    if rel_path not in _seen_files:
                                        _seen_files.add(rel_path)
                                        total_content_files += 1
                                    if len(content_hits) < MAX_MATCHES:
                                        content_hits.append((rel_path, lineno, line.rstrip()))
                    except OSError:
                        continue

        # ── render (terminal + optional plain-text for ctx) ───────────────
        mode      = "filenames only" if only_files else ("content only" if only_content else "filenames + content")
        flag_note = " (case-insensitive)" if case_insens else ""
        ext_note  = f" [.{ext_filter.lstrip('.')}]" if ext_filter else ""

        print(f"{_DIM}[find] {_R}{_BOLD}{pattern_raw}{_R}{_DIM}  {mode}{flag_note}{ext_note}{_R}")
        ctx_lines = [f"[find] pattern: {pattern_raw!r}  {mode}{flag_note}{ext_note}"]

        if not only_content:
            if name_hits:
                print(f"{_DIM}─── filenames ({len(name_hits)}) {'─'*20}{_R}")
                ctx_lines.append(f"\nfilenames ({len(name_hits)}):")
                for p in name_hits[:MAX_MATCHES]:
                    hi = rx.sub(lambda m: f"{_YELL}{m.group()}{_R}{_DIM}", p)
                    print(f"  {_DIM}{hi}{_R}")
                    ctx_lines.append(f"  {p}")
                if len(name_hits) > MAX_MATCHES:
                    note = f"  ... {len(name_hits) - MAX_MATCHES} more"
                    print(f"  {_DIM}{note.strip()}{_R}")
                    ctx_lines.append(note)
            elif only_files:
                print(f"  {_DIM}no filename matches{_R}")

        if not only_files:
            if content_hits:
                trunc = total_content_matches - len(content_hits)
                print(f"{_DIM}─── content ({total_content_matches} matches in {total_content_files} files) {'─'*10}{_R}")
                ctx_lines.append(f"\ncontent ({total_content_matches} matches in {total_content_files} files):")
                from itertools import groupby
                for rel_path, file_hits in groupby(content_hits, key=lambda t: t[0]):
                    label = rel_path if rel_path else "(project root)"
                    sys.stdout.write(f"{_R}\n  {label}\n")
                    sys.stdout.flush()
                    ctx_lines.append(f"  {label}")
                    for _, lineno, line in file_hits:
                        hi_line = rx.sub(lambda m: f"{_YELL}{m.group()}{_R}", line)
                        print(f"    {_DIM}{lineno:>4}:{_R}  {hi_line}")
                        ctx_lines.append(f"    {lineno:>4}:  {line}")
                if trunc > 0:
                    note = f"  ... {trunc} more matches"
                    print(f"  {_DIM}{note.strip()}{_R}")
                    ctx_lines.append(note)
            elif not name_hits:
                print(f"  {_DIM}no matches{_R}")

        # ── inject into context ───────────────────────────────────────────
        has_results = bool(name_hits or content_hits)
        if has_results:
            # collect unique files preserving order: content matches first, then name-only hits
            seen = {}
            for rel_path, _, _ in content_hits:
                seen[rel_path] = None
            for rel_path in name_hits:
                seen[rel_path] = None
            self._vars["find_files"] = ",".join(seen)
            print(f"{_DIM}[find] {{{{find_files}}}} set — {len(seen)} file(s){_R}")

            ctx_text = "\n".join(ctx_lines)
            if not inject_ctx:
                inject_ctx = self._confirm("Add results to context? [Y/n]", ctx_add=ctx_text)
            if inject_ctx and not self._auto_apply:
                self.messages.append({"role": "user", "content": ctx_text})
                _ok(f"[find] injected into context ({len(ctx_lines)} lines)")

    # ── /find -r (ranked BM25) ────────────────────────────────────────────────

    def _cmd_find_ranked(self, terms: list, ext_filter, inject_ctx: bool,
                         search_dir: str = None, label_prefix: str = "find -r"):
        """BM25-ranked content search via in-memory FTS5."""
        root = search_dir or WORKDIR
        terms_lower = [t.lower() for t in terms]

        # ── pre-filter: collect files containing at least one term ────────────
        candidates: dict = {}  # rel_path → full_text
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in self._FIND_SKIP_DIRS and not d.startswith('.')]
            for fname in filenames:
                if ext_filter and not fname.endswith(ext_filter):
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    with open(full, "rb") as fh:
                        if b"\x00" in fh.read(8192):
                            continue
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    if any(t in text.lower() for t in terms_lower):
                        rel = os.path.relpath(full, root)
                        candidates[rel] = text
                except OSError:
                    continue

        if not candidates:
            print(f"  {_DIM}no matches for: {', '.join(terms)}{_R}")
            return

        # ── rank with FTS5 ────────────────────────────────────────────────────
        try:
            ranked = _fts_rank(terms, candidates, top_k=10)
        except Exception as e:
            _err(f"FTS5 ranking failed: {e}")
            return

        # ── render ────────────────────────────────────────────────────────────
        ext_note = f" [.{ext_filter.lstrip('.')}]" if ext_filter else ""
        print(f"{_DIM}[{label_prefix}] terms: {_R}{_BOLD}{', '.join(terms)}{_R}"
              f"{_DIM}  top-{len(ranked)} of {len(candidates)} candidates{ext_note}{_R}")

        ctx_lines = [f"[{label_prefix}] terms: {', '.join(terms)}{ext_note}  "
                     f"top-{len(ranked)} of {len(candidates)} candidates"]

        for i, (rel_path, score) in enumerate(ranked, 1):
            # score is negative in FTS5 (lower = better), show as positive rank
            print(f"  {_DIM}{i:>2}.{_R}  {rel_path}")
            ctx_lines.append(f"  {i:>2}.  {rel_path}")

        file_list = ",".join(r for r, _ in ranked)
        self._vars["find_files"] = file_list
        print(f"\n{_DIM}[find] {{{{find_files}}}} set — {len(ranked)} file(s){_R}")
        ctx_lines.append(f"\nfiles: {file_list}")

        ctx_text = "\n".join(ctx_lines)
        if not inject_ctx:
            inject_ctx = self._confirm("Add results to context? [Y/n]", ctx_add=ctx_text)
        if inject_ctx and not self._auto_apply:
            self.messages.append({"role": "user", "content": ctx_text})
            _ok(f"[find] injected into context")

    # ── /read ──────────────────────────────────────────────────────────────────

    def _cmd_read(self, user_input: str):
        ln = user_input.split()[0] == "/readln"
        tokens = user_input.split()[1:]
        if not tokens:
            cmd = "/readln" if ln else "/read"
            print(f"usage: {cmd} <file> [file2 ...] [start-end]")
            return
        # expand comma-separated tokens (e.g. from {{find_files}} or {{map_files}})
        expanded = []
        for t in tokens:
            expanded.extend(p for p in t.split(",") if p)
        tokens = expanded
        # detect trailing range token (digits-digits), only for single-file use
        start = end = None
        range_re = re.compile(r'^(\d+)-(\d+)$')
        if len(tokens) >= 2:
            m = range_re.match(tokens[-1])
            if m and len(tokens) == 2:
                start, end = int(m.group(1)), int(m.group(2))
                tokens = tokens[:-1]
        for path in tokens:
            try:
                content, total = read_file(path, start, end, line_numbers=ln)
                label = path + (f" lines {start}-{end}" if start else f" ({total} lines)")
                if self._auto_apply:
                    # inside agent: print to stdout so Tee captures it for agent_msgs
                    print(f"[file: {label}]\n```\n{content}```")
                else:
                    self.messages.append({"role": "user", "content": f"[file: {label}]\n```\n{content}```"})
                    _ok(f"context: injected {label}")
            except FileNotFoundError:
                print(f"file not found: {path}")
            except (OSError, ValueError) as e:
                _err(e)

    # ── /view ──────────────────────────────────────────────────────────────────

    def _cmd_view(self, user_input: str):
        import base64
        tokens = user_input.split()
        if len(tokens) < 2:
            print("usage: /view [--keep] <image> [image2 ...] [prompt]")
            print("       supported: png jpg jpeg gif webp bmp")
            print("       --keep  retain image in context after reply (default: strip)")
            return
        tokens = tokens[1:]
        keep = "--keep" in tokens
        if keep:
            tokens = [t for t in tokens if t != "--keep"]
        IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        # split into image paths and trailing prompt words
        img_paths = []
        prompt_words = []
        for t in tokens:
            ext = os.path.splitext(t)[1].lower()
            if ext in IMG_EXTS or os.path.isfile(t):
                img_paths.append(t)
            else:
                prompt_words.append(t)
        if not img_paths:
            print("[view] no image files found in arguments")
            return
        b64_images = []
        labels = []
        for path in img_paths:
            if not os.path.isfile(path):
                print(f"[view] file not found: {path}")
                continue
            try:
                with open(path, "rb") as f:
                    b64_images.append(base64.b64encode(f.read()).decode())
                labels.append(os.path.basename(path))
            except OSError as e:
                _err(e)
        if not b64_images:
            return
        has_prompt = bool(prompt_words)
        prompt = " ".join(prompt_words) if has_prompt else f"[image: {', '.join(labels)}]"
        msg = {"role": "user", "content": prompt, "images": b64_images}
        if self._auto_apply:
            print(f"[image: {', '.join(labels)}] {prompt}")
        else:
            self.messages.append(msg)
            if has_prompt:
                self._sep("AI")
                reply = self._stream_chat(self.messages)
                print()
                if reply:
                    self.last_reply = reply
                    self._last_output = reply
                    self.messages.append({"role": "assistant", "content": reply})
                if not keep:
                    # strip base64 from context — keep only the text prompt
                    for m in self.messages:
                        if "images" in m:
                            del m["images"]
            else:
                _ok(f"context: injected image(s): {', '.join(labels)}"
                    + ("" if keep else "  (use --keep to retain; default strips on next reply)"))

    def _cmd_edit(self, user_input: str):
        tokens = user_input.split()
        if len(tokens) < 3:
            print("usage: /edit <file> <line>  |  /edit <file> [line] code")
            return
        path = tokens[1]
        rest = tokens[2:]
        has_code = rest[-1].lower()[:4] == "code"
        if self._in_agent and not has_code:
            _warn(f"[agent] /edit {path} — missing 'code' keyword, agent reply won't be used")
        if has_code:
            rest = rest[:-1]
        line_start = line_end = None
        if rest:
            m = re.match(r'^(\d+)(?:-(\d+))?$', rest[0])
            if not m:
                print("hint: to save whole file use /edit <file> code  |  to patch use /patch <file> code")
                return
            line_start = int(m.group(1))
            line_end = int(m.group(2)) if m.group(2) else None
        if has_code:
            if not self.last_reply:
                print("no AI response yet")
                return
            new_code = "\n".join(
                _strip_line_numbers(_extract_code_block(self.last_reply).splitlines())
            )
            try:
                with open(path, "r", encoding="utf-8") as f:
                    file_lines = f.readlines()
            except FileNotFoundError:
                file_lines = []
                line_start = line_end = None
                _info(f"[new file: {path}]")
            except OSError as e:
                _err(e)
                return
            new_lines = new_code.splitlines(keepends=True)
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            if line_start is not None:
                offset = line_start - 1
                if line_end is not None:
                    # range given: replace lines start–end
                    original_segment = file_lines[offset:line_end]
                    new_file_lines = file_lines[:offset] + new_lines + file_lines[line_end:]
                    label = f"{line_start}-{line_end}"
                else:
                    # single line: insert before that line, nothing removed
                    original_segment = []
                    new_file_lines = file_lines[:offset] + new_lines + file_lines[offset:]
                    label = f"{line_start} (insert)"
                diff = list(difflib.unified_diff(
                    original_segment, new_lines,
                    fromfile=f"{path}:{label} (current)",
                    tofile=f"{path}:{label} (proposed)",
                    lineterm="",
                ))
            else:
                new_file_lines = new_lines
                diff = list(difflib.unified_diff(
                    file_lines, new_lines,
                    fromfile=f"{path} (current)",
                    tofile=f"{path} (proposed)",
                    lineterm="",
                ))
            if not diff:
                print("[no changes detected]")
                return
            for dline in diff:
                print(_cdiff(dline))
            if self._confirm("  apply? [Y/n]:"):
                try:
                    parent = os.path.dirname(path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        f.writelines(new_file_lines)
                    _ok(f"[saved {path}]")
                except OSError as e:
                    _err(e)
            else:
                print("[skipped]")
        else:
            if line_start is None:
                print("hint: to save whole file use /edit <file> code  |  to patch use /patch <file> code")
                return
            try:
                content, _ = read_file(path, line_start, line_start)
                current = content.split(":", 1)[1].strip() if ":" in content else content.strip()
                print(f"  current [{line_start}]: {current}")
            except (FileNotFoundError, OSError) as e:
                _err(e)
                return
            new_content = self._prompt_input("  new content (blank = keep):")
            if new_content:
                try:
                    edit_line(path, line_start, new_content)
                    print(f"[line {line_start} updated in {path}]")
                except (ValueError, OSError) as e:
                    _err(e)
            else:
                print("[no change]")

    def _cmd_insert(self, user_input: str):
        """Insert last AI reply (or its code block) before line N in file."""
        tokens = user_input.split()
        # /insert <file> <line> [code]
        if len(tokens) < 3:
            print("usage: /insert <file> <line> [code]")
            return
        path = tokens[1]
        try:
            line_n = int(tokens[2])
        except ValueError:
            print("usage: /insert <file> <line> [code]  — line must be a number")
            return
        has_code   = len(tokens) > 3 and tokens[3].lower() == "code"
        if self._in_agent and not has_code:
            _warn(f"[agent] /insert {tokens[1] if len(tokens)>1 else ''} — missing 'code' keyword, agent reply won't be used")
        inline_text = None
        if len(tokens) > 3 and tokens[3].lower() != "code":
            # Preserve indentation: skip past cmd, file, line_n in original string,
            # then take everything verbatim (including leading spaces).
            m = re.match(r'\S+\s+\S+\s+\S+(.*)', user_input, re.DOTALL)
            inline_text = m.group(1) if m else user_input.split(None, 3)[3]

        if inline_text is not None:
            text = inline_text
        else:
            if not self.last_reply:
                print("no AI response yet")
                return
            if has_code:
                raw = _extract_code_block(self.last_reply)
                if not raw:
                    print("[insert] no code block found in last reply")
                    return
                text = "\n".join(_strip_line_numbers(raw.splitlines()))
            else:
                text = self.last_reply.strip()

        new_lines = text.splitlines(keepends=False)
        new_lines = [ln + "\n" for ln in new_lines]

        try:
            with open(path, "r", encoding="utf-8") as f:
                file_lines = f.readlines()
        except FileNotFoundError:
            file_lines = []
        except OSError as e:
            _err(e); return

        offset = max(0, line_n - 1)
        new_file_lines = file_lines[:offset] + new_lines + file_lines[offset:]

        diff = list(difflib.unified_diff(
            file_lines, new_file_lines,
            fromfile=f"{path} (current)",
            tofile=f"{path} (after insert at {line_n})",
            lineterm="",
        ))
        for dline in diff:
            print(_cdiff(dline))
        if self._confirm("  apply? [Y/n]:"):
            try:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(new_file_lines)
                _ok(f"[inserted {len(new_lines)} line(s) at {line_n} in {path}]")
            except OSError as e:
                _err(e)
        else:
            print("[skipped]")

    def _cmd_fix(self, user_input: str):
        parts = user_input[4:].strip().split(None, 2)
        path = parts[0] if parts else ""
        start = end = None
        hint = ""
        if not path:
            print("usage: /fix <file> [start-end] [hint]")
            return
        if len(parts) >= 2:
            if re.match(r'^\d+-\d+$', parts[1]):
                try:
                    s, e = parts[1].split("-")
                    start, end = int(s), int(e)
                except ValueError:
                    pass
                hint = parts[2] if len(parts) >= 3 else ""
            else:
                hint = " ".join(parts[1:])
        try:
            content, total = read_file(path, start, end)
            label = path + (f" lines {start}-{end}" if start else f" ({total} lines)")
        except (FileNotFoundError, OSError) as e:
            _err(e)
            return
        if hint:
            print(f"hint: {hint}")
        self._sep("AI")
        accumulated = []
        def on_chunk(c):
            sys.stdout.write(c)
            sys.stdout.flush()
            accumulated.append(c)
        try:
            lineno, new_content = ai_fix(self.base_url, self.model, content, label, hint, on_chunk, self.provider)
        except KeyboardInterrupt:
            print("\n[interrupted]")
            return
        except requests.exceptions.RequestException as e:
            print(f"\nerror: {e}")
            return
        print()
        if lineno is None:
            print("could not parse LINE N: format — try a more capable model")
            return
        try:
            current_text, _ = read_file(path, lineno, lineno)
            current_text = current_text.split(":", 1)[1][1:].rstrip("\n") if ":" in current_text else current_text.rstrip()
            print(f"  current [{lineno}]: {current_text}")
            print(f"  new     [{lineno}]: {new_content}")
        except (FileNotFoundError, OSError):
            print(f"  new [{lineno}]: {new_content}")
        if self._confirm("  apply? [Y/n]:"):
            try:
                edit_line(path, lineno, new_content)
                print(f"[line {lineno} updated in {path}]")
            except (ValueError, OSError) as e:
                _err(e)
        else:
            print("[skipped]")

    def _cmd_fill(self, user_input: str):
        parts = user_input[4:].strip().split(None, 2)
        path = parts[0] if parts else ""
        start = end = None
        hint = ""
        if not path:
            print("usage: /fim <file> <line or start-end> [-w N] [hint]")
            return
        if len(parts) >= 2:
            m = re.match(r'^(\d+)(?:-(\d+))?$', parts[1])
            if m:
                start = int(m.group(1))
                end   = int(m.group(2)) if m.group(2) else start
                hint  = parts[2] if len(parts) >= 3 else ""
            else:
                hint = " ".join(parts[1:])
        if start is None and not hint:
            print("usage: /fim <file> [line or start-end] [-w N] [hint]")
            return
        # extract -w <window> from hint
        window = None
        wm = re.search(r'(?:^|\s)-w\s+(\d+)', hint)
        if wm:
            window = int(wm.group(1))
            hint = (hint[:wm.start()] + hint[wm.end():]).strip()
        try:
            with open(path, "r", encoding="utf-8") as f:
                file_lines = f.readlines()
        except (FileNotFoundError, OSError) as e:
            _err(e)
            return
        total = len(file_lines)
        if start is not None and (start < 1 or end > total):
            _err(f"line {start}-{end} out of range (file has {total} lines)")
            return
        # compute slice for windowed mode (only when line range is given)
        if start is not None and window is not None:
            w_start = max(0, start - 1 - window)          # 0-indexed inclusive
            w_end   = min(total, end + window)             # 0-indexed exclusive
            slice_lines = file_lines[w_start:w_end]
            rel_start   = start - w_start                  # 1-indexed within slice
            rel_end     = end   - w_start
        else:
            slice_lines = file_lines
            rel_start, rel_end = start, end
            w_start, w_end = 0, total
        if hint:
            print(f"hint: {hint}")
        if window is not None:
            print(f"[fim] window: lines {w_start+1}–{w_end} of {total}")
        self._sep("AI")
        def on_chunk(c):
            sys.stdout.write(c)
            sys.stdout.flush()
        try:
            raw = ai_fill(self.base_url, self.model, slice_lines, rel_start, rel_end, hint, on_chunk, self.provider)
        except KeyboardInterrupt:
            print("\n[interrupted]")
            return
        except requests.exceptions.RequestException as e:
            print(f"\nerror: {e}")
            if hasattr(e, "response") and e.response is not None:
                body = e.response.text.strip()
                if body:
                    print(f"  detail: {body[:500]}")
            return
        print()
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        m = re.search(r'```(?:\w+)?\n(.*?)\n```', raw, re.DOTALL)
        new_content = m.group(1) if m else raw.strip()
        new_slice = [l if l.endswith('\n') else l + '\n' for l in new_content.splitlines()]
        # reconstruct full file: unchanged prefix + new slice + unchanged suffix
        full_new = file_lines[:w_start] + new_slice + file_lines[w_end:]
        diff = list(difflib.unified_diff(file_lines, full_new, fromfile=path, tofile=path + " (fixed)", lineterm=""))
        if not diff:
            print("[fim] no changes detected — try a different hint or model")
            return
        for dline in diff:
            print(_cdiff(dline))
        if self._confirm("  apply? [Y/n]:"):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(full_new)
                changed = sum(1 for d in diff if d.startswith('+') and not d.startswith('+++'))
                print(f"[{changed} line(s) updated in {path}]")
            except OSError as e:
                _err(e)
        else:
            print("[skipped]")

    # ── hook support ──────────────────────────────────────────────────────────

    def _extract_hook_args(self, user_input: str) -> dict:
        """Extract file and range from a command string for hook variable injection."""
        tokens = user_input.split()
        file = tokens[1] if len(tokens) > 1 else ""
        range_str = ""
        if len(tokens) > 2:
            if re.match(r'^\d+(?:-\d+)?$', tokens[2]):
                range_str = tokens[2]
        return {"file": file, "range": range_str}

    def _run_hook(self, event: str, args: dict) -> bool:
        """Run hook script for event. Returns False to cancel the command.
        .py files are run as guard subprocesses (stdin=trigger text, stdout parsed for BLOCK:/ALERT:).
        .txt files are run as 1bcoder scripts (existing behaviour).
        args (file, range, ...) are injected as session variables for .txt scripts,
        and passed as env vars BCODER_<KEY> for .py guards."""
        script_name = self._hooks.get(event)
        if not script_name:
            return True

        # resolve: proc dirs first (for .py guards), then scripts dirs, then absolute
        script_path = None
        if os.path.isabs(script_name):
            script_path = script_name if os.path.isfile(script_name) else None
        else:
            search_dirs = (LOCAL_PROC_DIR, PROC_DIR, SCRIPTS_DIR, GLOBAL_SCRIPTS_DIR)
            for d in search_dirs:
                p = os.path.join(d, script_name)
                if os.path.isfile(p):
                    script_path = p
                    break

        if not script_path:
            _warn(f"[hook:{event}] script not found: {script_name}")
            return event.startswith("after")

        # ── .py guard: run as subprocess, parse BLOCK:/ALERT: ─────────────────
        if script_path.endswith(".py"):
            stdin_text = args.get("content") or args.get("file") or ""
            env = dict(os.environ)
            env["BCODER_EVENT"] = event
            ctx_used = sum(len(m.get("content", "")) // 4 for m in self.messages)
            ctx_max  = getattr(self, "num_ctx", NUM_CTX)
            env["BCODER_CTX_USED"] = str(ctx_used)
            env["BCODER_CTX_MAX"]  = str(ctx_max)
            env["BCODER_CTX_PCT"]  = str(int(ctx_used * 100 / ctx_max) if ctx_max else 0)
            for k, v in args.items():
                if v:
                    env[f"BCODER_{k.upper()}"] = str(v)
            try:
                result = subprocess.run(
                    [sys.executable, script_path],
                    input=stdin_text, capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=15, env=env,
                )
            except subprocess.TimeoutExpired:
                _warn(f"[hook:{event}] guard timeout: {script_name}")
                return True
            except Exception as e:
                _warn(f"[hook:{event}] guard error: {e}")
                return True

            stdout = (result.stdout or "").strip()
            for line in stdout.splitlines():
                if line.startswith("BLOCK:"):
                    reason = line[len("BLOCK:"):].strip()
                    print(f"{_RED}[guard:{event}] blocked — {reason}{_R}")
                    return False
                if line.startswith("ALERT:"):
                    reason = line[len("ALERT:"):].strip()
                    print(f"{_YELL}[guard:{event}] alert — {reason}{_R}")
                elif line.startswith("ACTION:"):
                    cmd = line[len("ACTION:"):].strip()
                    self._route(cmd, auto=True)
            if result.returncode != 0:
                _warn(f"[hook:{event}] guard exited {result.returncode}")
            return True

        # ── .txt script: original behaviour ───────────────────────────────────
        saved_vars = dict(self._vars)
        self._vars.update({k: v for k, v in args.items() if v})
        try:
            lines = _load_script(script_path)
            for line in lines:
                line = line.rstrip("\n")
                if not line or line.strip().startswith("#") or line.startswith("[v]"):
                    continue
                self._route(_apply_params(line, self._vars), auto=True)
        finally:
            self._vars = saved_vars
        return True

    def _cmd_hook(self, user_input: str):
        """Define, list, or clear before/after hooks on commands.

        /hook before|after <cmd> <script>   Set hook (script in scripts dir or absolute path).
        /hook list                           Show all active hooks.
        /hook clear                          Remove all hooks.
        /hook clear before|after [<cmd>]     Remove specific hook(s).
        """
        parts = user_input.split(None, 3)
        sub = parts[1] if len(parts) > 1 else ""

        if sub == "list":
            if not self._hooks:
                print("[no hooks defined]")
                return
            for event, script in sorted(self._hooks.items()):
                timing, cmd = event.split("_", 1)
                print(f"  {timing:6} {cmd:12} → {script}")
            return

        if sub == "clear":
            if len(parts) == 2:
                self._hooks.clear()
                _ok("[hook] all hooks cleared")
            elif len(parts) == 3:
                timing = parts[2]
                removed = [k for k in list(self._hooks) if k.startswith(timing + "_")]
                for k in removed:
                    del self._hooks[k]
                _ok(f"[hook] cleared all {timing} hooks ({len(removed)} removed)")
            else:
                event = f"{parts[2]}_{parts[3]}"
                if self._hooks.pop(event, None) is not None:
                    _ok(f"[hook] cleared {event}")
                else:
                    print(f"[hook] not found: {event}")
            return

        if sub in ("before", "after"):
            if len(parts) < 4:
                print("usage: /hook before|after <cmd> <script>")
                return
            cmd    = parts[2]
            script = parts[3]
            event  = f"{sub}_{cmd}"
            self._hooks[event] = script
            _ok(f"[hook] {sub} {cmd} → {script}")
            return

        print("usage: /hook before|after <cmd> <script>  |  /hook list  |  /hook clear [before|after [cmd]]")

    def _cmd_bkup(self, user_input: str):
        parts = user_input.split(None, 2)
        if len(parts) < 3:
            print("usage: /bkup save <file>  |  /bkup restore <file>")
            return
        sub, path = parts[1], parts[2]
        bkup_path = path + ".bkup"

        if sub == "save":
            if not os.path.isfile(path):
                _err(f"file not found: {path}")
                return
            import shutil
            if os.path.isfile(bkup_path):
                n = 1
                while os.path.isfile(f"{bkup_path}({n})"):
                    n += 1
                os.rename(bkup_path, f"{bkup_path}({n})")
            shutil.copy2(path, bkup_path)
            print(f"[bkup] saved {path} → {bkup_path}")

        elif sub == "restore":
            if not os.path.isfile(bkup_path):
                _err(f"backup not found: {bkup_path}")
                return
            import shutil
            os.remove(path) if os.path.isfile(path) else None
            shutil.copy2(bkup_path, path)
            print(f"[bkup] restored {bkup_path} → {path}")

        else:
            _err(f"unknown subcommand '{sub}' — use save or restore")

    def _cmd_patch(self, user_input: str):
        parts = user_input[6:].strip().split(None, 2)
        path = parts[0] if parts else ""
        start = end = None
        hint = ""
        if not path:
            print("usage: /patch <file> [start-end] [hint]  |  /patch <file> code")
            return

        # /patch <file> code — apply SEARCH/REPLACE block from last AI reply
        if self._in_agent and (len(parts) < 2 or parts[-1].lower() != "code"):
            _warn(f"[agent] /patch {path} — missing 'code' keyword, will run interactive mode instead of using agent reply")
        if len(parts) >= 2 and parts[-1].lower() == "code":
            if not self.last_reply:
                print("no AI response yet")
                return
            raw = self.last_reply
        else:
            if len(parts) >= 2:
                if re.match(r'^\d+-\d+$', parts[1]):
                    try:
                        s, e = parts[1].split("-")
                        start, end = int(s), int(e)
                    except ValueError:
                        pass
                    hint = parts[2] if len(parts) >= 3 else ""
                else:
                    hint = " ".join(parts[1:])
            try:
                content, total = read_file(path, start, end, line_numbers=False)
                label = path + (f" lines {start}-{end}" if start else f" ({total} lines)")
            except (FileNotFoundError, OSError) as e:
                _err(e)
                return
            user_msg = f"Fix the code in this file ({label}):\n```\n{content}```"
            if hint:
                user_msg = f"{hint}\n\n{user_msg}"
            msgs = [
                {"role": "system", "content": PATCH_SYSTEM},
                {"role": "user", "content": user_msg},
            ]
            self._sep("AI")
            raw = self._stream_chat(msgs)
            if not raw:
                return
        search_text, replace_text = _parse_patch(raw)
        if search_text is None:
            print("could not parse SEARCH/REPLACE block — try a more capable model")
            return
        if search_text == replace_text:
            _warn("[patch] SEARCH and REPLACE are identical — model included the new code in both blocks (no-op)")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except (FileNotFoundError, OSError) as e:
            print(f"error reading {path}: {e}")
            return
        si, ei = _find_in_lines(lines, search_text)
        if si is None:
            _err("SEARCH text not found in file — model may have hallucinated the code")
            slines = [l.rstrip('\n') for l in search_text.splitlines() if l.strip()]
            flines = [l.rstrip('\n') for l in lines]
            # find best matching window in file by counting matching stripped lines
            best_i, best_score = 0, -1
            n = max(1, len(slines))
            sset = {l.lstrip() for l in slines}
            for i in range(max(1, len(flines) - n + 1)):
                score = sum(1 for l in flines[i:i + n] if l.lstrip() in sset)
                if score > best_score:
                    best_score, best_i = score, i
            print(f"\n  {_YELL}SEARCH ({len(slines)} lines):{_R}")
            for l in slines[:8]:
                print(f"    {_RED}-{_R} {l}")
            print(f"\n  {_YELL}nearest match in file (lines {best_i+1}-{best_i+n}):{_R}")
            for l in flines[best_i:best_i + n][:8]:
                print(f"    {_GREEN}+{_R} {l}")
            return
        replace_lines = replace_text.splitlines(keepends=True)
        if replace_lines and not replace_lines[-1].endswith("\n"):
            replace_lines[-1] += "\n"
        diff = list(difflib.unified_diff(
            lines[si:ei], replace_lines,
            fromfile=f"{path} (current)", tofile=f"{path} (patched)",
            lineterm="",
        ))
        print(f"  match: lines {si+1}–{ei}")
        for dline in diff:
            print(dline)
        if self._confirm("  apply? [Y/n]:"):
            new_lines = lines[:si] + replace_lines + lines[ei:]
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                print(f"[patched {path}: lines {si+1}–{ei} replaced]")
            except OSError as e:
                _err(e)
        else:
            print("[skipped]")

    def _cmd_save(self, user_input: str):
        _MODE_KEYWORDS = {
            "code", "overwrite",
            "append-above", "append_above", "-aa",
            "append-below", "append_below", "-ab",
            "add-suffix", "add_suffix",
        }
        raw_tokens = user_input.split()[1:]
        if not raw_tokens:
            print("usage: /save <file> [code] [overwrite|append-above|append-below|add-suffix]")
            return
        if not self.last_reply:
            print("no AI response yet")
            return
        tokens = [p for t in raw_tokens for p in t.split(",") if p]
        files = [t for t in tokens if t.lower() not in _MODE_KEYWORDS]
        flags = {t.lower() for t in tokens if t.lower() in _MODE_KEYWORDS}
        if not files:
            print("usage: /save <file> [code] [mode]")
            return
        if flags & {"-ab", "append-below", "append_below"}:
            action = "append_below"
        elif flags & {"-aa", "append-above", "append_above"}:
            action = "append_above"
        elif flags & {"add-suffix", "add_suffix"}:
            action = "add_suffix"
        else:
            action = "overwrite"
        if self._in_agent and "code" not in flags:
            _warn(f"[agent] /save {files[0] if files else ''} — missing 'code' keyword, raw reply will be saved instead of code block")
        if "code" in flags:
            blocks = _extract_all_code_blocks(self.last_reply) or [self.last_reply]
            contents = [blocks[i] if i < len(blocks) else blocks[-1] for i in range(len(files))]
        else:
            contents = [self.last_reply] * len(files)
        for path, content in zip(files, contents):
            try:
                dirpart = os.path.dirname(path)
                if dirpart:
                    os.makedirs(dirpart, exist_ok=True)
                if action == "overwrite":
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                    print(f"saved → {path}")
                elif action == "append_below":
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(content)
                    print(f"appended below → {path}")
                elif action == "append_above":
                    existing = ""
                    if os.path.exists(path):
                        with open(path, "r", encoding="utf-8") as f:
                            existing = f.read()
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content + existing)
                    print(f"prepended → {path}")
                elif action == "add_suffix":
                    target = _next_suffix_path(path)
                    with open(target, "w", encoding="utf-8") as f:
                        f.write(content)
                    print(f"saved → {target}")
            except OSError as e:
                _err(e)

    def _cmd_diff(self, user_input: str):
        tokens = user_input.split()
        if len(tokens) < 3:
            print("usage: /diff <file_a> <file_b> [-y]")
            return
        file_a, file_b = tokens[1], tokens[2]
        inject = "-y" in tokens
        try:
            with open(file_a, encoding="utf-8") as f:
                lines_a = f.readlines()
        except FileNotFoundError:
            _err(f"file not found: {file_a}"); return
        except OSError as e:
            _err(e); return
        try:
            with open(file_b, encoding="utf-8") as f:
                lines_b = f.readlines()
        except FileNotFoundError:
            _err(f"file not found: {file_b}"); return
        except OSError as e:
            _err(e); return

        diff = list(difflib.unified_diff(lines_a, lines_b, fromfile=file_a, tofile=file_b, lineterm=""))
        if not diff:
            print("[diff] files are identical")
            return
        for dline in diff:
            print(_cdiff(dline))
        plain = "\n".join(diff)
        if inject or self._confirm("  add diff to context? [Y/n]:", ctx_add=plain):
            if not self._auto_apply:
                self.messages.append({"role": "user", "content": f"[diff: {file_a} vs {file_b}]\n{plain}"})
                _ok(f"[diff] injected into context")

    def _cmd_run(self, shell_cmd: str):
        print(f"$ {shell_cmd}")
        run_timeout = self.params.get("run_timeout", 30)
        try:
            proc = subprocess.run(
                shell_cmd, shell=True, capture_output=True,
                timeout=run_timeout if run_timeout else None,
                encoding="utf-8", errors="replace",
            )
            output = proc.stdout + proc.stderr
            print(output if output else "(no output)")
            status = f"exit code {proc.returncode}"
            self._last_output = output or "(no output)"
            if not self._auto_apply:
                self.messages.append(
                    {"role": "user", "content": f"[run: {shell_cmd}  ({status})]\n```\n{output or '(no output)'}```"}
                )
            print(f"{status} — injected into context")
        except subprocess.TimeoutExpired:
            print("timeout after 30s")
        except OSError as e:
            _err(e)

    def _cmd_script(self, user_input: str):
        parts = user_input.split(None, 2)
        sub = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""

        def _need_script():
            if not self._script_file:
                print("no script open — use /script open or /script create")
                return False
            return True

        if sub == "list":
            global_plans, local_plans = _list_script_files()
            if not global_plans and not local_plans:
                print("[no scripts found — use /script create]")
            else:
                current = self._script_file
                all_plans = [("g", l, p) for l, p in global_plans] + [("l", l, p) for l, p in local_plans]
                for i, (src, label, path) in enumerate(all_plans, 1):
                    marker = " *" if path == current else ""
                    prefix = f"{_DIM}g:{_R} " if src == "g" else "   "
                    print(f"  {i}. {prefix}{label}{marker}")

        elif sub == "open":
            global_plans, local_plans = _list_script_files()
            all_plans = [("g", l, p) for l, p in global_plans] + [("l", l, p) for l, p in local_plans]
            if not all_plans:
                print("[no scripts found — use /script create]")
                return
            for i, (src, label, _) in enumerate(all_plans, 1):
                prefix = f"{_DIM}g:{_R} " if src == "g" else "   "
                print(f"  {i}. {prefix}{label}")
            # if a number was passed directly (e.g. /script open 3), skip the prompt
            raw = rest.strip() if rest.strip().isdigit() else self._prompt_input("  type number (Enter to cancel):")
            if not raw:
                print("[cancelled]")
                return
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(all_plans):
                    src, label, path = all_plans[idx]
                    self._script_file = path
                    tag = "global" if src == "g" else "project"
                    print(f"[opened {tag} script: {label}]")
                else:
                    print("invalid choice")
            except ValueError:
                print("invalid choice")

        elif sub == "create":
            # /script create ctx [name] — build plan from this session's command history
            toks = rest.strip().split(None, 1)
            from_ctx = toks and toks[0] == "ctx"
            name_arg = (toks[1] if len(toks) > 1 else "") if from_ctx else rest.strip()

            name = name_arg or self._prompt_input("  script name:")
            if not name:
                print("[cancelled]")
                return
            name = name.replace("\\", "/")
            if not name.endswith(".txt"):
                name += ".txt"
            path = os.path.join(SCRIPTS_DIR, name)
            if os.path.exists(path):
                print(f"script already exists: {name}")
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)

            if from_ctx:
                if not self.cmd_history:
                    print("[script] no commands recorded this session")
                    return
                with open(path, "w", encoding="utf-8") as f:
                    for cmd in self.cmd_history:
                        f.write(cmd + "\n")
                self._script_file = path
                print(f"[script] created '{name}' from session history ({len(self.cmd_history)} step(s)):")
                for cmd in self.cmd_history:
                    print(f"  {cmd}")
            else:
                open(path, "w").close()
                self._script_file = path
                print(f"[created and opened script: {name}]")

        elif sub == "show":
            # /script show [N] — if N given, open script N from list then show it;
            # otherwise show current open script
            global_plans, local_plans = _list_script_files()
            all_plans = [("g", l, p) for l, p in global_plans] + [("l", l, p) for l, p in local_plans]
            if rest.strip().isdigit():
                for i, (src, label, _) in enumerate(all_plans, 1):
                    prefix = f"{_DIM}g:{_R} " if src == "g" else "   "
                    print(f"  {i}. {prefix}{label}")
                try:
                    idx = int(rest.strip()) - 1
                    if 0 <= idx < len(all_plans):
                        src, label, path = all_plans[idx]
                        self._script_file = path
                        tag = "global" if src == "g" else "project"
                        print(f"[opened {tag} script: {label}]")
                    else:
                        print("invalid choice")
                        return
                except ValueError:
                    print("invalid choice")
                    return
            if not _need_script():
                return
            lines = _load_script(self._script_file)
            if not lines:
                print("[script is empty]")
                return
            for i, line in enumerate(lines, 1):
                line = line.rstrip("\n")
                if line.strip().startswith("#"):
                    print(f"       {_DIM}{line}{_R}")
                else:
                    tick = "v " if line.startswith("[v]") else ". "
                    print(f"  {i:2}. {tick}{line.replace('[v] ', '', 1)}")

        elif sub == "add":
            if not _need_script():
                return
            if not rest:
                print("usage: /script add <command>")
                return
            with open(self._script_file, "a", encoding="utf-8") as f:
                f.write(rest + "\n")
            print(f"script: added '{rest}'")

        elif sub == "clear":
            if not _need_script():
                return
            _save_script([], self._script_file)
            print("script cleared")

        elif sub == "reset":
            if not _need_script():
                return
            lines = _load_script(self._script_file)
            new_lines = [l[4:] if l.startswith("[v] ") else l for l in lines]
            _save_script(new_lines, self._script_file)
            n_steps = sum(1 for l in new_lines if not l.strip().startswith("#"))
            print(f"script reset — {n_steps} step(s) unmarked")

        elif sub == "reapply":
            if not _need_script():
                return
            lines = _load_script(self._script_file)
            new_lines = [l[4:] if l.startswith("[v] ") else l for l in lines]
            _save_script(new_lines, self._script_file)
            n_steps = sum(1 for l in new_lines if not l.strip().startswith("#"))
            print(f"script reset — {n_steps} step(s) unmarked, applying...")
            self._cmd_script(f"/script apply -y {rest}")

        elif sub == "refresh":
            if not _need_script():
                return
            lines = _load_script(self._script_file)
            n_steps = sum(1 for l in lines if not l.strip().startswith("#"))
            print(f"script: {n_steps} step(s)")
            for i, line in enumerate(lines, 1):
                line = line.rstrip("\n")
                if line.strip().startswith("#"):
                    print(f"       {_DIM}{line}{_R}")
                else:
                    tick = "v " if line.startswith("[v]") else ". "
                    print(f"  {i:2}. {tick}{line.replace('[v] ', '', 1)}")

        elif sub == "run":
            # /script run <file|N> [key=value ...] — shorthand for /script apply -y
            # N can be a list index matching /script list / /script open / /script show
            auto_yes, filename, params = _parse_script_apply_args("-y " + rest if rest else "-y")
            if not filename:
                print("usage: /script run <file> [key=value ...]")
                return
            # resolve numeric index → actual path (same list as /script list|open|show)
            if filename.isdigit():
                global_plans, local_plans = _list_script_files()
                all_plans = [("g", l, p) for l, p in global_plans] + [("l", l, p) for l, p in local_plans]
                idx = int(filename) - 1
                if 0 <= idx < len(all_plans):
                    _, label, resolved_path = all_plans[idx]
                    # forward slashes: shlex.split strips backslashes on Windows
                    rest = f"-y {resolved_path.replace(os.sep, '/')}"
                else:
                    print(f"invalid script index {filename} — use /script list to see available scripts")
                    return
            else:
                rest = "-y " + rest
            # delegate to apply logic; flag reset so apply starts fresh
            sub = "apply"
            _script_run_reset = True

        if sub == "apply":
            auto_yes, filename, params = _parse_script_apply_args(rest)
            if filename:
                if os.path.isabs(filename):
                    path = filename
                else:
                    path = next(
                        (os.path.join(d, filename) for d in (SCRIPTS_DIR, GLOBAL_SCRIPTS_DIR)
                         if os.path.isfile(os.path.join(d, filename))),
                        os.path.join(SCRIPTS_DIR, filename)
                    )
                if not os.path.exists(path):
                    print(f"script file not found: {path}")
                    return
                self._script_file = path
            elif not _need_script():
                return
            if locals().get("_script_run_reset"):
                _raw = _load_script(self._script_file)
                _save_script([l[4:] if l.startswith("[v] ") else l for l in _raw], self._script_file)
            lines = _load_script(self._script_file)
            pending = [(i, l.rstrip("\n")) for i, l in enumerate(lines)
                       if not l.startswith("[v]") and not l.strip().startswith("#")]
            if not pending:
                print("nothing to apply")
                return
            # merge session vars as defaults; explicit params take priority
            params = {**self._vars, **params}
            for key in _find_template_keys(pending):
                if not params.get(key):   # missing or NaN sentinel (empty string)
                    value = self._prompt_input(f"  {key} = ? ")
                    if value:
                        params[key] = value
                        self._vars[key] = value   # persist into session vars
            suffix = "— auto-applying all" if auto_yes else "— Y/n/q per step"
            print(f"script: {len(pending)} step(s) {suffix}")
            original_confirm = self._confirm
            if auto_yes:
                self._confirm = lambda prompt, **kw: True
                self._auto_apply = True
            try:
                for step_num, (idx, cmd_str) in enumerate(pending, 1):
                    cmd_str = _apply_params(cmd_str, params)
                    self._sep(f"Step {step_num}/{len(pending)}")
                    print(cmd_str)
                    if not auto_yes:
                        ans = self._prompt_input("  run? [Y/n/q]:")
                        if ans.lower() == "q":
                            print("[stopped]")
                            return
                        if ans.lower() not in ("", "y", "yes"):
                            print("[skipped]")
                            continue
                    # mark done
                    plan_lines = _load_script(self._script_file)
                    plan_lines[idx] = f"[v] {plan_lines[idx]}"
                    _save_script(plan_lines, self._script_file)
                    self._route(cmd_str, auto=True)
            finally:
                self._confirm = original_confirm
                self._auto_apply = False
            print("script complete")
            # auto-reset: remove [v] markers so script is ready to reuse
            _done = _load_script(self._script_file)
            _reset = [l[4:] if l.startswith("[v] ") else l for l in _done]
            _save_script(_reset, self._script_file)

        else:
            print("usage: /script list | open [N] | create | show [N] | run <file> | add <cmd> | clear | reset | reapply | refresh | apply [-y]")

    def _save_prompt(self, name: str, text: str):
        """Save text as a named prompt entry to prompts.txt. Newlines stored as \\n."""
        name = name.strip().replace(" ", "-")
        if not os.path.isfile(PROMPTS_FILE):
            entries = []
        else:
            entries = []
            with open(PROMPTS_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line or line.startswith("#"):
                        continue
                    n, _, t = line.partition(":")
                    if t:
                        entries.append((n.strip(), t.strip().replace('\\n', '\n')))
        if any(n == name for n, _ in entries):
            ow = self._prompt_input(f"  '{name}' already exists — overwrite? [y/N]:")
            if ow.lower() not in ("y", "yes"):
                print("[cancelled]")
                return
            entries = [(n, t) for n, t in entries if n != name]
        entries.append((name, text))
        os.makedirs(os.path.dirname(PROMPTS_FILE), exist_ok=True)
        with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
            for n, t in entries:
                f.write(f"{n}: {t.replace(chr(10), chr(92) + 'n')}\n")
        first_line = text.split('\n')[0]
        suffix = ' …' if '\n' in text else ''
        print(f"[prompt] saved → {name}: {first_line}{suffix}")

    def _cmd_prompt(self, user_input: str):
        """Manage one-line prompt templates stored in prompts.txt.

        Format:  name: prompt text with optional {{param}} placeholders
        """
        parts = user_input.split(None, 2)
        sub   = parts[1] if len(parts) > 1 else ""
        rest  = parts[2].strip() if len(parts) > 2 else ""

        def _load_prompts() -> list:
            """Return list of (name, text) from prompts.txt. Unescapes \\n → newline."""
            if not os.path.isfile(PROMPTS_FILE):
                return []
            entries = []
            with open(PROMPTS_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line or line.startswith("#"):
                        continue
                    name, _, text = line.partition(":")
                    if text:
                        entries.append((name.strip(), text.strip().replace('\\n', '\n')))
            return entries

        if sub == "save":
            # /prompt save <name> <text...>  — inline text takes priority
            inline_parts = rest.split(None, 1)
            if len(inline_parts) >= 2:
                name = inline_parts[0]
                text = inline_parts[1].strip()
            else:
                # fall back to last user message in context
                name = inline_parts[0] if inline_parts else ""
                last_user = ""
                for msg in reversed(self.messages):
                    if msg["role"] == "user":
                        last_user = msg["content"]
                        break
                if not last_user:
                    print("[prompt] no user message in context yet — provide text inline: /prompt save <name> <text>")
                    return
                text = last_user
            name = name or self._prompt_input("  prompt name:")
            if not name:
                print("[cancelled]")
                return
            self._save_prompt(name, text)

        elif sub == "load":
            entries = _load_prompts()
            if not entries:
                print("[prompt] no prompts saved yet — use /prompt save <name> first")
                return
            for i, (name, text) in enumerate(entries, 1):
                first = text.split('\n')[0]
                suffix = ' …' if '\n' in text else ''
                print(f"  {i}. {name}: {_DIM}{first[:80]}{suffix}{_R}")
            # accept number inline: /prompt load 3
            if rest.strip().isdigit():
                raw = rest.strip()
            else:
                raw = self._prompt_input("  type number (Enter to cancel):")
            if not raw:
                print("[cancelled]")
                return
            try:
                idx = int(raw) - 1
                if not (0 <= idx < len(entries)):
                    print("invalid choice")
                    return
            except ValueError:
                print("invalid choice")
                return
            name, text = entries[idx]
            # fill {{param}} placeholders — use session vars first, prompt only if not set
            keys = sorted(set(re.findall(r'\{\{(\w+)\}\}', text)))
            for key in keys:
                value = self._vars.get(key, "")
                if value and value != "NaN":
                    print(f"  {key}: {value}  (from /var)")
                else:
                    value = self._prompt_input(f"  {key}:")
                if value:
                    text = text.replace(f"{{{{{key}}}}}", value)
            # always show the filled prompt
            print(f"\n{_CYAN}[prompt] {name}{_R}")
            print(text)
            print()
            self._route(text)

        elif sub == "list":
            entries = _load_prompts()
            if not entries:
                print("[prompt] no prompts saved yet")
                return
            for i, (name, text) in enumerate(entries, 1):
                first = text.split('\n')[0]
                suffix = ' …' if '\n' in text else ''
                print(f"  {i}. {name}: {_DIM}{first[:80]}{suffix}{_R}")

        elif sub == "delete":
            name = rest
            if not name:
                print("usage: /prompt delete <name>")
                return
            entries = _load_prompts()
            new = [(n, t) for n, t in entries if n != name]
            if len(new) == len(entries):
                print(f"[prompt] not found: {name}")
                return
            with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
                for n, t in new:
                    f.write(f"{n}: {t.replace(chr(10), chr(92) + 'n')}\n")
            print(f"[prompt] deleted: {name}")

        else:
            print("usage: /prompt save <name> | /prompt load [N] | /prompt list | /prompt delete <name>")

    # ── /proc helpers ──────────────────────────────────────────────────────────

    def _run_proc(self, name: str, auto: bool = False, input_text: str | None = None) -> str:
        """Run a proc script against self.last_reply (or input_text if provided).

        auto=True  — persistent mode: suppress ACTION execution, no injection prompt.
        auto=False — manual /proc run: confirm ACTION, ask to inject result.
        Returns captured stdout or "" on failure.
        """
        parts = name.split()
        name_only = parts[0]
        extra_args = [a for a in parts[1:] if a != "translated"]
        use_translated = "translated" in parts[1:]
        if input_text is None:
            if use_translated:
                if not self.last_translated_reply:
                    print("[proc] no translated reply yet — run /translate last first")
                    return ""
                input_text = self.last_translated_reply
            else:
                if not self.last_reply:
                    print("[proc] no LLM reply yet")
                    return ""
                input_text = self.last_reply
        fname = name_only if name_only.endswith(".py") else name_only + ".py"
        path = None
        for d in (PROC_DIR, LOCAL_PROC_DIR):
            p = os.path.join(d, fname)
            if os.path.isfile(p):
                path = p
                break
        if not path:
            print(f"[proc] not found: {fname}")
            return ""
        ctx_used = sum(len(m.get("content", "")) // 4 for m in self.messages)
        ctx_max  = getattr(self, "num_ctx", NUM_CTX)
        proc_env = dict(os.environ)
        proc_env["BCODER_CTX_USED"] = str(ctx_used)
        proc_env["BCODER_CTX_MAX"]  = str(ctx_max)
        proc_env["BCODER_CTX_PCT"]  = str(int(ctx_used * 100 / ctx_max) if ctx_max else 0)
        proc_env["BCODER_WORKDIR"]  = WORKDIR
        if self._current_task:
            proc_env["BCODER_TASK"] = self._current_task
        if self._in_agent and self._agent_msgs:
            agent_used  = sum(len(m.get("content", "")) // 4 for m in self._agent_msgs)
            agent_limit = self._agent_ctx or ctx_max
            proc_env["BCODER_AGENT_CTX_USED"] = str(agent_used)
            proc_env["BCODER_AGENT_CTX_PCT"]  = str(int(agent_used * 100 / agent_limit) if agent_limit else 0)
        try:
            result = subprocess.run(
                [sys.executable, path] + extra_args,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=proc_env,
            )
        except subprocess.TimeoutExpired:
            print(f"[proc] timeout: {name}")
            return ""
        except Exception as e:
            print(f"[proc] error running {name}: {e}")
            return ""

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if stdout:
            self._last_proc_stdout = stdout
            self._last_output = stdout

        if result.returncode != 0:
            print(f"{_YELL}[proc] {name} exited {result.returncode}{_R}")
            if stderr:
                print(f"  {_DIM}{stderr}{_R}")
            return ""

        if not stdout:
            if not auto:
                print(f"[proc] {name}: (no output)")
            return ""

        # ── BLOCK / ALERT lines ─────────────────────────────────────────────────
        for line in stdout.splitlines():
            if line.startswith("BLOCK:"):
                reason = line[len("BLOCK:"):].strip()
                print(f"{_RED}[guard:{name}] blocked — {reason}{_R}")
                return "BLOCK"
            if line.startswith("ALERT:"):
                reason = line[len("ALERT:"):].strip()
                print(f"{_YELL}[guard:{name}] alert — {reason}{_R}")

        # ── display result ──────────────────────────────────────────────────────
        print(f"\n{_DIM}[proc:{name}]{_R}")
        for line in stdout.splitlines():
            if not line.startswith(("BLOCK:", "ALERT:", "ACTION:")):
                print(f"  {line}")
        if stderr:
            print(f"  {_DIM}{stderr}{_R}")

        # ── parse key=value params ──────────────────────────────────────────────
        params = {}
        for line in stdout.splitlines():
            if re.match(r'^\w+=\S', line) and ':' not in line.split('=')[0]:
                k, _, v = line.partition('=')
                params[k.strip()] = v.strip()
        if params and not auto:
            print(f"  {_DIM}params: {params}{_R}")

        # ── ACTION line — one-shot mode only ────────────────────────────────────
        if not auto:
            action_line = None
            for line in reversed(stdout.splitlines()):
                if line.startswith("ACTION:"):
                    action_line = line[len("ACTION:"):].strip()
                    break
            if action_line:
                # substitute extracted params
                for k, v in params.items():
                    action_line = action_line.replace(f"{{{{{k}}}}}", v)
                print(f"\n{_YELL}[proc] action:{_R} {action_line}")
                if self._confirm("  execute? [Y/n]:"):
                    self._sep("tool")
                    self._route(action_line)
            elif self._confirm("  inject proc result into context? [Y/n]:"):
                if not self._auto_apply:
                    self.messages.append({"role": "user", "content": f"[proc:{name}]\n{stdout}"})
                print("[proc] injected")

        return stdout

    # ── /translate ──────────────────────────────────────────────────────────

    _TRANSLATE_CFG = os.path.join(os.path.expanduser("~"), ".1bcoder", "translate.json")

    def _translate_load_cfg(self) -> dict:
        try:
            with open(self._TRANSLATE_CFG, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            print(f"[translate] cannot read config: {e}")
            return {}

    def _translate_save_cfg(self, cfg: dict):
        os.makedirs(os.path.dirname(self._TRANSLATE_CFG), exist_ok=True)
        with open(self._TRANSLATE_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _translate_run(self, text: str, from_lang: str, to_lang: str, mode: str = "") -> str:
        if not mode:
            mode = self._translate_load_cfg().get("mode", "online")
        try:
            if mode == "online":
                from deep_translator import GoogleTranslator as _GT
                _MAX = 4500
                if len(text) <= _MAX:
                    return _GT(source=from_lang, target=to_lang).translate(text) or text
                # chunk by paragraphs to stay under Google's limit
                _parts, _buf, _results = text.split("\n\n"), "", []
                for _p in _parts:
                    if len(_buf) + len(_p) + 2 <= _MAX:
                        _buf = (_buf + "\n\n" + _p) if _buf else _p
                    else:
                        if _buf:
                            _results.append(_GT(source=from_lang, target=to_lang).translate(_buf) or _buf)
                        _buf = _p
                if _buf:
                    _results.append(_GT(source=from_lang, target=to_lang).translate(_buf) or _buf)
                return "\n\n".join(_results)
            elif mode == "lm":
                cfg = self._translate_load_cfg()
                lm_timeout = int(cfg.get("lm_timeout", 120))
                lm_host  = cfg.get("lm_host",  "localhost:11434")
                lm_model = cfg.get("lm_model", "translategemma:4b")
                import urllib.request as _ur, json as _j
                _LANG_NAMES = {
                    "af":"Afrikaans","am":"Amharic","ar":"Arabic","az":"Azerbaijani",
                    "be":"Belarusian","bg":"Bulgarian","bn":"Bengali","bs":"Bosnian",
                    "ca":"Catalan","cs":"Czech","cy":"Welsh","da":"Danish","de":"German",
                    "el":"Greek","en":"English","eo":"Esperanto","es":"Spanish","et":"Estonian",
                    "eu":"Basque","fa":"Persian","fi":"Finnish","fr":"French","ga":"Irish",
                    "gl":"Galician","gu":"Gujarati","ha":"Hausa","he":"Hebrew","hi":"Hindi",
                    "hr":"Croatian","hu":"Hungarian","hy":"Armenian","id":"Indonesian",
                    "ig":"Igbo","is":"Icelandic","it":"Italian","ja":"Japanese","ka":"Georgian",
                    "kk":"Kazakh","km":"Khmer","kn":"Kannada","ko":"Korean","ky":"Kyrgyz",
                    "lo":"Lao","lt":"Lithuanian","lv":"Latvian","mg":"Malagasy","mk":"Macedonian",
                    "ml":"Malayalam","mn":"Mongolian","mr":"Marathi","ms":"Malay","mt":"Maltese",
                    "my":"Burmese","ne":"Nepali","nl":"Dutch","no":"Norwegian","ny":"Chichewa",
                    "pa":"Punjabi","pl":"Polish","ps":"Pashto","pt":"Portuguese","ro":"Romanian",
                    "ru":"Russian","sd":"Sindhi","si":"Sinhala","sk":"Slovak","sl":"Slovenian",
                    "sn":"Shona","so":"Somali","sq":"Albanian","sr":"Serbian","st":"Sesotho",
                    "sv":"Swedish","sw":"Swahili","ta":"Tamil","te":"Telugu","tg":"Tajik",
                    "th":"Thai","tl":"Filipino","tr":"Turkish","uk":"Ukrainian","ur":"Urdu",
                    "uz":"Uzbek","vi":"Vietnamese","xh":"Xhosa","yo":"Yoruba",
                    "zh-cn":"Chinese","zh-tw":"Chinese Traditional","zu":"Zulu",
                }
                src_name = _LANG_NAMES.get(from_lang.lower(), from_lang)
                tgt_name = _LANG_NAMES.get(to_lang.lower(), to_lang)
                prompt = (
                    f"You are a professional {src_name} ({from_lang}) to {tgt_name} ({to_lang}) translator. "
                    f"Your goal is to accurately convey the meaning and nuances of the original {src_name} text "
                    f"while adhering to {tgt_name} grammar, vocabulary, and cultural sensitivities.\n"
                    f"Produce only the {tgt_name} translation, without any additional explanations or commentary. "
                    f"Please translate the following {src_name} text into {tgt_name}:\n\n{text}"
                )
                # parse host prefix: openai:// → OpenAI-compatible, ollama:// or plain → Ollama
                if lm_host.startswith("openai://"):
                    base = lm_host[len("openai://"):]
                    url  = f"http://{base}/v1/chat/completions"
                    is_translategemma = "translategemma" in lm_model.lower()
                    content = [{"type": "text", "source_lang_code": from_lang,
                                "target_lang_code": to_lang, "text": text}] if is_translategemma else prompt
                    payload_dict = {
                        "model": lm_model,
                        "messages": [{"role": "user", "content": content}],
                        "stream": False,
                        "temperature": 0.1,
                    }
                    payload = _j.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
                    req = _ur.Request(url, payload, {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer lm-studio",
                    })
                    try:
                        with _ur.urlopen(req, timeout=lm_timeout) as resp:
                            return _j.loads(resp.read())["choices"][0]["message"]["content"].strip()
                    except _ur.HTTPError as e:
                        body = e.read().decode("utf-8", errors="replace")
                        raise RuntimeError(f"HTTP {e.code}: {body[:300]}")
                else:
                    base = lm_host[len("ollama://"):] if lm_host.startswith("ollama://") else lm_host
                    url  = f"http://{base}/api/generate"
                    payload = _j.dumps({"model": lm_model, "prompt": prompt, "stream": False}, ensure_ascii=False).encode("utf-8")
                    req = _ur.Request(url, payload, {"Content-Type": "application/json"})
                    try:
                        with _ur.urlopen(req, timeout=lm_timeout) as resp:
                            raw = resp.read()
                            data = _j.loads(raw)
                            return data["response"].strip()
                    except _ur.HTTPError as e:
                        body = e.read().decode("utf-8", errors="replace")
                        raise RuntimeError(f"HTTP {e.code} from {url}: {body[:300]}")
                    except _ur.URLError as e:
                        raise RuntimeError(f"cannot connect to {url}: {e.reason}")
            elif mode == "offline":
                import re as _re, ctranslate2, sentencepiece as _spm, os as _os
                _blocks: list[str] = []
                def _stash(m):
                    _blocks.append(m.group(0))
                    return f"[CODEBLK_{len(_blocks)-1}]"
                text = _re.sub(r"```[\s\S]*?```", _stash, text)
                _ph_re = _re.compile(r"^\[CODEBLK_\d+\]$")
                def _restore(s):
                    for i, b in enumerate(_blocks): s = s.replace(f"[CODEBLK_{i}]", b)
                    return s
                _NLLB_DIR = _os.path.join(_os.path.expanduser("~"), ".1bcoder", "nllb-200")
                _SP_PATH  = _os.path.join(_NLLB_DIR, "sentencepiece.bpe.model")
                if not _os.path.isdir(_NLLB_DIR):
                    raise RuntimeError(f"NLLB model not found at {_NLLB_DIR} — run /translate setup mode:offline to download")
                _FLORES = {
                    "en": "eng_Latn", "uk": "ukr_Cyrl", "de": "deu_Latn", "fr": "fra_Latn",
                    "es": "spa_Latn", "pl": "pol_Latn", "ru": "rus_Cyrl", "zh": "zho_Hans",
                    "zh-CN": "zho_Hans", "zh-TW": "zho_Hant", "ja": "jpn_Jpan", "ko": "kor_Hang",
                    "ar": "arb_Arab", "hi": "hin_Deva", "tr": "tur_Latn", "it": "ita_Latn",
                    "pt": "por_Latn", "nl": "nld_Latn", "cs": "ces_Latn", "sk": "slk_Latn",
                    "ro": "ron_Latn", "hu": "hun_Latn", "sv": "swe_Latn", "da": "dan_Latn",
                    "fi": "fin_Latn", "no": "nob_Latn", "bg": "bul_Cyrl", "hr": "hrv_Latn",
                    "sr": "srp_Cyrl", "vi": "vie_Latn", "id": "ind_Latn", "ms": "zsm_Latn",
                    "fa": "pes_Arab", "he": "heb_Hebr", "th": "tha_Thai",
                }
                src_flores = _FLORES.get(from_lang, f"{from_lang}_Latn")
                tgt_flores = _FLORES.get(to_lang,   f"{to_lang}_Latn")
                sp = _spm.SentencePieceProcessor()
                sp.Load(_SP_PATH)
                translator = ctranslate2.Translator(_NLLB_DIR, device="cpu")

                def _translate_chunk(chunk):
                    toks = sp.encode(chunk, out_type=str)
                    src = [src_flores] + toks + ["</s>"]
                    res = translator.translate_batch(
                        [src],
                        target_prefix=[[tgt_flores]],
                        max_decoding_length=512,
                        repetition_penalty=1.3,
                        beam_size=4,
                    )
                    return sp.decode(res[0].hypotheses[0][1:])

                # translate line by line — NLLB is sentence-level, long input causes repetition
                # placeholder lines are passed through unchanged
                lines = text.split("\n")
                out_lines = []
                for line in lines:
                    stripped = line.strip()
                    if not stripped or _ph_re.match(stripped):
                        out_lines.append(line)
                    else:
                        out_lines.append(_translate_chunk(stripped))
                return _restore("\n".join(out_lines))
            else:
                import re as _re, argostranslate.translate as _at
                _blocks: list[str] = []
                def _stash(m):
                    _blocks.append(m.group(0))
                    return f"[CODEBLK_{len(_blocks)-1}]"
                text = _re.sub(r"```[\s\S]*?```", _stash, text)
                def _restore(s):
                    for i, b in enumerate(_blocks): s = s.replace(f"[CODEBLK_{i}]", b)
                    return s
                # split on placeholders, translate text segments only, rejoin
                _ph_re = _re.compile(r"(\[CODEBLK_\d+\])")
                parts = _ph_re.split(text)
                out_parts = []
                for part in parts:
                    if _ph_re.fullmatch(part):
                        out_parts.append(part)
                    elif part.strip():
                        out_parts.append(_at.translate(part, from_lang, to_lang))
                    else:
                        out_parts.append(part)
                return _restore("".join(out_parts))
        except Exception as e:
            print(f"[translate] error ({mode}): {e}")
            return None

    # ── /web + /webask ──────────────────────────────────────────────────────

    @staticmethod
    def _web_strip_html(html_bytes: bytes) -> str:
        from html.parser import HTMLParser
        class _S(HTMLParser):
            SKIP = {"script","style","nav","header","footer","aside","noscript"}
            def __init__(self):
                super().__init__(); self._d=0; self.out=[]
            def handle_starttag(self,t,a):
                if t in self.SKIP: self._d+=1
            def handle_endtag(self,t):
                if t in self.SKIP and self._d: self._d-=1
            def handle_data(self,d):
                if not self._d:
                    s=d.strip()
                    if s: self.out.append(s)
        p=_S(); p.feed(html_bytes.decode("utf-8","replace")); return "\n".join(p.out)

    @staticmethod
    def _web_ddg_search(term: str, n: int = 8):
        """Return list of (title, url, snippet) from DuckDuckGo HTML search."""
        import requests as _r, re as _re
        from urllib.parse import parse_qs, urlparse, unquote
        from html.parser import HTMLParser

        class _DDG(HTMLParser):
            def __init__(self):
                super().__init__()
                self.results = []
                self._cur = {}
                self._in = None
            def handle_starttag(self, tag, attrs):
                d = dict(attrs)
                cls = d.get("class","")
                if tag == "a" and "result__a" in cls:
                    href = d.get("href","")
                    try:
                        params = parse_qs(urlparse(href).query)
                        url = params.get("uddg",[""])[0]
                        if not url: url = unquote(href)
                    except Exception:
                        url = href
                    self._cur["url"] = url
                    self._in = "title"
                elif tag == "a" and "result__snippet" in cls:
                    self._in = "snippet"
            def handle_endtag(self, tag):
                if self._in and tag == "a":
                    if "url" in self._cur and "title" in self._cur:
                        if "snippet" not in self._cur:
                            self._cur["snippet"] = ""
                        self.results.append((
                            self._cur.get("title",""),
                            self._cur.get("url",""),
                            self._cur.get("snippet",""),
                        ))
                        self._cur = {}
                    self._in = None
            def handle_data(self, data):
                if self._in == "title":
                    self._cur["title"] = self._cur.get("title","") + data
                elif self._in == "snippet":
                    self._cur["snippet"] = self._cur.get("snippet","") + data

        resp = _r.post(
            "https://html.duckduckgo.com/html/",
            data={"q": term},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        parser = _DDG()
        parser.feed(resp.text)
        return parser.results[:n]

    # ── /visual ─────────────────────────────────────────────────────────────

    _VISUAL_CFG = os.path.join(os.path.expanduser("~"), ".1bcoder", "visual.json")

    def _visual_load_cfg(self) -> dict:
        try:
            with open(self._VISUAL_CFG, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            print(f"[visual] cannot read config: {e}")
            return {}

    def _visual_save_cfg(self, cfg: dict):
        os.makedirs(os.path.dirname(self._VISUAL_CFG), exist_ok=True)
        with open(self._VISUAL_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _visual_run(self, b64_image: str, prompt: str) -> str:
        """Send image + prompt to the configured vision model. Returns text description."""
        import urllib.request as _ur, json as _j
        cfg = self._visual_load_cfg()
        if not cfg:
            raise RuntimeError("visual model not configured — run /visual setup first")
        host  = cfg.get("host",  "localhost:11434")
        model = cfg.get("model", "moondream:v2")
        timeout = int(cfg.get("timeout", 120))
        msg = {"role": "user", "content": prompt, "images": [b64_image]}
        if host.startswith("openai://"):
            base = host[len("openai://"):]
            url  = f"http://{base}/v1/chat/completions"
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}},
            ]
            payload = _j.dumps({
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "stream": False,
            }, ensure_ascii=False).encode("utf-8")
            req = _ur.Request(url, payload, {
                "Content-Type": "application/json",
                "Authorization": "Bearer lm-studio",
            })
            with _ur.urlopen(req, timeout=timeout) as resp:
                return _j.loads(resp.read())["choices"][0]["message"]["content"].strip()
        else:
            base = host[len("ollama://"):] if host.startswith("ollama://") else host
            url  = f"http://{base}/api/chat"
            payload = _j.dumps({
                "model": model,
                "messages": [msg],
                "stream": False,
            }, ensure_ascii=False).encode("utf-8")
            req = _ur.Request(url, payload, {"Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=timeout) as resp:
                return _j.loads(resp.read())["message"]["content"].strip()

    def _cmd_visual(self, user_input: str):
        import base64
        parts = user_input.split()
        sub   = parts[1].lower() if len(parts) > 1 else ""

        if sub == "status":
            cfg = self._visual_load_cfg()
            if not cfg:
                print("[visual] not configured — run /visual setup first")
                return
            print(f"  host    : {cfg.get('host',  'localhost:11434')}")
            print(f"  model   : {cfg.get('model', 'moondream:v2')}")
            print(f"  timeout : {cfg.get('timeout', 120)}s")
            return

        if sub == "setup":
            raw = " ".join(parts[2:]).strip() if len(parts) > 2 else ""
            if not raw:
                print("usage: /visual setup host:<url> model:<name> [timeout:<s>] [profile:<name>]")
                print("  e.g.  /visual setup host:localhost:11434 model:moondream:v2")
                print("        /visual setup profile:moondream")
                return
            params = {}
            for token in raw.split():
                if ':' in token:
                    k, _, v = token.partition(':')
                    params[k.lower()] = v
            cfg = self._visual_load_cfg()
            if 'host'    in params: cfg['host']    = params['host']
            if 'model'   in params: cfg['model']   = params['model']
            if 'timeout' in params: cfg['timeout'] = int(params['timeout'])
            if 'profile' in params:
                prof = _load_profile(params['profile'])
                if not prof:
                    print(f"[visual] profile '{params['profile']}' not found")
                    return
                cfg['host']  = prof[0][0]
                cfg['model'] = prof[0][1]
            self._visual_save_cfg(cfg)
            print(f"[visual] saved — host:{cfg.get('host')} model:{cfg.get('model')}")
            return

        if sub == "explain":
            auto_inject = self._auto_apply or "-y" in parts
            tokens = [t for t in parts[2:] if t != "-y"]
            IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
            img_paths, prompt_words = [], []
            for t in tokens:
                if os.path.splitext(t)[1].lower() in IMG_EXTS or os.path.isfile(t):
                    img_paths.append(t)
                else:
                    prompt_words.append(t)
            # fall back to last image in main context if no file given
            b64 = None
            label = ""
            if img_paths:
                path = img_paths[0]
                if not os.path.isfile(path):
                    print(f"[visual] file not found: {path}")
                    return
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                label = os.path.basename(path)
            else:
                for m in reversed(self.messages):
                    if "images" in m:
                        b64 = m["images"][0]
                        label = "(image from context)"
                        break
            if not b64:
                print("[visual] no image — provide a file path or inject with /view first")
                return
            prompt = " ".join(prompt_words) if prompt_words else "Describe this image in detail."
            cfg = self._visual_load_cfg()
            vis_model = cfg.get("model", "moondream:v2")
            print(f"{_LBLUE}─── {vis_model} ───{_R}", flush=True)
            try:
                description = self._visual_run(b64, prompt)
            except Exception as e:
                print(f"[visual] error: {e}")
                return
            print(description)
            self._last_output = description
            inject_text = f"[visual description of {label}]\n{description}"
            if auto_inject:
                self.messages.append({"role": "user", "content": inject_text})
                _ok("[visual] description injected into context")
            else:
                ans = self._confirm("Inject description into context? [Y/n]", ctx_add=inject_text)
                if not ans:
                    self.messages.append({"role": "user", "content": inject_text})
            return

        print("usage: /visual setup | status | explain <image> [prompt] [-y]")

    def _cmd_web(self, user_input: str):
        """
/web search <term> [-> varname]   Search DuckDuckGo; optionally save URL list to variable.
/web fetch <url> [-n 4000]        Fetch URL, strip HTML, inject cleaned text into context.
        """
        parts = user_input.split(None, 2)
        sub = parts[1].lower() if len(parts) > 1 else ""

        ctx = self._agent_msgs if (self._in_agent and self._agent_msgs) else self.messages

        if sub == "search":
            raw = parts[2] if len(parts) > 2 else ""
            var_name = None
            if " -> " in raw:
                raw, _, var_name = raw.partition(" ->")
                var_name = var_name.strip().lstrip("$")
            term = raw.strip()
            if not term:
                print("usage: /web search <term> [-> varname]"); return
            print(f"[web] searching: {term} ...")
            try:
                results = self._web_ddg_search(term)
            except Exception as e:
                print(f"[web] search failed: {e}"); return
            if not results:
                print("[web] no results"); return
            urls = []
            lines = []
            for i, (title, url, snippet) in enumerate(results, 1):
                print(f"  {i}. {title}")
                print(f"     {url}")
                if snippet: print(f"     {snippet}")
                urls.append(url)
                lines.append(f"{i}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else ""))
            ctx.append({"role": "user",
                        "content": f"[WEB SEARCH: {term}]\n" + "\n\n".join(lines)})
            if var_name:
                self._vars[var_name] = "\n".join(urls)
                print(f"[web] {len(urls)} URLs saved to {{{var_name}}}")
            return

        if sub == "fetch":
            raw = parts[2] if len(parts) > 2 else ""
            max_chars = 4000
            if " -n " in raw:
                raw, _, n_str = raw.partition(" -n ")
                try: max_chars = int(n_str.strip().split()[0])
                except ValueError: pass
            url = raw.strip()
            if not url:
                print("usage: /web fetch <url> [-n 4000]"); return
            print(f"[web] fetching {url} ...")
            try:
                import requests as _r
                resp = _r.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                text = self._web_strip_html(resp.content)
                text = text[:max_chars]
            except Exception as e:
                print(f"[web] fetch failed: {e}"); return
            ctx.append({"role": "user",
                        "content": f"[WEB CONTENT from {url}]\n{text}"})
            print(f"[web] {len(text)} chars injected into context")
            return

        print("usage: /web search <term> [-> varname]")
        print("       /web fetch <url> [-n 4000]")

    # ── /flow ───────────────────────────────────────────────────────────────

    def _cmd_flow(self, user_input: str):
        """
/flow <name> [args]   Run a flow from _bcoder_data/flows/<name>.py.
/flow list            List available flows.
        """
        import importlib.util as _ilu
        parts = user_input.split(None, 2)
        sub = parts[1].strip() if len(parts) > 1 else ""

        def _find_flow(name):
            for d in (os.path.join(BCODER_DIR, "flows"),
                      os.path.join(HOME_BCODER_DIR, "flows"),
                      os.path.join(INSTALL_BCODER_DIR, "flows")):
                p = os.path.join(d, name + ".py")
                if os.path.isfile(p):
                    return p
            return None

        if not sub or sub == "list":
            dirs = [
                os.path.join(BCODER_DIR, "flows"),
                os.path.join(HOME_BCODER_DIR, "flows"),
                os.path.join(INSTALL_BCODER_DIR, "flows"),
            ]
            shown = set()
            for d in dirs:
                if not os.path.isdir(d): continue
                for f in sorted(os.listdir(d)):
                    if f.endswith(".py") and not f.startswith("_") and f not in shown:
                        shown.add(f)
                        spec = _ilu.spec_from_file_location("_flow", os.path.join(d, f))
                        mod  = _ilu.module_from_spec(spec)
                        try:
                            spec.loader.exec_module(mod)
                            doc = (getattr(mod, "run", None) or (lambda:None)).__doc__ or ""
                            desc = doc.strip().splitlines()[0] if doc.strip() else ""
                        except Exception:
                            desc = ""
                        tag = "[local]" if d.startswith(BCODER_DIR) else ("[global]" if d.startswith(HOME_BCODER_DIR) else "")
                        print(f"  {f[:-3]:<20} {desc}  {tag}")
            if not shown:
                print("[flow] no flows found")
            return

        if sub == "help":
            name = parts[2].strip() if len(parts) > 2 else ""
            if not name:
                print("usage: /flow help <name>"); return
            path = _find_flow(name)
            if not path:
                print(f"[flow] '{name}' not found — use /flow list"); return
            try:
                src = open(path, encoding="utf-8").read()
                import ast as _ast
                doc = _ast.get_docstring(_ast.parse(src)) or ""
                print(doc if doc else f"[flow] '{name}' has no docstring")
            except Exception as e:
                print(f"[flow] could not read {name}: {e}")
            return

        name = sub
        args = parts[2].strip() if len(parts) > 2 else ""
        path = _find_flow(name)
        if not path:
            print(f"[flow] '{name}' not found — use /flow list"); return

        spec = _ilu.spec_from_file_location(f"_flow_{name}", path)
        mod  = _ilu.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"[flow] load error: {e}"); return
        if not hasattr(mod, "run"):
            print(f"[flow] '{name}.py' has no run(chat, args) function"); return
        try:
            mod.run(self, args)
        except Exception as e:
            import traceback
            print(f"[flow] runtime error: {e}")
            traceback.print_exc()

    def _translate_activate(self):
        pass  # translation is handled inline in the main loop via translate.json

    def _translate_deactivate(self):
        pass  # translation is handled inline in the main loop via translate.json

    def _translate_autoload(self):
        cfg = self._translate_load_cfg()
        if cfg.get("enabled") and cfg.get("lang"):
            mode = cfg.get("mode", "mini")
            print(f"  [translate] on — {cfg['lang']} ↔ en ({mode})")

    def _cmd_translate(self, user_input: str):
        """
/translate setup [lang:<code>] [mode:online|mini|offline|lm] [host:<url>] [model:<name>] [profile:<name>]
                  All params optional — unspecified ones keep existing values.
/translate on | off | status
/translate last [mode:<m>] [lang:<code>]

Modes: online  = Google Translate — needs internet, not for confidential projects
       mini    = argostranslate — fully local, ~100MB packages per language pair
       offline = NLLB-200 ctranslate2 — fully local, ~2.4GB one-time download → ~600MB after conversion, better quality, no model switch
       lm      = local/remote Ollama — fully private, uses dedicated translation model
Config stored in ~/.1bcoder/translate.json
        """
        parts = user_input.split(None, 3)
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub == "status":
            cfg = self._translate_load_cfg()
            if not cfg:
                print("[translate] not configured — run /translate setup <lang>")
                return
            mode = cfg.get("mode", "mini")
            print(f"  lang    : {cfg.get('lang', '?')}")
            print(f"  mode    : {mode}")
            if mode == "lm":
                print(f"  lm_host   : {cfg.get('lm_host', 'localhost:11434')}")
                print(f"  lm_model  : {cfg.get('lm_model', 'translategemma:4b')}")
                print(f"  lm_timeout: {cfg.get('lm_timeout', 120)}s")
            print(f"  enabled : {'yes' if cfg.get('enabled') else 'no'}")
            if mode == "online":
                print("  WARNING : online mode sends text to Google servers")
            return

        if sub == "off":
            cfg = self._translate_load_cfg()
            cfg["enabled"] = False
            self._translate_save_cfg(cfg)
            print("[translate] off")
            return

        if sub == "on":
            cfg = self._translate_load_cfg()
            if not cfg.get("lang"):
                print("[translate] no language set — run /translate setup <lang> first")
                return
            cfg["enabled"] = True
            self._translate_save_cfg(cfg)
            mode = cfg.get("mode", "mini")
            print(f"[translate] on — {cfg['lang']} ↔ en ({mode})")
            return

        if sub == "setup":
            raw = " ".join(parts[2:]).strip() if len(parts) > 2 else ""
            if not raw:
                print("usage: /translate setup [lang:uk] [mode:lm] [host:<url>] [model:<name>] [profile:<name>]")
                print("  All parameters optional — unset ones keep existing values from translate.json")
                print("  e.g. /translate setup lang:uk mode:lm host:192.168.0.229:11434 model:translategemma:4b")
                print("       /translate setup host:openai://192.168.0.229:1234")
                print("       /translate setup mode:online")
                return
            # parse key:value tokens (partition on first : to handle colons in values)
            params = {}
            for token in raw.split():
                if ':' in token:
                    k, _, v = token.partition(':')
                    params[k.lower()] = v
            # load existing config and apply only what was provided
            cfg = self._translate_load_cfg()
            if 'lang'    in params: cfg['lang']       = params['lang']
            if 'mode'    in params: cfg['mode']       = params['mode']
            if 'host'    in params: cfg['lm_host']    = params['host']
            if 'model'   in params: cfg['lm_model']   = params['model']
            if 'timeout' in params: cfg['lm_timeout'] = int(params['timeout'])
            if 'profile' in params:
                prof = _load_profile(params['profile'])
                if not prof:
                    print(f"[translate] profile '{params['profile']}' not found")
                    return
                cfg['lm_host']  = prof[0][0]
                cfg['lm_model'] = prof[0][1]
                cfg['mode']     = 'lm'
            cfg['enabled'] = True
            mode = cfg.get('mode', 'online')
            lang = cfg.get('lang', '')
            # install dependencies if mode requires it
            import subprocess as _sp
            if mode == 'online':
                try:
                    import deep_translator  # noqa
                except ImportError:
                    print("[translate] installing deep-translator...")
                    r = _sp.run([sys.executable, "-m", "pip", "install", "deep-translator", "-q"],
                                capture_output=True, text=True)
                    if r.returncode != 0:
                        print(f"[translate] pip failed: {r.stderr.strip()}"); return
                    print("[translate] deep-translator installed")
                print("  WARNING: online mode sends your text to Google servers.")
                print("           Do not use with confidential projects.")
            elif mode == 'mini' and lang:
                try:
                    import argostranslate.translate  # noqa
                except ImportError:
                    print("[translate] installing argostranslate...")
                    r = _sp.run([sys.executable, "-m", "pip", "install", "argostranslate", "-q"],
                                capture_output=True, text=True)
                    if r.returncode != 0:
                        print(f"[translate] pip failed: {r.stderr.strip()}"); return
                    print("[translate] argostranslate installed")
                if 'lang' in params or 'mode' in params:
                    print(f"[translate] downloading packages {lang} ↔ en...")
                    try:
                        import argostranslate.package as _pkg
                        _pkg.update_package_index()
                        available = _pkg.get_available_packages()
                        for fc, tc in [(lang, "en"), ("en", lang)]:
                            try:
                                pkg = next(p for p in available if p.from_code == fc and p.to_code == tc)
                                print(f"  {fc} → {tc} ...")
                                _pkg.install_from_path(pkg.download())
                                print(f"  OK")
                            except StopIteration:
                                print(f"  WARNING: {fc}→{tc} not found in index")
                    except Exception as e:
                        print(f"[translate] error: {e}"); return
            elif mode == 'offline':
                _NLLB_DIR = os.path.join(os.path.expanduser("~"), ".1bcoder", "nllb-200")
                for pkg_name in ("ctranslate2", "sentencepiece"):
                    try:
                        __import__(pkg_name)
                    except ImportError:
                        print(f"[translate] installing {pkg_name}...")
                        r = _sp.run([sys.executable, "-m", "pip", "install", pkg_name, "-q"],
                                    capture_output=True, text=True)
                        if r.returncode != 0:
                            print(f"[translate] pip failed: {r.stderr.strip()}"); return
                        print(f"[translate] {pkg_name} installed")
                if not os.path.isdir(_NLLB_DIR):
                    print("[translate] downloading NLLB-200-distilled-600M (~2.4 GB, converts to ~600 MB after int8 conversion)...")
                    print("  This is a one-time download. Stored in ~/.1bcoder/nllb-200/")
                    try:
                        from huggingface_hub import snapshot_download
                    except ImportError:
                        r = _sp.run([sys.executable, "-m", "pip", "install", "huggingface_hub", "-q"],
                                    capture_output=True, text=True)
                        if r.returncode != 0:
                            print(f"[translate] pip failed: {r.stderr.strip()}"); return
                        from huggingface_hub import snapshot_download
                    import ctranslate2
                    ct2_dir = os.path.join(os.path.expanduser("~"), ".1bcoder", "nllb-200-hf")
                    snapshot_download("facebook/nllb-200-distilled-600M", local_dir=ct2_dir)
                    print("[translate] converting to ctranslate2 format...")
                    ctranslate2.converters.TransformersConverter(ct2_dir).convert(_NLLB_DIR, quantization="int8")
                    import shutil as _sh
                    _sp_src = os.path.join(ct2_dir, "sentencepiece.bpe.model")
                    _sp_dst = os.path.join(_NLLB_DIR, "sentencepiece.bpe.model")
                    _sh.copy2(_sp_src, _sp_dst)
                    if not os.path.isfile(_sp_dst):
                        print(f"[translate] WARNING: failed to copy sentencepiece model to {_sp_dst}")
                        print(f"  Copy manually: {_sp_src}  →  {_sp_dst}")
                    else:
                        print("[translate] NLLB model ready")
                        _sh.rmtree(ct2_dir, ignore_errors=True)
                        print(f"[translate] removed source download (~2.4 GB freed)")
                else:
                    print(f"[translate] NLLB model already at {_NLLB_DIR}")
            self._translate_save_cfg(cfg)
            # show summary
            print(f"[translate] saved — {lang or '?'} ↔ en ({mode})")
            if mode == 'lm':
                print(f"  host    : {cfg.get('lm_host',  'localhost:11434')}")
                print(f"  model   : {cfg.get('lm_model', 'translategemma:4b')}")
                print(f"  timeout : {cfg.get('lm_timeout', 120)}s")
            return

        if sub == "last":
            if not self.last_reply:
                print("[translate] no reply to translate yet")
                return
            cfg = self._translate_load_cfg()
            mode = cfg.get("mode", "online")
            lang = cfg.get("lang", "")
            tokens = " ".join(parts[2:]).split() if len(parts) > 2 else []
            i = 0
            while i < len(tokens):
                tok = tokens[i]
                if ":" in tok:
                    k, _, v = tok.partition(":")
                    if k == "mode": mode = v
                    elif k == "lang": lang = v
                elif tok in ("online", "mini", "offline", "lm"):
                    mode = tok
                elif tok == "lang" and i + 1 < len(tokens):
                    lang = tokens[i + 1]
                    i += 1
                elif tok == "mode" and i + 1 < len(tokens):
                    mode = tokens[i + 1]
                    i += 1
                i += 1
            if not lang:
                print("[translate] no language set — run /translate setup <lang> first")
                return
            print(f"[translate] en → {lang} ({mode})...")
            translated = self._translate_run(self.last_reply, "en", lang, mode=mode)
            self.last_translated_reply = translated
            print(f"\n{_LBLUE}─── {lang.upper()} ───{_R}")
            if translated:
                print(translated)
            else:
                print("[translate] translation returned empty result — check language code or network")
            return

        print("usage: /translate setup [lang:uk] [mode:lm] [host:<url>] [model:<name>] [profile:<name>]")
        print("       /translate on | off | status")
        print("       /translate last [mode:<m>] [lang:<code>]")

    # ── /proc ───────────────────────────────────────────────────────────────

    def _cmd_proc(self, user_input: str):
        parts = user_input.split(None, 2)
        sub   = parts[1] if len(parts) > 1 else ""
        rest  = parts[2].strip() if len(parts) > 2 else ""

        if sub == "list":
            import ast as _ast
            active_names = {p.split()[0] for p in self._proc_active}
            all_files: dict[str, str] = {}
            for d in (LOCAL_PROC_DIR, PROC_DIR):
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        if f.endswith(".py") and f not in all_files:
                            all_files[f] = os.path.join(d, f)
            if not all_files:
                print("[proc] no processors found — use /proc new <name> to create one")
                return
            for f in sorted(all_files):
                name_no_ext = f[:-3]
                marker = " *" if name_no_ext in active_names or f in active_names else ""
                try:
                    src = open(all_files[f], encoding="utf-8").read()
                    doc = _ast.get_docstring(_ast.parse(src)) or ""
                    first = doc.strip().splitlines()[0] if doc.strip() else ""
                    # strip leading "procname — " or "procname: " prefix if present
                    for sep in (" — ", " - ", ": "):
                        if first.lower().startswith(name_no_ext.lower() + sep):
                            first = first[len(name_no_ext) + len(sep):]
                            break
                except Exception:
                    first = ""
                tag = " [local]" if all_files[f].startswith(LOCAL_PROC_DIR) else ""
                print(f"  {name_no_ext:<22} {first}{marker}{tag}")

        elif sub == "help":
            name = rest.split()[0] if rest else ""
            if not name:
                print("usage: /proc help <name>"); return
            fname = name if name.endswith(".py") else name + ".py"
            path = next((os.path.join(d, fname) for d in (LOCAL_PROC_DIR, PROC_DIR)
                         if os.path.isfile(os.path.join(d, fname))), None)
            if not path:
                print(f"[proc] '{name}' not found — use /proc list"); return
            try:
                import ast as _ast
                src = open(path, encoding="utf-8").read()
                doc = _ast.get_docstring(_ast.parse(src)) or ""
                print(doc if doc else f"[proc] '{name}' has no docstring")
            except Exception as e:
                print(f"[proc] could not read {name}: {e}")

        elif sub == "run":
            if not rest:
                print("usage: /proc run <name> [-f <file>]")
                return
            # parse optional -f <file> flag
            file_input = None
            tokens = rest.split()
            if "-f" in tokens:
                fi = tokens.index("-f")
                if fi + 1 < len(tokens):
                    fpath = tokens[fi + 1]
                    fpath = fpath if os.path.isabs(fpath) else os.path.join(WORKDIR, fpath)
                    try:
                        with open(fpath, encoding="utf-8", errors="replace") as fh:
                            file_input = fh.read()
                    except OSError as e:
                        _err(f"[proc] cannot read file: {e}")
                        return
                    tokens = tokens[:fi] + tokens[fi + 2:]
                    rest = " ".join(tokens)
            self._run_proc(rest, auto=False, input_text=file_input)

        elif sub in ("on", "before", "gate"):
            # /proc on <name>      — after each reply
            # /proc before on <name> — before each LLM call
            # /proc gate on <name>   — PASS/FAIL gate after each reply
            if sub in ("before", "gate"):
                # expect: /proc before on <name> or /proc gate on <name>
                rest_parts = rest.split(None, 1)
                if rest_parts[0] != "on" or len(rest_parts) < 2:
                    print(f"usage: /proc {sub} on <name> [args...]")
                    return
                rest = rest_parts[1]
            target_list = (self._proc_before if sub == "before"
                           else self._proc_gates if sub == "gate"
                           else self._proc_active)
            label_str   = ("before each LLM call" if sub == "before"
                           else "gate (PASS/FAIL after reply)" if sub == "gate"
                           else "after every reply")
            name_part = rest.split()[0]
            extra_args = rest.split()[1:]
            fname = name_part if name_part.endswith(".py") else name_part + ".py"
            path = next((os.path.join(d, fname) for d in (PROC_DIR, LOCAL_PROC_DIR)
                         if os.path.isfile(os.path.join(d, fname))), None)
            if not path:
                print(f"[proc] not found: {fname}")
                return
            if rest not in target_list:
                target_list.append(rest)
            suffix = f" {' '.join(extra_args)}" if extra_args else ""
            print(f"[proc] persistent: {name_part}{suffix} ({label_str})")

        elif sub == "off":
            all_lists = [self._proc_active, self._proc_before, self._proc_gates]
            if not any(all_lists):
                print("[proc] no persistent processors active")
            elif rest:
                removed = []
                for lst in all_lists:
                    to_remove = [p for p in lst if p.split()[0] == rest]
                    for p in to_remove:
                        lst.remove(p)
                        removed.append(p)
                if removed:
                    print(f"[proc] stopped: {rest}")
                else:
                    print(f"[proc] not active: {rest}")
            else:
                names = [p.split()[0] for lst in all_lists for p in lst]
                print(f"[proc] stopped: {', '.join(names)}")
                self._proc_active = []
                self._proc_before = []
                self._proc_gates  = []

        elif sub == "new":
            name = rest or self._prompt_input("  processor name:")
            if not name:
                print("[cancelled]")
                return
            name = name.strip()
            if not name.endswith(".py"):
                name += ".py"
            os.makedirs(PROC_DIR, exist_ok=True)
            path = os.path.join(PROC_DIR, name)
            if os.path.exists(path):
                print(f"[proc] already exists: {path}")
                return
            template = (
                "import sys, re\n\n"
                "reply = sys.stdin.read()\n\n"
                "# --- your logic here ---\n"
                "# print key=value lines to expose params\n"
                "# print ACTION: /command  to trigger a follow-up command\n"
                "# exit with sys.exit(1) to signal failure\n\n"
                "print(reply[:200])  # replace with real logic\n"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(template)
            print(f"[proc] created: {path}")

        elif sub == "" or sub == "proc":
            if self._proc_active:
                for p in self._proc_active:
                    print(f"[proc] active: {p}")
            else:
                print("[proc] no persistent processor active")

        else:
            print("usage: /proc list | help <name> | run <name> | on <name> | off | new <name>")

    # ── /text ───────────────────────────────────────────────────────────────────

    def _cmd_note(self):
        """Open multi-line editor, then send the composed text to the AI."""
        text, prompt_name = _multiline_input()
        if not text.strip():
            print("[note] empty — nothing sent")
            return
        if prompt_name is not None:
            if not prompt_name:
                prompt_name = self._prompt_input("  prompt name:").strip().replace(" ", "-")
            if prompt_name:
                self._save_prompt(prompt_name, text)
        self.messages.append({"role": "user", "content": text})
        self._sep("AI")
        reply = self._stream_chat(self.messages)
        if reply:
            self.last_reply = reply
            self._last_output = reply
            self.messages.append({"role": "assistant", "content": reply})
            for _proc in self._proc_active:
                self._run_proc(_proc, auto=True)
        elif self.messages:
            self.messages.pop()

    # ── /var ────────────────────────────────────────────────────────────────────

    def _cmd_role(self, user_input: str):
        parts = user_input.split(None, 1)
        sub   = parts[1].strip() if len(parts) > 1 else ""

        if not sub:
            print("usage: /role <persona> | /role show | /role clear")
            return

        if sub == "show":
            if self._role:
                print(f"[role] {self._role}")
            else:
                print("[role] not set")
            return

        if sub == "clear":
            self._role = ""
            _ok("[role] cleared")
            return

        self._role = sub
        _ok(f"[role] {self._role}")

    def _cmd_var(self, user_input: str):
        """Session variable store.  Variables expand as {{name}} in any command.

        /var set <name> [<key>]   — capture from last proc output or last reply
        /var set <name> =<value>  — set literal value
        /var def <name> [...]     — declare variable(s) with NaN value (skip if already set)
        /var get                  — list all active variables
        /var save <file>          — save all vars to file (key=value, auto .var extension)
        /var load <file>          — load vars from key=value file (.var or any text)
        /var extract              — show {{placeholders}} in current script with set/NaN status
        /var del <name>           — remove a variable
        """
        parts = user_input.split(None, 3)
        sub   = parts[1] if len(parts) > 1 else ""
        arg2  = parts[2] if len(parts) > 2 else ""
        arg3  = parts[3] if len(parts) > 3 else ""

        if sub == "set":
            name = arg2
            if not name:
                print("usage: /var set <name> [<proc_key> | =<literal>]")
                return

            # shorthand: /var set name=value  (no space around =)
            if "=" in name and not arg3:
                name, _, val = name.partition("=")
                self._vars[name] = val
                _ok(f"[var] {name} = {val}")
                return

            # literal value: /var set name =value  or  /var set name = value
            if arg3.startswith("="):
                self._vars[name] = arg3[1:].strip()
                _ok(f"[var] {name} = {self._vars[name]}")
                return

            if arg3:
                # key-based: look in last proc stdout for key=value
                source = self._last_proc_stdout
                if not source:
                    print("[var] no proc output yet — run /proc first")
                    return
                found = None
                for line in source.splitlines():
                    if re.match(rf'^{re.escape(arg3)}=', line):
                        found = line[len(arg3) + 1:]
                        break
                if found is None:
                    print(f"[var] key '{arg3}' not found in last proc output")
                    # show available keys
                    keys = [l.split("=")[0] for l in source.splitlines()
                            if re.match(r'^\w+=\S', l) and ':' not in l.split('=')[0]]
                    if keys:
                        print(f"  available keys: {', '.join(keys)}")
                    return
                self._vars[name] = found
                _ok(f"[var] {name} = {found}")
            else:
                # no key: first non-empty, non-param line from last proc stdout, else first line of last reply
                source = self._last_proc_stdout
                if source:
                    value = ""
                    for line in source.splitlines():
                        stripped = line.strip()
                        if stripped and not re.match(r'^\w+=\S', stripped):
                            value = stripped
                            break
                    if not value:
                        value = source.splitlines()[0].strip()
                else:
                    if not self.last_reply:
                        print("[var] no content yet — run a command or /proc first")
                        return
                    value = self.last_reply.strip().splitlines()[0].strip()
                self._vars[name] = value
                _ok(f"[var] {name} = {value}")

        elif sub == "get":
            if arg2:
                name = arg2.strip("{} ")
                if name in self._vars:
                    print(self._vars[name])
                else:
                    print(f"[var] not set: {name}")
                return
            if not self._vars:
                print("[var] no variables set")
                return
            w = max(len(k) for k in self._vars)
            for k in sorted(self._vars):
                v = self._vars[k]
                display = f"{_GREEN}{v}{_R}" if v else f"{_RED}NaN{_R}"
                print(f"  {_CYAN}{k:<{w}}{_R}  =  {display}")

        elif sub == "extract":
            if arg2:
                # /var extract <file> — scan arbitrary file
                src_path = arg2 if os.path.isabs(arg2) else os.path.join(WORKDIR, arg2)
                if not os.path.isfile(src_path):
                    print(f"[var] file not found: {arg2}")
                    return
                try:
                    with open(src_path, encoding="utf-8") as f:
                        text = f.read()
                except OSError as e:
                    print(f"[var] cannot read {arg2}: {e}")
                    return
                keys = set(re.findall(r'\{\{(\w+)\}\}', text))
                src_label = arg2
            else:
                # /var extract — scan current open plan
                if not self._script_file:
                    print("[var] no script open — use /script open or /var extract <file>")
                    return
                lines = _load_script(self._script_file)
                keys = set()
                for l in lines:
                    if not l.strip().startswith("#"):
                        keys.update(re.findall(r'\{\{(\w+)\}\}', l))
                src_label = self._script_file
            if not keys:
                print(f"[var] no {{{{variables}}}} found in {src_label}")
                return
            # register unset keys into _vars so /var get shows them
            for k in keys:
                if k not in self._vars:
                    self._vars[k] = ""
            w = max(len(k) for k in keys)
            print(f"[var] variables in {src_label}:")
            for k in sorted(keys):
                v = self._vars[k]
                display = f"{_GREEN}{v}{_R}" if v else f"{_RED}NaN{_R}"
                print(f"  {_CYAN}{k:<{w}}{_R}  =  {display}")

        elif sub == "save":
            if not arg2:
                print("usage: /var save <file>")
                return
            path = arg2 if os.path.isabs(arg2) else os.path.join(WORKDIR, arg2)
            # add .var extension if no extension given
            if '.' not in os.path.basename(path):
                path += ".var"
            to_save = {k: v for k, v in self._vars.items() if v and k != "description"}
            description = self._vars.get("description", "")
            if not to_save and not description:
                print("[var] no variables to save")
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"# {description}\n" if description else "# (no description)\n")
                    for k, v in sorted(to_save.items()):
                        f.write(f"{k}={v}\n")
                _ok(f"[var] saved {len(to_save)} variable(s) → {path}")
            except OSError as e:
                print(f"[var] cannot write: {e}")

        elif sub == "load":
            if not arg2:
                print("usage: /var load <file>")
                return
            path = arg2 if os.path.isabs(arg2) else os.path.join(WORKDIR, arg2)
            if not os.path.isfile(path):
                print(f"[var] file not found: {path}")
                return
            loaded = 0
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if '=' in line:
                            k, _, v = line.partition('=')
                            k = k.strip().strip('{}')  # handle both key= and {{key}}=
                            v = v.strip()
                            if k:
                                self._vars[k] = v
                                loaded += 1
                _ok(f"[var] loaded {loaded} variable(s) ← {path}")
            except OSError as e:
                print(f"[var] cannot read: {e}")

        elif sub == "def":
            names = parts[2:]  # all remaining tokens are variable names
            if not names:
                print("usage: /var def <name> [<name2> ...]")
                return
            for n in names:
                if n not in self._vars:
                    self._vars[n] = ""
                    _ok(f"[var] {n} = NaN")
                else:
                    print(f"[var] {n} already set (use /var del to reset)")

        elif sub == "del":
            if not arg2:
                print("usage: /var del <name>")
                return
            if arg2 in self._vars:
                del self._vars[arg2]
                print(f"[var] deleted: {arg2}")
            else:
                print(f"[var] not found: {arg2}")

        else:
            print("usage: /var set <name> [<key> | =<value>] | /var def <name> | /var get | /var save <file> | /var load <file> | /var extract | /var del <name>")

    # ── /team helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _find_team_path(name: str) -> str | None:
        """Return absolute path for team yaml, checking local then global dir."""
        fname = name if name.endswith(".yaml") else name + ".yaml"
        local  = os.path.join(LOCAL_TEAMS_DIR, fname)
        global_ = os.path.join(TEAMS_DIR, fname)
        if os.path.isfile(local):
            return local
        if os.path.isfile(global_):
            return global_
        return None

    @staticmethod
    def _parse_team_file(path: str) -> list:
        """Parse a team yaml → list of {host, model, script, name, depends_on} dicts.
        No external dependencies — handles only the fixed team yaml format.
        depends_on: comma-separated list of worker names this worker waits for.
        """
        workers = []
        current = None
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip()
                    if not line or line.strip().startswith('#'):
                        continue
                    stripped = line.strip()
                    if stripped in ('workers:', 'workers:'):
                        continue
                    if stripped.startswith('- '):
                        if current is not None:
                            workers.append(current)
                        current = {}
                        rest = stripped[2:].strip()
                        if ':' in rest:
                            k, _, v = rest.partition(':')
                            current[k.strip()] = v.strip()
                    elif ':' in stripped and current is not None:
                        k, _, v = stripped.partition(':')
                        current[k.strip()] = v.strip()
            if current is not None:
                workers.append(current)
        except OSError as e:
            print(f"[team] cannot read {path}: {e}")
        # normalise: auto-assign names, parse depends_on into list
        for i, w in enumerate(workers, 1):
            if not w.get("name"):
                w["name"] = str(i)
            raw_deps = w.get("depends_on", "")
            w["depends_on"] = [d.strip() for d in raw_deps.split(",") if d.strip()] if raw_deps else []
        return workers

    # ── /config helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_config_yml(text: str) -> dict:
        cfg: dict = {}
        section = None
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line or line.strip().startswith('#'):
                continue
            if line.startswith('  ') or line.startswith('\t'):
                stripped = line.strip()
                if stripped.startswith('- '):
                    if section:
                        cfg.setdefault(section, []).append(stripped[2:].strip())
                elif ':' in stripped and section:
                    k, _, v = stripped.partition(':')
                    cfg.setdefault(section, {})[k.strip()] = v.strip()
            else:
                section = None
                if ':' not in line:
                    continue
                k, _, v = line.partition(':')
                k, v = k.strip(), v.strip()
                if k in ('params', 'vars', 'procs', 'hooks'):
                    section = k
                elif k == 'auto':
                    cfg['auto'] = v.lower() in ('true', '1', 'yes')
                elif k == 'ctx':
                    try:
                        cfg['ctx'] = int(v)
                    except ValueError:
                        pass
                elif v:
                    cfg[k] = v
        return cfg

    @staticmethod
    def _write_config_yml(cfg: dict) -> str:
        lines = []
        if 'auto' in cfg:
            lines.append(f"auto: {'true' if cfg['auto'] else 'false'}")
        for k in ('host', 'model'):
            if cfg.get(k):
                lines.append(f"{k}: {cfg[k]}")
        if cfg.get('ctx'):
            lines.append(f"ctx: {cfg['ctx']}")
        for section in ('params', 'vars', 'hooks'):
            d = cfg.get(section)
            if d:
                lines.append(f"{section}:")
                for sk, sv in d.items():
                    if sv:
                        lines.append(f"  {sk}: {sv}")
        procs = cfg.get('procs')
        if procs:
            lines.append("procs:")
            for p in procs:
                lines.append(f"  - {p}")
        if cfg.get('active_project'):
            lines.append(f"active_project: {cfg['active_project']}")
        return '\n'.join(lines) + '\n' if lines else ''

    def _load_config_file(self, path: str = "") -> dict:
        p = path or CONFIG_FILE
        if not os.path.isfile(p):
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                return self._parse_config_yml(f.read())
        except OSError:
            return {}

    def _apply_config(self, cfg: dict):
        if cfg.get("host"):
            self._route(f"/host {cfg['host']}")
        if cfg.get("model"):
            self._route(f"/model {cfg['model']}")
        if cfg.get("ctx"):
            self._route(f"/ctx {cfg['ctx']}")
        for k, v in cfg.get("params", {}).items():
            self._route(f"/param {k} {v}")
        self._vars.update({k: v for k, v in cfg.get("vars", {}).items()})
        for p in cfg.get("procs", []):
            if p not in self._proc_active:
                self._proc_active.append(p)
        self._hooks.update(cfg.get("hooks", {}))
        if cfg.get("active_project"):
            self._proj_key = cfg["active_project"]
            _info(f"[proj] {self._proj_key}")

    def _cmd_config(self, user_input: str):
        """Project-level session config saved in .1bcoder/config.yml.

        /config save [file]            Save current state (host, model, ctx, params, vars, procs).
        /config load [file]            Restore state from config file.
        /config show [file]            Print config file contents.
        /config auto on|off            Enable/disable auto-load at startup.
        /config del model|host|ctx     Remove top-level key from config.
        /config del var <name>         Remove specific variable.
        /config del vars               Remove entire vars section.
        /config del param <name>       Remove specific param.
        /config del params             Remove entire params section.
        /config del proc <name>        Remove specific proc.
        /config del procs              Remove entire procs section.
        """
        parts = user_input.split(None, 3)
        sub  = parts[1] if len(parts) > 1 else ""
        arg2 = parts[2] if len(parts) > 2 else ""
        arg3 = parts[3] if len(parts) > 3 else ""

        if sub == "save":
            _SAVE_FIELDS = {"host", "model", "ctx", "params", "vars", "procs", "hooks"}
            if arg2 == "global":
                cfg_path = GLOBAL_CONFIG_FILE
                field = arg3 if arg3 in _SAVE_FIELDS else None
            elif arg2 in _SAVE_FIELDS:
                cfg_path = CONFIG_FILE
                field = arg2
            elif arg2:
                cfg_path = arg2
                field = None
            else:
                cfg_path = CONFIG_FILE
                field = None
            # load existing to merge into (preserve what's already there)
            cfg = self._load_config_file(cfg_path)
            host_str = (self.base_url.replace("http://", "openai://")
                        if self.provider == "openai"
                        else self.base_url.replace("http://", "ollama://"))
            if field is None or field == "host":
                cfg["host"] = host_str
            if field is None or field == "model":
                cfg["model"] = self.model
            if field is None or field == "ctx":
                cfg["ctx"] = self.num_ctx
            if field is None or field == "params":
                if self.params:
                    cfg["params"] = dict(self.params)
            if field is None or field == "vars":
                vars_to_save = {k: v for k, v in self._vars.items() if v}
                if vars_to_save:
                    cfg["vars"] = vars_to_save
            if field is None or field == "procs":
                if self._proc_active:
                    cfg["procs"] = list(self._proc_active)
            if field is None or field == "hooks":
                if self._hooks:
                    cfg["hooks"] = dict(self._hooks)
            if field is None and self._proj_key:
                cfg["active_project"] = self._proj_key
            try:
                os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(self._write_config_yml(cfg))
                saved = field or "all"
                _ok(f"[config] saved {saved} → {cfg_path}")
            except OSError as e:
                print(f"[config] cannot write: {e}")

        elif sub == "load":
            cfg_path = GLOBAL_CONFIG_FILE if arg2 == "global" else (arg2 if arg2 else CONFIG_FILE)
            cfg = self._load_config_file(cfg_path)
            if not cfg:
                print(f"[config] not found or empty: {cfg_path}")
                return
            self._apply_config(cfg)
            _ok(f"[config] loaded ← {cfg_path}")

        elif sub == "show":
            cfg_path = GLOBAL_CONFIG_FILE if arg2 == "global" else (arg2 if arg2 else CONFIG_FILE)
            if not os.path.isfile(cfg_path):
                print(f"[config] no config file: {cfg_path}")
                return
            with open(cfg_path, encoding="utf-8") as f:
                print(f.read())

        elif sub == "auto":
            use_global = arg3 == "global" or arg2 == "global"
            onoff = arg3 if arg2 == "global" else arg2
            if onoff not in ("on", "off"):
                print("usage: /config auto on|off [global]")
                return
            cfg_path = GLOBAL_CONFIG_FILE if use_global else CONFIG_FILE
            cfg = self._load_config_file(cfg_path)
            cfg["auto"] = (onoff == "on")
            try:
                os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(self._write_config_yml(cfg))
                _ok(f"[config] auto {'on' if cfg['auto'] else 'off'} → {cfg_path}")
            except OSError as e:
                print(f"[config] cannot write: {e}")

        elif sub == "del":
            target = arg2.lower()
            if not target:
                print("usage: /config del model|host|ctx|var <n>|vars|param <n>|params|proc <n>|procs")
                return
            cfg = self._load_config_file()
            if not cfg:
                print("[config] no config file to modify")
                return
            if target in ("model", "host", "ctx"):
                cfg.pop(target, None)
                _ok(f"[config] removed: {target}")
            elif target == "vars":
                cfg.pop("vars", None)
                _ok("[config] removed: vars section")
            elif target == "var":
                if not arg3:
                    print("usage: /config del var <name>")
                    return
                cfg.get("vars", {}).pop(arg3, None)
                _ok(f"[config] removed var: {arg3}")
            elif target == "params":
                cfg.pop("params", None)
                _ok("[config] removed: params section")
            elif target == "param":
                if not arg3:
                    print("usage: /config del param <name>")
                    return
                cfg.get("params", {}).pop(arg3, None)
                _ok(f"[config] removed param: {arg3}")
            elif target == "procs":
                cfg.pop("procs", None)
                _ok("[config] removed: procs section")
            elif target == "proc":
                if not arg3:
                    print("usage: /config del proc <name>")
                    return
                cfg["procs"] = [p for p in cfg.get("procs", []) if not p.startswith(arg3)]
                _ok(f"[config] removed proc: {arg3}")
            else:
                print(f"[config] unknown target: {target}")
                return
            try:
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    f.write(self._write_config_yml(cfg))
            except OSError as e:
                print(f"[config] cannot write: {e}")

        else:
            print("usage: /config save [file] | load [file] | show [file] | auto on|off | del <target> [name]")

    # ── /proj helpers ────────────────────────────────────────────────────────────

    def _proj_read_project_txt(self, proj_file: str) -> dict:
        """Parse project.txt → {description, keywords: list, files: list}."""
        data = {"description": "", "keywords": [], "files": []}
        if not os.path.isfile(proj_file):
            return data
        try:
            with open(proj_file, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return data
        section = None
        for line in lines:
            stripped = line.rstrip("\n")
            if stripped.startswith("Description:"):
                data["description"] = stripped[len("Description:"):].strip()
                section = None
            elif stripped.startswith("Keywords:"):
                kw_str = stripped[len("Keywords:"):].strip()
                data["keywords"] = [k.strip() for k in kw_str.split(",") if k.strip()]
                section = None
            elif stripped.startswith("Files:"):
                section = "files"
            elif section == "files" and stripped.strip():
                data["files"].append(stripped.strip())
        return data

    def _proj_write_project_txt(self, proj_file: str, data: dict):
        """Write project.txt from data dict."""
        lines = [
            f"Description: {data.get('description', '')}",
            f"Keywords: {', '.join(data.get('keywords', []))}",
            "Files:",
        ]
        for f in data.get("files", []):
            lines.append(f)
        try:
            with open(proj_file, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as e:
            _err(f"cannot write project.txt: {e}")

    def _proj_update_keywords(self, new_kws: list):
        proj_file = os.path.join(PROJECTS_DIR, self._proj_key, "project.txt")
        data = self._proj_read_project_txt(proj_file)
        existing = set(data["keywords"])
        added = [k for k in new_kws if k not in existing]
        data["keywords"].extend(added)
        self._proj_write_project_txt(proj_file, data)

    def _proj_update_files(self, new_files: list):
        proj_file = os.path.join(PROJECTS_DIR, self._proj_key, "project.txt")
        data = self._proj_read_project_txt(proj_file)
        existing = set(data["files"])
        added = [f for f in new_files if f not in existing]
        data["files"].extend(added)
        self._proj_write_project_txt(proj_file, data)

    def _proj_index(self):
        """Extract file paths from command args in ctx files → add to project.txt."""
        proj_dir = os.path.join(PROJECTS_DIR, self._proj_key)
        CMD_FILE_RE = re.compile(
            r'^>?\s*(?:/read|/readln|/edit|/patch|/save|/insert)\s+([\w./\\-]+\.\w{1,6})',
            re.MULTILINE
        )
        found = set()
        try:
            ctx_files = [f for f in os.listdir(proj_dir)
                         if f != "project.txt" and os.path.isfile(os.path.join(proj_dir, f))]
        except OSError as e:
            _err(f"cannot read project dir: {e}")
            return
        for fname in ctx_files:
            fpath = os.path.join(proj_dir, fname)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                for m in CMD_FILE_RE.finditer(text):
                    found.add(m.group(1))
            except OSError:
                pass
        if not found:
            print("[proj index] no file references found in ctx files")
            return
        self._proj_update_files(sorted(found))
        _ok(f"[proj index] extracted {len(found)} file(s) → project.txt")

    def _proj_find(self, term: str, mode: str):
        """Search across all projects in .1bcoder/projects/."""
        if not os.path.isdir(PROJECTS_DIR):
            print("[proj] no projects directory")
            return
        try:
            proj_dirs = [e for e in os.scandir(PROJECTS_DIR) if e.is_dir()]
        except OSError as e:
            _err(f"cannot read projects: {e}")
            return

        term_lower = term.lower()
        found_any = False
        self._last_find_results = []  # reset for /ctx compose add N

        for proj_entry in sorted(proj_dirs, key=lambda e: e.name):
            proj_name = proj_entry.name
            proj_file = os.path.join(proj_entry.path, "project.txt")

            # --- search project.txt ---
            matches_proj = []
            data = self._proj_read_project_txt(proj_file)
            if term_lower in data["description"].lower():
                matches_proj.append(f"description: {data['description']}")
            kw_hits = [k for k in data["keywords"] if term_lower in k.lower()]
            if kw_hits:
                matches_proj.append(f"keywords: {', '.join(kw_hits)}")
            file_hits = [f for f in data["files"] if term_lower in f.lower()]
            if file_hits:
                matches_proj.append(f"files: {', '.join(file_hits)}")

            # --- search ctx filenames ---
            ctx_name_hits = []
            try:
                ctx_files = [f for f in os.listdir(proj_entry.path)
                             if f != "project.txt" and os.path.isfile(os.path.join(proj_entry.path, f))]
            except OSError:
                ctx_files = []
            for fname in ctx_files:
                if term_lower in fname.lower():
                    ctx_name_hits.append(fname)

            def _register(full_path):
                n = len(self._last_find_results) + 1
                self._last_find_results.append(full_path)
                return n

            if mode == "f":
                # fast mode: project.txt + filenames only
                if matches_proj or ctx_name_hits:
                    found_any = True
                    parts_out = matches_proj[:]
                    if ctx_name_hits:
                        nums = [str(_register(os.path.join(proj_entry.path, f))) for f in ctx_name_hits]
                        parts_out.append(f"ctx: {', '.join(f'[{n}]{f}' for n, f in zip(nums, ctx_name_hits))}")
                    print(f"  {proj_name} — {' | '.join(parts_out)}")
            else:
                # content mode: also grep inside ctx files
                content_hits = []
                for fname in ctx_files:
                    fpath = os.path.join(proj_entry.path, fname)
                    try:
                        with open(fpath, encoding="utf-8", errors="replace") as f:
                            for lineno, line in enumerate(f, 1):
                                if term_lower in line.lower():
                                    content_hits.append((fname, lineno, line.rstrip()))
                    except OSError:
                        pass

                if matches_proj or ctx_name_hits or content_hits:
                    found_any = True
                    if matches_proj or ctx_name_hits:
                        parts_out = matches_proj[:]
                        if ctx_name_hits:
                            nums = [str(_register(os.path.join(proj_entry.path, f))) for f in ctx_name_hits]
                            parts_out.append(f"ctx: {', '.join(f'[{n}]{f}' for n, f in zip(nums, ctx_name_hits))}")
                        print(f"  {proj_name} — {' | '.join(parts_out)}")
                    # register ctx files hit by content too (if not already registered)
                    hit_fnames = list(dict.fromkeys(f for f, _, _ in content_hits))
                    for fname in hit_fnames:
                        fpath = os.path.join(proj_entry.path, fname)
                        if fpath not in self._last_find_results:
                            _register(fpath)
                    if content_hits:
                        if not (matches_proj or ctx_name_hits):
                            print(f"  {proj_name}")
                        last_fname = None
                        for fname, lineno, line in content_hits[:20]:
                            if fname != last_fname:
                                n = next((i+1 for i, p in enumerate(self._last_find_results)
                                          if os.path.basename(p) == fname), "?")
                                print(f"    [{n}] {fname}")
                                last_fname = fname
                            print(f"    {lineno:>5}: {line[:120]}")

        # also search .1bcoder/ctx/ filenames and content
        if os.path.isdir(CTX_DIR):
            try:
                ctx_files = [f for f in os.listdir(CTX_DIR)
                             if os.path.isfile(os.path.join(CTX_DIR, f))]
            except OSError:
                ctx_files = []
            name_hits = [f for f in ctx_files if term_lower in f.lower()]
            content_hits_ctx = []
            if mode == "c":
                for fname in ctx_files:
                    fpath = os.path.join(CTX_DIR, fname)
                    try:
                        with open(fpath, encoding="utf-8", errors="replace") as f:
                            for lineno, line in enumerate(f, 1):
                                if term_lower in line.lower():
                                    content_hits_ctx.append((fname, lineno, line.rstrip()))
                    except OSError:
                        pass
            if name_hits or content_hits_ctx:
                found_any = True
                if name_hits:
                    numbered = [f"[{len(self._last_find_results)+i+1}]{f}" for i, f in enumerate(name_hits)]
                    for f in name_hits:
                        self._last_find_results.append(os.path.join(CTX_DIR, f))
                    print(f"  [ctx/] ctx: {', '.join(numbered)}")
                if content_hits_ctx:
                    last_fname = None
                    for fname, lineno, line in content_hits_ctx[:20]:
                        if fname != last_fname:
                            fpath = os.path.join(CTX_DIR, fname)
                            if fpath not in self._last_find_results:
                                self._last_find_results.append(fpath)
                            n = next((i+1 for i, p in enumerate(self._last_find_results)
                                      if p == fpath), "?")
                            print(f"    [{n}] ctx/{fname}")
                            last_fname = fname
                        print(f"    {lineno:>5}: {line[:120]}")

        if self._last_find_results:
            print(f"\n  use /ctx compose add <N,M|all> to add to compose queue")
        if not found_any:
            print(f"[proj find] no results for '{term}'")

    def _cmd_proj(self, user_input: str):
        """Project context management — local .1bcoder/projects/<key>/

        /proj set <key>              Set active project (create if needed)
        /proj status                 Show current project + project.txt
        /proj list                   List all projects (newest first)
        /proj save <file>            Save current ctx to project folder
        /proj show                   List ctx files in current project
        /proj find <term> [-f|-c]    Search projects: -f fast (default), -c with content
        /proj keyword add <k1,k2>    Add keywords to project.txt
        /proj file add <f1,f2>       Add files to project.txt
        /proj index                  Extract file refs from ctx files → project.txt
        """
        parts = user_input.split(None, 2)
        sub  = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""

        if sub == "set":
            key = rest.strip()
            if not key:
                print("usage: /proj set <key>")
                return
            proj_dir = os.path.join(PROJECTS_DIR, key)
            try:
                os.makedirs(proj_dir, exist_ok=True)
            except OSError as e:
                _err(f"cannot create project folder: {e}")
                return
            proj_file = os.path.join(proj_dir, "project.txt")
            if not os.path.isfile(proj_file):
                try:
                    with open(proj_file, "w", encoding="utf-8") as f:
                        f.write("Description: \nKeywords: \nFiles:\n")
                except OSError as e:
                    _err(f"cannot create project.txt: {e}")
                    return
            self._proj_key = key
            _ok(f"[proj] {key}")

        elif sub == "status":
            if not self._proj_key:
                print("[proj] no active project — use /proj set <key>")
                return
            proj_dir = os.path.join(PROJECTS_DIR, self._proj_key)
            proj_file = os.path.join(proj_dir, "project.txt")
            print(f"[proj] {self._proj_key}  ({proj_dir})")
            if os.path.isfile(proj_file):
                with open(proj_file, encoding="utf-8") as f:
                    print(f.read())
            else:
                print("  (no project.txt)")

        elif sub == "list":
            if not os.path.isdir(PROJECTS_DIR):
                print("[proj] no projects — use /proj set <key>")
                return
            try:
                entries = [e for e in os.scandir(PROJECTS_DIR) if e.is_dir()]
            except OSError as e:
                _err(f"cannot read projects dir: {e}")
                return
            if not entries:
                print("[proj] no projects yet")
                return
            entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
            for e in entries:
                marker = " *" if e.name == self._proj_key else ""
                proj_file = os.path.join(e.path, "project.txt")
                desc = ""
                if os.path.isfile(proj_file):
                    try:
                        with open(proj_file, encoding="utf-8") as f:
                            for line in f:
                                if line.startswith("Description:"):
                                    desc = line[len("Description:"):].strip()
                                    break
                    except OSError:
                        pass
                desc_str = f"  — {desc}" if desc else ""
                print(f"  {e.name}{marker}{desc_str}")

        elif sub == "save":
            if not self._proj_key:
                _err("no active project — use /proj set <key> first")
                return
            fname = rest.strip()
            if not fname:
                print("usage: /proj save <filename>")
                return
            if not self.messages:
                print("[proj] context is empty — nothing to save")
                return
            proj_dir = os.path.join(PROJECTS_DIR, self._proj_key)
            try:
                os.makedirs(proj_dir, exist_ok=True)
            except OSError as e:
                _err(f"cannot create project folder: {e}")
                return
            dest = os.path.join(proj_dir, fname)
            try:
                with open(dest, "w", encoding="utf-8") as f:
                    for msg in self.messages:
                        f.write(f"=== {msg['role']} ===\n{msg['content']}\n\n")
                _ok(f"[proj] saved {len(self.messages)} messages → {dest}")
            except OSError as e:
                _err(f"cannot save: {e}")

        elif sub == "show":
            if not self._proj_key:
                print("[proj] no active project — use /proj set <key>")
                return
            proj_dir = os.path.join(PROJECTS_DIR, self._proj_key)
            if not os.path.isdir(proj_dir):
                print(f"[proj] project folder not found: {proj_dir}")
                return
            try:
                files = sorted(
                    [f for f in os.listdir(proj_dir)
                     if f != "project.txt" and os.path.isfile(os.path.join(proj_dir, f))],
                    key=lambda f: os.path.getmtime(os.path.join(proj_dir, f)),
                    reverse=True
                )
            except OSError as e:
                _err(f"cannot list project: {e}")
                return
            if not files:
                print(f"[proj] {self._proj_key} — no ctx files yet")
                return
            print(f"[proj] {self._proj_key}  ({len(files)} ctx file(s)):")
            for fname in files:
                size = os.path.getsize(os.path.join(proj_dir, fname))
                print(f"  {fname}  ({size:,} bytes)")
            print(f"\n  Load: /ctx load {proj_dir}/<filename>")

        elif sub == "find":
            find_parts = rest.split()
            if not find_parts:
                print("usage: /proj find <term> [-f|-c]")
                return
            mode = "f"
            terms = []
            for p in find_parts:
                if p in ("-f", "-c"):
                    mode = p[1:]
                else:
                    terms.append(p.lower())
            if not terms:
                print("usage: /proj find <term> [-f|-c]")
                return
            self._proj_find(" ".join(terms), mode)

        elif sub == "keyword":
            if not self._proj_key:
                _err("no active project — use /proj set <key> first")
                return
            kw_parts = rest.split(None, 1)
            if len(kw_parts) < 2 or kw_parts[0] != "add":
                print("usage: /proj keyword add <k1>, <k2>, ...")
                return
            new_kws = [k.strip() for k in kw_parts[1].split(",") if k.strip()]
            self._proj_update_keywords(new_kws)
            _ok(f"[proj] keywords added: {', '.join(new_kws)}")

        elif sub == "file":
            if not self._proj_key:
                _err("no active project — use /proj set <key> first")
                return
            f_parts = rest.split(None, 1)
            if len(f_parts) < 2 or f_parts[0] != "add":
                print("usage: /proj file add <f1>, <f2>, ...")
                return
            new_files = [f.strip() for f in f_parts[1].split(",") if f.strip()]
            self._proj_update_files(new_files)
            _ok(f"[proj] files added: {', '.join(new_files)}")

        elif sub == "index":
            if not self._proj_key:
                _err("no active project — use /proj set <key> first")
                return
            self._proj_index()

        elif sub == "load":
            if not self._proj_key:
                _err("no active project — use /proj set <key> first")
                return
            fname = rest.strip()
            if not fname:
                print("usage: /proj load <file>")
                return
            proj_dir = os.path.join(PROJECTS_DIR, self._proj_key)
            load_path = os.path.join(proj_dir, fname)
            if not os.path.isfile(load_path):
                # fuzzy-match against files in the project directory
                try:
                    candidates = [f for f in os.listdir(proj_dir)
                                  if f != "project.txt" and os.path.isfile(os.path.join(proj_dir, f))]
                except OSError:
                    candidates = []
                fixed = _fuzzy_fix(fname, candidates, cutoff=0.65)
                if fixed:
                    _warn(f"[proj] '{fname}' → '{fixed}'")
                    load_path = os.path.join(proj_dir, fixed)
                else:
                    _err(f"file not found in project {self._proj_key}: {fname}")
                    return
            self._route(f"/ctx load {load_path}")

        else:
            print("usage: /proj set <key> | status | list | save <file> | load <file> | show")
            print("       /proj find <term> [-f|-c] | keyword add <k1,k2> | file add <f1,f2> | index")

    def _cmd_doc(self, user_input: str):
        """List or display documentation articles from the doc/ folder."""
        LOCAL_DOC_DIR  = os.path.join(BCODER_DIR, "doc")
        GLOBAL_DOC_DIR = os.path.join(HOME_BCODER_DIR, "doc")
        DOC_DIRS = [d for d in (LOCAL_DOC_DIR, GLOBAL_DOC_DIR) if os.path.isdir(d)]
        tokens = user_input.split(None, 2)
        sub = tokens[1].lower() if len(tokens) >= 2 else "list"

        if sub == "list" or sub == "ls":
            if not DOC_DIRS:
                _err("doc/ folder not found")
                return
            seen = set()
            files = []
            for d in DOC_DIRS:
                for f in os.listdir(d):
                    if f.lower().endswith(".md") and f.lower() not in seen:
                        seen.add(f.lower())
                        files.append(f)
            files.sort()
            if not files:
                print("  (no articles in doc/)")
                return
            print("  Available articles (use /doc <name> to read):")
            for f in files:
                print(f"  {f[:-3]}")
            return

        # direct path: /doc path/to/file.md
        if os.path.isfile(sub) or (sub.endswith(".md") and os.path.isfile(sub)):
            path = sub
        elif os.path.sep in sub or "/" in sub:
            _err(f"file not found: {sub}")
            return
        else:
            # find article — local overrides global, case-insensitive, .md optional
            name = sub if sub.endswith(".md") else sub + ".md"
            path = None
            for d in DOC_DIRS:
                for f in os.listdir(d):
                    if f.lower() == name.lower():
                        path = os.path.join(d, f)
                        break
                if path:
                    break
            if path is None:
                _err(f"doc not found: {sub}  (try /doc list)")
                return

        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            _err(f"cannot read {path}: {e}")
            return

        qualifier = tokens[2].lower() if len(tokens) >= 3 else ""
        if qualifier == "ctx":
            self.messages.append({"role": "user", "content": f"[doc/{sub.upper()}.md]\n{text}"})
            _ok(f"[doc] {sub.upper()}.md added to context ({len(text):,} chars)")
        elif qualifier == "raw":
            print(text)
        else:
            from rich.console import Console
            from rich.markdown import Markdown
            Console().print(Markdown(text))

    def _cmd_team(self, user_input: str):
        import shlex, concurrent.futures

        parts = user_input.split(None, 1)
        rest  = parts[1].strip() if len(parts) > 1 else ""

        # parse subcommand and flags from rest
        tokens = shlex.split(rest) if rest else []
        sub    = tokens[0] if tokens else ""
        args   = tokens[1:] if len(tokens) > 1 else []

        # ── list ────────────────────────────────────────────────────────────────
        if sub == "list":
            all_files = {}
            for d, tag in [(LOCAL_TEAMS_DIR, ""), (TEAMS_DIR, "g:")]:
                if os.path.isdir(d):
                    for f in sorted(os.listdir(d)):
                        if f.endswith(".yaml") and f not in all_files:
                            all_files[f] = (os.path.join(d, f), tag)
            if not all_files:
                print("[team] no teams found — use /team new <name> to create one")
                return
            for f, (fpath, tag) in sorted(all_files.items()):
                workers = self._parse_team_file(fpath)
                print(f"  {tag}{f[:-5]}  ({len(workers)} worker(s))")

        # ── show ────────────────────────────────────────────────────────────────
        elif sub == "show":
            name = args[0] if args else ""
            if not name:
                print("usage: /team show <name>")
                return
            path = self._find_team_path(name)
            if not path:
                print(f"[team] not found: {name}")
                return
            workers = self._parse_team_file(path)
            if not workers:
                print("[team] no workers defined")
                return
            for i, w in enumerate(workers, 1):
                deps = f"  depends_on: {', '.join(w['depends_on'])}" if w.get('depends_on') else ""
                print(f"  {i}. [{w.get('name',i)}] host={w.get('host','')}  model={w.get('model','')}  script={w.get('script','')}{deps}")

        # ── new ─────────────────────────────────────────────────────────────────
        elif sub == "new":
            name = args[0] if args else self._prompt_input("  team name:")
            if not name:
                print("[cancelled]")
                return
            if not name.endswith(".yaml"):
                name += ".yaml"
            os.makedirs(LOCAL_TEAMS_DIR, exist_ok=True)
            path = os.path.join(LOCAL_TEAMS_DIR, name)
            if os.path.exists(path):
                print(f"[team] already exists: {path}")
                return
            template = (
                "workers:\n"
                "  - name: worker1\n"
                "    host: localhost:11434\n"
                "    model: qwen2.5-coder:1.5b\n"
                "    script: worker1.txt\n"
                "  - name: worker2\n"
                "    host: localhost:11435\n"
                "    model: qwen2.5-coder:1.5b\n"
                "    script: worker2.txt\n"
                "    depends_on: worker1\n"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(template)
            print(f"[team] created: {path}")

        # ── run ─────────────────────────────────────────────────────────────────
        elif sub == "run":
            name = args[0] if args else ""
            if not name:
                print("usage: /team run <name> [--param k=v ...]")
                return
            path = self._find_team_path(name)
            if not path:
                print(f"[team] not found: {name}")
                return
            workers = self._parse_team_file(path)
            if not workers:
                print("[team] no workers defined in team file")
                return

            # collect --param flags from remaining args
            param_args = []
            i = 1
            while i < len(args):
                if args[i] == "--param" and i + 1 < len(args):
                    param_args += ["--param", args[i + 1]]
                    i += 2
                else:
                    i += 1

            chat_py = os.path.abspath(__file__)
            missing = []
            for w in workers:
                for field in ("host", "model", "script"):
                    if not w.get(field):
                        missing.append(f"worker missing '{field}': {w}")
            if missing:
                for m in missing:
                    print(f"[team] {m}")
                return

            log_dir = os.path.join(BCODER_DIR, "team-logs")
            os.makedirs(log_dir, exist_ok=True)
            chat_py = os.path.abspath(__file__)

            def _resolve_script(script):
                if os.path.isabs(script):
                    return script
                for d in (SCRIPTS_DIR, GLOBAL_SCRIPTS_DIR):
                    p = os.path.join(d, script)
                    if os.path.isfile(p):
                        return p
                return os.path.join(SCRIPTS_DIR, script)

            # dependency-aware scheduler
            import time as _time
            total     = len(workers)
            done      = {}   # name → returncode
            running   = {}   # name → (proc, log_f, log_path, worker)
            pending   = {w["name"]: w for w in workers}
            has_deps  = any(w["depends_on"] for w in workers)

            if has_deps:
                print(f"[team] starting {total} worker(s) with dependency ordering...")
            else:
                print(f"[team] starting {total} worker(s)...")

            while pending or running:
                # start all workers whose deps are satisfied
                for wname in list(pending):
                    w = pending[wname]
                    if all(d in done for d in w["depends_on"]):
                        plan_path = _resolve_script(w["script"])
                        cmd = [
                            sys.executable, chat_py,
                            "--host",        w["host"],
                            "--model",       w["model"],
                            "--scriptapply", plan_path,
                        ] + param_args
                        log_path = os.path.join(log_dir, f"{name}-{wname}.log")
                        log_f = open(log_path, "w", encoding="utf-8")
                        env = os.environ.copy()
                        env["PYTHONIOENCODING"] = "utf-8"
                        p = subprocess.Popen(cmd, stdout=log_f, stderr=log_f, env=env)
                        running[wname] = (p, log_f, log_path, w)
                        del pending[wname]
                        dep_str = f"  (after: {', '.join(w['depends_on'])})" if w["depends_on"] else ""
                        print(f"  [{wname}] {w['model']}@{w['host']} started{dep_str}  log:{log_path}")

                # check for finished workers
                for wname in list(running):
                    p, log_f, log_path, w = running[wname]
                    if p.poll() is not None:
                        log_f.close()
                        done[wname] = p.returncode
                        del running[wname]
                        status = "done" if p.returncode == 0 else f"FAILED (exit {p.returncode})"
                        print(f"  [{wname}] {w['model']}@{w['host']} — {status}")

                if pending or running:
                    _time.sleep(0.5)

            failed = [n for n, rc in done.items() if rc != 0]
            if failed:
                print(f"[team] finished — {len(failed)} worker(s) failed: {failed}")
            else:
                print(f"[team] all {total} worker(s) finished successfully")

        else:
            print("usage: /team list | show <name> | new <name> | run <name> [--param k=v ...]")

    def _cmd_mcp(self, user_input: str):
        parts = user_input.split(None, 3)
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "connect":
            if len(parts) < 4:
                print("usage: /mcp connect <name> <command> [--cwd <dir>]")
                return
            name, cmd = parts[2], parts[3]
            # extract --cwd <dir> from the command string if present
            cwd = None
            import re as _re
            m = _re.search(r'--cwd\s+(\S+)', cmd)
            if m:
                cwd = m.group(1)
                cmd = cmd[:m.start()].rstrip() + cmd[m.end():]
            print(f"[mcp] connecting to {name}..." + (f" (cwd={cwd})" if cwd else ""))
            try:
                client = MCPClient(cmd, cwd=cwd)
                if name in self._mcp:
                    self._mcp[name].close()
                self._mcp[name] = client
                tools = client.list_tools()
                print(f"[mcp] {name}: connected — {len(tools)} tool(s)")
                for t in tools:
                    print(f"  {t['name']}: {t.get('description', '')[:60]}")
            except Exception as e:
                print(f"[mcp] connect failed: {e}")
        elif sub == "tools":
            if not self._mcp:
                print("[mcp] no servers connected")
                return
            name_filter = parts[2] if len(parts) > 2 else None
            for name, client in self._mcp.items():
                if name_filter and name != name_filter:
                    continue
                try:
                    tools = client.list_tools()
                    print(f"[mcp] {name}:")
                    for t in tools:
                        print(f"  {t['name']}: {t.get('description', '')[:60]}")
                except Exception as e:
                    print(f"[mcp] {name}: error: {e}")
        elif sub == "call":
            if len(parts) < 3:
                print("usage: /mcp call <server/tool> [json]")
                return
            target = parts[2]
            args_str = parts[3] if len(parts) > 3 else ""
            if "/" in target:
                server_name, tool_name = target.split("/", 1)
            elif len(self._mcp) == 1:
                server_name = next(iter(self._mcp))
                tool_name = target
            else:
                print("ambiguous: use /mcp call <server>/<tool>")
                return
            client = self._mcp.get(server_name)
            if not client:
                print(f"[mcp] unknown server '{server_name}'")
                return
            try:
                arguments = json.loads(args_str) if args_str.strip() else {}
            except json.JSONDecodeError as e:
                print(f"[mcp] bad JSON: {e}")
                return
            try:
                result = client.call_tool(tool_name, arguments)
                print(f"[mcp] {tool_name}:")
                print(result)
                if not self._auto_apply:
                    self.messages.append({"role": "user", "content": f"[mcp: {tool_name}]\n{result}"})
                print("[mcp] injected into context")
            except Exception as e:
                print(f"[mcp] call failed: {e}")
        elif sub == "disconnect":
            name = parts[2] if len(parts) > 2 else ""
            client = self._mcp.pop(name, None)
            if client:
                client.close()
                print(f"[mcp] disconnected {name}")
            else:
                print(f"[mcp] unknown server '{name}'")
        else:
            print("usage: /mcp connect <name> <cmd> [--cwd <dir>] | tools [name] | call <server/tool> {json} | disconnect <name>")

    def _cmd_parallel(self, user_input: str):
        import concurrent.futures

        # ── tokenizer: no shlex required ──────────────────────────────────────
        # Handles quoted strings, $/$~, commas as list separators, key: values.
        def _tok(raw: str) -> list[str]:
            tokens: list[str] = []
            buf = ""
            in_q = None
            for ch in raw:
                if in_q:
                    if ch == in_q:
                        in_q = None
                        tokens.append(buf)
                        buf = ""
                    else:
                        buf += ch
                elif ch in ('"', "'"):
                    if buf.strip():
                        tokens.append(buf.strip())
                    buf = ""
                    in_q = ch
                elif ch == ",":
                    if buf.strip():
                        tokens.append(buf.strip())
                    buf = ""
                    tokens.append(",")
                elif ch in (" ", "\t"):
                    if buf.strip():
                        tokens.append(buf.strip())
                    buf = ""
                else:
                    buf += ch
            if buf.strip():
                tokens.append(buf.strip())
            return tokens

        raw_args = user_input.split(None, 1)[1].strip() if " " in user_input else ""

        # ── profile subcommands — detect before main parse ─────────────────────
        _first = raw_args.split()
        _sub0  = _first[0].rstrip(":") if _first else ""
        _sub1  = _first[1] if len(_first) > 1 else ""
        if _sub0 == "profile" and _sub1 in ("list", "show", "create", "add"):
            words = raw_args.split()
            sub   = words[1]

            if sub == "list":
                profiles = _list_profiles()
                if not profiles:
                    print("[parallel] no profiles found")
                    return
                for pname, wlist, comment, source in profiles:
                    tag = f"  [{source}]" if source == "global" else ""
                    print(f"\n{pname}:{tag}" + (f"  # {comment}" if comment else ""))
                    for h, m, fn in wlist:
                        print(f"    {h}  |  {m}  →  {fn}")
                return

            if sub == "show":
                pname = words[2] if len(words) > 2 else ""
                if not pname:
                    print("usage: /parallel profile show <name>")
                    return
                for pf, source in ((PROFILES_FILE, "local"), (GLOBAL_PROFILES_FILE, "global")):
                    if not os.path.exists(pf):
                        continue
                    with open(pf, "r", encoding="utf-8") as f:
                        for line in f:
                            s = line.strip()
                            if s and not s.startswith("#"):
                                n, _, _ = s.partition(":")
                                if n.strip() == pname:
                                    print(f"[{source}] {s}")
                                    return
                print(f"[parallel] profile '{pname}' not found")
                return

            if sub == "create":
                name = words[2] if len(words) > 2 else ""
                if not name:
                    name = self._prompt_input("profile name:").strip()
                if not name:
                    return
                inline_specs = [t for t in words[3:] if "|" in t]
                if inline_specs:
                    wlist = []
                    for spec in inline_specs:
                        p = spec.split("|", 2)
                        if len(p) == 3:
                            wlist.append(tuple(p))
                        else:
                            _err(f"[parallel] bad worker spec '{spec}' — expected host|model|file")
                            return
                    if not wlist:
                        print("[parallel] no valid workers parsed")
                        return
                else:
                    wlist = []
                    print(f"[parallel] creating profile '{name}' — add workers (blank host to finish)")
                    while True:
                        host    = self._prompt_input("  host (e.g. localhost:11434):").strip()
                        if not host: break
                        model   = self._prompt_input("  model:").strip()
                        if not model: break
                        outfile = self._prompt_input("  output file (e.g. ans/model.txt):").strip()
                        if not outfile: break
                        wlist.append((host, model, outfile))
                        print(f"  added: {host}|{model}|{outfile}")
                    if not wlist:
                        print("[parallel] no workers added, profile not saved")
                        return
                comment  = self._prompt_input("  comment (optional):").strip()
                replaced = _save_profile(name, wlist, comment)
                print(f"[parallel] profile '{name}' {'updated' if replaced else 'saved'} "
                      f"({len(wlist)} worker(s)) → .1bcoder/profiles.txt")
                return

            if sub == "add":
                pname = words[2] if len(words) > 2 else ""
                if not pname:
                    print("usage: /parallel profile add <name>")
                    return
                existing    = _load_profile(pname) or []
                safe_model  = self.model.replace(":", "-").replace("/", "-")
                default_file = f"ans/{safe_model}.txt"
                outfile     = self._prompt_input(f"  output file [{default_file}]:").strip() or default_file
                existing.append((self.host, self.model, outfile))
                replaced = _save_profile(pname, existing)
                print(f"[parallel] added {self.host}|{self.model}|{outfile} to profile '{pname}' "
                      f"({'updated' if replaced else 'created'})")
                return

        # ── expand $ and ~ before tokenizing (no shell-quoting needed) ────────
        raw_args = raw_args.replace("$", self._last_output or "")
        raw_args = raw_args.replace("~", self._last_input  or "")

        # ── parse main args ────────────────────────────────────────────────────
        tokens       = _tok(raw_args)
        prompts: list[str] = []
        workers: list[tuple] = []
        ctx_mode     = "last"   # default: send last message only
        file_path    = None
        file_n       = None     # number of chunks (None = no file:)
        collect      = None     # 'compact'
        collect_prof = None
        sequential   = False

        i = 0
        prompt_buf: list[str] = []
        aspects:    list[str] = []   # items from list: — distributed one per worker
        in_list     = False          # True after list: keyword seen
        main_prompt = ""             # text before list:

        def _flush_buf():
            text = " ".join(prompt_buf).strip()
            prompt_buf.clear()
            if not text:
                return
            if in_list:
                aspects.append(text)
            else:
                prompts.append(text)

        def _next_val(default=""):
            nonlocal i
            i += 1
            return tokens[i] if i < len(tokens) else default

        while i < len(tokens):
            tok = tokens[i]

            # comma → flush current buffer as one item (prompt or aspect)
            if tok == ",":
                _flush_buf()
                i += 1
                continue

            # ctx: full|last|none  and  legacy --ctx / --last / --no-ctx
            _ctx_map = {
                "ctx:full": "full",  "ctx:last": "last",  "ctx:none": "none",
                "--ctx":    "full",  "--last":   "last",   "--no-ctx": "none",
            }
            if tok in _ctx_map:
                _flush_buf()
                ctx_mode = _ctx_map[tok]
                i += 1
                continue
            if tok == "ctx:":
                _flush_buf()
                ctx_mode = {"full": "full", "last": "last", "none": "none"}.get(_next_val(), "last")
                i += 1
                continue

            # --seq
            if tok == "--seq":
                sequential = True
                i += 1
                continue

            # profile: name  (also bare 'profile name' for backward compat)
            _pname = None
            if tok.startswith("profile:") and len(tok) > 8:
                _pname = tok[8:]
            elif tok in ("profile:", "profile"):
                _pname = _next_val()
            if _pname is not None:
                _flush_buf()
                loaded = _load_profile(_pname)
                if loaded is None:
                    print(f"[parallel] profile '{_pname}' not found")
                    return
                workers.extend(loaded)
                i += 1
                continue

            # file: path [-n N|auto]
            _fpath = None
            if tok.startswith("file:") and len(tok) > 5:
                _fpath = tok[5:]
            elif tok == "file:":
                _fpath = _next_val()
            if _fpath is not None:
                _flush_buf()
                file_path = _fpath
                # peek for -n N
                if i + 1 < len(tokens) and tokens[i + 1] == "-n":
                    i += 2
                    _nv = tokens[i] if i < len(tokens) else ""
                    file_n = _nv if _nv == "auto" else (int(_nv) if _nv.isdigit() else None)
                i += 1
                continue

            # collect: compact [profile: name]
            _cval = None
            if tok.startswith("collect:") and len(tok) > 8:
                _cval = tok[8:]
            elif tok == "collect:":
                _cval = _next_val()
            if _cval is not None:
                _flush_buf()
                collect = _cval
                # peek for profile: name
                if i + 1 < len(tokens) and tokens[i + 1].rstrip(":") == "profile":
                    i += 1 if tokens[i + 1] == "profile:" else 1
                    collect_prof = _next_val()
                i += 1
                continue

            # list: — everything after this (until next keyword) are aspects
            if tok in ("list:", "list"):
                _flush_buf()
                # flush prompts accumulated so far as the main_prompt
                if prompts:
                    main_prompt = " ".join(prompts)
                    prompts.clear()
                in_list = True
                i += 1
                continue

            # worker spec host|model|file
            if "|" in tok:
                _flush_buf()
                p = tok.split("|", 2)
                if len(p) == 3:
                    workers.append(tuple(p))
                else:
                    print(f"[parallel] bad worker spec (need host|model|file): {tok}")
                    return
                i += 1
                continue

            # everything else → prompt/aspect text
            prompt_buf.append(tok)
            i += 1

        _flush_buf()

        # if list: was used, build per-worker prompts: main_prompt + "\n\nAspect: aspect_i"
        if aspects:
            if not main_prompt and prompts:
                main_prompt = " ".join(prompts)
                prompts.clear()
            prompts = [
                f"{main_prompt}\n\nAspect: {a}" if main_prompt else a
                for a in aspects
            ]

        if not workers:
            print("usage: /parallel [list: p1, p2]  profile: name  [ctx: last|full|none]")
            print("                 [file: path [-n N]]  [collect: compact [profile: name]]")
            return

        # ── file: chunking ─────────────────────────────────────────────────────
        if file_path:
            try:
                content = open(file_path, encoding="utf-8", errors="ignore").read()
            except OSError as e:
                print(f"[parallel] cannot read file '{file_path}': {e}")
                return
            # split by === separator if present, else by equal line chunks
            if re.search(r"^===\s*$", content, re.MULTILINE):
                chunks = [c.strip() for c in re.split(r"^===\s*$", content, flags=re.MULTILINE) if c.strip()]
            else:
                n_chunks = len(workers) if file_n == "auto" or file_n is None else file_n
                lines = content.splitlines()
                size  = max(1, len(lines) // n_chunks)
                chunks = ["\n".join(lines[k:k + size]) for k in range(0, len(lines), size)]
                if len(chunks) > n_chunks:          # merge last overflow chunk
                    chunks[-2] = chunks[-2] + "\n" + chunks[-1]
                    chunks = chunks[:-1]
            prompts = chunks[:len(workers)]         # one chunk per worker

        # ── build base context ─────────────────────────────────────────────────
        if ctx_mode == "none":
            base_messages = []
        elif ctx_mode == "last":
            base_messages = self.messages[-1:] if self.messages else []
        else:
            base_messages = list(self.messages)

        if not prompts and not base_messages:
            print("[parallel] no prompt and no context — nothing to send")
            return

        # ── if collect: compact, set savepoint before workers run ─────────────
        if collect == "compact":
            self._savepoint = len(self.messages)

        def get_prompt(idx: int):
            if not prompts:
                return None
            return prompts[idx] if idx < len(prompts) else prompts[-1]

        print(f"[parallel] {len(workers)} worker(s), ctx:{ctx_mode}"
              + (f", file:{os.path.basename(file_path)}" if file_path else "")
              + (f", {'seq' if sequential else 'parallel'}")
              + "...")

        def call_one(idx, host, model, filename):
            prompt = get_prompt(idx)
            msgs   = base_messages + ([{"role": "user", "content": prompt}] if prompt else [])
            url, prov = parse_host(host)
            try:
                if prov == "openai":
                    resp = requests.post(f"{url}/v1/chat/completions",
                                         json={"model": model, "messages": msgs, "stream": False},
                                         timeout=300)
                    resp.raise_for_status()
                    reply = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
                else:
                    resp = requests.post(f"{url}/api/chat",
                                         json={"model": model, "messages": msgs, "stream": False,
                                               "options": {"num_ctx": self.num_ctx}},
                                         timeout=300)
                    resp.raise_for_status()
                    reply = resp.json().get("message", {}).get("content", "")
            except Exception as e:
                return host, model, filename, None, str(e)
            if filename != "ctx":
                dirpart = os.path.dirname(filename)
                if dirpart:
                    os.makedirs(dirpart, exist_ok=True)
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(reply)
            return host, model, filename, reply, None

        # ── execute workers ────────────────────────────────────────────────────
        ctx_replies: list[tuple] = []
        if sequential:
            results = [call_one(idx, h, m, f) for idx, (h, m, f) in enumerate(workers)]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(workers)) as pool:
                futures = {pool.submit(call_one, idx, h, m, f): (h, m, f)
                           for idx, (h, m, f) in enumerate(workers)}
                results = [future.result() for future in concurrent.futures.as_completed(futures)]

        for host, model, filename, reply, err in results:
            if err:
                print(f"[parallel] {model}@{host} — error: {err}")
            elif filename == "ctx":
                ctx_replies.append((model, reply))
                print(f"[parallel] {model}@{host} → context ({len(reply)} chars)")
            else:
                print(f"[parallel] {model}@{host} → {filename} ({len(reply)} chars)")

        # inject ctx replies into main context
        for model, reply in ctx_replies:
            self.messages.append({"role": "assistant", "content": f"[{model}]\n{reply}"})
            label = f" [{model}] "
            line  = f"{_CYAN}{'─' * 4}{label}{'─' * max(0, 60 - len(label))}{_R}"
            print(f"\n{line}\n{reply}")

        has_files = any(f != "ctx" for _, _, f in workers)
        if has_files:
            print("[parallel] done — use /read <file> to load answers into context")
        else:
            print("[parallel] done")

        # ── collect: compact — run after workers ───────────────────────────────
        if collect == "compact":
            cp_cmd = "/ctx compact savepoint"
            if collect_prof:
                cp_cmd += f" profile: {collect_prof}"
            self._route(cp_cmd, auto=True)

    def _cmd_about(self):
        print(f"""
{_BOLD}1bcoder{_R} — AI coding assistant for resource-constrained environments
{_DIM}Offline-first tool for 1B–7B local language models{_R}

{_CYAN}(c) 2026 Stanislav Zholobetskyi{_R}
{_DIM}https://github.com/szholobetsky/1bcoder{_R}

{_DIM}PhD research: «Intelligent Technology for Software Development and Maintenance Support»{_R}
{_DIM}Institute for Information Recording, National Academy of Sciences of Ukraine, Kyiv{_R}
""")

    def _cmd_help(self, user_input: str):
        parts = user_input.split(None, 2)

        # /help → full help text
        if len(parts) == 1:
            print(HELP_TEXT)
            return

        cmd = parts[1].lstrip('/')
        ctx = len(parts) > 2 and parts[2].strip() == "ctx"

        # /help all ctx — inject full help into context
        if cmd == "all":
            if ctx:
                self.messages.append({"role": "user", "content": f"[help: all commands]\n{HELP_TEXT}"})
                print(f"[help] full help injected into context ({len(HELP_TEXT):,} chars)")
            else:
                print(HELP_TEXT)
            return

        # find paragraphs whose first line starts with /<cmd>
        paragraphs = HELP_TEXT.split('\n\n')
        pattern    = re.compile(r'^/' + re.escape(cmd) + r'(\s|$)', re.MULTILINE)
        matches    = [p.strip() for p in paragraphs if pattern.search(p)]

        if not matches:
            # check if it's a user-defined alias
            alias_key = f"/{cmd}"
            expansion = self._aliases.get(alias_key)
            if expansion:
                print(f"/{cmd}  →  {expansion}  (alias)")
                # if it expands to /agent <name>, show the agent file info
                m = re.match(r'^/agent\s+(\S+)', expansion)
                if m:
                    agent_name = m.group(1)
                    agent_path = self._find_agent_def(agent_name)
                    if agent_path:
                        cfg = self._load_agent_def(agent_path)
                        print(f"  agent file : {agent_path}")
                        if cfg["description"]:
                            print(f"  description: {cfg['description']}")
                        print(f"  max_turns  : {cfg['max_turns']}  auto_exec: {cfg['auto_exec']}  auto_apply: {cfg['auto_apply']}")
                        print(f"  tools      : {', '.join(cfg['tools']) if cfg['tools'] else '(default)'}")
                        if cfg["aliases"]:
                            print(f"  aliases    :")
                            for k, v in cfg["aliases"].items():
                                print(f"    {k} = {v}")
                else:
                    # show help for the top-level target command
                    target = expansion.lstrip('/').split()[0]
                    if target != cmd:
                        target_pattern = re.compile(r'^/' + re.escape(target) + r'(\s|$)', re.MULTILINE)
                        target_matches = [p.strip() for p in paragraphs if target_pattern.search(p)]
                        if target_matches:
                            print()
                            print('\n\n'.join(target_matches))
            else:
                print(f"[help] no section found for '{cmd}'")
                print(f"  try: /help read | /help map | /help fix | /help script | /help mcp | /help parallel | /help bkup | /help ctx")
            return

        result = '\n\n'.join(matches)
        print(result)

        if ctx and not self._auto_apply:
            self.messages.append({"role": "user",
                                   "content": f"[help: /{cmd}]\n{result}"})
            print(f"\n[help] /{cmd} injected into context")

    def _cmd_map(self, user_input: str):
        parts = user_input.split(None, 2)
        sub   = parts[1] if len(parts) > 1 else ""

        if sub == "index":
            raw   = parts[2].strip() if len(parts) > 2 else "."
            toks  = raw.split()
            root  = os.path.abspath(toks[0])
            depth = int(toks[1]) if len(toks) > 1 and toks[1].isdigit() else 2
            self._map_index(root, depth)
        elif sub == "find":
            query = parts[2].strip() if len(parts) > 2 else ""
            if not os.path.exists(os.path.join(BCODER_DIR, "map.txt")):
                _warn("[map] map.txt not found. /map index can take a long time on large projects.")
                if not self._confirm("[map] run /map index now? [Y/n]:"):
                    return
                self._map_index(os.path.abspath("."), 2)
            self._map_find(query)
        elif sub == "trace":
            query = parts[2].strip() if len(parts) > 2 else ""
            if not os.path.exists(os.path.join(BCODER_DIR, "map.txt")):
                _warn("[map] map.txt not found. /map index can take a long time on large projects.")
                if not self._confirm("[map] run /map index now? [Y/n]:"):
                    return
                self._map_index(os.path.abspath("."), 2)
            self._map_trace(query)
        elif sub == "diff":
            self._map_diff()
        elif sub == "idiff":
            raw   = parts[2].strip() if len(parts) > 2 else "."
            toks  = raw.split()
            root  = os.path.abspath(toks[0])
            depth = int(toks[1]) if len(toks) > 1 and toks[1].isdigit() else 2
            self._map_index(root, depth)
            self._map_diff()
            self._map_delta_asymmetry()
        elif sub == "keyword":
            rest  = parts[2].strip() if len(parts) > 2 else ""
            rtoks = rest.split()
            sub2  = rtoks[0] if rtoks else ""
            if sub2 == "index":
                if not os.path.exists(os.path.join(BCODER_DIR, "map.txt")):
                    _warn("[map] map.txt not found. /map index can take a long time on large projects.")
                    if not self._confirm("[map] run /map index now? [Y/n]:"):
                        return
                    self._map_index(os.path.abspath("."), 2)
                self._map_keyword_index()
            elif sub2 == "extract":
                if not os.path.exists(os.path.join(BCODER_DIR, "keyword.txt")):
                    _info("[map] keyword.txt not found — running /map keyword index first...")
                    if not os.path.exists(os.path.join(BCODER_DIR, "map.txt")):
                        _warn("[map] map.txt not found. /map index can take a long time on large projects.")
                        if not self._confirm("[map] run /map index now? [Y/n]:"):
                            return
                        self._map_index(os.path.abspath("."), 2)
                    self._map_keyword_index()
                self._map_keyword_extract(rtoks[1:])
            else:
                print("usage:")
                print("  /map keyword index                  — build .1bcoder/keyword.txt from map.txt")
                print("  /map keyword extract <text> [-a|-f] — extract known keywords from text")
                print("  /map keyword extract <file> [-a|-f] — extract known keywords from file")
                print("  -a  alphabetical order   -f  frequency order (most common first)")
        else:
            print("usage:")
            print("  /map index [path] [2|3]        — scan project, build .1bcoder/map.txt")
            print("  /map find                      — inject full map into context")
            print("  /map find term                 — filename contains term")
            print("  /map find !term                — exclude if filename contains term")
            print("  /map find \\term               — include block if any child line contains term")
            print("  /map find \\!term              — exclude block if any child contains term")
            print("  /map find -term                — show ONLY child lines containing term")
            print("  /map find -!term               — hide child lines containing term")
            print("  /map find +term                — like -term but also hides files with no matching children")
            print("  combine freely: auth \\register !mock -!deprecated -y -d 2")
            print("  /map trace <identifier> [-d N] [-y]   — follow call chain backwards from identifier")
            print("  /map diff                      — diff map.txt vs map.prev.txt (no re-index)")
            print("  /map idiff [path] [2|3]        — re-index then diff + ORPHAN_DRIFT + GHOST alert")
            print("  /map keyword index             — build keyword vocabulary from map.txt")
            print("  /map keyword extract <text>    — extract known keywords from text or file")

    def _map_index(self, root: str, depth: int = 2):
        if not os.path.isdir(root):
            print(f"not a directory: {root}")
            return
        depth = max(2, min(depth, 3))
        print(f"[map] scanning {root} (depth {depth}) ...")

        os.makedirs(BCODER_DIR, exist_ok=True)
        map_path  = os.path.join(BCODER_DIR, "map.txt")
        map_text = map_index.build_map(root, depth, map_path=map_path)
        prev_path = os.path.join(BCODER_DIR, "map.prev.txt")

        # partial scan: root is a subfolder of WORKDIR
        rel_root = os.path.relpath(root, WORKDIR).replace("\\", "/")
        is_partial = rel_root != "." and not rel_root.startswith("..")

        if is_partial:
            # adjust paths so they are relative to WORKDIR, not subfolder
            map_text = _adjust_map_paths(map_text, rel_root)
            # save segment file in .1bcoder/
            seg_name = _path_to_seg_name(rel_root)
            seg_path = os.path.join(BCODER_DIR, seg_name)
            with open(seg_path, "w", encoding="utf-8") as f:
                f.write(map_text)
            print(f"[map] partial index → {seg_path}")
            # patch map.txt: remove stale blocks, append new content
            if os.path.exists(map_path):
                import shutil
                shutil.copy2(map_path, prev_path)
                removed = _map_patch_remove(map_path, rel_root)
                # strip comment header from partial map before appending
                sep = "\n\n"
                first_sep = map_text.find(sep)
                body = map_text[first_sep + len(sep):] if first_sep != -1 else map_text
                with open(map_path, "a", encoding="utf-8") as f:
                    f.write(sep + body)
                print(f"[map] patched map.txt (removed {removed}, appended {body.count(chr(10)+chr(10))+1} blocks)")
            else:
                with open(map_path, "w", encoding="utf-8") as f:
                    f.write(map_text)
                print(f"[map] created map.txt from partial index")
        else:
            # full scan — overwrite map.txt
            if os.path.exists(map_path):
                import shutil
                shutil.copy2(map_path, prev_path)
            with open(map_path, "w", encoding="utf-8") as f:
                f.write(map_text)
            print(f"[map] indexed → {map_path}")

    def _map_keyword_index(self):
        """Scan map.txt, extract all identifiers/words → .1bcoder/keyword.txt (CSV)."""
        import csv as _csv
        from collections import defaultdict
        map_path = os.path.join(BCODER_DIR, "map.txt")
        kw_path  = os.path.join(BCODER_DIR, "keyword.txt")
        if not os.path.exists(map_path):
            _err("map.txt not found — run /map index first")
            return
        with open(map_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        token_re = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]{1,}')  # identifiers ≥ 2 chars
        word_lines: dict = defaultdict(set)
        for lineno, line in enumerate(lines, 1):
            for m in token_re.finditer(line):
                word_lines[m.group()].add(lineno)
        sorted_words = sorted(word_lines, key=str.lower)
        with open(kw_path, "w", encoding="utf-8", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["word", "count", "lines"])
            for word in sorted_words:
                lns = sorted(word_lines[word])
                w.writerow([word, len(lns), ";".join(str(l) for l in lns)])
        _ok(f"[keyword] {len(sorted_words)} keywords → {kw_path}")

    def _map_keyword_extract(self, args: list):
        """Extract words from text/file that are present in keyword.txt."""
        import csv as _csv
        kw_path = os.path.join(BCODER_DIR, "keyword.txt")
        if not os.path.exists(kw_path):
            _err("keyword.txt not found — run /map keyword index first")
            return
        sort_alpha  = "-a" in args
        sort_count  = "-s" in args
        fuzzy       = "-f" in args
        like        = "-l" in args
        show_counts = "-n" in args
        csv_out     = "-c" in args
        show_origin = "-o" in args
        src_tokens  = [a for a in args if a not in ("-a", "-s", "-f", "-l", "-n", "-c", "-o")]
        if not src_tokens:
            print("usage: /map keyword extract <text or file> [-a] [-s] [-f] [-l] [-n] [-c] [-o]")
            return
        # load keyword vocab: word → count
        _csv.field_size_limit(10_000_000)  # lines field can be large for common words
        kw_freq: dict = {}
        with open(kw_path, encoding="utf-8", newline="") as f:
            reader = _csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if len(row) >= 2:
                    try:
                        kw_freq[row[0]] = int(row[1])
                    except ValueError:
                        pass
        # resolve source: single existing file, or inline text
        source = " ".join(src_tokens)
        if len(src_tokens) == 1 and os.path.exists(src_tokens[0]):
            try:
                with open(src_tokens[0], encoding="utf-8", errors="replace") as f:
                    text = f.read()
                _info(f"[keyword extract] reading {src_tokens[0]}")
            except OSError as e:
                _err(e); return
        else:
            text = source
        token_re = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]{1,}')
        seen: dict = {}  # real keyword → order of first query-token match
        if fuzzy:
            # precompute subword sets for all keywords (real identifiers only stored)
            kw_parts = {kw: frozenset(_split_identifier(kw)) for kw in kw_freq}
            for i, m in enumerate(token_re.finditer(text)):
                # require subwords >= 5 chars to skip common English stopwords
                # (is, in, as, it, of, by, we, to, for, all, and, any, the, main, pull, code ...)
                query_parts = frozenset(
                    w for w in _split_identifier(m.group()) if len(w) >= 5
                )
                if not query_parts:
                    continue
                for kw, kp in kw_parts.items():
                    # keyword matches if ALL query subwords are present in keyword's subwords
                    if query_parts <= kp and kw not in seen:
                        seen[kw] = i
        elif like:
            for i, m in enumerate(token_re.finditer(text)):
                token = m.group().lower()
                for j, kw in enumerate(kw_freq):
                    if token in kw.lower() and kw not in seen:
                        seen[kw] = i * 100000 + j
        else:
            # default: exact identifier match
            kw_set = set(kw_freq)
            for i, m in enumerate(token_re.finditer(text)):
                w = m.group()
                if w in kw_set and w not in seen:
                    seen[w] = i
        if not seen:
            print("(no matching keywords found)")
            return
        if sort_alpha:
            result = sorted(seen, key=lambda w: w.lower())
        elif sort_count or show_counts:
            result = sorted(seen, key=lambda w: (-kw_freq[w], w.lower()))
        else:
            result = sorted(seen, key=lambda w: (seen[w], w.lower()))
        if show_origin:
            map_path = os.path.join(BCODER_DIR, "map.txt")
            line_to_file = map_query._build_line_to_file(map_path)
            # load line numbers column from keyword.txt
            kw_lines: dict = {}
            with open(kw_path, encoding="utf-8", newline="") as f:
                reader2 = _csv.reader(f)
                next(reader2, None)
                for row in reader2:
                    if len(row) >= 3 and row[0] in seen:
                        kw_lines[row[0]] = [int(x) for x in row[2].split(";") if x]
            lines_out = []
            for w in result:
                label = f"{w}({kw_freq[w]})" if show_counts else w
                files: list = []
                seen_files: set = set()
                for ln in kw_lines.get(w, []):
                    f = line_to_file.get(ln)
                    if f and f not in seen_files:
                        seen_files.add(f)
                        files.append(f)
                lines_out.append(f"{label} -> {', '.join(files)}" if files else label)
            print("\n".join(lines_out))
            return
        if show_counts:
            items = [f"{w}({kw_freq[w]})" for w in result]
        else:
            items = list(result)
        if csv_out:
            print(", ".join(items))
        else:
            print("\n".join(items))

    def _map_find(self, query: str):
        map_path = os.path.join(BCODER_DIR, "map.txt")
        if not os.path.exists(map_path):
            print("[map] no map.txt found — run /map index first")
            return

        tokens   = query.split()
        auto_yes = "-y" in tokens

        # parse -d N depth flag
        depth = 3
        clean_tokens = []
        i = 0
        while i < len(tokens):
            if tokens[i] == "-d" and i + 1 < len(tokens) and tokens[i+1].isdigit():
                depth = int(tokens[i+1])
                i += 2
            elif tokens[i] != "-y":
                clean_tokens.append(tokens[i])
                i += 1
            else:
                i += 1
        clean_q = " ".join(clean_tokens)

        hits, result = map_query.find_map(map_path, clean_q)

        # apply depth filter
        def _depth_filter(block: str) -> str:
            lines = block.split('\n')
            if depth == 1:
                return lines[0]
            if depth == 2:
                kept = [l for l in lines[1:] if 'links' not in l]
                return lines[0] + ('\n' + '\n'.join(kept) if kept else '')
            return block  # depth 3 — full block

        if depth < 3 and hits:
            hits   = [_depth_filter(b) for b in hits]
            result = '\n'.join(hits)

        if not clean_q:
            # full map
            print(result)
            if auto_yes or self._confirm("  add full map to context? [Y/n]:", ctx_add=result):
                if not self._auto_apply:
                    self.messages.append({"role": "user", "content": f"[project map]\n{result}"})
                print("[map] full map injected into context")
            return

        if not hits:
            print(f"[map] no matches for: {clean_q}")
            return

        print(result)
        print(f"\n[map] {len(hits)} match(es)")

        paths = [b.split('\n')[0].strip() for b in hits]
        self._vars["map_files"] = ",".join(paths)
        print(f"[map] {{{{map_files}}}} set — {len(paths)} file(s)")

        if auto_yes or self._confirm("  add to context? [Y/n]:", ctx_add=result):
            if not self._auto_apply:
                self.messages.append({"role": "user",
                                       "content": f"[map find: {clean_q}]\n{result}"})
            print("[map] injected into context")

    def _map_trace(self, query: str):
        tokens   = query.split()
        auto_yes = "-y" in tokens
        tokens   = [t for t in tokens if t != "-y"]

        # extract -d N
        max_depth = 8
        di = next((i for i, t in enumerate(tokens) if t == "-d"), None)
        if di is not None and di + 1 < len(tokens) and tokens[di + 1].isdigit():
            max_depth = int(tokens[di + 1])
            tokens = tokens[:di] + tokens[di + 2:]

        if not tokens:
            print("usage: /map trace <identifier> [-d N] [-y]")
            print("       /map trace <start> <end> [-y]   — find path between two points")
            return

        map_path = os.path.join(BCODER_DIR, "map.txt")
        if not os.path.exists(map_path):
            print("[map] no map.txt found — run /map index first")
            return

        if tokens[0] == "deps" and len(tokens) >= 2:
            # forward dependency tree: what does this identifier depend on?
            identifier  = tokens[1]
            leaves_only = "-leaf" in tokens
            result = map_query.trace_deps(map_path, identifier, max_depth, leaves_only)
            label  = f"deps:{identifier}"
            if result is None:
                print(f"[map] '{identifier}' not found in any defines — try /map find \\{identifier}")
                return
            print(result)
            print()
            if auto_yes or self._confirm("  add to context? [Y/n]:", ctx_add=result):
                if not self._auto_apply:
                    self.messages.append({"role": "user",
                                           "content": f"[map trace: {label}]\n{result}"})
                print("[map] trace injected into context")

        elif len(tokens) >= 2:
            # pathfinding mode with [Y/c/n] loop for alternative paths
            start_id, end_id = tokens[0], tokens[1]
            label    = f"{start_id} → {end_id}"
            blocked   = set()
            path_idx  = 1
            collected = []   # paths added to context so far
            auto_loop = 0    # remaining auto-Y iterations from /l

            while True:
                result, intermediates = map_query.find_path(
                    map_path, start_id, end_id, blocked, path_idx)
                print(result)
                print()
                if intermediates is None:
                    break
                if auto_yes or auto_loop > 0 or self._auto_apply:
                    collected.append(result)
                    blocked |= intermediates
                    path_idx += 1
                    if auto_loop > 0:
                        auto_loop -= 1
                    if auto_yes or self._auto_apply:
                        break
                    continue
                ans = input("  [Y]es add + next / [s]kip next / [l]oop N / [n]o stop: ").strip().lower()
                if ans in ("y", "yes", ""):
                    collected.append(result)
                    blocked |= intermediates
                    path_idx += 1
                elif ans in ("s", "skip"):
                    blocked |= intermediates
                    path_idx += 1
                elif ans.startswith("l"):
                    # "l" → ask, "l 10" or "l10" → parse inline
                    parts = ans.split()
                    n_str = parts[1] if len(parts) > 1 else re.sub(r'\D', '', ans)
                    if n_str.isdigit() and int(n_str) > 0:
                        auto_loop = int(n_str)
                    else:
                        n_str = input("  how many paths? ").strip()
                        auto_loop = int(n_str) if n_str.isdigit() else 1
                    # collect current path and start looping
                    collected.append(result)
                    blocked |= intermediates
                    path_idx += 1
                    auto_loop -= 1   # one already consumed above
                else:
                    break

            if collected:
                content = "\n\n".join(collected)
                if not self._auto_apply:
                    self.messages.append({"role": "user",
                                           "content": f"[map trace: {label}]\n{content}"})
                print(f"[map] {len(collected)} path(s) injected into context")

        else:
            # single-identifier BFS tree (existing behaviour)
            identifier = tokens[0]
            result = map_query.trace_map(map_path, identifier, max_depth)
            label  = identifier
            if result is None:
                print(f"[map] '{identifier}' not found in any defines — try /map find \\{identifier}")
                return
            print(result)
            print()
            if auto_yes or self._confirm("  add to context? [Y/n]:", ctx_add=result):
                if not self._auto_apply:
                    self.messages.append({"role": "user",
                                           "content": f"[map trace: {label}]\n{result}"})
                print("[map] trace injected into context")

    def _map_diff(self):
        map_path  = os.path.join(BCODER_DIR, "map.txt")
        prev_path = os.path.join(BCODER_DIR, "map.prev.txt")

        if not os.path.exists(map_path):
            print("[map] no map.txt — run /map index first")
            return
        if not os.path.exists(prev_path):
            print("[map] no map.prev.txt — run /map index at least twice to get a diff")
            return

        old_defs, _ = map_query.parse_map(prev_path)
        new_defs, _ = map_query.parse_map(map_path)

        all_files = sorted(set(old_defs) | set(new_defs))
        lines_out  = ["[map diff]  map.prev.txt → map.txt"]
        changes    = 0

        for frel in all_files:
            in_old = frel in old_defs
            in_new = frel in new_defs

            if in_old and not in_new:
                lines_out.append(f"\n- {frel}  (file removed from index)")
                changes += 1
                continue
            if in_new and not in_old:
                new_names = sorted(new_defs[frel])
                lines_out.append(f"\n+ {frel}  (new file)")
                if new_names:
                    lines_out.append(f"  + defines: {', '.join(new_names)}")
                changes += 1
                continue

            # both present — compare defines
            old_names = set(old_defs[frel])
            new_names = set(new_defs[frel])
            removed   = sorted(old_names - new_names)
            added     = sorted(new_names - old_names)

            if removed or added:
                lines_out.append(f"\n  {frel}")
                for n in removed:
                    ln = old_defs[frel][n]
                    lines_out.append(f"  - defines: {n}(ln:{ln})")
                for n in added:
                    ln = new_defs[frel][n]
                    lines_out.append(f"  + defines: {n}(ln:{ln})")
                if removed:
                    lines_out.append(f"  ! WARNING: {len(removed)} identifier(s) removed")
                changes += 1

        if changes == 0:
            lines_out.append("\n  (no changes detected)")

        result = "\n".join(lines_out)
        print("\n".join(_cdiff(l) for l in lines_out))
        print()

        if changes > 0 and self._confirm("  add diff to context? [Y/n]:", ctx_add=result):
            if not self._auto_apply:
                self.messages.append({"role": "user", "content": result})
            print(f"{_GREEN}[map] diff injected into context{_R}")

    def _map_delta_asymmetry(self):
        """Print ORPHAN_DRIFT + GHOST alerts after a re-index. Called automatically by /map idiff."""
        map_path  = os.path.join(BCODER_DIR, "map.txt")
        prev_path = os.path.join(BCODER_DIR, "map.prev.txt")
        if not os.path.exists(prev_path):
            return  # first run, no baseline
        result = map_query.idiff_report(prev_path, map_path)
        for line in result.splitlines():
            if 'DEGRADATION' in line or 'GHOST ALERT' in line or line.startswith('  !'):
                print(f"{_RED}{line}{_R}")
            elif 'HEALING' in line:
                print(f"{_GREEN}{line}{_R}")
            elif line.startswith('new orphans') or line.startswith('  +') or line.startswith('    called'):
                print(line)

# ── agent ───────────────────────────────────────────────────────────────────

    def _load_agent_config(self) -> dict:
        """Read .1bcoder/agent.txt → dict with keys: max_turns, auto_apply, tools, advanced_tools."""
        config = {
            "max_turns": 10,
            "auto_apply": True,
            "tools": list(DEFAULT_AGENT_TOOLS),
            "advanced_tools": list(DEFAULT_AGENT_TOOLS_ADVANCED),
        }
        if not os.path.exists(AGENT_CONFIG_FILE):
            return config
        tools = []
        advanced_tools = []
        in_tools = False
        in_advanced = False
        with open(AGENT_CONFIG_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("max_turns"):
                    try:
                        config["max_turns"] = int(stripped.split("=", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
                    in_tools = in_advanced = False
                elif stripped.startswith("auto_apply"):
                    val = stripped.split("=", 1)[1].strip().lower()
                    config["auto_apply"] = val in ("true", "1", "yes")
                    in_tools = in_advanced = False
                elif stripped.startswith("advanced_tools"):
                    in_tools = False
                    in_advanced = True
                elif stripped.startswith("tools"):
                    in_tools = True
                    in_advanced = False
                elif (in_tools or in_advanced) and (line.startswith("    ") or line.startswith("\t")):
                    if in_tools:
                        tools.append(stripped)
                    else:
                        advanced_tools.append(stripped)
        if tools:
            config["tools"] = tools
        if advanced_tools:
            config["advanced_tools"] = advanced_tools
        return config

    def _agent_confirm(self, cmd: str):
        """Interactive prompt for an agent action.
        Returns (action, value):
          ('run',      cmd_str)   — execute (possibly edited) command
          ('skip',     None)      — skip this action
          ('quit',     None)      — stop the agent
          ('feedback', text)      — inject user note, skip action
        """
        while True:
            try:
                answer = input("  execute? [Y/n/e/f/q]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return ('quit', None)
            al = answer.lower()
            if al in ('', 'y'):
                return ('run', cmd)
            if al == 'q':
                return ('quit', None)
            if al == 'n':
                return ('skip', None)
            if al == 'e':
                print(f"  {_DIM}current:{_R} {_YELL}{cmd}{_R}")
                # try to copy to clipboard so user can Ctrl+V and edit
                _clipped = False
                try:
                    import ctypes
                    ctypes.windll.user32.OpenClipboard(0)
                    ctypes.windll.user32.EmptyClipboard()
                    encoded = cmd.encode('utf-16-le') + b'\x00\x00'
                    hMem = ctypes.windll.kernel32.GlobalAlloc(0x0002, len(encoded))
                    pMem = ctypes.windll.kernel32.GlobalLock(hMem)
                    ctypes.memmove(pMem, encoded, len(encoded))
                    ctypes.windll.kernel32.GlobalUnlock(hMem)
                    ctypes.windll.user32.SetClipboardData(13, hMem)  # CF_UNICODETEXT
                    ctypes.windll.user32.CloseClipboard()
                    _clipped = True
                except Exception:
                    pass
                if _clipped:
                    print(f"  {_DIM}[copied to clipboard — paste with Ctrl+V]{_R}")
                try:
                    new_cmd = input("  edit> ").strip()
                except (EOFError, KeyboardInterrupt):
                    return ('quit', None)
                return ('run', new_cmd if new_cmd else cmd)
            if al == 'f':
                print(f"  {_DIM}feedback to AI (blank = cancel):{_R}")
                try:
                    fb = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    return ('quit', None)
                if fb:
                    return ('feedback', fb)
                continue  # blank → re-prompt
            # unknown key → re-prompt

    def _agent_exec(self, cmd: str, auto_apply: bool) -> str:
        """Run a /command, capture and return its output as a string.

        Uses a Tee so the user still sees output in real time.
        In auto_apply mode, confirmation prompts are bypassed (→ True).
        """
        import io

        class _Tee(io.StringIO):
            def __init__(self, real):
                super().__init__()
                self._real = real
            def write(self, s):
                self._real.write(s)
                return super().write(s)
            def flush(self):
                self._real.flush()
                super().flush()

        tee = _Tee(sys.stdout)
        original_confirm = self._confirm
        if auto_apply:
            self._confirm = lambda _prompt, **kw: True
            self._auto_apply = True

        original_stdout = sys.stdout
        sys.stdout = tee
        try:
            self._route(cmd, auto=True)
        except SystemExit:
            pass
        finally:
            sys.stdout = original_stdout
            if auto_apply:
                self._confirm = original_confirm
                self._auto_apply = False

        return tee.getvalue().strip() or "(no output)"

    # ── aliases ────────────────────────────────────────────────────────────────

    def _load_aliases(self) -> dict:
        """Load aliases from global then local aliases.txt (local overrides global)."""
        aliases = {}
        for path in (GLOBAL_ALIASES_FILE, ALIASES_FILE):
            if not os.path.exists(path):
                continue
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    name, _, value = line.partition("=")
                    name  = name.strip()
                    value = value.strip()
                    if name:
                        if not name.startswith("/"):
                            name = "/" + name
                        aliases[name] = value
        return aliases

    def _expand_alias(self, user_input: str, depth: int = 0) -> str:
        """Expand a single alias level. {{args}} is replaced by everything after the command name."""
        if depth > 10:
            return user_input
        parts      = user_input.split(None, 1)
        name       = parts[0]
        args       = parts[1] if len(parts) > 1 else ""
        template   = self._aliases.get(name)
        if template is None:
            return user_input
        if "{{args}}" in template:
            expanded = template.replace("{{args}}", args)
        else:
            expanded = (template + (" " + args if args else "")).strip()
        return self._expand_alias(expanded, depth + 1)

    def _cmd_alias(self, user_input: str):
        rest = user_input[6:].strip()   # strip "/alias"
        if not rest or rest == "list":
            if not self._aliases:
                print("[aliases] none defined")
            else:
                for name, value in sorted(self._aliases.items()):
                    print(f"  {name} = {value}")
            return
        if rest.startswith("clear"):
            name = rest[5:].strip()
            if not name.startswith("/"):
                name = "/" + name
            if name in self._aliases:
                del self._aliases[name]
                _ok(f"[alias] removed {name}")
            else:
                print(f"[alias] {name} not found")
            return
        if rest.startswith("save"):
            name = rest[4:].strip()
            if not name.startswith("/"):
                name = "/" + name
            if name not in self._aliases:
                print(f"[alias] {name} not defined")
                return
            os.makedirs(BCODER_DIR, exist_ok=True)
            lines = []
            if os.path.exists(ALIASES_FILE):
                lines = open(ALIASES_FILE, encoding="utf-8").readlines()
            # replace existing entry or append
            key = name + " ="
            new_line = f"{name} = {self._aliases[name]}\n"
            replaced = False
            for i, l in enumerate(lines):
                if l.strip().startswith(name + " =") or l.strip().startswith(name + "="):
                    lines[i] = new_line
                    replaced = True
                    break
            if not replaced:
                lines.append(new_line)
            with open(ALIASES_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines)
            _ok(f"[alias] saved {name} to {ALIASES_FILE}")
            return
        if "=" in rest:
            name, _, value = rest.partition("=")
            name  = name.strip()
            value = value.strip()
            if not name.startswith("/"):
                name = "/" + name
            self._aliases[name] = value
            _ok(f"[alias] {name} = {value}")
            return
        print("usage: /alias [list] | /alias /name = expansion | /alias clear /name | /alias save /name")

    # ── /ask ───────────────────────────────────────────────────────────────────

    def _truncate_ask_result(self, cmd: str, result: str) -> str:
        """Truncate a tool result that is too large for a small model's context.
        Override limits with: /param ask_limit 4000  /param ask_show 2000"""
        limit = int(self.params.get("ask_limit", ASK_RESULT_LIMIT_CHARS))
        show  = int(self.params.get("ask_show",  ASK_RESULT_SHOW_CHARS))
        if len(result) <= limit:
            return result
        truncated = result[:show]
        c = cmd.lstrip().lower()
        if c.startswith("/map find"):
            hint = "add more keywords, use -d 1 or -d 2, or add !exclude terms"
        elif c.startswith("/map trace"):
            hint = "use -d 2 to limit depth"
        elif c.startswith("/map keyword"):
            hint = "use -c flag for comma output or add a more specific phrase"
        elif c.startswith("/tree"):
            hint = "use /tree <subfolder> or /tree -d 2 to narrow down"
        elif c.startswith("/find"):
            hint = "add -f or -c flag, use --ext, or make the pattern more specific"
        elif c.startswith("/read"):
            # extract filename from cmd for a specific hint
            parts = cmd.split()
            fname = parts[1] if len(parts) > 1 else "file"
            hint = f"use a line range, e.g. /read {fname} 1-50"
        else:
            hint = "refine your query to get fewer results"
        return f"{truncated}\n[TRUNCATED — result too large for context. {hint}]"

    # ── shared agent loop ──────────────────────────────────────────────────────

    def _run_agent_loop(self, label, agent_msgs, max_turns, auto_exec, auto_apply,
                        plan_steps=None, use_procs=False, plan_context="", on_done=""):
        """Shared loop for /ask, /agent, and future custom agents."""
        ACTION_RE   = re.compile(r'ACTION:\s*(/\S+(?:[ \t]+[^\n]+)?)', re.MULTILINE)
        plan_steps  = list(plan_steps) if plan_steps else []
        total_plan  = len(plan_steps)
        if plan_context:
            agent_msgs.append({"role": "user", "content": f"[plan context]\n{plan_context}"})
            plan_context = ""
        msgs_offset = 1 + len(self.messages)   # stable: self.messages not modified during loop
        self._in_agent   = True
        self._agent_msgs = agent_msgs          # expose to /tempctx
        try:
            for turn in range(1, max_turns + 1):
                est_tokens = sum(len(m["content"]) for m in agent_msgs) // 4
                ctx_pct    = est_tokens * 100 // self.num_ctx
                print(f"\n{_CYAN}{_BOLD}[{label}] ── turn {turn}/{max_turns}{_R}{_DIM} " +
                      "─" * 20 + f"  ctx {est_tokens}/{self.num_ctx} ({ctx_pct}%)" + _R)
                if est_tokens >= int(self.num_ctx * 0.85):
                    _warn(f"[{label}] context {ctx_pct}% full — stopping to avoid overflow")
                    break

                step = None
                step_num = None
                if plan_steps:
                    step     = plan_steps.pop(0)
                    step_num = total_plan - len(plan_steps)
                    hint     = f"[plan step {step_num}/{total_plan}: {step}]"
                    agent_msgs.append({"role": "user", "content": hint})
                    print(f"{_DIM}  {hint}{_R}")

                # before procs — run before LLM call, inject output into agent context
                if use_procs and self._proc_before:
                    last_reply = next((m["content"] for m in reversed(agent_msgs)
                                       if m["role"] == "assistant"), "")
                    for _bp in self._proc_before:
                        bp_out = self._run_proc(_bp, auto=True, input_text=last_reply)
                        if bp_out:
                            # CAPTURE: line — set self._last_output so $ expands to it in ACTION
                            for _line in bp_out.splitlines():
                                if _line.startswith("CAPTURE:"):
                                    self._last_output = _line[8:].strip()
                                    break
                            # Execute ACTION lines: run command, capture LLM reply into agent context
                            # (result is removed from self.messages so main context stays clean)
                            for _line in bp_out.splitlines():
                                if _line.startswith("ACTION:"):
                                    _action_cmd = _line[7:].strip()
                                    _n = len(self.messages)
                                    self._route(_action_cmd, auto=True)
                                    _new = self.messages[_n:]
                                    self.messages = self.messages[:_n]   # don't pollute main ctx
                                    for _m in _new:
                                        if _m.get("role") == "assistant":
                                            _hint = _m["content"].strip()
                                            agent_msgs.append({"role": "user",
                                                               "content": f"[context] {_hint}"})
                                            print(f"{_DIM}  [before] {_hint[:80]}{'...' if len(_hint) > 80 else ''}{_R}")
                            # inject plain text output (non-protocol lines) as context too
                            _SKIP = ("ACTION:", "BLOCK:", "FAIL:", "PASS", "CAPTURE:")
                            ctx_lines = [l for l in bp_out.splitlines()
                                         if not any(l.startswith(k) for k in _SKIP)]
                            ctx_text = "\n".join(ctx_lines).strip()
                            if ctx_text:
                                agent_msgs.append({"role": "user", "content": f"[context] {ctx_text}"})
                                print(f"{_DIM}  [before] {ctx_text[:80]}{'...' if len(ctx_text) > 80 else ''}{_R}")

                self._sep("AI")
                reply = self._stream_chat(agent_msgs)
                if reply is None:   # keyboard interrupt sentinel
                    try:
                        choice = input("  Response interrupted. Retry current turn? [Y/n/q]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        choice = "q"
                    if choice == "q":
                        break
                    try:
                        note = input("  Add comment if needed: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        note = ""
                    if choice == "n":       # skip — mark as interrupted, continue
                        agent_msgs.append({"role": "assistant", "content": "[interrupted]"})
                        if note:
                            agent_msgs.append({"role": "user", "content": note})
                        continue
                    else:                   # Y or enter — retry
                        if step is not None:    # plan mode: restore step and remove hint
                            plan_steps.insert(0, step)
                            agent_msgs.pop()
                        if note:
                            agent_msgs.append({"role": "user", "content": note})
                        continue
                    reply = ""
                print()
                if not reply:
                    if plan_steps:
                        agent_msgs.append({"role": "user", "content": "[no response — continuing to next plan step]"})
                        continue
                    print(f"[{label}] empty reply, stopping")
                    break

                self.last_reply = reply
                self._last_output = reply
                agent_msgs.append({"role": "assistant", "content": reply})

                proc_actions: list[str] = []
                proc_failures: list[str] = []
                if use_procs:
                    for _proc in self._proc_active:
                        proc_out = self._run_proc(_proc, auto=True)
                        if proc_out:
                            proc_actions += ACTION_RE.findall(proc_out)
                            for _line in proc_out.splitlines():
                                if _line.startswith("FAIL:"):
                                    proc_failures.append(_line[5:].strip())

                # gate procs — PASS/FAIL validators (also merged with proc_failures)
                if use_procs and self._proc_gates:
                    for _gp in self._proc_gates:
                        gp_out = self._run_proc(_gp, auto=True)
                        if gp_out:
                            proc_actions += ACTION_RE.findall(gp_out)
                            for line in gp_out.splitlines():
                                if line.startswith("FAIL:"):
                                    proc_failures.append(line[5:].strip())
                failures = proc_failures
                if failures:
                        feedback = ("[gate failures — fix before continuing]\n"
                                    + "\n".join(f"- {f}" for f in failures)
                                    + "\nUse ACTION: to fix the issues above.")
                        print(f"{_DIM}[{label}] gates: {len(failures)} failure(s) — retrying{_R}")
                        # restore current plan step so agent retries it
                        if step is not None:
                            plan_steps.insert(0, step)
                            # remove the [plan step N/M: ...] hint already in context
                            for i in range(len(agent_msgs) - 1, -1, -1):
                                if agent_msgs[i]["role"] == "user" and agent_msgs[i]["content"].startswith("[plan step"):
                                    agent_msgs.pop(i)
                                    break
                        agent_msgs.append({"role": "user", "content": feedback})
                        continue  # force next turn regardless of actions

                model_actions = ACTION_RE.findall(reply)
                actions       = model_actions + proc_actions
                if not actions:
                    if plan_steps:
                        print(f"{_DIM}[{label}] no ACTION — {len(plan_steps)} plan step(s) remaining, continuing{_R}")
                        continue
                    print(f"{_DIM}[{label}] no ACTION and no plan steps remaining — done{_R}")
                    if on_done:
                        self._route(on_done)
                    ans = input("  Add to main context? [s]ummary / [a]ll / [n]one: ").strip().lower()
                    if ans == "a":
                        self.messages.extend(agent_msgs[msgs_offset:])
                        _ok(f"[{label}] {len(agent_msgs) - msgs_offset} message(s) added to context")
                    elif ans == "s":
                        last = next((m for m in reversed(agent_msgs) if m["role"] == "assistant"), None)
                        if last:
                            self.messages.append({"role": "user", "content": f"[{label} result]\n{last['content']}"})
                            _ok(f"[{label}] last reply added to context")
                    else:
                        print(f"[{label}] nothing added to context")
                    break
                # only proc actions fired (model said nothing actionable) and no steps left → done
                if not model_actions and not plan_steps and proc_actions:
                    print(f"{_DIM}[{label}] proc-only actions, no plan steps remaining — finishing{_R}")
                    for _pa in proc_actions:
                        self._route(_pa, auto=True)
                    if on_done:
                        self._route(on_done)
                    ans = input("  Add to main context? [s]ummary / [a]ll / [n]one: ").strip().lower()
                    if ans == "a":
                        self.messages.extend(agent_msgs[msgs_offset:])
                        _ok(f"[{label}] {len(agent_msgs) - msgs_offset} message(s) added to context")
                    elif ans == "s":
                        last = next((m for m in reversed(agent_msgs) if m["role"] == "assistant"), None)
                        if last:
                            self.messages.append({"role": "user", "content": f"[{label} result]\n{last['content']}"})
                            _ok(f"[{label}] last reply added to context")
                    else:
                        print(f"[{label}] nothing added to context")
                    break

                tool_results = []
                stop_agent   = False
                for cmd in actions:
                    cmd = cmd.strip()
                    print(f"\n{_YELL}[{label}] action:{_R} {cmd}")
                    if cmd.rstrip().endswith("code") and self.last_reply:
                        preview = _extract_code_block(self.last_reply)
                        if preview:
                            print(f"{_DIM}  ┌─ code to apply ──────────────────────{_R}")
                            for ln in preview.splitlines():
                                print(f"{_DIM}  │{_R} {ln}")
                            print(f"{_DIM}  └───────────────────────────────────────{_R}")
                    if not auto_exec:
                        action, val = self._agent_confirm(cmd)
                        if action == 'quit':
                            print(f"[{label}] stopped by user")
                            stop_agent = True
                            break
                        if action == 'skip':
                            tool_results.append(f"[tool skipped: {cmd}]")
                            continue
                        if action == 'feedback':
                            print(f"{_DIM}[{label}] feedback noted{_R}")
                            tool_results.append(f"[tool skipped: {cmd}]\n[user note: {val}]")
                            continue
                        cmd = val
                    self._sep("tool")
                    cmd = self._expand_alias(cmd)
                    result = self._agent_exec(cmd, auto_apply)
                    result = self._truncate_ask_result(cmd, result)
                    print()
                    tool_results.append(f"[tool result: {cmd}]\n{result}")

                if stop_agent:
                    break
                combined = "\n\n".join(tool_results) if tool_results else "[all tools skipped]"
                agent_msgs.append({"role": "user", "content": combined})

                agent_limit = self._agent_ctx or self.num_ctx
                est_tokens  = sum(len(m["content"]) for m in agent_msgs) // 4
                ctx_pct     = est_tokens * 100 // agent_limit
                _CTX_CLEAR  = ("/tempctx", "/ctx cut", "/ctx clear", "/ctx compact", "/clear")
                ctx_cleared = any(a.strip().startswith(_CTX_CLEAR) for a in actions)
                if est_tokens >= int(agent_limit * 0.85) and not ctx_cleared:
                    _warn(f"[{label}] context {ctx_pct}% full after tool results — stopping")
                    break

            else:
                print(f"\n[{label}] reached max_turns ({max_turns}), stopping")
        finally:
            self._in_agent    = False
            self._agent_msgs  = []

    # ── named agents ───────────────────────────────────────────────────────────

    def _find_agent_def(self, name: str) -> str | None:
        """Return path to <name>.txt in local agents dir then global, or None."""
        for d in (AGENTS_DIR, GLOBAL_AGENTS_DIR):
            p = os.path.join(d, name + ".txt")
            if os.path.exists(p):
                return p
        return None

    def _load_agent_def(self, path: str) -> dict:
        """Parse an agent definition file. Returns dict with all agent settings."""
        cfg = {
            "description": "",
            "system":      AGENT_SYSTEM_BASIC,   # default: inline basic prompt
            "max_turns":   10,
            "auto_apply":  True,
            "auto_exec":   False,
            "tools":       None,   # None = use default from agent.txt
            "aliases":     {},
            "procs":       [],     # procs activated after each reply
            "before":      [],     # procs activated before each LLM call
            "gates":       [],     # procs that gate exit: must return PASS
            "vars":        {},     # session vars set for duration of agent run; {{task}} = task string
            "on_done":     "",     # slash command executed once on natural loop termination
            "params":      {},     # model params overrides for duration of agent run (num_predict, temperature…)
        }
        tools, aliases, procs, before, gates = [], {}, [], [], []
        agent_vars, agent_params, system_lines = {}, {}, []
        in_tools = in_aliases = in_procs = in_before = in_gates = False
        in_vars = in_params = in_system = False
        tools_declared = False  # True if tools = section was seen (even empty)
        for raw in open(path, encoding="utf-8"):
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped:
                # blank line: preserve inside system block, reset other block flags
                if in_system:
                    system_lines.append("")
                else:
                    in_tools = in_aliases = in_procs = in_vars = False
                continue
            if stripped.startswith("#"):
                continue   # comments don't break blocks
            if line.startswith(" ") or line.startswith("\t"):
                if in_system:
                    system_lines.append(stripped)
                elif in_tools:
                    tools.append(stripped)
                elif in_procs:
                    procs.append(stripped)
                elif in_before:
                    before.append(stripped)
                elif in_gates:
                    gates.append(stripped)
                elif in_vars and "=" in stripped:
                    k, _, v = stripped.partition("=")
                    agent_vars[k.strip()] = v.strip()
                elif in_params and "=" in stripped:
                    k, _, v = stripped.partition("=")
                    k, v = k.strip(), v.strip()
                    if v.lower() in ("true", "false"):
                        agent_params[k] = v.lower() == "true"
                    else:
                        try:
                            agent_params[k] = int(v)
                        except ValueError:
                            try:
                                agent_params[k] = float(v)
                            except ValueError:
                                agent_params[k] = v
                elif in_aliases and "=" in stripped:
                    k, _, v = stripped.partition("=")
                    k = k.strip()
                    if not k.startswith("/"):
                        k = "/" + k
                    aliases[k] = v.strip()
                continue
            # top-level key = value line — ends any open block
            in_tools = in_aliases = in_procs = in_before = in_gates = False
            in_vars = in_params = in_system = False
            if "=" not in stripped:
                continue
            key, _, val = stripped.partition("=")
            key = key.strip().lower()
            val = val.strip()
            if key == "description":
                cfg["description"] = val
            elif key == "system":
                system_lines = [val] if val else []
                in_system = True
            elif key == "max_turns":
                cfg["max_turns"] = int(val)
            elif key == "auto_apply":
                cfg["auto_apply"] = val.lower() in ("true", "1", "yes")
            elif key == "auto_exec":
                cfg["auto_exec"] = val.lower() in ("true", "1", "yes")
            elif key == "on_done":
                cfg["on_done"] = val
            elif key == "tools":
                in_tools = True
                tools_declared = True
            elif key == "aliases":
                in_aliases = True
            elif key == "procs":
                in_procs = True
            elif key == "before":
                in_before = True
            elif key == "gates":
                in_gates = True
            elif key == "vars":
                in_vars = True
            elif key == "params":
                in_params = True
        if system_lines:
            cfg["system"] = "\n".join(system_lines)
        if tools_declared:
            cfg["tools"] = tools  # empty list = explicitly no tools
        if aliases:
            cfg["aliases"] = aliases
        if procs:
            cfg["procs"] = procs
        if before:
            cfg["before"] = before
        if gates:
            cfg["gates"] = gates
        if agent_vars:
            cfg["vars"] = agent_vars
        if agent_params:
            cfg["params"] = agent_params
        return cfg

    def _parse_agent_flags(self, task: str, cfg: dict):
        """Strip -t N / -y flags from task string. Returns (task, max_turns, auto_exec, auto_apply)."""
        max_turns  = cfg["max_turns"]
        auto_exec  = cfg["auto_exec"]
        auto_apply = cfg["auto_apply"]
        if re.search(r'(?:^|\s)-y(?:\s|$)', task):
            auto_exec = auto_apply = True
            task = re.sub(r'(?:^|\s)-y(?=\s|$)', ' ', task).strip()
        while True:
            m = re.match(r'^-t\s+(\d+)\s*(.*)', task, re.DOTALL)
            if m:
                max_turns = int(m.group(1))
                task = m.group(2).strip()
                continue
            break
        return task, max_turns, auto_exec, auto_apply

    def _make_chunk_steps(self, filepath: str, n_arg: str, theme: str) -> list[str]:
        """Pre-compute plan steps for chunked file scanning.

        Reads the file and embeds actual content into each step — no /read needed.
        Chunk size is based on real character density, not estimated lines.
        """
        abs_path = filepath if os.path.isabs(filepath) else os.path.join(WORKDIR, filepath)
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            _err(f"[scan] cannot read {filepath}: {e}")
            return []

        total       = len(lines)
        total_chars = sum(len(l) for l in lines)

        if not n_arg or n_arg.lower() == "auto":
            # target ~25% of ctx per chunk (chars); compute proportional line count
            chars_per_chunk = max(400, int(self.num_ctx * 0.25 * 4))
            lines_per_chunk = max(10, round(total * chars_per_chunk / max(1, total_chars)))
            n_chunks = max(1, -(-total // lines_per_chunk))   # ceil div
        else:
            n_chunks        = max(1, int(n_arg))
            lines_per_chunk = max(1, -(-total // n_chunks))

        overlap = max(1, lines_per_chunk // 10)
        print(f"[scan] {filepath}: {total} lines / {total_chars} chars → "
              f"{n_chunks} chunks (~{lines_per_chunk} lines each, overlap {overlap})")

        steps = []
        for i in range(n_chunks):
            start = max(0,      i     * lines_per_chunk - overlap)
            end   = min(total, (i+1)  * lines_per_chunk + overlap)
            chunk_text = "".join(lines[start:end])
            steps.append(
                f"[chunk {i+1}/{n_chunks} of {filepath}]\n"
                f"{chunk_text}"
            )
        return steps

    def _run_named_agent(self, name: str, task: str, path: str):
        """Load agent def from path and run it."""
        cfg  = self._load_agent_def(path)
        task, max_turns, auto_exec, auto_apply = self._parse_agent_flags(task, cfg)

        # also parse plan steps (inline list or file)
        plan_steps = []
        plan_context = ""
        if task.startswith('"'):
            end_q = task.find('"', 1)
            if end_q != -1:
                rest = task[end_q + 1:].strip()
                task = task[1:end_q]
                pm = re.match(r'^(plan|file):\s*(.*)', rest, re.IGNORECASE | re.DOTALL)
                if pm:
                    kw, arg = pm.group(1).lower(), pm.group(2).strip()
                    if kw == "file":
                        cm = re.match(r'^(\S+)\s+-n\s*(\d+|auto)?\s*$', arg, re.IGNORECASE)
                        if cm:
                            plan_steps = self._make_chunk_steps(cm.group(1), cm.group(2) or "auto", task)
                        else:
                            plan_steps, plan_context = _parse_file_arg(arg)
                    else:
                        plan_steps, plan_context = _parse_plan_arg(arg)
        else:
            pm = re.search(r'\b(plan|file):\s*(.*)', task, re.IGNORECASE | re.DOTALL)
            if pm:
                kw, arg = pm.group(1).lower(), pm.group(2).strip()
                task     = task[:pm.start()].strip()
                if kw == "file":
                    cm = re.match(r'^(\S+)\s+-n\s*(\d+|auto)?\s*$', arg, re.IGNORECASE)
                    if cm:
                        plan_steps = self._make_chunk_steps(cm.group(1), cm.group(2) or "auto", task)
                    else:
                        plan_steps, plan_context = _parse_file_arg(arg)
                else:
                    plan_steps, plan_context = _parse_plan_arg(arg)
        if self._vars and plan_steps:
            plan_steps = [_apply_params(s, self._vars) for s in plan_steps]
        if len(plan_steps) > max_turns:
            max_turns = len(plan_steps)

        tools = cfg["tools"] if cfg["tools"] is not None else self._load_agent_config().get("tools", DEFAULT_AGENT_TOOLS)
        tpl   = cfg["system"]
        tool_list     = get_help_list(tools)
        system_prompt = tpl.format(tool_list=tool_list) if "{tool_list}" in tpl else tpl
        system_msg    = {"role": "system", "content": system_prompt}

        desc = f"  {cfg['description']}" if cfg["description"] else ""
        print(f"[{name}]{desc}")
        print(f"[{name}] tools: {', '.join(tools)}")
        print(f"[{name}] max_turns: {max_turns}  auto_exec: {auto_exec}")
        if plan_steps:
            print(f"[{name}] plan ({len(plan_steps)} steps): {', '.join(plan_steps)}")
        print(f"[{name}] task: {task}\n")

        agent_msgs = [system_msg] + list(self.messages) + [{"role": "user", "content": task}]

        # apply agent-scoped vars, aliases, and procs for the duration of this run
        saved_vars    = dict(self._vars)
        saved_aliases = dict(self._aliases)
        saved_procs   = list(self._proc_active)
        saved_before  = list(self._proc_before)
        saved_gates   = list(self._proc_gates)
        saved_params  = dict(self.params)
        # resolve vars: {{task}} → task string, other {{name}} → existing session vars
        for k, v in cfg["vars"].items():
            resolved = v.replace("{{task}}", task)
            resolved = _apply_params(resolved, self._vars) if self._vars else resolved
            self._vars[k] = resolved
        self._aliases.update(cfg["aliases"])
        # apply agent-scoped model params (num_predict, temperature, …)
        saved_agent_ctx = self._agent_ctx
        if cfg["params"]:
            p = dict(cfg["params"])
            # agent_ctx sets the tempctx limit for this agent run
            if "agent_ctx" in p:
                self._agent_ctx = int(p.pop("agent_ctx"))
            # map num_predict → max_tokens for OpenAI-compatible providers
            if self.provider == "openai" and "num_predict" in p:
                p["max_tokens"] = p.pop("num_predict")
            self.params.update(p)
        # activate procs / before / gates — substitute vars into proc spec
        for p in cfg["procs"]:
            p_resolved = _apply_params(p, self._vars) if self._vars else p
            if p_resolved not in self._proc_active:
                self._proc_active.append(p_resolved)
        for p in cfg["before"]:
            p_resolved = _apply_params(p, self._vars) if self._vars else p
            if p_resolved not in self._proc_before:
                self._proc_before.append(p_resolved)
        for p in cfg["gates"]:
            p_resolved = _apply_params(p, self._vars) if self._vars else p
            if p_resolved not in self._proc_gates:
                self._proc_gates.append(p_resolved)
        on_done = _apply_params(cfg["on_done"], self._vars) if self._vars else cfg["on_done"]
        self._current_task = task
        try:
            self._run_agent_loop(name, agent_msgs, max_turns, auto_exec, auto_apply,
                                 plan_steps=plan_steps, plan_context=plan_context,
                                 on_done=on_done, use_procs=True)
        finally:
            self._current_task = ""
            self._vars        = saved_vars
            self._aliases     = saved_aliases
            self._proc_active = saved_procs
            self._proc_before = saved_before
            self._proc_gates  = saved_gates
            self.params       = saved_params
            self._agent_ctx   = saved_agent_ctx

    # ── /ask ───────────────────────────────────────────────────────────────────

    def _cmd_ask(self, user_input: str):
        """Read-only research agent for 4B models. Explores with tree/find/map, no file edits."""
        task = user_input[4:].strip()
        if not task:
            print("usage: /ask <question>")
            print("  Explores the project using read-only navigation tools.")
            print("  Best with 4B models: nemotron, qwen3:4b, ministral3:3b")
            return

        max_turns = 15
        while True:
            m = re.match(r'^-t\s+(\d+)\s*(.*)', task, re.DOTALL)
            if m:
                max_turns = int(m.group(1))
                task = m.group(2).strip()
                continue
            break

        tools      = DEFAULT_AGENT_TOOLS_ASK
        system_msg = {"role": "system", "content": AGENT_SYSTEM_ASK.format(tool_list=get_help_list(tools))}
        print(f"[ask] tools: {', '.join(tools)}")
        print(f"[ask] max_turns: {max_turns}  (read-only, auto-exec)")
        print(f"[ask] question: {task}\n")
        agent_msgs = [system_msg] + list(self.messages) + [{"role": "user", "content": task}]
        self._current_task = task
        try:
            self._run_agent_loop("ask", agent_msgs, max_turns,
                                 auto_exec=True, auto_apply=True)
        finally:
            self._current_task = ""

    def _cmd_agent(self, user_input: str):
        task = user_input[6:].strip()
        if not task:
            print("usage: /agent [-t N] [-y] <task> | /agent <name> [-t N] [-y] <task>")
            print("  configure: .1bcoder/agent.txt  |  .1bcoder/agents/<name>.txt")
            print("  /agent list  — show all available named agents")
            return

        if task.strip() == "list":
            seen = {}  # name → (label, description)
            for label, d in (("global", GLOBAL_AGENTS_DIR), ("local", AGENTS_DIR)):
                if not os.path.isdir(d):
                    continue
                for fname in sorted(os.listdir(d)):
                    if not fname.endswith(".txt"):
                        continue
                    name = fname[:-4]
                    desc = ""
                    try:
                        with open(os.path.join(d, fname), encoding="utf-8") as f:
                            for line in f:
                                ls = line.strip()
                                if ls.startswith("description"):
                                    _, _, v = ls.partition("=")
                                    desc = v.strip()
                                    break
                    except OSError:
                        pass
                    seen[name] = (label, desc)
            if not seen:
                print("[agent] no named agents found")
                return
            print("Named agents  (* = local project,  g = global ~/.1bcoder):\n")
            for name, (label, desc) in sorted(seen.items()):
                tag = "*" if label == "local" else "g"
                suffix = f"  —  {desc}" if desc else ""
                print(f"  [{tag}] /{name}{suffix}")
            print()
            return

        # check if first non-flag word is a named agent
        probe = task
        for _ in range(5):   # skip leading flags
            m = re.match(r'^(-y|-t\s+\d+)\s*(.*)', probe, re.DOTALL)
            if m:
                probe = m.group(2)
            else:
                break
        first_word = probe.split()[0] if probe.split() else ""
        agent_path = self._find_agent_def(first_word)
        if agent_path:
            task_rest = probe[len(first_word):].strip()
            # reattach any leading flags
            leading = task[:task.index(first_word)].strip()
            full_task = (leading + " " + task_rest).strip()
            self._run_named_agent(first_word, full_task, agent_path)
            return

        config     = self._load_agent_config()
        max_turns  = config["max_turns"]
        auto_apply = config["auto_apply"]

        # /agent advance → full toolset + advanced system prompt (strip before flag parsing)
        advanced = task.startswith("advance")
        if advanced:
            task = task[7:].strip()

        # parse flags: -t N, -y  (flags may appear anywhere in the string)
        auto_exec = False
        if re.search(r'(?:^|\s)-y(?:\s|$)', task):
            auto_exec = True
            task = re.sub(r'(?:^|\s)-y(?=\s|$)', ' ', task).strip()
        while True:
            m = re.match(r'^-t\s+(\d+)\s*(.*)', task, re.DOTALL)
            if m:
                max_turns = int(m.group(1))
                task      = m.group(2).strip()
                continue
            break

        # parse: /agent task description plan: step1, step2, step3
        # also accepts: /agent "task description" plan: step1, step2, step3
        # also accepts: /agent task file: steps.md  (load steps from file)
        # also accepts: /agent task file: large.md -n 10  (chunked scan)
        plan_steps = []
        plan_context = ""
        if task.startswith('"'):
            end_q = task.find('"', 1)
            if end_q != -1:
                rest = task[end_q + 1:].strip()
                task = task[1:end_q]
                pm = re.match(r'^(plan|file):\s*(.*)', rest, re.IGNORECASE | re.DOTALL)
                if pm:
                    kw, arg = pm.group(1).lower(), pm.group(2).strip()
                    if kw == "file":
                        cm = re.match(r'^(\S+)\s+-n\s*(\d+|auto)?\s*$', arg, re.IGNORECASE)
                        if cm:
                            plan_steps = self._make_chunk_steps(cm.group(1), cm.group(2) or "auto", task)
                        else:
                            plan_steps, plan_context = _parse_file_arg(arg)
                    else:
                        plan_steps, plan_context = _parse_plan_arg(arg)
        else:
            pm = re.search(r'\b(plan|file):\s*(.*)', task, re.IGNORECASE | re.DOTALL)
            if pm:
                kw, arg = pm.group(1).lower(), pm.group(2).strip()
                task = task[:pm.start()].strip()
                if kw == "file":
                    cm = re.match(r'^(\S+)\s+-n\s*(\d+|auto)?\s*$', arg, re.IGNORECASE)
                    if cm:
                        plan_steps = self._make_chunk_steps(cm.group(1), cm.group(2) or "auto", task)
                    else:
                        plan_steps, plan_context = _parse_file_arg(arg)
                else:
                    plan_steps, plan_context = _parse_plan_arg(arg)
        if self._vars and plan_steps:
            plan_steps = [_apply_params(s, self._vars) for s in plan_steps]
        if len(plan_steps) > max_turns:
            max_turns = len(plan_steps)
        total_plan = len(plan_steps)

        if advanced:
            tools = config.get("advanced_tools", DEFAULT_AGENT_TOOLS_ADVANCED)
            system_tpl = AGENT_SYSTEM_ADVANCED
            print(f"[agent] mode: advanced")
        else:
            tools = config.get("tools", DEFAULT_AGENT_TOOLS)
            system_tpl = AGENT_SYSTEM_BASIC

        tool_list     = get_help_list(tools)
        system_prompt = system_tpl.format(tool_list=tool_list)
        system_msg    = {"role": "system", "content": system_prompt}

        print(f"[agent] tools: {', '.join(tools)}")
        print(f"[agent] max_turns: {max_turns}  auto_apply: {auto_apply}  auto_exec: {auto_exec}")
        if plan_steps:
            print(f"[agent] plan ({total_plan} steps):")
            for _i, _s in enumerate(plan_steps, 1):
                print(f"  {_DIM}{_i}. {_s}{_R}")
        print(f"[agent] task: {task}\n")

        agent_msgs = [system_msg] + list(self.messages) + [{"role": "user", "content": task}]
        self._current_task = task
        try:
            self._run_agent_loop("agent", agent_msgs, max_turns, auto_exec, auto_apply,
                                 plan_steps=plan_steps, use_procs=True, plan_context=plan_context)
        finally:
            self._current_task = ""



# ── entry point ────────────────────────────────────────────────────────────────

def _load_startup_config() -> tuple[dict, str]:
    """Load the first config with auto: true (local → global).
    Returns (cfg_dict, config_path), or ({}, '') if none have auto: true."""
    for cfg_path in (CONFIG_FILE, GLOBAL_CONFIG_FILE):
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    cfg = CoderCLI._parse_config_yml(f.read())
                if cfg.get("auto"):
                    return cfg, cfg_path
            except OSError:
                pass
    return {}, ""


def _pick_model(models: list) -> str:
    """Interactively prompt user to pick a model by number. Exits on Ctrl+C."""
    while True:
        try:
            raw = input("Pick [1]: ").strip() or "1"
            idx = int(raw) - 1
            if 0 <= idx < len(models):
                return models[idx]
        except (ValueError, KeyboardInterrupt, EOFError):
            print()
            sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="1bcoder — AI coder for 1B models")
    parser.add_argument("--host", default=None,
                        help="Ollama host (default: from config.yml or http://localhost:11434)")
    parser.add_argument("--model",
                        help="Model name to use (skips selection prompt)")
    parser.add_argument("--init", action="store_true",
                        help="Create .1bcoder/scripts/ in current directory and run")
    parser.add_argument("--scriptapply", metavar="SCRIPT",
                        help="Run a script headlessly (no UI). "
                             "Path relative to .1bcoder/scripts/ or absolute.")
    parser.add_argument("--param", metavar="KEY=VALUE", action="append", default=[],
                        help="Script parameter substitution (repeatable). "
                             "e.g. --param file=calc.py --param range=1-4")
    parser.add_argument("-p", "--pipe", dest="print_prompt", nargs="?", const="",
                        metavar="PROMPT",
                        help="Non-interactive mode: send PROMPT and print response to stdout. "
                             "Reads from stdin if no prompt given. Enables pipeline use.")
    parser.add_argument("--ctx", metavar="FILE",
                        help="Load context from file before starting (e.g. myctx/ctx.txt)")
    args = parser.parse_args()
    _bootstrap_global_dir()

    if args.init:
        existed = os.path.isdir(BCODER_DIR)
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        os.makedirs(AGENTS_DIR, exist_ok=True)
        if existed:
            print(f".1bcoder already exists in {WORKDIR}")
        else:
            print(f"Initialized .1bcoder/scripts/ in {WORKDIR}")

    explicit_host  = args.host  is not None
    explicit_model = args.model is not None

    if args.scriptapply:
        script = args.scriptapply
        if not os.path.isabs(script):
            local_path  = os.path.join(SCRIPTS_DIR, script)
            global_path = os.path.join(GLOBAL_SCRIPTS_DIR, script)
            if os.path.isfile(local_path):
                script = local_path
            elif os.path.isfile(global_path):
                script = global_path
            else:
                script = local_path  # will produce clear error below
        if not os.path.exists(script):
            print(f"Script not found: {script}")
            sys.exit(1)
        cfg, _ = ({}, "") if (explicit_host and explicit_model) else _load_startup_config()
        host_str = args.host if explicit_host else (cfg.get("host") or "http://localhost:11434")
        base_url, provider = parse_host(host_str)
        model = args.model or cfg.get("model") or ""
        models = []
        if not model:
            try:
                models = list_models(base_url, provider)
                model = models[0]
                print(f"[model: {model}]")
            except Exception as e:
                print(f"Cannot connect to {base_url}: {e}")
                sys.exit(1)
        params = {}
        for p in args.param:
            key, _, value = p.partition("=")
            if key:
                params[key.strip()] = value.strip()
        cli = CoderCLI(base_url, model, models, provider)
        # reset script — remove [v] markers so every headless run starts fresh
        lines = _load_script(script)
        reset = [re.sub(r'^\[v\]\s*', '', l) for l in lines]
        if reset != lines:
            _save_script(reset, script)
        param_tokens = " ".join(f"{k}={v}" for k, v in params.items())
        script_fwd = script.replace("\\", "/")   # shlex.split strips backslashes on Windows
        cli._cmd_script(f"/script apply -y {script_fwd} {param_tokens}".strip())
        sys.exit(0)

    # ── load config unless both host and model were explicitly given ──────────
    cfg, cfg_path = ({}, "") if (explicit_host and explicit_model) else _load_startup_config()

    # ── resolve host ──────────────────────────────────────────────────────────
    host_str = args.host if explicit_host else (cfg.get("host") or "http://localhost:11434")
    base_url, provider = parse_host(host_str)

    # ── connect ───────────────────────────────────────────────────────────────
    try:
        models = list_models(base_url, provider)
    except requests.exceptions.ConnectionError:
        print(f"Cannot connect to {base_url}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        _err(e)
        sys.exit(1)

    if not models:
        print("No models available. Run: ollama pull <model>")
        sys.exit(1)

    # ── resolve model ─────────────────────────────────────────────────────────
    desired = args.model if explicit_model else cfg.get("model")
    if desired and desired in models:
        model = desired
    elif desired:
        print(f"Model '{desired}' not available on {base_url}.")
        print("Available models:")
        for i, m in enumerate(models, 1):
            print(f"  {i}. {m}")
        model = _pick_model(models)
    elif len(models) == 1:
        model = models[0]
        print(f"Model: {model}")
    else:
        print("Available models:")
        for i, m in enumerate(models, 1):
            print(f"  {i}. {m}")
        model = _pick_model(models)

    if args.print_prompt is not None:
        cli = CoderCLI(base_url, model, models, provider)
        if cfg.get("role"):
            cli._role = cfg["role"]
        if args.ctx:
            cli._cmd_ctx(f"/ctx load {args.ctx}")

        prompt = args.print_prompt
        if not sys.stdin.isatty():
            stdin_data = sys.stdin.read()
            prompt = (stdin_data + "\n\n" + prompt).strip() if prompt else stdin_data

        if not prompt:
            print("error: no prompt — pass text after -p or pipe via stdin", file=sys.stderr)
            sys.exit(1)

        cli.messages = [{"role": "user", "content": prompt}]
        cli._stream_chat(cli.messages)
        print()
        sys.exit(0)
    else:
        cli = CoderCLI(base_url, model, models, provider)
        if args.ctx:
            cli._cmd_ctx(f"/ctx load {args.ctx}")
        cli.run()


if __name__ == "__main__":
    main()
