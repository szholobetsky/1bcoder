# 1bcoder — Post-processor & Guard Reference (`/proc`)

Post-processors are Python scripts that receive the last LLM reply on `stdin` and do something with it — extract data, render it, validate it, or trigger follow-up commands.

Guards are processors that output `ALERT:` or `BLOCK:` lines to warn the user or cancel a command. They live in the same `proc/` directory and use the same protocol.

```
/proc run <name>      One-shot: run against last reply, print result
/proc on  <name>      Persistent: run automatically after every reply
/proc off             Stop the active persistent processor
/proc list            List available processors and guards
/proc new <name>      Create a new processor from template
```

---

## Protocol

| Stream | Content |
|---|---|
| `stdin` | Full last LLM reply (UTF-8) — or command string when used as hook |
| `stdout` | Result shown in terminal; injected into context if user confirms |
| `stderr` | Warning shown in terminal; does not affect context |
| Exit code | 0 = success · non-zero = failure (stderr shown, ACTION skipped) |

**Special stdout lines:**

| Line | Effect |
|---|---|
| `key=value` | Extracted as session variable (readable with `/var set name key`) |
| `ACTION: /command` | Shown to user for confirmation, then executed (one-shot mode only) |
| `ALERT: message` | Warning printed in yellow — continues normally |
| `BLOCK: reason` | Printed in red — cancels the triggering command (hook mode only) |

---

## Built-in processors

### `extract-files`
Finds filenames mentioned in the reply. If exactly one is found, emits `ACTION: /read <file>` so you can load it into context immediately.

```
/proc run extract-files
```

### `extract-code`
Extracts fenced code blocks. If one block and one filename are found, emits `ACTION: /save <file>`.

```
/proc run extract-code
```

### `extract-list`
Converts the first bullet or numbered list in the reply to a comma-separated string. Useful for feeding lists into `/var`.

```
/proc run extract-list
/var set items first
```

### `regexp-extract`
Extracts regex matches from the reply. Supports capture groups, case-insensitive matching, and deduplication.

```
/proc run regexp-extract <pattern> [-g N] [-i] [-u]

  -g N   use capture group N instead of full match
  -i     case-insensitive
  -u     deduplicate results

# Examples
/proc run regexp-extract \b[0-9]{3}\b
/proc run regexp-extract "def (\w+)\(" -g 1 -u
/proc run regexp-extract [\w./\\-]+\.py -u
```

### `grounding-check`
Scores identifiers in the reply against the project's `map.txt`. Warns if less than 50% of identifiers are found in the map — useful for catching hallucinated function/class names.

```
/proc on grounding-check      # run after every reply
/proc off
```

### `collect-files`
Accumulates filenames mentioned across multiple replies into `.1bcoder/collected-files.txt`. Useful for building a reading list during a multi-turn exploration session.

```
/proc on collect-files
```

### `add-save`
Appends the last code block to a growing output file. Designed for the `/agent -y` automated accumulation workflow.

```
/proc on add-save
/agent -y implement all functions in plan.txt
```

### `md`
Renders the last reply as formatted Markdown in the terminal using `rich`. Tables, code blocks, bold/italic are all styled.

```
/proc run md
```

### `mdx`
Renders the last reply as a full-featured HTML page opened in the default browser. Supports:
- Markdown (via marked.js)
- LaTeX formulas — `$inline$` and `$$display$$` (via KaTeX)
- Mermaid diagrams — `graph`, `flowchart`, `sequenceDiagram`, etc.

```
/proc run mdx
```

---

## Built-in guards

Guards use the same protocol as processors but output `ALERT:` or `BLOCK:` lines. They can be used persistently (scan every reply) or as hooks (intercept commands).

### `ctx_cut`
Auto-runs `/ctx cut` when context usage exceeds a threshold. Prevents context overflow during long sessions.

```
/proc on ctx_cut          # default threshold: 90%
/proc on ctx_cut 80       # custom threshold %
```

Reads `BCODER_CTX_PCT` environment variable injected by 1bcoder.

### `rude_words`
Alerts if the LLM reply contains profanity. Useful when working in shared environments or recording sessions.

```
/proc on rude_words        # English word list
/proc on rude_words ua     # + Ukrainian word list
```

### `secret_check`
Alerts if the reply contains sensitive company names or custom keywords. Useful when working on client projects where certain names should not appear in saved output.

```
/proc on secret_check                       # default: google, microsoft, anthropic, openai…
/proc on secret_check client=acme,invoice   # + custom keywords (comma-separated)
```

### `sql_readonly_guard`
Detects write SQL statements. Behaviour depends on how it is invoked:

| Mode | Trigger | `DELETE/DROP/ALTER` | `UPDATE/INSERT` |
|---|---|---|---|
| `/hook before run` | command string | `BLOCK:` | `ALERT:` |
| `/proc on` | LLM reply text | `ALERT:` | `ALERT:` |

```
/hook before run sql_readonly_guard.py    # block /run with destructive SQL
/proc on sql_readonly_guard               # alert if reply suggests write SQL
```

---

## Writing your own processor or guard

```
/proc new my-proc      # creates .1bcoder/proc/my-proc.py from template
```

Minimal processor:

```python
import sys

reply = sys.stdin.read()

result = reply.upper()
print(result)                              # shown in terminal
print("char_count=" + str(len(reply)))     # extracted as session variable
# print("ACTION: /read some-file")         # triggers follow-up command
```

Minimal guard:

```python
import sys, re

text = sys.stdin.read()

if re.search(r'password\s*=', text, re.IGNORECASE):
    print("ALERT: reply may contain a hardcoded password")
```

Hook guard that blocks:

```python
import sys, os

cmd = sys.stdin.read()
if "rm -rf" in cmd:
    print("BLOCK: dangerous rm -rf detected")
```

Use `BCODER_EVENT` env var to detect whether you are running as a hook or a proc:

```python
import os
is_hook = bool(os.environ.get("BCODER_EVENT"))
```

Available env vars injected by 1bcoder:

| Variable | Value |
|---|---|
| `BCODER_EVENT` | Hook event name (e.g. `before_run`) — empty when run as proc |
| `BCODER_CTX_PCT` | Context usage as integer percent (e.g. `85`) |
| `BCODER_CTX_USED` | Estimated tokens used |
| `BCODER_CTX_MAX` | Context window size |
| `BCODER_FILE` | File argument of the triggering command (hook only) |
| `BCODER_RANGE` | Line range of the triggering command (hook only) |

Processors live in:
- `~/.1bcoder/proc/` — global (available in all projects)
- `.1bcoder/proc/` — local (project-specific, checked first)

---

## Capture output into a variable

Any proc result can be captured with `->`:

```
/proc run extract-list -> items
/ask what do you know about {{items}}?
```
