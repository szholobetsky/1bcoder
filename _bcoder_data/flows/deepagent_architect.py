"""deepagent_architect -- research-grounded architecture decomposition.

Sits between deepagent_task (backlog/plan, pure text) and deepagent_code
(function-level skeleton -> leaf implementation, no notion of interface or
class). Walks a fixed five-level axis for ONE backlog task at a time:

    0  interface   -- contract: capability, method signatures, one-line purpose
    1  abstract    -- shared base behavior (may be skipped, on evidence)
    2  approach    -- DECISION node: which concrete strategy, and why
    3  class       -- concrete shell: fields, constructor, method list
    4  method/     -- LEAF: delegated to the existing deepagent_code engine,
       query/         one invocation per method
       formula

Before each level's design decision, a bounded research step runs first --
the `research` agent (_bcoder_data/agents/research.txt), gated by
`tool-allowlist-gate` (_bcoder_data/proc/tool-allowlist-gate.py) so the
read-only guarantee is actually enforced, not just documented. See
concepts/DEEPAGENT_ARCHITECT.md for the full design rationale.

Cross-ticket duplicate avoidance: the research step is explicitly pointed
at .1bcoder/code/ -- the REAL files deepagent_code has already generated
for earlier tickets in this plan (not a separate hand-maintained registry;
the actual generated code is the source of truth). If interface/abstract/
class decide step finds this exact thing already implemented there, it
outputs "REUSE: <file path(s)> -- <summary>" instead of a new design --
at the class level this skips leaf/method generation entirely (nothing
gets sent to deepagent_code a second time). Partial overlap is handled
too: the class decide step can list already-implemented methods under
EXISTING: and only the genuinely missing ones under METHODS:, so only
those get delegated.

Ids are purely numeric dot-notation (1, 1.1, 1.1.1, ...), same convention as
deepagent_md's item_<id>.md tree -- no text segments embedded in the id
itself, so this stays compatible with deepagent_merge's _shift_id and
friends if this tree is ever folded into another one later. Which level a
given depth represents is fixed by this flow (the same "plan: semantic
label per depth" idea deepagent_md already uses), not encoded in the id.

Output: .1bcoder/arch/<plan_name>/<task_key>/item_<id>.md (the decision) and
item_<id>-research.md (what the research step found), sibling to planMD/
spec/tasks under .1bcoder/, same per-plan nesting convention.

Resumable, same convention as deepagent_md/deepagent_spec: if interrupted
(Ctrl+C, killed process, crash -- all observed this session), just re-run
the identical command. Each level checks for its own item_<id>.md before
doing any research/decide work and skips straight to the next one if it's
already there; each leaf method checks for its own leaf_<id>.done marker
before delegating to deepagent_code again. Nothing gets regenerated or
overwritten, and nothing gets delegated twice.

A level is skipped either explicitly (--skip) or on research evidence (the
`abstract` level's own decide step can answer "SKIP: <reason>" when no
duplicated boilerplate was found -- same "evidence-based skip, fixed set of
levels" principle as deepagent_code's explicit --depth: the *decision* to
skip a level is evidence-based, but *which levels exist at all* is fixed
and external, never an open-ended model-judged search).

Honest limitation (see DEEPAGENT_ARCHITECT.md section 8): this narrows, but
does not eliminate, the Calendar Problem -- research grounding prevents
reinventing what already exists, it does not give a small model the ability
to reason about the compatibility of two contracts it is inventing for the
first time in the same run. Levels 0-1 remain, in the hard cases, architect
(human) work.

Usage:
  /flow deepagent_architect <plan_name> <task_id> [--lang py] [--skip abstract]
  /flow deepagent_architect <plan_name> "<raw task text>" [--lang py]
  /flow deepagent_architect <plan_name> --next [--lang py]
  /flow deepagent_architect <plan_name> --loop [--lang py] [--max N]

<task_id> is looked up as spec_<task_id>.<n>.md under deepagent_spec's
project dir if it matches a bare dotted-id shape (e.g. "3.2"); otherwise the
argument is used verbatim as the root task description. --skip takes a
comma-separated list of level names to force out regardless of research
evidence (only "abstract" is meaningful to skip in practice -- interface/
approach/class are not optional in v1).

--next / --loop: pick the next open (not "[x]") row from tasks.md instead of
a caller-supplied task_id. Before starting work on a ticket, it is claimed
by atomically creating .1bcoder/arch/<plan>/<id>/.claim (os.O_EXCL -- fails
if the file already exists, the standard cross-platform way to implement a
lock without a database). This is what lets two deepagent_architect
processes -- different machines, different models, doesn't matter -- run
--loop against the SAME plan concurrently and correctly divide the backlog
without duplicating work: each one claims whatever the other hasn't. A
claim older than one hour with no matching .done is treated as abandoned
(observed multiple times this session: a killed background process leaves
a claim nothing will ever release) and can be retaken. --next processes
exactly one ticket; --loop repeats --next until the backlog is empty (or
--max is hit, or every remaining ticket is already claimed by someone
else's live process).

Example:
  /flow deepagent_architect plan5 3.2 --lang py
  /flow deepagent_architect plan5 "add retry logic to the payment gateway client"
  /flow deepagent_architect plan5 --next
  /flow deepagent_architect plan5 --loop --max 5
"""
import os as _os
import re as _re
import sys as _sys
import time as _time
import socket as _socket


def _load(name: str, filename: str):
    import importlib.util as _iu
    here = _os.path.dirname(_os.path.abspath(__file__))
    path = _os.path.join(here, filename)
    spec = _iu.spec_from_file_location(name, path)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_dspec = _load("_dspec", "deepagent_spec.py")
_dcode = _load("_dcode", "deepagent_code.py")
_dtask = _load("_dtask", "deepagent_task.py")

_CLAIM_STALE_SECONDS = 60 * 60  # 1 hour -- abandoned claim, safe to re-take


LEVELS = ["interface", "abstract", "approach", "class"]

