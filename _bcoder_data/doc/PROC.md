# 1bcoder — Post-processor Reference (`/proc`)

Post-processors are Python scripts that receive the last LLM reply on `stdin` and do something with it — extract data, render it, validate it, or trigger follow-up commands.

```
/proc run <name>      One-shot: run against last reply, print result
/proc on  <name>      Persistent: run automatically after every reply
/proc off             Stop the active persistent processor
/proc list            List available processors
/proc new <name>      Create a new processor from template
```

---

## Protocol

| Stream | Content |
|---|---|
| `stdin` | Full last LLM reply (UTF-8) |
| `stdout` | Result shown in terminal; injected into context if user confirms |
| `stderr` | Warning shown in terminal; does not affect context |
| Exit code | 0 = success · non-zero = failure (stderr shown, ACTION skipped) |

**Special stdout conventions:**

- `key=value` lines — extracted as session variables (readable with `/var set name key`)
- `ACTION: /command` — shown to user for confirmation, then executed (one-shot mode only)

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
Renders the last reply as formatted Markdown in the terminal using `rich`. Tables, code blocks, bold/italic are all styled. Requires `pip install rich` (included in `requirements.txt`).

```
/proc run md
```

### `mdx`
Renders the last reply as a full-featured HTML page opened in the default browser. Supports:
- Markdown (via marked.js)
- LaTeX formulas — `$inline$` and `$$display$$` (via KaTeX)
- Mermaid diagrams — `graph`, `flowchart`, `sequenceDiagram`, etc.
- Auto-fixes common LLM Mermaid errors (multi-word node names with spaces)

Requires internet access to load CDN scripts on first use.

```
/proc run mdx
```

---

## Writing your own processor

```
/proc new my-proc      # creates .1bcoder/proc/my-proc.py from template
```

Minimal processor:

```python
import sys

reply = sys.stdin.buffer.read().decode("utf-8", errors="replace")

# do something with reply
result = reply.upper()

print(result)                    # shown in terminal
print("char_count=" + str(len(reply)))   # extracted as session variable
# print("ACTION: /read some-file")       # triggers follow-up command
```

Processors live in:
- `<install>/.1bcoder/proc/` — global (available in all projects)
- `.1bcoder/proc/` — local (overrides global for the current project)

---

## Capture output into a variable

Any proc result can be captured with `->`:

```
/proc run extract-list -> items
/ask what do you know about {{items}}?
```
