"""Recursive code decomposition into a tree of small functions.

Adapts deepagent_md's BFS expansion for code: levels 0..N-1 produce
function skeletons, level N produces implementations. Each function
is a separate file. An index.md maps the tree.

Usage:
  /flow deepagent_code "Budget management CRUD for SQLite" --lang py
  /flow deepagent_code "REST API client" --lang py --depth 2
  /flow deepagent_code "TODO manager" --lang js --depth 1 --profile local
  /flow deepagent_code "Calculator" --lang py --depth 1 --join
  /flow deepagent_code "REST API" --lang py --depth 2 --think
  /flow deepagent_code "CSV tool" --lang py --depth 2 --think --test
  /flow deepagent_code --file task.txt --lang py --depth 2 --think
  /flow deepagent_code "short desc" --file requirements.txt --lang py --depth 2
  /flow deepagent_code "Syryn BT beacon" --lang py --depth 2 --think --ask --web 1
  /flow deepagent_code "My app" --lang py --depth 2 --ask --rag my_project
  /flow deepagent_code join code1              ← join existing dir
  /flow deepagent_code join code1 --lang py

Flags:
  --lang py|js      target language (required)
  --depth N         decomposition depth (default 2)
  --file path       read task description from a text file
  --profile name    /parallel profile for multi-worker BFS
  --join            produce joined file after generation
  --think           two-pass: plan in text first, then write code
  --test            generate unit test for each function (mock helpers)
  --syntax          check syntax after generation, retry on error (max 2)
  --distinct        skip functions with names already generated in other branches
  --ctx N           conversation context messages (default 0)
  --ask             enrich each leaf with API call examples before generation
  --web N           fetch top N DDG results for external calls (default 1, requires --ask)
  --rag project     simargl RAG project name for internal calls (requires --ask)
"""
import os as _os
import re as _re


# ── language config ──────────────────────────────────────────────────────────

LANGS = {
    # scripting / interpreted
    "py":     {"ext": ".py",    "comment": "#",  "syntax_cmd": None,
               "func_re": r'(?:def)\s+(\w+)'},
    "js":     {"ext": ".js",    "comment": "//", "syntax_cmd": "node --check {file}",
               "func_re": r'(?:function)\s+(\w+)|(?:const|let|var)\s+(\w+)\s*='},
    "ts":     {"ext": ".ts",    "comment": "//", "syntax_cmd": "npx tsc --noEmit --allowJs {file}",
               "func_re": r'(?:function)\s+(\w+)|(?:const|let|var)\s+(\w+)\s*='},
    "rb":     {"ext": ".rb",    "comment": "#",  "syntax_cmd": "ruby -c {file}",
               "func_re": r'(?:def)\s+(\w+)'},
    "php":    {"ext": ".php",   "comment": "//", "syntax_cmd": "php -l {file}",
               "func_re": r'(?:function)\s+(\w+)'},
    # compiled / JVM
    "go":     {"ext": ".go",    "comment": "//", "syntax_cmd": "go vet {file}",
               "func_re": r'(?:func)\s+(\w+)'},
    "java":   {"ext": ".java",  "comment": "//", "syntax_cmd": "javac -d /tmp {file}",
               "func_re": r'(?:public|private|protected|static|\s)+\s+\w+\s+(\w+)\s*\('},
    "kt":     {"ext": ".kt",    "comment": "//", "syntax_cmd": "kotlinc -script {file}",
               "func_re": r'(?:fun)\s+(\w+)'},
    "scala":  {"ext": ".scala", "comment": "//", "syntax_cmd": "scalac {file}",
               "func_re": r'(?:def)\s+(\w+)'},
    # SQL / database
    "sql":    {"ext": ".sql",   "comment": "--", "syntax_cmd": None,
               "func_re": r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|FUNCTION|PROCEDURE|TRIGGER|VIEW)\s+(\w+)'},
    "plsql":  {"ext": ".sql",   "comment": "--", "syntax_cmd": None,
               "func_re": r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|FUNCTION|PROCEDURE|TRIGGER|VIEW|PACKAGE)\s+(\w+)'},
    "mysql":  {"ext": ".sql",   "comment": "--", "syntax_cmd": None,
               "func_re": r'CREATE\s+(?:TABLE|FUNCTION|PROCEDURE|TRIGGER|VIEW)\s+(\w+)'},
}

_INTERNAL_PARAMS = {"timeout", "num_ctx", "think_exclude", "ask_limit",
                    "ask_show", "run_timeout", "log", "keep_alive"}


def _load_lang_prompt(lang: str, kind: str) -> str:
    """Load prompt from _bcoder_data/deepagent_code/{lang}_{kind}.txt."""
    here = _os.path.dirname(_os.path.abspath(__file__))
    base = _os.path.join(_os.path.dirname(here), "deepagent_code")
    path = _os.path.join(base, f"{lang}_{kind}.txt")
    if _os.path.isfile(path):
        return open(path, encoding="utf-8").read().strip()
    return _DEFAULT_PROMPTS.get(kind, "You are a programmer.")