_RESEARCH_QUESTIONS = {
    "interface": (
        "We are about to design an interface/protocol for the following need:\n"
        "{task}\n\n"
        "Search this codebase for an existing interface, protocol, or abstract "
        "contract with a similar responsibility -- including .1bcoder/code/, "
        "which holds REAL files deepagent_code has already generated (use "
        "ACTION: /tree .1bcoder/code to see what's there, then ACTION: /read on "
        "anything that looks relevant -- these are actual classes and method "
        "signatures, not descriptions). Report exact file paths, names, and "
        "method signatures of anything you find. If nothing similar exists, say "
        "so explicitly."
    ),
    "abstract": (
        "We are about to decide whether an abstract base class is warranted for:\n"
        "{task}\n\n"
        "Search this codebase, including .1bcoder/code/ (real generated files -- "
        "ACTION: /tree .1bcoder/code, then ACTION: /read the relevant ones), for "
        "two or more existing concrete implementations related to this "
        "responsibility. Report whether they share duplicated boilerplate that "
        "would justify an abstract base, and the exact file paths where you "
        "found it."
    ),
    "approach": (
        "We need to choose a concrete implementation strategy for:\n"
        "{task}\n\n"
        "Search this codebase for existing strategies, libraries, or patterns "
        "that could satisfy this. If nothing internal fits, consult external "
        "sources. Report every candidate you find, with its source."
    ),
    "class": (
        "We are about to design a concrete class for:\n"
        "{task}\n\n"
        "Search this codebase, ESPECIALLY .1bcoder/code/ -- this holds the REAL "
        "files deepagent_code has already generated for earlier tickets in this "
        "same plan, not descriptions. Use ACTION: /tree .1bcoder/code to see "
        "what directories exist, then ACTION: /read on any file whose name "
        "suggests it might already cover this class or its methods. Also check "
        "for sibling or related classes to learn naming/constructor "
        "conventions.\n\n"
        "IMPORTANT: a file that IS an interface or abstract-class SKELETON -- "
        "its methods' bodies are just `...`, `pass`, `raise NotImplementedError`, "
        "`panic(\"not implemented\")`, or an equivalent stub, with NO real "
        "logic -- is NOT a finished implementation, even if it's a file this "
        "same ticket wrote earlier at the interface/abstract step. Report it "
        "as a base to extend, never as \"already implemented\".\n\n"
        "Report exact file paths, class/method names, signatures, and for "
        "each file whether its method bodies contain real logic or are stubs."
    ),
}

_DECIDE_SYSTEM = {
    "interface": (
        "You are proposing an interface/protocol design, given research "
        "findings about what already exists in the codebase -- including real "
        "files under .1bcoder/code/ that deepagent_code has already generated "
        "for earlier tickets.\n\n"
        "If the research found this interface ALREADY implemented as real code "
        "(not just something similar -- the actual thing this ticket needs), "
        "output exactly:\n"
        "REUSE: <exact file path(s)> -- <one-line summary of what's already there>\n"
        "Do NOT invent a duplicate of something that already exists as real code.\n"
        "Otherwise, propose ONE new interface. Output exactly in this form:\n"
        "NAME: <interface name>\n"
        "CONTRACT: <one line>\n"
        "METHODS:\n"
        "  method_name(args) -> return_type  # one-line purpose\n"
    ),
    "abstract": (
        "You are deciding whether an abstract base class is warranted, given "
        "research findings -- including real files under .1bcoder/code/ that "
        "deepagent_code has already generated for earlier tickets.\n\n"
        "If the research found this abstract base ALREADY implemented as real "
        "code, output exactly:\n"
        "REUSE: <exact file path(s)> -- <one-line summary of what's already there>\n"
        "If the research found no real duplicated boilerplate across two or "
        "more implementations (nothing to justify a new abstract base either), "
        "output exactly:\n"
        "SKIP: <one-line reason>\n"
        "Otherwise output exactly in this form:\n"
        "NAME: <abstract class name>\n"
        "CONTRACT: <one line -- what shared behavior it provides>\n"
        "METHODS:\n"
        "  method_name(args) -> return_type  # template method or shared helper\n"
    ),
    "approach": (
        "You are choosing a concrete implementation strategy, given research "
        "findings about candidates that already exist locally or externally.\n\n"
        "Output exactly in this form:\n"
        "CHOSEN: <the strategy, library, or pattern you picked>\n"
        "WHY: <one line>\n"
        "REJECTED:\n"
        "  <alternative> -- <one-line reason rejected>\n"
        "(REJECTED may be left empty if research found only one real candidate.)\n"
    ),
    "class": (
        "You are proposing a concrete class design, given research findings "
        "about sibling classes' conventions -- including real files under "
        ".1bcoder/code/ that deepagent_code has already generated for earlier "
        "tickets in this same plan.\n\n"
        "A file whose methods are stubs (`...`, `pass`, `raise "
        "NotImplementedError`, `panic(\"not implemented\")`, or equivalent -- "
        "including this same ticket's OWN interface/abstract file, which is "
        "always a stub at this point in the pipeline) is NOT an implemented "
        "class, no matter how closely its name or shape matches. Treat it as "
        "the base to extend (via CONSTRUCTOR/base class), never as REUSE.\n\n"
        "If the research found this exact class ALREADY implemented as real "
        "code with real method bodies (every method this ticket needs), "
        "output exactly:\n"
        "REUSE: <exact file path(s)> -- <one-line summary of what's already there>\n"
        "Do NOT redesign or duplicate a class that already exists as real code.\n"
        "If SOME but not all needed methods already exist as real code, list "
        "ONLY the still-missing ones under METHODS: below, and name the "
        "already-existing ones under EXISTING: instead of redefining them:\n"
        "NAME: <class name>\n"
        "CONSTRUCTOR: <constructor signature>\n"
        "FIELDS:\n"
        "  field_name: type\n"
        "EXISTING:\n"
        "  method_name(args) -> return_type  # already implemented at <file path>\n"
        "METHODS:\n"
        "  method_name(args) -> return_type  # one-line purpose -- still needs to be generated\n"
        "If NOTHING needed already exists, output the same form with an empty "
        "EXISTING: section and every method under METHODS:.\n"
    ),
}

_METHOD_LINE_RE = _re.compile(
    r'^([A-Za-z_][A-Za-z0-9_]*)\(([^)]*)\)\s*(?:->\s*(\S+))?\s*(?:#\s*(.*))?$'
)

