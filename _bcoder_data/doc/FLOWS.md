# Writing custom flows

A **flow** is a Python file in `~/.1bcoder/flows/` (or `.1bcoder/flows/` for project-local).
Run with `/flow <name> [args]`. List with `/flow list`.

## Minimal template

```python
"""One-line description shown by /flow list. Usage: /flow myflow <args>"""


def run(chat, args: str):
    # 1. validate args
    if not args.strip():
        print("usage: /flow myflow <something>")
        return

    # 2. collect data
    data = chat._agent_exec("/some command", auto_apply=True)

    # 3. ask LLM in temp context
    temp_msgs = [{"role": "system", "content": chat._role},
                 {"role": "user",   "content": f"Your question:\n{data}"}]
    chat._sep("AI")
    reply = chat._stream_chat(temp_msgs)

    # 4. inject only summary into main context
    if reply:
        chat.last_reply   = reply
        chat._last_output = reply
        chat.messages.append({"role": "user",      "content": "[myflow: label]"})
        chat.messages.append({"role": "assistant", "content": reply})
```

## The four parts every flow has

### 1. Validate args
Check required input early and print usage if missing.

### 2. Collect data
Run commands, read files, call external tools — build the raw material for the LLM.
The key rule: **collect as little as needed**. Prefer narrow searches first, fall back to broad ones.

```python
# narrow first, stop when you have something
result = chat._agent_exec("/map find MyClass -d 2", auto_apply=True)
if not result or "no matches" in result:
    result = chat._agent_exec("/find MyClass -f", auto_apply=True)
```

### 3. Ask LLM in temporary context
Always use `temp_msgs` — never append raw collected data to `chat.messages`.
Raw data stays in temp context and is discarded after the LLM responds.

```python
temp_msgs = [{"role": "system", "content": chat._role},
             {"role": "user",   "content": prompt}]
reply = chat._stream_chat(temp_msgs)
```

### 4. Inject only the summary
After the LLM replies, add a short label + the reply to main context.
The raw data (which may be thousands of chars) never pollutes the conversation.

```python
chat.messages.append({"role": "user",      "content": "[myflow: label]"})
chat.messages.append({"role": "assistant", "content": reply})
```

## chat object — what you can use

| API | Description |
|---|---|
| `chat._agent_exec(cmd, auto_apply=True)` | Run any `/command`, return output as string |
| `chat._stream_chat(messages)` | Send messages to LLM, stream output, return reply |
| `chat._sep("AI")` | Print the `─── AI ───` separator before streaming |
| `chat._role` | Current system persona string |
| `chat.messages` | Main conversation history (append summary here) |
| `chat.last_reply` | Last LLM reply — set this after your LLM call |
| `chat._last_output` | Last output shown to user — set same as last_reply |
| `chat._vars` | Session variables dict — read/write `{{varname}}` values |
| `chat._web_ddg_search(term, n=8)` | DuckDuckGo search → list of (title, url, snippet) |
| `chat._web_strip_html(bytes)` | Strip HTML tags from fetched page bytes |

## Parsing args

For simple flags use `re.search`:

```python
import re as _re

n = 5
m = _re.search(r"-n\s+(\d+)", args)
if m:
    n = int(m.group(1))
    args = (args[:m.start()] + args[m.end():]).strip()
```

For file input with `-f`:

```python
m = _re.match(r"-f\s+(\S+)", args.strip())
if m:
    with open(m.group(1), encoding="utf-8") as f:
        content = f.read()
```

## Standard flags — use these names, don't invent new ones