_DEFAULT_PROMPTS = {
    "decompose": (
        "You are decomposing a function into sub-functions.\n\n"
        "RULES:\n"
        "- Write ONLY function calls with comments. Do NOT write any logic, loops, or if statements.\n"
        "- Do NOT implement the sub-functions. Just call them.\n"
        "- Each line must be: result = function_name(args)  # what it does\n"
        "- Use descriptive names. No step_1(), do_stuff(), process_data().\n"
        "- Output ONLY the code in a single code fence.\n\n"
        "EXAMPLE — if asked to decompose 'process user registration':\n\n"
        "```\n"
        "def process_registration(email, password):\n"
        "    validated = validate_input(email, password)  # check email format and password strength\n"
        "    user = create_user_record(validated)  # insert new user into database\n"
        "    token = generate_confirmation_token(user)  # create email confirmation token\n"
        "    send_confirmation_email(user, token)  # send email with confirmation link\n"
        "    return user\n"
        "```\n\n"
        "Now decompose the function described below. Follow the EXACT same pattern."
    ),
    "implement": (
        "You are implementing a single function.\n"
        "Write ONLY the function with its body. Keep it to 5-15 lines.\n"
        "Do not call undefined helper functions.\n"
        "Output ONLY the code in a single code fence."
    ),
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_code_dir(base: str) -> str:
    n = 1
    while True:
        d = _os.path.join(base, f"code{n}")
        if not _os.path.exists(d):
            _os.makedirs(d)
            return d
        n += 1


def _node_id_to_prefix(node_id: str) -> str:
    return node_id.replace(".", "-")


def _code_path(code_dir: str, node_id: str, name: str, ext: str) -> str:
    prefix = _node_id_to_prefix(node_id)
    safe = _re.sub(r'[^\w]', '_', name)
    return _os.path.join(code_dir, f"{prefix}-{safe}{ext}")


def _extract_code(text: str) -> tuple:
    """Split model output into (code_from_fences, text_outside_fences)."""
    blocks = _re.findall(r'```[^\n]*\n(.*?)```', text, _re.DOTALL)
    if blocks:
        code = "\n\n".join(b.strip() for b in blocks)
        outside = _re.sub(r'```[^\n]*\n.*?```', '', text, flags=_re.DOTALL).strip()
        return code, outside
    return text.strip(), ""


def _parse_skeleton(content: str, lang: str = "py") -> list:
    """Extract sub-function calls or SQL objects from a skeleton.

    For code languages: looks for func_name(args) # description
    For SQL languages: looks for CREATE TABLE/FUNCTION/PROCEDURE/TRIGGER/VIEW name

    Returns: [(name, call_expr, description), ...]
    """
    if lang in ("sql", "plsql", "mysql"):
        return _parse_skeleton_sql(content)
    results = []
    seen = set()
    _builtins = {"print", "return", "import", "open", "close", "len",
                 "str", "int", "float", "list", "dict", "set", "tuple",
                 "range", "enumerate", "isinstance", "type", "super",
                 "self", "True", "False", "None", "not", "and", "or",
                 "if", "else", "for", "while", "break", "continue",
                 "raise", "try", "except", "finally", "with", "as",
                 "assert", "yield", "lambda", "pass", "del", "in",
                 "round", "abs", "min", "max", "sum", "sorted", "map",
                 "filter", "zip", "any", "all", "next", "iter", "hash",
                 "id", "repr", "hex", "oct", "bin", "chr", "ord",
                 "hasattr", "getattr", "setattr", "delattr", "callable",
                 "classmethod", "staticmethod", "property",
                 "ValueError", "TypeError", "KeyError", "IndexError",
                 "AttributeError", "RuntimeError", "Exception",
                 "FileNotFoundError", "IOError", "OSError",
                 "pd", "np", "os", "sys", "re", "json", "csv",
                 "datetime", "timedelta", "defaultdict"}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("def ") or line.startswith("return"):
            continue
        # with comment
        m = _re.match(
            r'(?:\w+\s*=\s*)?(\w+)\s*(\([^)]*\))\s*#\s*(.+)',
            line
        )
        if m:
            name, args, desc = m.group(1), m.group(2), m.group(3).strip()
        else:
            # without comment
            m = _re.match(
                r'(?:\w+\s*=\s*)?(\w+)\s*(\([^)]*\))',
                line
            )
            if m:
                name = m.group(1)
                args = m.group(2)
                desc = name.replace("_", " ")
            else:
                continue
        call_expr = f"{name}{args}"
        if name not in seen and name.lower() not in _builtins:
            seen.add(name)
            results.append((name, call_expr, desc))
    return results


def _parse_skeleton_sql(content: str) -> list:
    """Extract database objects from SQL: both CREATE stubs and referenced objects.

    Finds:
    - CREATE TABLE/FUNCTION/PROCEDURE/TRIGGER/VIEW (explicit stubs)
    - FROM/JOIN table_name (referenced tables)
    - function_name() calls in SELECT/WHERE/HAVING (referenced functions)
    - Comments above CREATE or -- Uses: hints
    """
    results = []
    seen = set()
    _sql_keywords = {"select", "from", "where", "join", "left", "right",
                     "inner", "outer", "on", "and", "or", "not", "in",
                     "exists", "between", "like", "is", "null", "as",
                     "group", "by", "having", "order", "limit", "offset",
                     "insert", "into", "values", "update", "set", "delete",
                     "create", "alter", "drop", "index", "table", "view",
                     "function", "procedure", "trigger", "replace",
                     "begin", "end", "if", "then", "else", "case", "when",
                     "true", "false", "avg", "sum", "count", "min", "max",
                     "distinct", "union", "all", "asc", "desc", "with",
                     "returns", "return", "language", "declare", "each",
                     "row", "after", "before", "for", "execute", "new",
                     "old", "delimiter", "primary", "key", "references",
                     "foreign", "check", "default", "not", "null",
                     "serial", "int", "integer", "varchar", "text",
                     "boolean", "timestamp", "date", "numeric", "float",
                     "double", "precision", "auto_increment", "identity"}
    lines = content.splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip()
        # CREATE statements
        m = _re.match(
            r'CREATE\s+(?:OR\s+REPLACE\s+)?'
            r'(TABLE|FUNCTION|PROCEDURE|TRIGGER|VIEW|PACKAGE)\s+'
            r'(\w+)',
            stripped, _re.IGNORECASE
        )
        if m:
            obj_type = m.group(1).lower()
            name = m.group(2)
            desc = ""
            if i > 0 and lines[i - 1].strip().startswith("--"):
                desc = lines[i - 1].strip().lstrip("-").strip()
            if not desc:
                desc = f"{obj_type} {name}"
            if name.lower() not in _sql_keywords and name not in seen:
                seen.add(name)
                results.append((name, f"CREATE {obj_type.upper()} {name}", desc))
            continue

        # skip comment-only lines for reference extraction
        if stripped.startswith("--"):
            continue

        # FROM / JOIN table references
        for tm in _re.finditer(r'(?:FROM|JOIN)\s+(\w+)', stripped, _re.IGNORECASE):
            name = tm.group(1)
            if name.lower() not in _sql_keywords and name not in seen:
                seen.add(name)
                results.append((name, f"CREATE TABLE {name}", f"table {name}"))

        # function() calls (word followed by parentheses, not SQL keywords)
        for fm in _re.finditer(r'(\w+)\s*\(', stripped):
            name = fm.group(1)
            if name.lower() not in _sql_keywords and name not in seen:
                seen.add(name)
                desc = ""
                if i > 0 and lines[i - 1].strip().startswith("--"):
                    desc = lines[i - 1].strip().lstrip("-").strip()
                if not desc:
                    desc = f"function {name}"
                results.append((name, f"CREATE FUNCTION {name}", desc))

    return results


def _extract_signature(content: str) -> str:
    """Extract the def line from code content."""
    for line in content.splitlines():
        if line.strip().startswith("def "):
            return line.strip().rstrip(":")
    return ""


def _syntax_check(fpath: str, lang_cfg: dict) -> str:
    """Check syntax of a generated file. Returns error message or empty string."""
    import subprocess
    ext = lang_cfg["ext"]
    if ext == ".py":
        try:
            import ast
            with open(fpath, encoding="utf-8") as f:
                ast.parse(f.read())
            return ""
        except SyntaxError as e:
            return f"SyntaxError: line {e.lineno}: {e.msg}"
    cmd = lang_cfg.get("syntax_cmd")
    if not cmd:
        return ""
    try:
        result = subprocess.run(
            cmd.format(file=fpath), shell=True,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return ""
        return (result.stderr or result.stdout or "syntax error").strip()[:500]
    except FileNotFoundError:
        return ""
    except Exception as e:
        return str(e)[:200]


# ── interrupt handler ────────────────────────────────────────────────────────

class _StopGeneration(Exception):
    pass


def _on_interrupt(node_name: str) -> str:
    """Called on Ctrl+C. Returns 'continue', 'hint:...', or 'quit'."""
    try:
        ans = input(
            f'\n  [Enter] skip this node   '
            f'h <hint> = retry with hint   '
            f'q = stop and save: '
        ).strip()
    except (EOFError, KeyboardInterrupt):
        ans = 'q'

    if ans.lower() == 'q':
        print('[deepagent_code] stopped — files saved')
        return 'quit'
    if ans.lower().startswith('h '):
        hint = ans[2:].strip()
        if hint:
            print(f'  [hint] retrying {node_name} with: {hint}')
            return f'hint:{hint}'
    print(f'  [skip] {node_name}')
    return 'continue'


# ── generation ───────────────────────────────────────────────────────────────

def _generate_local(chat, system_prompt: str, user_prompt: str) -> str:
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]
    result = chat._stream_chat(msgs)
    if result is None:
        return None
    return result or ""


def _serialize_ctx(messages: list, n: int) -> str:
    """Serialize last N user/assistant messages as a readable block for injection."""
    if not messages or n == 0:
        return ""
    recent = [m for m in messages if m.get("role") in ("user", "assistant")][-n:]
    if not recent:
        return ""
    lines = ["[Conversation context — use these details in your implementation]"]
    for m in recent:
        role = "User" if m["role"] == "user" else "Assistant"
        text = m.get("content", "")[:800]
        lines.append(f"{role}: {text}")
    return "\n".join(lines) + "\n"


def _generate_worker(host: str, model: str, system_prompt: str,
                     user_prompt: str, num_ctx: int, params: dict) -> str:
    import requests as _r
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]
    opts = {"num_ctx": num_ctx}
    opts.update({k: v for k, v in params.items() if k not in _INTERNAL_PARAMS})
    base = host if host.startswith("http") else f"http://{host}"
    body = {"model": model, "messages": msgs, "stream": False, "options": opts}
    keep_alive = params.get("keep_alive")
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    try:
        resp = _r.post(f"{base}/api/chat", json=body, timeout=300)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "") or ""
    except Exception as e:
        print(f"  [worker {host}] error: {e}")
        return ""