# Method lines are always parsed in the same "name(args) -> ret" shape
# regardless of target language, but args/ret must be written using the
# TARGET language's own type syntax or the generated .java/.go/.ts file is
# syntactically wrong (caught in testing: `id: str` inside a .java file).
# This hint is appended to the decide-step system prompt for whichever
# language was requested.
_LANG_TYPE_HINTS = {
    "py":   "Python type hints, e.g. find(id: str) -> Book",
    "java": "Java syntax, e.g. find(String id) -> Book",
    "ts":   "TypeScript syntax, e.g. find(id: string) -> Book",
    "js":   "plain JS (no types), e.g. find(id) -> Book",
    "go":   "Go syntax, e.g. find(id string) -> Book",
}


# ── headless sub-agent runner ────────────────────────────────────────────────

def _run_research_agent(chat, question: str) -> str:
    """Run the `research` agent (see _bcoder_data/agents/research.txt)
    headlessly: no blocking "add to context?" prompt at the end (monkeypatch
    the module-level _input for the duration of this call only -- verified
    this session that _run_agent_loop always calls it on natural completion,
    and that nothing else short of this avoids it), isolated from the main
    session's own message history (deliberately NOT prefixed with
    chat.messages, unlike _run_named_agent -- each node's research should
    start fresh, not accumulate across nodes or carry the user's unrelated
    conversation). Gated by tool-allowlist-gate so the read-only guarantee is
    enforced, not just documented in tools=. Returns the agent's last
    assistant message (its findings report)."""
    path = chat._find_agent_def("research")
    if not path:
        return ("[deepagent_architect] 'research' agent not found -- "
                 "expected _bcoder_data/agents/research.txt. Proceeding "
                 "without research findings.")

    mod = _sys.modules[chat.__class__.__module__]
    cfg = chat._load_agent_def(path)
    tools = cfg["tools"] or []
    tool_list = mod.get_help_list(tools)
    tpl = cfg["system"]
    system_prompt = tpl.format(tool_list=tool_list) if "{tool_list}" in tpl else tpl
    agent_msgs = [{"role": "system", "content": system_prompt},
                  {"role": "user", "content": question}]

    saved = (list(chat._proc_active), list(chat._proc_before),
             list(chat._proc_gates), list(chat._proc_gates_post),
             dict(chat._aliases))
    chat._proc_active, chat._proc_before, chat._proc_gates_post = [], [], []
    chat._proc_gates = ["tool-allowlist-gate"]
    chat._aliases.update(cfg["aliases"])

    orig_input = mod._input
    mod._input = lambda prompt="": "n"
    try:
        chat._run_agent_loop("research", agent_msgs, cfg["max_turns"],
                             auto_exec=True, auto_apply=True, use_procs=True)
    finally:
        mod._input = orig_input
        (chat._proc_active, chat._proc_before,
         chat._proc_gates, chat._proc_gates_post, chat._aliases) = saved

    for m in reversed(agent_msgs):
        if m["role"] == "assistant":
            return m["content"]
    return "[deepagent_architect] research agent produced no findings"


def _decide(chat, level: str, task_text: str, research_findings: str,
           lang: str = "py") -> str:
    """Second call: propose the actual design, given research findings."""
    system = _DECIDE_SYSTEM[level]
    if level in ("interface", "abstract", "class"):
        hint = _LANG_TYPE_HINTS.get(lang, _LANG_TYPE_HINTS["py"])
        system += f"\nWrite every method's args and return type using {hint}.\n"
    user = (
        f"Task/need:\n{task_text}\n\n"
        f"Research findings:\n{research_findings}\n\n"
        f"Now produce your {level} design in exactly the form described above."
    )
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return (chat._stream_chat(msgs) or "").strip()


# ── parsing helpers ───────────────────────────────────────────────────────────

_REUSE_NEGATIVE_RE = _re.compile(
    r'^(none|n/?a|nothing|no(\s+existing)?|-|null)\b', _re.IGNORECASE
)


_STUB_MARKERS = (
    "notimplementederror", "not implemented", "raise notimplemented",
    "pass  # todo", "panic(\"not implemented\")", "throw new error(\"not implemented\")",
    "unsupportedoperationexception",
)


def _reuse_target_is_stub(decision_text: str) -> bool:
    """Deterministic safety net for the class-level REUSE claim: a small
    local model has already been observed (real CBC run, plan1 ticket 1) to
    call its OWN just-written interface stub (Protocol with `...` bodies)
    "already implemented" at the class level, despite an explicit prompt
    instruction to the contrary -- prompt-following alone isn't reliable
    enough here (same lesson as _is_reuse's "REUSE: None" case). Reads the
    file(s) named in the REUSE: line and returns True if ANY of them still
    look like a stub, in which case the caller should NOT trust the REUSE
    claim and should fall through to normal class design instead."""
    value = _parse_field(decision_text, "REUSE")
    if not value:
        return False
    path_part = value.split("--")[0].strip().strip("`").split()[0] if value.split("--")[0].strip() else ""
    for path in _re.split(r'[,;]\s*', path_part) if path_part else []:
        path = path.strip()
        if not path or not _os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().lower()
        except OSError:
            continue
        if any(marker in content for marker in _STUB_MARKERS) or _re.search(r':\s*\.\.\.\s*(#|$)', content, _re.MULTILINE):
            return True
    return False


def _is_reuse(decision_text: str) -> bool:
    """True only if the decision's REUSE: line names an actual existing
    reference, not a small local model's habit of writing "REUSE: None --
    <reason nothing exists>" when it means "nothing to reuse, see my new
    design below". A bare startswith("REUSE") check was fooled by exactly
    this on a real qwen3-4b-instruct-2507 CBC run (ticket 1: class level
    produced zero code because "REUSE: None -- No existing dataclass-style
    ..." was treated as a genuine reuse hit) -- confirmed via log_next.txt."""
    value = _parse_field(decision_text, "REUSE")
    if not value:
        return False
    return not _REUSE_NEGATIVE_RE.match(value.strip())


def _parse_field(decision_text: str, field: str) -> str:
    for line in decision_text.splitlines():
        s = line.strip()
        if s.upper().startswith(field.upper() + ":"):
            return s[len(field) + 1:].strip()
    return ""


