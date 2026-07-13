"""deepagent_task — roll up a deepagent_md tree + deepagent_spec units into
a flat, nested-checkbox tasks.md.

No LLM calls — this is a deterministic compiler, not a generator (same
principle as ENCYCLOPEDIA.md's Change History section or GLOSSARY.md's flat
index over term.md: the deep artifact is generative, the flat rollup is
not). Every row comes from a title already on disk — an item_<id>.md's own
title for a container node, or a spec_<leaf_id>.<i>.md's own title for an
atomic unit.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow deepagent_task <plan_name>

Tree shape: walks the full deepagent_md tree (_collect_node_ids), container
nodes (with children) become rows using their own item_<id>.md title; a
LEAF node expands one level deeper using its own spec_<leaf_id>.<i>.md
files (if deepagent_spec has been run on it yet — if not, the leaf is just
a childless row, same as any other, not an error).

Row format (one line per task, 2-space indent per depth level), tag-based
and order-independent — NOT positional fields (rejected that design: the
first version used "; lane; status; assignee; date", replaced after
deciding to adopt a todo.txt-flavored tag convention instead, see below):

  [ ] <plan_name>-<id>. <title> #tag @module +flag %user due:YYYY-MM-DD _done:YYYY-MM-DD

  #tag        classification, any number — filterable (e.g. #backend #api)
  @module     single grouping tag — drives card color via a separately
              maintained .1bcoder/tasks/colors.md legend (@module=color
              pairs), which this tool never reads or writes
  +flag       status/priority, any number, open-ended (e.g. +backlog,
              +active, +urgent as a modifier alongside either lane)
  %user       assignee (% chosen over !/$/&/= specifically to avoid
              colliding with markdown image syntax "![" and with KaTeX's
              "$...$" math delimiters, both already in use elsewhere in
              this codebase, e.g. /proc mdx)
  due:/_done: dates; _done: presence (non-empty) is what marks the row [x]

Everything from the title up to (not including) the first token starting
with one of "#@+%_" is the title; everything from there to end of line is
the tag string, kept byte-for-byte on re-runs — this tool never parses or
re-derives individual tag values, only preserves the whole trailing string
per id. A first-seen id gets "+backlog" and nothing else; a human/UI adds
everything past that. Re-running is safe: existing rows keep their full tag
string untouched, only the title is refreshed from the current source and
new ids get appended with the "+backlog" default.

Checkbox mark: "[x]" if the row's tag string contains a "_done:" with a
non-empty date, "[ ]" otherwise.

Output: .1bcoder/tasks/<plan_name>/tasks.md — its own top-level artifact
folder, sibling to .1bcoder/planMD/<plan_name>/ and .1bcoder/spec/<plan_name>/,
same per-plan subdirectory nesting as both. colors.md lives one level up,
directly in .1bcoder/tasks/ — shared across every plan, not per-plan.

Example:
  /flow deepagent_task plan5
"""
import os as _os
import re as _re


def _load(name: str, filename: str):
    import importlib.util as _iu
    here = _os.path.dirname(_os.path.abspath(__file__))
    path = _os.path.join(here, filename)
    spec = _iu.spec_from_file_location(name, path)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_dmd = _load("_dmd", "deepagent_md.py")
_dspec = _load("_dspec", "deepagent_spec.py")

_DEFAULT_TAGS = "+backlog"

# matches "[ ] <anything>-<dotted-id>. <rest>" — greedy up to the LAST "-"
# before the id, so plan names containing "-" themselves still parse
_ROW_RE = _re.compile(r'^\[([ x])\]\s+.+-([0-9]+(?:\.[0-9]+)*)\.\s*(.*)$')

# first whitespace-delimited token starting with one of these sigils ends
# the title and starts the tag string — kept in sync with the sigil set
# documented in the module docstring (#, @, +, %, _)
_TAG_START_RE = _re.compile(r'(?:^|\s)([#@+%_]\S*)')
_DONE_RE = _re.compile(r'_done:(\S+)')

_SPEC_FILENAME_RE_TMPL = r'^spec_{}\.(\d+)\.md$'
_SPEC_TITLE_RE = _re.compile(r'^#\s*[^:]+:\s*(.+)$')


def _sanitize_title(title: str) -> str:
    """Tag sigils in a title would be misread as the start of the tag
    string on the next parse — strip them from title text specifically
    (mid-word occurrences are fine, only a token *starting* with one of
    these matters, but stripping the character everywhere is simpler and
    safe: none of #@+%_ are meaningful inside a plain title anyway)."""
    return _re.sub(r'[#@+%_]', '', title).strip()


def _split_title_and_tags(rest: str) -> str:
    """Everything from the first #/@/+/%/_ -prefixed token onward — kept
    as one opaque string, never parsed field-by-field."""
    m = _TAG_START_RE.search(rest)
    return rest[m.start(1):].strip() if m else ""


def _tasks_root(plan_dir: str) -> str:
    """.1bcoder/tasks/ — shared root across ALL plans. Only colors.md lives
    directly here; per-plan output goes one level deeper, see _tasks_dir."""
    dot1bcoder = _os.path.dirname(_os.path.dirname(plan_dir))
    d = _os.path.join(dot1bcoder, "tasks")
    _os.makedirs(d, exist_ok=True)
    return d