def _build_decompose_prompt(name: str, signature: str, description: str,
                            root_task: str, parent_ctx: str) -> str:
    parts = [
        f"Your function is called: {name}",
        f"It must do: {description}",
        f"This is part of a larger program: {root_task}",
        f"",
        f"Write ONLY the function {name}. Not main. Not anything else.",
        f"Inside {name}, call helper functions that your team will implement.",
    ]
    if parent_ctx:
        parts.append(f"\nThe parent function that calls {name} looks like:\n{parent_ctx}")
    return "\n".join(parts)


def _build_implement_prompt(name: str, signature: str, description: str,
                            root_task: str, parent_skeleton: str,
                            use_ask: bool = False) -> str:
    parts = [f"Implement this function.\n"]
    if signature:
        parts.append(f"Function: {signature}")
    else:
        parts.append(f"Function: {name}")
    parts.append(f"Description: {description}")
    parts.append(f"Part of: {root_task}")
    if parent_skeleton:
        parts.append(f"\nCalling context (parent skeleton):\n{parent_skeleton}")
    if use_ask:
        parts.append(
            "\nBefore writing code, list the specific API calls, raises, and "
            "annotations you will use.\nFormat exactly:\n## Calls\n"
            "ClassName.method(), module.function(), raise ExceptionName\n"
            "One comma-separated line. Then write the function."
        )
    return "\n".join(parts)


# ── ask: call extraction + context enrichment ────────────────────────────────

def _parse_calls(text: str) -> list:
    """Extract comma-separated call list from ## Calls section."""
    for i, line in enumerate(text.splitlines()):
        if line.strip().startswith("## Calls"):
            for next_line in text.splitlines()[i + 1:]:
                next_line = next_line.strip()
                if next_line:
                    return [c.strip() for c in next_line.split(",") if c.strip()]
    return []


def _call_root_name(call: str) -> str:
    """Extract root identifier: 'psutil.net_if_addrs()' → 'psutil', 'raise Foo' → 'Foo'."""
    call = call.strip().lstrip("@")
    if call.lower().startswith("raise "):
        call = call[6:].strip()
    return _re.split(r'[\s.(]', call)[0]


def _is_internal(name: str, project_dir: str) -> bool:
    """True if a file with this exact stem exists anywhere in project_dir."""
    from pathlib import Path as _P
    try:
        for f in _P(project_dir).rglob("*"):
            if f.is_file() and f.stem == name:
                return True
    except Exception:
        pass
    return False