def _parse_methods(decision_text: str) -> list:
    """Extract 'method_name(args) -> ret  # purpose' lines from the indented
    METHODS: block of a decide-step's output. Best-effort, same tolerance
    for imperfect model output as deepagent_code's own skeleton parser --
    a line that doesn't match is silently dropped, not an error."""
    methods = []
    in_methods = False
    for line in decision_text.splitlines():
        if not line.strip():
            continue
        if not (line.startswith(" ") or line.startswith("\t")):
            in_methods = line.strip().upper().startswith("METHODS:")
            continue
        if not in_methods:
            continue
        m = _METHOD_LINE_RE.match(line.strip())
        if m:
            name, args, ret, purpose = m.groups()
            # The model routinely writes its own "self"/"this" as the first
            # arg (real case: "validate_task(self, task_data: ...) -> bool:"
            # from a live CBC run) even though every renderer that needs a
            # receiver token (only _py_* does) adds its own -- left as-is,
            # Python renders "def f(self, self, ...)". Strip it here once,
            # for every language, since none of them want the model's own
            # self/this echoed back.
            args = (args or "").strip()
            args = _re.sub(r'^(self|this)\s*,\s*', '', args)
            args = _re.sub(r'^(self|this)$', '', args)
            # The same real run also produced "-> bool:" (trailing colon
            # swallowed by the greedy \S+ ret group), which rendered as a
            # syntax-breaking "::" once the line's own trailing ":" was
            # added back by the renderer.
            ret = (ret or "").rstrip(":").strip()
            methods.append({"name": name, "args": args,
                            "ret": ret, "purpose": purpose or ""})
    return methods


_FIELD_LINE_RE = _re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S+)')


def _parse_fields(decision_text: str) -> list:
    """Extract 'field_name: type' lines from the indented FIELDS: block of a
    class decide-step's output. Same tolerance as _parse_methods."""
    fields = []
    in_fields = False
    for line in decision_text.splitlines():
        if not line.strip():
            continue
        if not (line.startswith(" ") or line.startswith("\t")):
            in_fields = line.strip().upper().startswith("FIELDS:")
            continue
        if not in_fields:
            continue
        m = _FIELD_LINE_RE.match(line.strip())
        if m:
            fields.append(m.group(1))
    return fields


def _slug(name: str) -> str:
    s = _re.sub(r'[^A-Za-z0-9_]+', '_', (name or "").strip())
    return s.strip('_') or "Unnamed"


# ── code-skeleton generation (interface/abstract/class), per language ─────────
#
# Writes REAL, importable source files -- not just item_<id>.md descriptions
# -- into .1bcoder/code/, the same directory the research step is told to
# check for reuse. A markdown description is not something a future ticket's
# research step can meaningfully "reuse"; an actual class/interface file is.
# Method bodies are stubs (raise NotImplementedError or the language's
# equivalent) here -- deepagent_code still generates the real
# implementations per method as its own files; assembling those bodies INTO
# these skeleton files is a further step, not yet done automatically
# (tracked as a known gap, not silently pretended to be solved).
#
# "interface" / "abstract class" are OOP concepts, and they don't map onto
# every target language the same way -- this is a real, honest limit, not
# an oversight:
#   - Java/C#-family: native interface + abstract class, direct 1:1 mapping.
#   - TypeScript: native `interface`; `abstract class` exists but plain JS
#     has neither, so JS gets a duck-typed class only (interface/abstract
#     skipped for JS specifically, not for TS).
#   - Go: structural interfaces (no `implements`), and NO abstract classes
#     at all -- Go's own idiom is composition over inheritance. The
#     "abstract" level is skipped for Go by design, not by accident.
#   - SQL/PL-SQL and similar: not OOP at all. A "table" or "stored
#     procedure" ticket doesn't fit interface->abstract->class -- pass
#     --skip interface,abstract for that kind of ticket rather than forcing
#     the axis where it doesn't belong.
# Languages without an adapter below (Kotlin, Groovy, Swift, C++, Pascal,
# PL/SQL, ...) fall back gracefully: the item_<id>.md decision is still
# written as always, only the extra code-skeleton file is skipped, with a
# clear message -- never a silently-wrong-syntax file.

def _code_dir(workdir: str, plan_name: str, task_key: str) -> str:
    return _os.path.join(workdir, ".1bcoder", "code", f"{plan_name}-{task_key}")


def _sig_args(meth: dict, self_token: str = "") -> str:
    args = meth.get("args", "")
    return f"{self_token}, {args}" if (self_token and args) else (args or self_token)


# ── Python ───────────────────────────────────────────────────────────────────

def _py_interface(name, methods, **_):
    lines = ["from typing import Protocol", "", "", f"class {name}(Protocol):"]
    if not methods:
        lines.append("    ...")
    for m in methods:
        ret = f" -> {m['ret']}" if m.get("ret") else ""
        purpose = f"  # {m['purpose']}" if m.get("purpose") else ""
        lines.append(f"    def {m['name']}({_sig_args(m, 'self')}){ret}: ...{purpose}")
    return "py", "\n".join(lines) + "\n"


def _py_abstract(name, methods, base_name="", **_):
    lines = ["from abc import ABC, abstractmethod"]
    if base_name:
        lines.append(f"from .{_slug(base_name).lower()} import {base_name}")
    lines += ["", ""]
    lines.append(f"class {name}({base_name or 'ABC'}):")
    if not methods:
        lines.append("    pass")
    for m in methods:
        ret = f" -> {m['ret']}" if m.get("ret") else ""
        lines.append("    @abstractmethod")
        lines.append(f"    def {m['name']}({_sig_args(m, 'self')}){ret}:")
        if m.get("purpose"):
            lines.append(f'        """{m["purpose"]}"""')
        lines.append("        raise NotImplementedError")
        lines.append("")
    return "py", "\n".join(lines).rstrip("\n") + "\n"


def _py_class(name, fields, methods, base_name="", **_):
    lines = []
    if base_name:
        lines.append(f"from .{_slug(base_name).lower()} import {base_name}")
        lines += ["", ""]
    lines.append(f"class {name}({base_name}):" if base_name else f"class {name}:")
    lines.append("    def __init__(self):")
    for fname in (fields or ["pass"]):
        lines.append("        pass" if fname == "pass" else f"        self.{fname} = None")
    for m in methods:
        ret = f" -> {m['ret']}" if m.get("ret") else ""
        lines += ["", f"    def {m['name']}({_sig_args(m, 'self')}){ret}:"]
        if m.get("purpose"):
            lines.append(f'        """{m["purpose"]}"""')
        lines.append("        raise NotImplementedError")
    return "py", "\n".join(lines).rstrip("\n") + "\n"