Flows are independent files (no shared arg-parser), but by convention they
converge on the same flag names via copy-paste. A new flow should reuse
these, not invent `file:`/`ctx:`-style bare keywords — that was the old
style (still seen in `deepagent.py`'s `plan:`/`list:`/`file:` and in
`creator.py`/`creator_plan.py`'s `file:`/`files:`/`test:`) before the rest
of the flows converged on `--flag value`.

| Flag | Meaning | Seen in |
|---|---|---|
| `--ctx N` | inject the last N main-session user/assistant turns (see `_serialize_ctx` below) | `interview`, `deepagent_code`, `deepagent_spec`, `deepagent_md`, `deepagent_tree`, `deepagent_tree_md` |
| `--ctx-worker N` | separate `--ctx` budget for `/parallel` workers (default: same as `--ctx`) | `deepagent_md`, `deepagent_tree`, `deepagent_tree_md` |
| `--file <path>` | seed context with one file's content | `interview`, `deepagent_code`, `deepagent_md`, `deepagent_tree`, `deepagent_tree_md` |
| `--model <name>` | switch model for this run only | `ctxtimer`, `history`, `creator`, `creator_plan` |
| `--host <host:port>` | Ollama host override | `history` |
| `--profile <name>` | `/parallel` profile — fan out across workers | `deepagent_test`, `deepagent_translate`, `deepagent_spec`, `deepagent_code`, `deepagent_md`, `deepagent_tree`, `deepagent_tree_md`, `external_help` |
| `--out <path>` / `--out ctx` | write result to a file, or inject into conversation context instead | `interview` |
| `--output <path>` | write result into an existing directory | `deepagent_spec` |
| `--web [N]` | inject DDG search results, N pages | `deepagent_md`, `deepagent_tree`, `deepagent_tree_md` |
| `--rag [name]` / `--rag-store <path>` | inject simargl RAG store results | `deepagent_md`, `deepagent_tree`, `deepagent_tree_md`, `deepagent_code --ask` |
| `--maxdepth N` | recursion/tree depth | `deepagent_md`, `deepagent_tree`, `deepagent_tree_md` |
| `--think` | two-pass: plan in text, then act | `deepagent_code`, `deepagent_test` |
| `--syntax` | check syntax after generation, retry on error | `deepagent_code`, `deepagent_test` |
| `--retries N` | cap correction retries | `deepagent_test` |
| `--focus "<text>"` | free-text priority/emphasis note | `deepagent_spec` |
| `--predictable` | fill in low-temperature/fixed-length params if unset | `deepagent_spec` |
| `--apply` | actually write (default is dry-run) | `deepagent_merge` |
| `--chunk N` / `--overlap N` | chunking size / sliding overlap in tokens | `glossary`, `deepagent_translate` |

### `--ctx N` — pull in what just happened in the main session

`run(chat, args)` gets the *same* `chat` object the main REPL uses (see
`_cmd_flow` in chat.py — it's a plain `mod.run(self, args)` call), so
`chat.messages` at call time already holds whatever happened earlier this
session — including a file you `/read` right before invoking the flow. A
flow only sees that if it explicitly pulls it in; `--ctx N` is the
established way to opt into that, default 0 (some of the `deepagent_*`
family default it to 6 instead — pick whichever fits the flow).

Duplicate this per-file, same as everything else here — don't import it:

```python
def _serialize_ctx(messages: list, n: int) -> str:
    """Serialize last N user/assistant messages as a readable block for injection."""
    if not messages or n == 0:
        return ""
    recent = [m for m in messages if m.get("role") in ("user", "assistant")][-n:]
    if not recent:
        return ""
    lines = ["[Conversation context]"]
    for m in recent:
        role = "User" if m["role"] == "user" else "Assistant"
        lines.append(f"{role}: {m.get('content', '')[:800]}")
    return "\n".join(lines) + "\n"
```

```python
ctx_n = 0
m = _re.search(r'--ctx\s+(\d+)', args)
if m:
    ctx_n = int(m.group(1))
    args = (args[:m.start()] + args[m.end():]).strip()

chat_ctx = _serialize_ctx(getattr(chat, "messages", []), ctx_n)
```

### `--file <path>` — seed one file, quoted-path safe

```python
m = _re.search(r'--file\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))', args)
if m:
    task_file = m.group(1) or m.group(2) or m.group(3)
    args = (args[:m.start()] + args[m.end():]).strip()
```

## Prompt tips for small models (1b–3b)

- Put the data **before** the question, not after
- One direct question only — no rule lists
- Short is better: `"Diff:\n{diff}\n\nWrite a one-line commit message."` beats ten bullet points

## File location priority

1. `.1bcoder/flows/` — project-local (highest priority, overrides others)
2. `~/.1bcoder/flows/` — user global
3. `_bcoder_data/flows/` — built-in defaults

## Built-in flows as reference

| Flow | Pattern |
|---|---|
| `webask` | external API (DDG) → loop fetch → temp LLM |
| `grounding` | 1bcoder command → parse list → progressive loop search → temp LLM |
| `simargl_files` | external tool via `/run` → parse list → loop `/read` → temp LLM |
| `py_error_trace` | parse input text → loop file read at line → temp LLM |
| `commit_message` | subprocess (git) → temp LLM |
| `interview` | `--ctx N` + `--file`, smallest example of both standard flags |
| `deepagent_code` | `--ctx N` + `--file` + `--profile` fan-out, full flag vocabulary |
| `deepagent_md` / `deepagent_tree` / `deepagent_tree_md` | `--ctx`/`--ctx-worker` split for `/parallel` workers, `--web`/`--rag` source injection |