def _enrich_ctx(calls: list, web_n: int, rag_project: str,
                node_name: str, desc: str, code_dir: str,
                node_prefix: str, chat) -> str:
    """Fetch web/RAG examples for calls, compact, save ctx.md. Returns compacted text."""
    if not calls:
        return ""

    project_dir = _os.getcwd()
    internal, external = [], []
    for call in calls:
        root = _call_root_name(call)
        if root and _is_internal(root, project_dir):
            internal.append(root)
        elif root:
            external.append(root)

    try:
        from .deepagent_md import _web_research, _rag_research
    except ImportError:
        try:
            from deepagent_md import _web_research, _rag_research
        except ImportError:
            print("  [ask] deepagent_md not importable, skipping enrichment")
            return ""

    parts = []

    if external and web_n > 0:
        query_title = ", ".join(external) + " code example"
        print(f"  [ask/web] query: {query_title}")
        try:
            web_text = _web_research(chat, title=query_title,
                                     root_task=f"{node_name}: {desc}",
                                     web_n=web_n)
            if web_text:
                parts.append(web_text[:web_n * 1200])
        except Exception as e:
            print(f"  [ask/web] error: {e}")

    if internal and rag_project:
        rag_query = " ".join(internal)
        print(f"  [ask/rag] query: {rag_query} project: {rag_project}")
        try:
            rag_text = _rag_research(rag_query, rag_project,
                                     store_dir=project_dir, chat=chat)
            if rag_text:
                parts.append(rag_text[:2400])
        except Exception as e:
            print(f"  [ask/rag] error: {e}")

    if not parts:
        return ""

    combined = "\n\n".join(parts)
    compact_system = (
        "Summarize these code examples into a concise reference under 500 tokens. "
        "Keep only concrete API usage patterns and signatures, no prose explanations."
    )
    compacted = _generate_local(chat, compact_system, combined) or combined[:2000]

    ctx_path = _os.path.join(code_dir, f"{node_prefix}-{node_name}-ctx.md")
    with open(ctx_path, "w", encoding="utf-8") as f:
        f.write(f"# ctx: {node_name}\n\n{compacted}")
    print(f"  [ask] ctx -> {_os.path.basename(ctx_path)} ({len(compacted)} chars)")
    return compacted


_TEST_SYSTEM = (
    "You are writing a unit test for a Python function.\n"
    "You are given the function code. The function calls helper functions\n"
    "that may not exist yet.\n\n"
    "Your test should verify:\n"
    "- That the function calls the right helpers in the right order\n"
    "- That it passes the right arguments depending on conditions\n"
    "- That some helpers are skipped when conditions are not met\n"
    "- Basic input/output expectations\n\n"
    "Use unittest.mock.patch to mock the helper functions.\n"
    "Use pytest style (plain functions, assert statements).\n"
    "Output ONLY the code in a single code fence."
)


_THINK_SYSTEM = (
    "You are a senior developer planning the implementation of a function.\n"
    "Describe in plain text HOW you would implement it. Do NOT write code.\n\n"
    "Cover:\n"
    "- What input data do you receive and what should you return?\n"
    "- What are the main steps?\n"
    "- What validations or error checks are needed?\n"
    "- What edge cases should be handled?\n"
    "- What libraries or data structures would you use?\n\n"
    "Be specific and concrete. 5-15 sentences."
)


def _build_think_prompt(name: str, signature: str, description: str,
                        root_task: str, parent_ctx: str) -> str:
    parts = [
        f"Function: {name}",
        f"Description: {description}",
        f"Part of: {root_task}",
    ]
    if signature:
        parts.insert(1, f"Signature: {signature}")
    if parent_ctx:
        parts.append(f"\nCalling context:\n{parent_ctx}")
    return "\n".join(parts)


def _build_root_prompt(root_task: str) -> str:
    return (
        f"Decompose this task into top-level functions.\n"
        f"Task: {root_task}\n\n"
        f"Write a main function that calls sub-functions.\n"
        f"Each sub-function call must have a comment describing what it does.\n"
        f"Do not implement the sub-functions — just call them."
    )


# ── BFS expansion ───────────────────────────────────────────────────────────

_SYNTAX_MAX_RETRIES = 2