# ── Java ─────────────────────────────────────────────────────────────────────

def _java_interface(name, methods, **_):
    lines = [f"public interface {name} {{"]
    for m in methods:
        ret = m.get("ret") or "void"
        purpose = f"  // {m['purpose']}" if m.get("purpose") else ""
        lines.append(f"    {ret} {m['name']}({m.get('args', '')});{purpose}")
    lines.append("}")
    return "java", "\n".join(lines) + "\n"


def _java_abstract(name, methods, interface_name="", **_):
    impl = f" implements {interface_name}" if interface_name else ""
    lines = [f"public abstract class {name}{impl} {{"]
    for m in methods:
        ret = m.get("ret") or "void"
        lines.append(f"    public abstract {ret} {m['name']}({m.get('args', '')});")
    lines.append("}")
    return "java", "\n".join(lines) + "\n"


def _java_class(name, fields, methods, base_name="", **_):
    ext = f" extends {base_name}" if base_name else ""
    lines = [f"public class {name}{ext} {{"]
    for fname in fields:
        lines.append(f"    private Object {fname};")
    for m in methods:
        ret = m.get("ret") or "void"
        lines.append(f"\n    public {ret} {m['name']}({m.get('args', '')}) {{")
        if m.get("purpose"):
            lines.append(f"        // {m['purpose']}")
        lines.append('        throw new UnsupportedOperationException("not implemented");')
        lines.append("    }")
    lines.append("}")
    return "java", "\n".join(lines) + "\n"


# ── TypeScript (JS gets class-only, see _LANG_ADAPTERS) ────────────────────────

def _ts_interface(name, methods, **_):
    lines = [f"export interface {name} {{"]
    for m in methods:
        ret = m.get("ret") or "void"
        purpose = f"  // {m['purpose']}" if m.get("purpose") else ""
        lines.append(f"  {m['name']}({m.get('args', '')}): {ret};{purpose}")
    lines.append("}")
    return "ts", "\n".join(lines) + "\n"


def _ts_abstract(name, methods, interface_name="", **_):
    impl = f" implements {interface_name}" if interface_name else ""
    lines = [f"export abstract class {name}{impl} {{"]
    for m in methods:
        ret = m.get("ret") or "void"
        lines.append(f"  abstract {m['name']}({m.get('args', '')}): {ret};")
    lines.append("}")
    return "ts", "\n".join(lines) + "\n"


def _ts_class(name, fields, methods, base_name="", **_):
    ext = f" extends {base_name}" if base_name else ""
    lines = [f"export class {name}{ext} {{"]
    for fname in fields:
        lines.append(f"  {fname}: any;")
    for m in methods:
        ret = m.get("ret") or "void"
        lines.append(f"\n  {m['name']}({m.get('args', '')}): {ret} {{")
        if m.get("purpose"):
            lines.append(f"    // {m['purpose']}")
        lines.append('    throw new Error("not implemented");')
        lines.append("  }")
    lines.append("}")
    return "ts", "\n".join(lines) + "\n"


def _js_class(name, fields, methods, base_name="", **_):
    ext = f" extends {base_name}" if base_name else ""
    lines = [f"class {name}{ext} {{", "  constructor() {"]
    if base_name:
        lines.append("    super();")
    for fname in fields:
        lines.append(f"    this.{fname} = null;")
    lines.append("  }")
    for m in methods:
        lines.append(f"\n  {m['name']}({m.get('args', '')}) {{")
        if m.get("purpose"):
            lines.append(f"    // {m['purpose']}")
        lines.append('    throw new Error("not implemented");')
        lines.append("  }")
    lines.append("}")
    lines.append("")
    lines.append(f"module.exports = {name};")
    return "js", "\n".join(lines) + "\n"


# ── Go -- no classes, no abstract classes; structural interfaces + structs ────

def _go_interface(name, methods, **_):
    lines = [f"type {name} interface {{"]
    for m in methods:
        ret = f" {m['ret']}" if m.get("ret") else ""
        purpose = f"  // {m['purpose']}" if m.get("purpose") else ""
        lines.append(f"\t{m['name'][0].upper()}{m['name'][1:]}({m.get('args', '')}){ret}{purpose}")
    lines.append("}")
    return "go", "\n".join(lines) + "\n"


def _go_class(name, fields, methods, base_name="", **_):
    # base_name ignored -- Go has no inheritance; embed the interface's
    # method set only insofar as this struct implements it structurally.
    lines = [f"type {name} struct {{"]
    for fname in fields:
        lines.append(f"\t{fname[0].upper()}{fname[1:]} interface{{}}")
    lines.append("}")
    for m in methods:
        ret = f" {m['ret']}" if m.get("ret") else ""
        mname = m['name'][0].upper() + m['name'][1:]
        lines.append(f"\nfunc (r *{name}) {mname}({m.get('args', '')}){ret} {{")
        if m.get("purpose"):
            lines.append(f"\t// {m['purpose']}")
        lines.append('\tpanic("not implemented")')
        lines.append("}")
    return "go", "\n".join(lines) + "\n"


_LANG_ADAPTERS = {
    # "case": "match" languages require the file name to exactly match the
    # type name, case included -- Java (and C#, not yet implemented) reject
    # compilation otherwise ("class Shop is public, should be declared in a
    # file named Shop.java"). "case": "lower" languages only have a lowercase
    # naming CONVENTION, not a compiler requirement -- lowercase-slug is fine.
    "py":   {"ext": "py",   "case": "lower", "interface": _py_interface,   "abstract": _py_abstract,   "class": _py_class},
    "java": {"ext": "java", "case": "match", "interface": _java_interface, "abstract": _java_abstract, "class": _java_class},
    "ts":   {"ext": "ts",   "case": "lower", "interface": _ts_interface,   "abstract": _ts_abstract,   "class": _ts_class},
    "js":   {"ext": "js",   "case": "lower", "interface": None,             "abstract": None,           "class": _js_class},
    "go":   {"ext": "go",   "case": "lower", "interface": _go_interface,    "abstract": None,           "class": _go_class},
}