def _tasks_dir(plan_dir: str) -> str:
    """.1bcoder/tasks/<plan_name>/ — per-plan subdirectory, same nesting
    convention as .1bcoder/planMD/<plan_name>/ and .1bcoder/spec/<plan_name>/.
    Bug fix: this originally wrote a flat <plan_name>.md file straight into
    the shared tasks root instead of its own subdirectory — inconsistent
    with both sibling conventions, and incompatible with pointing a future
    viewer service at "the directory tasks/plan5 lives in" as a single,
    self-contained folder."""
    plan_name = _os.path.basename(plan_dir)
    d = _os.path.join(_tasks_root(plan_dir), plan_name)
    _os.makedirs(d, exist_ok=True)
    return d


# One shared legend for ALL plans under this project (not per-plan — a
# task from plan3 and a task from plan5 can both use "@backend" and expect
# the same color) — lives in the shared _tasks_root, NOT inside any
# per-plan _tasks_dir. Never touched by this tool once it exists; only
# created, with generic starter defaults, the first time it's missing —
# same "create once, then hands-off" convention as everything else this
# session that a human/UI is meant to own afterward.
_COLORS_FILENAME = "colors.md"
_DEFAULT_COLORS = (
    "@backend=#4A90D9\n"
    "@frontend=#7ED321\n"
    "@database=#F5A623\n"
    "@api=#BD10E0\n"
    "@infra=#9B9B9B\n"
)


def _ensure_colors_file(plan_dir: str) -> str:
    path = _os.path.join(_tasks_root(plan_dir), _COLORS_FILENAME)
    if not _os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_COLORS)
    return path


def _tasks_path(plan_dir: str) -> str:
    return _os.path.join(_tasks_dir(plan_dir), "tasks.md")


def _parse_existing_tasks(tasks_path: str) -> dict:
    """id -> tag string (everything from the first #/@/+/%/_ token to end
    of line, verbatim). Title is deliberately NOT kept from the old file —
    it's always refreshed from the current source."""
    result = {}
    if not _os.path.isfile(tasks_path):
        return result
    with open(tasks_path, encoding="utf-8") as f:
        for line in f:
            m = _ROW_RE.match(line.strip())
            if not m:
                continue
            task_id = m.group(2)
            result[task_id] = _split_title_and_tags(m.group(3))
    return result


def _leaf_spec_children(spec_dir: str, leaf_id: str) -> list:
    """[(sec_index, title), ...] from spec_<leaf_id>.<i>.md files, sorted by
    sec_index. Empty list if deepagent_spec hasn't been run on this leaf
    yet (spec_dir missing, or no matching files) — not an error."""
    if not _os.path.isdir(spec_dir):
        return []
    pattern = _re.compile(_SPEC_FILENAME_RE_TMPL.format(_re.escape(leaf_id)))
    hits = []
    for fname in _os.listdir(spec_dir):
        m = pattern.match(fname)
        if not m:
            continue
        sec_index = int(m.group(1))
        first_line = open(_os.path.join(spec_dir, fname), encoding="utf-8").readline().strip()
        tm = _SPEC_TITLE_RE.match(first_line)
        title = tm.group(1).strip() if tm else first_line.lstrip("#").strip()
        hits.append((sec_index, title))
    hits.sort(key=lambda x: x[0])
    return hits


def _build_rows(plan_dir: str, spec_dir: str) -> list:
    """[(id, title, depth), ...] — container nodes from item_<id>.md titles;
    each leaf additionally expands one level deeper using its own spec
    units, if any exist yet."""
    all_ids = _dmd._collect_node_ids(plan_dir)
    rows = []
    for item_id in sorted(all_ids, key=lambda x: tuple(int(p) for p in x.split("."))):
        depth = item_id.count(".") + 1
        title = _dmd._read_file_title(plan_dir, item_id)
        rows.append((item_id, title, depth))
        if not _dmd._child_ids(item_id, all_ids):
            for sec_index, spec_title in _leaf_spec_children(spec_dir, item_id):
                rows.append((f"{item_id}.{sec_index}", spec_title, depth + 1))
    return rows


def run(chat, args: str):
    """Roll up a deepagent_md tree + deepagent_spec units into tasks.md — no LLM calls."""
    plan_name = args.strip().strip('"\'')
    if not plan_name:
        print("usage: /flow deepagent_task <plan_name>")
        return

    base = _os.path.join(_os.getcwd(), ".1bcoder", "planMD")
    plan_dir = plan_name if _os.path.isabs(plan_name) else _os.path.join(base, plan_name)
    if not _os.path.isdir(plan_dir):
        print(f"[deepagent_task] not found: {plan_dir}")
        return

    spec_dir = _dspec._default_spec_dir(plan_dir)
    tasks_path = _tasks_path(plan_dir)
    colors_path = _ensure_colors_file(plan_dir)
    existing = _parse_existing_tasks(tasks_path)
    rows = _build_rows(plan_dir, spec_dir)

    if not rows:
        print(f"[deepagent_task] no nodes found in {plan_dir}")
        return

    lines = []
    new_count = 0
    for task_id, title, depth in rows:
        if task_id in existing:
            tags = existing[task_id]
        else:
            tags = _DEFAULT_TAGS
            new_count += 1
        mark = "x" if (m := _DONE_RE.search(tags)) and m.group(1) else " "
        indent = "  " * (depth - 1)
        safe_title = _sanitize_title(title)
        lines.append(f"{indent}[{mark}] {plan_name}-{task_id}. {safe_title} {tags}".rstrip())

    with open(tasks_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[deepagent_task] plan   : {plan_dir}")
    print(f"[deepagent_task] specs  : {spec_dir}")
    print(f"[deepagent_task] colors : {colors_path}")
    print(f"[deepagent_task] wrote {len(rows)} row(s) ({new_count} new) to {tasks_path}")