def _expand(chat, root_task: str, code_dir: str, max_depth: int,
            lang_cfg: dict, lang: str, workers: list, stats: dict,
            use_think: bool = False, use_test: bool = False,
            use_syntax: bool = False, use_distinct: bool = False,
            use_ask: bool = False, web_n: int = 1, rag_project: str = None,
            chat_ctx: str = ""):
    ext = lang_cfg["ext"]
    decompose_prompt = _load_lang_prompt(lang, "decompose")
    implement_prompt = _load_lang_prompt(lang, "implement")

    # tree: node_id -> {name, sig, desc, depth, children, status, file}
    tree = {}

    # ── Step 1: generate root skeleton ────────────────────────────────────
    root_file = _code_path(code_dir, "1", "main", ext)
    if _os.path.isfile(root_file):
        print(f"[skip] root — already exists")
        content = open(root_file, encoding="utf-8").read()
        code = content
    else:
        print(f"\n[gen] root: {root_task}")

        # think step for root
        if use_think:
            root_md = root_file.replace(ext, ".md")
            if _os.path.isfile(root_md):
                think_text = open(root_md, encoding="utf-8").read()
            else:
                print(f"  [think] main...")
                think_prompt = _build_think_prompt("main", "", root_task, root_task, "")
                think_text = _generate_local(chat, _THINK_SYSTEM, think_prompt)
                if think_text:
                    with open(root_md, "w", encoding="utf-8") as f:
                        f.write(f"# main\n\n{think_text}")
                    print(f"  [think] -> {_os.path.basename(root_md)} ({len(think_text)} chars)")

        user_prompt = _build_root_prompt(root_task)
        if chat_ctx:
            user_prompt += f"\n\n{chat_ctx}"
        if use_think and think_text:
            user_prompt += f"\n\nYour implementation plan:\n{think_text}"

        raw = _generate_local(chat, decompose_prompt, user_prompt)
        if not raw:
            print("[error] empty reply for root")
            return tree
        code, notes = _extract_code(raw)
        with open(root_file, "w", encoding="utf-8") as f:
            f.write(code)
        if notes:
            with open(root_file.replace(ext, ".md"), "w", encoding="utf-8") as f:
                f.write(notes)
        stats["files"] += 1

    children = _parse_skeleton(code, lang)
    sig = _extract_signature(code)
    tree["1"] = {"name": "main", "sig": sig, "desc": root_task,
                 "depth": 0, "children": [], "status": "skeleton", "file": root_file}
    print(f"  -> {_os.path.basename(root_file)} (skeleton, {len(children)} children)")

    # enqueue children
    _seen_names = {"main"}
    queue = []
    for i, (cname, csig, cdesc) in enumerate(children, 1):
        if use_distinct and cname in _seen_names:
            print(f"  [distinct] skip {cname} — already exists")
            continue
        _seen_names.add(cname)
        nid = f"1.{i}"
        tree["1"]["children"].append(nid)
        queue.append((nid, cname, csig, cdesc, 1, code))

    # ── Step 2: BFS level-by-level ────────────────────────────────────────
    current_depth = 1
    while queue:
        level_nodes = [n for n in queue if n[4] == current_depth]
        queue = [n for n in queue if n[4] != current_depth]

        if not level_nodes:
            current_depth += 1
            continue

        is_leaf = (current_depth >= max_depth)
        mode_label = "implement" if is_leaf else "decompose"
        n_workers = len(workers) if workers else 1
        print(f"\n[level {current_depth} — {mode_label}] nodes={len(level_nodes)}  workers={n_workers}")

        def _process_one(args):
            nid, name, sig_expr, desc, depth, parent_code = args
            fpath = _code_path(code_dir, nid, name, ext)

            if _os.path.isfile(fpath):
                content = open(fpath, encoding="utf-8").read()
                return nid, name, sig_expr, desc, depth, content, True, fpath

            # ── think step (optional) ─────────────────────────────────
            think_text = ""
            if use_think:
                md_path = fpath.replace(ext, ".md")
                if _os.path.isfile(md_path):
                    think_text = open(md_path, encoding="utf-8").read()
                else:
                    think_prompt = _build_think_prompt(name, sig_expr, desc,
                                                       root_task, parent_code)
                    print(f"    [think] {name}...")
                    idx = [n[0] for n in level_nodes].index(nid)
                    if workers:
                        host, model, _ = workers[idx % len(workers)]
                        think_text = _generate_worker(host, model, _THINK_SYSTEM,
                                                       think_prompt, chat.num_ctx,
                                                       chat.params)
                    else:
                        think_text = _generate_local(chat, _THINK_SYSTEM, think_prompt)
                    if think_text:
                        with open(md_path, "w", encoding="utf-8") as f:
                            f.write(f"# {name}\n\n{think_text}")
                        print(f"    [think] -> {_os.path.basename(md_path)} ({len(think_text)} chars)")

            # ── ask: pre-flight call extraction + enrichment ─────────
            ctx_text = ""
            if use_ask and is_leaf:
                ask_prompt = _build_implement_prompt(name, sig_expr, desc,
                                                     root_task, parent_code,
                                                     use_ask=True)
                if think_text:
                    ask_prompt += f"\n\nYour implementation plan:\n{think_text}"
                print(f"    [ask] extracting calls for {name}...")
                ask_raw = _generate_local(chat, implement_prompt, ask_prompt) or ""
                calls = _parse_calls(ask_raw)
                if calls:
                    print(f"    [ask] calls: {', '.join(calls)}")
                    node_prefix = _node_id_to_prefix(nid)
                    ctx_text = _enrich_ctx(calls, web_n, rag_project,
                                           name, desc, code_dir,
                                           node_prefix, chat)

            # ── code step ─────────────────────────────────────────────
            sys_prompt = implement_prompt if is_leaf else decompose_prompt
            if is_leaf:
                user_prompt = _build_implement_prompt(name, sig_expr, desc,
                                                      root_task, parent_code)
            else:
                user_prompt = _build_decompose_prompt(name, sig_expr, desc,
                                                      root_task, parent_code)
            if chat_ctx:
                user_prompt += f"\n\n{chat_ctx}"
            if think_text:
                user_prompt += f"\n\nYour implementation plan:\n{think_text}"
            if ctx_text:
                user_prompt += f"\n\nRelevant API examples:\n{ctx_text}"

            def _call_llm(s_prompt, u_prompt):
                idx_ = [n[0] for n in level_nodes].index(nid)
                if workers:
                    h_, m_, _ = workers[idx_ % len(workers)]
                    return _generate_worker(h_, m_, s_prompt, u_prompt,
                                            chat.num_ctx, chat.params)
                result = _generate_local(chat, s_prompt, u_prompt)
                if result is None:
                    action = _on_interrupt(name)
                    if action == 'quit':
                        raise _StopGeneration()
                    if action.startswith('hint:'):
                        hint = action[5:]
                        u_with_hint = u_prompt + f"\n\nAdditional instruction: {hint}"
                        return _generate_local(chat, s_prompt, u_with_hint) or ""
                    return ""
                return result

            raw = _call_llm(sys_prompt, user_prompt)

            # ── syntax check + retry (optional) ──────────────────────
            if use_syntax and raw:
                code_check, _ = _extract_code(raw)
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(code_check)
                for attempt in range(_SYNTAX_MAX_RETRIES):
                    err = _syntax_check(fpath, lang_cfg)
                    if not err:
                        break
                    print(f"    [syntax] {name}: {err}")
                    fix_prompt = (
                        f"This code has a syntax error:\n\n"
                        f"```\n{code_check}\n```\n\n"
                        f"Error: {err}\n\n"
                        f"Fix the error. Output ONLY the corrected code in a single code fence."
                    )
                    fix_raw = _call_llm(sys_prompt, fix_prompt)
                    if fix_raw:
                        code_check, _ = _extract_code(fix_raw)
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write(code_check)
                        raw = fix_raw
                else:
                    err = _syntax_check(fpath, lang_cfg)
                    if err:
                        print(f"    [syntax] {name}: FAILED after {_SYNTAX_MAX_RETRIES} retries")

            # ── test step (only for leaf implementations) ─────────────
            if use_test and raw and is_leaf:
                code_content, _ = _extract_code(raw)
                test_path = fpath.replace(ext, f"_test{ext}")
                if not _os.path.isfile(test_path) and code_content.strip():
                    test_prompt = (
                        f"Write a unit test for this function:\n\n"
                        f"```\n{code_content}\n```\n\n"
                        f"The function is in file: {_os.path.basename(fpath)}\n"
                        f"Mock all helper function calls with unittest.mock.patch."
                    )
                    print(f"    [test] {name}...")
                    idx2 = [n[0] for n in level_nodes].index(nid)
                    if workers:
                        host, model, _ = workers[idx2 % len(workers)]
                        test_raw = _generate_worker(host, model, _TEST_SYSTEM,
                                                     test_prompt, chat.num_ctx,
                                                     chat.params)
                    else:
                        test_raw = _generate_local(chat, _TEST_SYSTEM, test_prompt)
                    if test_raw:
                        test_code, _ = _extract_code(test_raw)
                        with open(test_path, "w", encoding="utf-8") as f:
                            f.write(test_code)
                        print(f"    [test] -> {_os.path.basename(test_path)}")
                        stats["tests"] += 1

            return nid, name, sig_expr, desc, depth, raw, False, fpath

        # execute: parallel if workers, sequential otherwise
        results = []
        if workers and len(level_nodes) > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futs = {pool.submit(_process_one, node): node for node in level_nodes}
                for fut in as_completed(futs):
                    results.append(fut.result())
        else:
            for node in level_nodes:
                results.append(_process_one(node))

        # sort for deterministic ordering
        results.sort(key=lambda x: tuple(int(p) for p in x[0].split(".")))

        done = 0
        for nid, name, sig_expr, desc, depth, raw, skipped, fpath in results:
            done += 1
            if not raw:
                print(f"  [{done}/{len(results)}] {nid}: {name} — empty, skipped")
                continue

            if skipped:
                code = raw
                print(f"  [{done}/{len(results)}] {nid}: {name} — exists, skipped")
            elif _os.path.isfile(fpath):
                # syntax check already wrote the file — read it back
                code = open(fpath, encoding="utf-8").read()
                stats["files"] += 1
                label = "impl" if is_leaf else "skel"
                print(f"  [{done}/{len(results)}] {nid}: {name} ({label}, {len(code.splitlines())} lines)")
            else:
                code, notes = _extract_code(raw)
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(code)
                if notes and not _os.path.isfile(fpath.replace(ext, ".md")):
                    md_path = fpath.replace(ext, ".md")
                    with open(md_path, "w", encoding="utf-8") as f:
                        f.write(notes)
                stats["files"] += 1
                label = "impl" if is_leaf else "skel"
                print(f"  [{done}/{len(results)}] {nid}: {name} ({label}, {len(code.splitlines())} lines)")

            sig = _extract_signature(code)
            node_children = []
            status = "implemented" if is_leaf else "skeleton"

            if not is_leaf:
                sub_funcs = _parse_skeleton(code, lang)
                for i, (cname, csig, cdesc) in enumerate(sub_funcs, 1):
                    if use_distinct and cname in _seen_names:
                        print(f"           [distinct] skip {cname} — already exists")
                        continue
                    _seen_names.add(cname)
                    child_nid = f"{nid}.{i}"
                    node_children.append(child_nid)
                    queue.append((child_nid, cname, csig, cdesc, depth + 1, code))
                if sub_funcs:
                    print(f"           {len(sub_funcs)} children: {', '.join(c[0] for c in sub_funcs)}")

            tree[nid] = {"name": name, "sig": sig or sig_expr, "desc": desc,
                         "depth": depth, "children": node_children,
                         "status": status, "file": fpath}

        current_depth += 1

    return tree