def _write_skeleton_file(code_dir: str, lang: str, artifact: str, name: str,
                         fields: list = None, methods: list = None,
                         base_name: str = "", interface_name: str = "") -> str:
    """Render and write an interface/abstract/class skeleton in the target
    language. Returns the written path, or "" if this language/artifact
    combination has no adapter (e.g. "abstract" for Go, or any artifact for
    a language nobody's added yet) -- callers must handle that gracefully,
    not treat it as an error."""
    adapter = _LANG_ADAPTERS.get(lang)
    if not adapter or not adapter.get(artifact):
        return ""
    render = adapter[artifact]
    if artifact == "interface":
        ext, content = render(name, methods or [])
    elif artifact == "abstract":
        ext, content = render(name, methods or [], interface_name=interface_name)
    else:  # class
        ext, content = render(name, fields or [], methods or [], base_name=base_name)
    _os.makedirs(code_dir, exist_ok=True)
    slug = _slug(name)
    basename = slug if adapter.get("case") == "match" else slug.lower()
    path = _os.path.join(code_dir, f"{basename}.{ext}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ── file layout ───────────────────────────────────────────────────────────────

def _arch_dir(workdir: str, plan_name: str, task_key: str) -> str:
    return _os.path.join(workdir, ".1bcoder", "arch", plan_name, task_key)


def _item_path(arch_dir: str, node_id: str) -> str:
    return _os.path.join(arch_dir, f"item_{node_id}.md")


def _research_path(arch_dir: str, node_id: str) -> str:
    return _os.path.join(arch_dir, f"item_{node_id}-research.md")


def _write_node(arch_dir: str, node_id: str, level: str, research: str, decision: str):
    _os.makedirs(arch_dir, exist_ok=True)
    with open(_research_path(arch_dir, node_id), "w", encoding="utf-8") as f:
        f.write(f"# research: {level} ({node_id})\n\n{research}\n")
    with open(_item_path(arch_dir, node_id), "w", encoding="utf-8") as f:
        f.write(f"# {level}: {node_id}\n\n{decision}\n")


def _read_node_decision(arch_dir: str, node_id: str) -> str:
    """Read back a previously-written item_<id>.md and strip its
    '# {level}: {node_id}' header line, recovering the raw decision text --
    the inverse of _write_node. Used to resume a node that was already
    generated in an earlier, interrupted run instead of regenerating it."""
    with open(_item_path(arch_dir, node_id), encoding="utf-8") as f:
        lines = f.read().splitlines()
    # header is "# {level}: {node_id}", then a blank line, then the decision
    body = lines[1:] if lines and lines[0].startswith("#") else lines
    while body and not body[0].strip():
        body = body[1:]
    return "\n".join(body).strip()


def _leaf_done_path(arch_dir: str, leaf_id: str) -> str:
    return _os.path.join(arch_dir, f"leaf_{leaf_id}.done")


def _resolve_root_task(plan_name: str, task_arg: str) -> str:
    """If task_arg is a bare dotted id (e.g. "3.2"), read spec_<id>.<n>.md
    files for that leaf as the root task description; otherwise task_arg
    itself is the literal task text."""
    if _re.match(r'^[0-9]+(\.[0-9]+)*$', task_arg.strip()):
        plan_dir = _os.path.join(_os.getcwd(), ".1bcoder", "planMD", plan_name)
        spec_dir = _dspec._default_spec_dir(plan_dir)
        if _os.path.isdir(spec_dir):
            parts = []
            i = 1
            while True:
                p = _dspec._spec_path(spec_dir, task_arg, i)
                if not _os.path.isfile(p):
                    break
                with open(p, encoding="utf-8") as f:
                    parts.append(f.read())
                i += 1
            if parts:
                return "\n\n".join(parts)
    return task_arg


# ── ticket claiming (for --next / --loop) ───────────────────────────────────────

def _plan_dir(plan_name: str) -> str:
    return _os.path.join(_os.getcwd(), ".1bcoder", "planMD", plan_name)


def _open_task_ids(plan_name: str) -> list:
    """Ordered (numeric-tuple sort) list of tasks.md row ids not marked
    done ('[x]'). Reuses deepagent_task's own row regex directly instead of
    re-deriving it, so both stay in sync if the tasks.md format ever
    changes."""
    tasks_path = _dtask._tasks_path(_plan_dir(plan_name))
    if not _os.path.isfile(tasks_path):
        return []
    ids = []
    with open(tasks_path, encoding="utf-8") as f:
        for line in f:
            m = _dtask._ROW_RE.match(line.strip())
            if not m:
                continue
            mark, tid = m.group(1), m.group(2)
            if mark != "x":
                ids.append(tid)
    ids.sort(key=lambda x: tuple(int(p) for p in x.split(".")))
    return ids


def _claim_path(arch_dir: str) -> str:
    return _os.path.join(arch_dir, ".claim")


def _done_path(arch_dir: str) -> str:
    return _os.path.join(arch_dir, ".done")


def _try_claim(arch_dir: str) -> bool:
    """Atomically claim a ticket's arch dir for this process. True if
    claimed, False if another live process already holds it (or it's
    already done). A claim older than _CLAIM_STALE_SECONDS with no
    matching .done is treated as abandoned -- e.g. the owning process was
    killed, which this session hit more than once with backgrounded runs --
    and gets removed so it can be retaken; the actual retake still goes
    through the same O_EXCL create below, so a genuinely-concurrent racer
    still can't double-claim it."""
    _os.makedirs(arch_dir, exist_ok=True)
    if _os.path.isfile(_done_path(arch_dir)):
        return False
    claim_file = _claim_path(arch_dir)
    if _os.path.isfile(claim_file):
        age = _time.time() - _os.path.getmtime(claim_file)
        if age < _CLAIM_STALE_SECONDS:
            return False
        try:
            _os.remove(claim_file)
        except OSError:
            pass
    try:
        fd = _os.open(claim_file, _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
    except FileExistsError:
        return False
    with _os.fdopen(fd, "w") as f:
        f.write(f"pid={_os.getpid()}\nhost={_socket.gethostname()}\n"
                f"claimed_at={_time.time()}\n")
    return True


def _mark_done(arch_dir: str):
    with open(_done_path(arch_dir), "w", encoding="utf-8") as f:
        f.write(f"done_at={_time.time()}\n")


def _release_claim(arch_dir: str):
    """Release a claim without marking done, e.g. after a failure, so a
    retry can pick it back up immediately instead of waiting out the
    staleness window."""
    try:
        _os.remove(_claim_path(arch_dir))
    except OSError:
        pass


# ── entry point ───────────────────────────────────────────────────────────────

def _run_one(chat, plan_name: str, task_arg: str, lang: str, skip_levels: set,
            arch_dir: str = None) -> bool:
    """Run the full interface->abstract->approach->class->leaf chain for ONE
    ticket. Returns True on success (a class + at least one method were
    produced), False otherwise -- callers (--next/--loop) use this to decide
    whether to mark the ticket done or release its claim for retry."""
    root_task = _resolve_root_task(plan_name, task_arg)
    task_key = _re.sub(r'[^A-Za-z0-9.]+', '_', task_arg.strip())[:40].strip("_") or "task"
    if arch_dir is None:
        arch_dir = _arch_dir(_os.getcwd(), plan_name, task_key)
    _os.makedirs(arch_dir, exist_ok=True)
    code_dir = _code_dir(_os.getcwd(), plan_name, task_key)

    print(f"[deepagent_architect] plan : {plan_name}")
    print(f"[deepagent_architect] task : {task_arg}")
    print(f"[deepagent_architect] dir  : {_os.path.relpath(arch_dir)}")
    print(f"[deepagent_architect] code : {_os.path.relpath(code_dir)}")
    if skip_levels:
        print(f"[deepagent_architect] skip : {', '.join(sorted(skip_levels))}")

    node_id = "1"
    first_level = True
    parent_task_text = root_task
    active_levels = []   # list of (level, node_id, decision_text)
    interface_name = ""  # tracked across levels for abstract/class inheritance
    abstract_name = ""

    for level in LEVELS:
        if level in skip_levels:
            print(f"\n[deepagent_architect] {level}: skipped (--skip)")
            continue
        if not first_level:
            node_id = node_id + ".1"
        first_level = False

        print(f"\n[deepagent_architect] === {level} ({node_id}) ===")

        if _os.path.isfile(_item_path(arch_dir, node_id)):
            print(f"[deepagent_architect] [resume] {node_id} already exists -- skipping")
            decision = _read_node_decision(arch_dir, node_id)
        else:
            question = _RESEARCH_QUESTIONS[level].format(task=parent_task_text)
            print("[deepagent_architect] researching...")
            research = _run_research_agent(chat, question)

            decision = _decide(chat, level, parent_task_text, research, lang=lang)
            _write_node(arch_dir, node_id, level, research, decision)
            print(f"[deepagent_architect] wrote item_{node_id}.md")

        decision_upper = decision.strip().upper()
        evidence_skipped = level == "abstract" and decision_upper.startswith("SKIP")
        reused = _is_reuse(decision)
        if evidence_skipped:
            print(f"[deepagent_architect] abstract: skipped by research evidence "
                  f"-- {decision[:120]}")
        else:
            if reused:
                print(f"[deepagent_architect] {level}: already implemented -- "
                      f"{decision[:160]}")
            elif level == "interface":
                name = _parse_field(decision, "NAME")
                if name:
                    interface_name = name
                    code_path = _write_skeleton_file(code_dir, lang, "interface", name,
                                                      methods=_parse_methods(decision))
                    if code_path:
                        print(f"[deepagent_architect] wrote {_os.path.relpath(code_path)}")
                    else:
                        print(f"[deepagent_architect] {lang}: no interface adapter -- "
                              f"kept item_{node_id}.md only")
            elif level == "abstract":
                name = _parse_field(decision, "NAME")
                if name:
                    abstract_name = name
                    code_path = _write_skeleton_file(code_dir, lang, "abstract", name,
                                                      methods=_parse_methods(decision),
                                                      interface_name=interface_name)
                    if code_path:
                        print(f"[deepagent_architect] wrote {_os.path.relpath(code_path)}")
                    else:
                        print(f"[deepagent_architect] {lang}: no abstract-class adapter "
                              f"(or none needed for this language) -- kept "
                              f"item_{node_id}.md only")
            active_levels.append((level, node_id, decision))
            parent_task_text = decision

    class_nodes = [(lvl, nid, dec) for lvl, nid, dec in active_levels if lvl == "class"]
    if not class_nodes:
        print("\n[deepagent_architect] no class node produced -- "
              "stopping before leaf generation")
        return False

    _, class_id, class_decision = class_nodes[-1]

    if _is_reuse(class_decision):
        if _reuse_target_is_stub(class_decision):
            print(f"\n[deepagent_architect] class: model claimed REUSE but the "
                  f"referenced file is still a stub (no real method bodies) -- "
                  f"retrying the class design: {class_decision.strip()[:160]}")
            try:
                with open(_research_path(arch_dir, class_id), encoding="utf-8") as f:
                    class_research = f.read()
            except OSError:
                class_research = ""
            class_research += (
                "\n\nCORRECTION: your previous REUSE claim above was rejected -- "
                "the file it named is a stub (interface/abstract skeleton with "
                "no real method bodies), not a finished class. Design the "
                "actual concrete class now; do not output REUSE again for "
                "that same file."
            )
            class_decision = _decide(chat, "class", parent_task_text, class_research, lang=lang)
            _write_node(arch_dir, class_id, "class", class_research, class_decision)
            print(f"[deepagent_architect] rewrote item_{class_id}.md")
            if _is_reuse(class_decision) and not _reuse_target_is_stub(class_decision):
                print(f"\n[deepagent_architect] class already fully implemented -- "
                      f"{class_decision.strip()[:160]}")
                print(f"[deepagent_architect] done -- reused existing implementation, "
                      f"no new code generated")
                return True
        else:
            print(f"\n[deepagent_architect] class already fully implemented -- "
                  f"{class_decision.strip()[:160]}")
            print(f"[deepagent_architect] done -- reused existing implementation, "
                  f"no new code generated")
            return True

    class_name = _parse_field(class_decision, "NAME") or "the class"
    fields = _parse_fields(class_decision)
    methods = _parse_methods(class_decision)

    class_base = abstract_name or interface_name
    class_code_path = _write_skeleton_file(code_dir, lang, "class", class_name,
                                            fields=fields, methods=methods,
                                            base_name=class_base)
    if class_code_path:
        print(f"\n[deepagent_architect] wrote {_os.path.relpath(class_code_path)}"
              + (f"  (extends {class_base})" if class_base else ""))
    else:
        print(f"\n[deepagent_architect] {lang}: no class adapter -- "
              f"kept item_{class_id}.md only")

    if not methods:
        print("\n[deepagent_architect] no methods parsed from the class design "
              "-- nothing to delegate to deepagent_code (or everything under "
              "EXISTING: was already implemented)")
        return False

    print(f"\n[deepagent_architect] === leaf: {len(methods)} method(s) -> "
          f"deepagent_code ===")
    for i, meth in enumerate(methods, 1):
        leaf_id = f"{class_id}.{i}"
        if _os.path.isfile(_leaf_done_path(arch_dir, leaf_id)):
            print(f"[deepagent_architect]   [resume] {leaf_id}. {meth['name']}"
                  f"({meth['args']}) already delegated -- skipping")
            continue
        contract = (f"Method {meth['name']} of class {class_name}: "
                    f"{meth['purpose']}. Signature: "
                    f"{meth['name']}({meth['args']}) -> {meth['ret']}")
        print(f"[deepagent_architect]   {leaf_id}. {meth['name']}({meth['args']})")
        _dcode.run(chat, f'"{contract}" --lang {lang} --depth 1')
        with open(_leaf_done_path(arch_dir, leaf_id), "w", encoding="utf-8") as f:
            f.write(f"delegated_at={_time.time()}\n")

    print(f"\n[deepagent_architect] done -- {len(active_levels)} architecture "
          f"node(s), {len(methods)} method(s) generated via deepagent_code")
    return True


def _run_next(chat, plan_name: str, lang: str, skip_levels: set) -> bool:
    """Claim and process exactly one not-yet-taken ticket from tasks.md.
    Returns True if a ticket was processed (successfully or not -- see
    _run_one's own success printout), False if none were available (empty
    backlog, or every remaining ticket is already claimed by another live
    process)."""
    ids = _open_task_ids(plan_name)
    if not ids:
        print(f"[deepagent_architect] no open tickets in {plan_name}'s tasks.md")
        return False
    for tid in ids:
        arch_dir = _arch_dir(_os.getcwd(), plan_name, tid)
        if not _try_claim(arch_dir):
            continue
        print(f"[deepagent_architect] claimed ticket {tid}  (pid {_os.getpid()})")
        ok = False
        try:
            ok = _run_one(chat, plan_name, tid, lang, skip_levels, arch_dir=arch_dir)
        finally:
            if ok:
                _mark_done(arch_dir)
                print(f"[deepagent_architect] ticket {tid} done")
            else:
                _release_claim(arch_dir)
                print(f"[deepagent_architect] ticket {tid} did not complete "
                      f"-- claim released for retry")
        return True
    print(f"[deepagent_architect] all {len(ids)} open ticket(s) already "
          f"claimed by another process")
    return False


def _run_loop(chat, plan_name: str, lang: str, skip_levels: set, max_tickets: int = 0):
    count = 0
    while True:
        if max_tickets and count >= max_tickets:
            print(f"[deepagent_architect] --loop stopping: reached --max {max_tickets}")
            break
        if not _run_next(chat, plan_name, lang, skip_levels):
            break
        count += 1
    print(f"[deepagent_architect] --loop finished: {count} ticket(s) "
          f"processed by this process (pid {_os.getpid()})")


def run(chat, args: str):
    args = args.strip()
    _usage = ('usage: /flow deepagent_architect <plan_name> <task_id_or_text> '
              '[--lang py] [--skip level1,level2]\n'
              '       /flow deepagent_architect <plan_name> --next [--lang py]\n'
              '       /flow deepagent_architect <plan_name> --loop [--lang py] [--max N]')
    if not args:
        print(_usage)
        return

    lang = "py"
    m = _re.search(r'--lang\s+(\S+)', args)
    if m:
        lang = m.group(1).lower()
        args = (args[:m.start()] + args[m.end():]).strip()

    skip_levels = set()
    m = _re.search(r'--skip\s+(\S+)', args)
    if m:
        skip_levels = {s.strip().lower() for s in m.group(1).split(",") if s.strip()}
        args = (args[:m.start()] + args[m.end():]).strip()

    loop_mode = bool(_re.search(r'--loop\b', args))
    if loop_mode:
        args = _re.sub(r'--loop\b', '', args).strip()

    next_mode = bool(_re.search(r'--next\b', args))
    if next_mode:
        args = _re.sub(r'--next\b', '', args).strip()

    max_tickets = 0
    m = _re.search(r'--max\s+(\d+)', args)
    if m:
        max_tickets = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    if loop_mode or next_mode:
        plan_name = args.strip()
        if not plan_name:
            print(_usage)
            return
        print(f"[deepagent_architect] plan : {plan_name}")
        print(f"[deepagent_architect] mode : {'--loop' if loop_mode else '--next'}"
              + (f"  (max {max_tickets})" if loop_mode and max_tickets else ""))
        if skip_levels:
            print(f"[deepagent_architect] skip : {', '.join(sorted(skip_levels))}")
        if loop_mode:
            _run_loop(chat, plan_name, lang, skip_levels, max_tickets)
        else:
            _run_next(chat, plan_name, lang, skip_levels)
        return

    if args.startswith('"'):
        end_q = args.find('"', 1)
        if end_q != -1:
            plan_name_rest = args[end_q + 1:].strip()
            task_arg = args[1:end_q]
            parts = plan_name_rest.split(None, 1)
            plan_name = parts[0] if parts else ""
        else:
            plan_name, task_arg = "", ""
    else:
        parts = args.split(None, 1)
        plan_name = parts[0] if parts else ""
        task_arg = parts[1].strip() if len(parts) > 1 else ""
        if task_arg.startswith('"') and task_arg.endswith('"') and len(task_arg) > 1:
            task_arg = task_arg[1:-1]

    if not plan_name or not task_arg:
        print(_usage)
        return

    _run_one(chat, plan_name, task_arg, lang, skip_levels)