# ── index.md ─────────────────────────────────────────────────────────────────

def _generate_index(code_dir: str, tree: dict, root_task: str, lang: str):
    lines = [f"# {root_task}\n"]
    lines.append(f"Language: {lang}  |  Depth: {max((n['depth'] for n in tree.values()), default=0)}\n")
    lines.append("## Tree\n")

    def _walk(nid, indent=0):
        node = tree.get(nid)
        if not node:
            return
        fname = _os.path.basename(node.get("file", ""))
        desc = node.get("desc", "")
        status = node.get("status", "")
        marker = "[impl]" if status == "implemented" else "[skel]"
        lines.append(f"{'  ' * indent}- `{fname}` {marker} — {desc}")
        for child_nid in node.get("children", []):
            _walk(child_nid, indent + 1)

    _walk("1")

    n_total = len(tree)
    n_impl = sum(1 for n in tree.values() if n["status"] == "implemented")
    n_skel = sum(1 for n in tree.values() if n["status"] == "skeleton")
    lines.append(f"\n## Stats\n")
    lines.append(f"- Total files: {n_total}")
    lines.append(f"- Skeletons: {n_skel}")
    lines.append(f"- Implemented: {n_impl}")

    index_path = _os.path.join(code_dir, "index.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return index_path


# ── duplicates report ────────────────────────────────────────────────────────

def _generate_duplicates(code_dir: str, tree: dict, lang_cfg: dict):
    """Find functions with the same name across different tree branches."""
    ext = lang_cfg["ext"]
    func_re = lang_cfg.get("func_re", r'def\s+(\w+)')
    by_name = {}
    for nid, node in tree.items():
        fpath = node.get("file", "")
        if not fpath or not _os.path.isfile(fpath):
            continue
        code = open(fpath, encoding="utf-8").read()
        # extract real function name from code
        name = None
        for line in code.splitlines():
            m = _re.search(func_re, line, _re.IGNORECASE)
            if m:
                name = next((g for g in m.groups() if g), None)
                if name:
                    break
        if not name:
            name = node.get("name", "")
        if not name:
            continue
        n_lines = len([l for l in code.splitlines() if l.strip()])
        sig = _extract_signature(code)
        n_params = sig.count(",") + 1 if "(" in sig else 0
        n_children = len(node.get("children", []))
        entry = {
            "nid": nid, "file": _os.path.basename(fpath),
            "lines": n_lines, "params": n_params,
            "children": n_children, "status": node.get("status", ""),
            "sig": sig,
        }
        if name not in by_name:
            by_name[name] = []
        by_name[name].append(entry)

    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    if not dupes:
        return None

    lines = ["# Duplicate Functions\n"]
    lines.append(f"Found {len(dupes)} function names with multiple implementations.\n")
    for name, entries in sorted(dupes.items()):
        lines.append(f"## {name} ({len(entries)} copies)\n")
        lines.append("| Node | File | Lines | Params | Children | Status | Signature |")
        lines.append("|------|------|-------|--------|----------|--------|-----------|")
        for e in sorted(entries, key=lambda x: x["nid"]):
            sig_short = e["sig"][:50] if e["sig"] else "—"
            lines.append(
                f"| {e['nid']} | {e['file']} | {e['lines']} | {e['params']} "
                f"| {e['children']} | {e['status']} | `{sig_short}` |"
            )
        lines.append("")

    out_path = _os.path.join(code_dir, "duplicates.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[duplicates] {len(dupes)} duplicate names -> {out_path}")
    return out_path


# ── join ─────────────────────────────────────────────────────────────────────

def _join(code_dir: str, tree: dict, lang_cfg: dict):
    ext = lang_cfg["ext"]
    comment = lang_cfg["comment"]

    # collect all nodes sorted by depth desc (leaves first), then by id
    nodes = sorted(tree.values(),
                   key=lambda n: (-n["depth"], n.get("file", "")))

    parts = []
    for node in nodes:
        fpath = node.get("file", "")
        if not fpath or not _os.path.isfile(fpath):
            continue
        fname = _os.path.basename(fpath)
        code = open(fpath, encoding="utf-8").read().strip()
        parts.append(f"{comment} === {fname} ===\n{code}")

    joined = "\n\n\n".join(parts) + "\n"
    out_path = _os.path.join(code_dir, f"joined{ext}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(joined)
    return out_path


def _join_from_dir(code_dir: str, lang: str):
    """Join an existing code dir by scanning files (no tree needed)."""
    lang_cfg = LANGS.get(lang)
    if not lang_cfg:
        print(f"[error] unknown language: {lang}")
        return
    ext = lang_cfg["ext"]
    comment = lang_cfg["comment"]

    files = sorted(f for f in _os.listdir(code_dir)
                   if f.endswith(ext) and f != f"joined{ext}")
    if not files:
        print(f"[error] no {ext} files in {code_dir}")
        return

    parts = []
    for fname in files:
        fpath = _os.path.join(code_dir, fname)
        code = open(fpath, encoding="utf-8").read().strip()
        parts.append(f"{comment} === {fname} ===\n{code}")

    joined = "\n\n\n".join(parts) + "\n"
    out_path = _os.path.join(code_dir, f"joined{ext}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(joined)
    print(f"[join] {len(files)} files -> {out_path}")

    # run duplicates scan on join
    _generate_duplicates_from_dir(code_dir, lang_cfg)


def _generate_duplicates_from_dir(code_dir: str, lang_cfg: dict):
    """Scan code dir for duplicate function names (no tree needed)."""
    ext = lang_cfg["ext"]
    by_name = {}
    for fname in sorted(_os.listdir(code_dir)):
        if not fname.endswith(ext) or fname.startswith("joined"):
            continue
        fpath = _os.path.join(code_dir, fname)
        code = open(fpath, encoding="utf-8").read()
        n_lines = len([l for l in code.splitlines() if l.strip()])
        sig = _extract_signature(code)
        n_params = sig.count(",") + 1 if "(" in sig else 0
        # extract function/object name using language-specific regex
        func_re = lang_cfg.get("func_re", r'def\s+(\w+)')
        func_name = None
        for line in code.splitlines():
            m = _re.search(func_re, line, _re.IGNORECASE)
            if m:
                func_name = next((g for g in m.groups() if g), None)
                if func_name:
                    break
        if not func_name:
            continue
        entry = {
            "file": fname, "lines": n_lines, "params": n_params,
            "sig": sig,
        }
        if func_name not in by_name:
            by_name[func_name] = []
        by_name[func_name].append(entry)

    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    if not dupes:
        print("[duplicates] no duplicates found")
        return None

    lines = ["# Duplicate Functions\n"]
    lines.append(f"Found {len(dupes)} function names with multiple implementations.\n")
    for name, entries in sorted(dupes.items()):
        lines.append(f"## {name} ({len(entries)} copies)\n")
        lines.append("| File | Lines | Params | Signature |")
        lines.append("|------|-------|--------|-----------|")
        for e in sorted(entries, key=lambda x: x["file"]):
            sig_short = e["sig"][:50] if e["sig"] else "—"
            lines.append(f"| {e['file']} | {e['lines']} | {e['params']} | `{sig_short}` |")
        lines.append("")

    # find next available number
    n = 1
    while _os.path.isfile(_os.path.join(code_dir, f"duplicates{'' if n == 1 else n}.md")):
        n += 1
    suffix = "" if n == 1 else str(n)
    out_path = _os.path.join(code_dir, f"duplicates{suffix}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[duplicates] {len(dupes)} duplicate names -> {out_path}")
    return out_path


# ── profile loading ──────────────────────────────────────────────────────────

def _load_profile(name: str) -> list:
    try:
        import sys as _sys
        import importlib as _il
        _flow_dir = _os.path.dirname(_os.path.abspath(__file__))
        _root = _os.path.dirname(_os.path.dirname(_flow_dir))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        _chat_mod = _il.import_module("chat")
        return _chat_mod._load_profile(name)
    except Exception:
        pass
    try:
        from chat import _load_profile as _lp
        return _lp(name)
    except Exception:
        return None


# ── entry point ──────────────────────────────────────────────────────────────

def run(chat, args: str):
    args = args.strip()

    # ── subcommand: join ──────────────────────────────────────────────────
    if args.startswith("join"):
        rest = args[4:].strip()
        lang = "py"
        m = _re.search(r'--lang\s+(\S+)', rest)
        if m:
            lang = m.group(1).lower()
            rest = (rest[:m.start()] + rest[m.end():]).strip()
        code_dir = rest.strip()
        if not code_dir:
            print("usage: /flow deepagent_code join <code_dir> [--lang py]")
            return
        base = _os.path.join(_os.getcwd(), ".1bcoder", "code")
        if not _os.path.isabs(code_dir):
            code_dir = _os.path.join(base, code_dir)
        if not _os.path.isdir(code_dir):
            print(f"[error] not found: {code_dir}")
            return
        _join_from_dir(code_dir, lang)
        return

    # ── parse flags ───────────────────────────────────────────────────────
    lang = "py"
    m = _re.search(r'--lang\s+(\S+)', args)
    if m:
        lang = m.group(1).lower()
        args = (args[:m.start()] + args[m.end():]).strip()

    max_depth = 2
    m = _re.search(r'--depth\s+(\d+)', args)
    if m:
        max_depth = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    do_join = "--join" in args
    args = args.replace("--join", "").strip()

    use_think = "--think" in args
    args = args.replace("--think", "").strip()

    use_test = "--test" in args
    args = args.replace("--test", "").strip()

    use_syntax = "--syntax" in args
    args = args.replace("--syntax", "").strip()

    use_distinct = "--distinct" in args
    args = args.replace("--distinct", "").strip()

    use_ask = "--ask" in args
    args = args.replace("--ask", "").strip()

    web_n = 1
    m = _re.search(r'--web\s+(\d+)', args)
    if m:
        web_n = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    rag_project = None
    m = _re.search(r'--rag\s+(\S+)', args)
    if m:
        rag_project = m.group(1)
        args = (args[:m.start()] + args[m.end():]).strip()

    profile_name = None
    m = _re.search(r'--profile\s+(\S+)', args)
    if m:
        profile_name = m.group(1)
        args = (args[:m.start()] + args[m.end():]).strip()

    ctx_n = 0
    m = _re.search(r'--ctx\s+(\d+)', args)
    if m:
        ctx_n = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    task_file = None
    m = _re.search(r'--file\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))', args)
    if m:
        task_file = (m.group(1) or m.group(2) or m.group(3))
        args = (args[:m.start()] + args[m.end():]).strip()

    root_task = args.strip().strip('"').strip("'")

    if task_file:
        if not _os.path.isabs(task_file):
            task_file = _os.path.join(_os.getcwd(), task_file)
        if _os.path.isfile(task_file):
            file_content = open(task_file, encoding="utf-8").read().strip()
            root_task = (root_task + "\n\n" + file_content).strip() if root_task else file_content
        else:
            print(f"[error] file not found: {task_file}")
            return
    if not root_task:
        print("usage: /flow deepagent_code \"task description\" --lang py [--depth 2] [--profile name] [--join]")
        return

    if lang not in LANGS:
        print(f"[error] unsupported language: {lang}")
        print(f"  supported: {', '.join(sorted(LANGS.keys()))}")
        return

    lang_cfg = LANGS[lang]

    # ── load profile workers ──────────────────────────────────────────────
    workers = None
    if profile_name:
        workers = _load_profile(profile_name)
        if workers:
            print(f"[deepagent_code] profile: {profile_name} ({len(workers)} workers)")
            for h, mdl, _ in workers:
                print(f"  {h}  {mdl}")
        else:
            print(f"[deepagent_code] profile '{profile_name}' not found — running single")

    # ── create output dir ─────────────────────────────────────────────────
    base = _os.path.join(_os.getcwd(), ".1bcoder", "code")
    _os.makedirs(base, exist_ok=True)
    code_dir = _make_code_dir(base)

    print(f"[deepagent_code] task  : {root_task}")
    print(f"[deepagent_code] lang  : {lang}  depth: {max_depth}  dir: {_os.path.relpath(code_dir)}")

    chat_ctx = _serialize_ctx(getattr(chat, "messages", []), ctx_n)
    if chat_ctx:
        print(f"[deepagent_code] chat ctx     : {ctx_n} msgs ({len(chat_ctx)} chars)")

    stats = {"files": 0, "tests": 0}
    try:
        tree = _expand(chat, root_task, code_dir, max_depth, lang_cfg, lang, workers, stats,
                        use_think=use_think, use_test=use_test,
                        use_syntax=use_syntax, use_distinct=use_distinct,
                        use_ask=use_ask, web_n=web_n, rag_project=rag_project,
                        chat_ctx=chat_ctx)
    except _StopGeneration:
        tree = {}
        for fname in _os.listdir(code_dir):
            if fname.endswith(lang_cfg["ext"]) and fname != f"joined{lang_cfg['ext']}":
                parts = fname.replace(lang_cfg["ext"], "").split("-", 1)
                nid = parts[0].replace("-", ".") if parts else "?"
                tree[nid] = {"name": fname, "sig": "", "desc": "",
                             "depth": nid.count("."), "children": [],
                             "status": "generated", "file": _os.path.join(code_dir, fname)}

    if not tree:
        print("[deepagent_code] no output generated")
        return

    # ── generate index ────────────────────────────────────────────────────
    idx_path = _generate_index(code_dir, tree, root_task, lang)
    print(f"\n[index] -> {idx_path}")

    # ── duplicates report ─────────────────────────────────────────────────
    _generate_duplicates(code_dir, tree, lang_cfg)

    # ── join if requested ─────────────────────────────────────────────────
    if do_join:
        joined_path = _join(code_dir, tree, lang_cfg)
        print(f"[join]  -> {joined_path}")

    # ── summary ───────────────────────────────────────────────────────────
    n_impl = sum(1 for n in tree.values() if n["status"] == "implemented")
    n_skel = sum(1 for n in tree.values() if n["status"] == "skeleton")
    test_info = f", {stats['tests']} tests" if stats["tests"] else ""
    print(f"\n[done] {stats['files']} files ({n_skel} skeletons, {n_impl} implemented{test_info})")
    print(f"[dir]  {code_dir}")

    chat.last_reply = f"Generated {stats['files']} files in {code_dir}"
    chat._last_output = chat.last_reply
