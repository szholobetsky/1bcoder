"""deepagent_merge — fold one deepagent_md/spec/task plan tree into another,
renumbering the source's top-level IDs to continue right after the target's
own highest top-level ID.

No LLM calls — deterministic tree surgery, same "compiler not generator"
principle as deepagent_task.py. Useful when a later `deepagent_md` run for a
new feature (plan2) should really have been a continuation of an
already-in-progress plan (plan1): plan1's last top-level branch being "4"
means plan2's "1.1.1.1" becomes "5.1.1.1" once merged in.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow deepagent_merge <source_plan> -> <target_plan> [--apply] [--delete-source]

  --apply           actually write the merge (default is a dry-run report
                     only — the first flow in this codebase to default to
                     dry-run, since this is the first genuinely destructive
                     multi-file flow)
  --delete-source   after a successful --apply, remove the source plan's
                     three folders (planMD/spec/tasks). Off by default —
                     the source plan is left completely untouched unless
                     this is explicitly passed alongside --apply.

Only the FIRST dot-segment of every id ever changes (parent/child
relationships live entirely in the remaining segments, which stay literal),
so renumbering is one pure string-shift function, not an id->id dict:

  _shift_id("1.1.1.4", offset=4) -> "5.1.1.4"

offset = target plan's current highest top-level id (0 if target is empty,
in which case the merge behaves like a straight copy). Every shifted id's
top segment is then strictly greater than every existing target id's top
segment, so collisions are structurally impossible.

What gets touched:
  item_<id>.md          copied + renamed; content never contains an id, so
                         no rewrite needed (confirmed against deepagent_md.py:
                         _read_file_title/_collect_node_ids never look inside
                         the body for structure)
  spec_<id>.<i>.md       re-written via deepagent_spec.py's own _write_spec()
                         with the shifted leaf id, so format fidelity is
                         guaranteed rather than hand-patched with regexes
  refs.json              "node" fields remapped and appended into target's
                         own refs.json (only if source used --ref)
  tasks.md               regenerated the same way deepagent_task.py already
                         does on every re-run (_build_rows over the now-
                         merged item/spec files), seeded with the source's
                         existing tags remapped onto their new ids, so
                         status/assignee/due/done survive the renumbering
                         instead of resetting to +backlog. Rows typed
                         directly into tasks.md with no backing item_<id>.md
                         or spec_<id>.<i>.md are preserved as-is (title,
                         tags, position) rather than silently dropped, and
                         they also count toward the offset — a manual "4"/
                         "4.1" in the target pushes the source to "5.x", not
                         a colliding "4.x"

What's deliberately left alone:
  colors.md              global across ALL plans already, never plan-keyed
  dashboard_flow.md      not a 1bcoder concept at all (Alkonost-only)
  _deepagent_meta.yaml   holds only generation-run metadata, no ids, and
                         there's no single correct way to merge two runs'
                         generation history — left untouched on both sides

Example:
  /flow deepagent_merge plan2 -> plan1
  /flow deepagent_merge plan2 -> plan1 --apply
  /flow deepagent_merge plan2 -> plan1 --apply --delete-source
"""
import os as _os
import re as _re
import json as _json
import shutil as _shutil


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
_dtask = _load("_dtask", "deepagent_task.py")

_SPEC_FILE_RE = _re.compile(r'^spec_(.+)\.(\d+)\.md$')


def _shift_id(old_id: str, offset: int) -> str:
    parts = old_id.split(".")
    parts[0] = str(int(parts[0]) + offset)
    return ".".join(parts)


def _resolve_plan_dir(plan_name: str) -> str:
    base = _os.path.join(_os.getcwd(), ".1bcoder", "planMD")
    return plan_name if _os.path.isabs(plan_name) else _os.path.join(base, plan_name)


def _tasks_path_readonly(plan_dir: str) -> str:
    """Same path _dtask._tasks_path would compute, without its side effect
    of os.makedirs()-ing the tasks tree into existence — needed so a
    dry-run report never touches disk."""
    dot1bcoder = _os.path.dirname(_os.path.dirname(plan_dir))
    plan_name = _os.path.basename(plan_dir)
    return _os.path.join(dot1bcoder, "tasks", plan_name, "tasks.md")


def _parse_args(args: str):
    do_apply = "--apply" in args
    delete_source = "--delete-source" in args
    rest = args.replace("--apply", "").replace("--delete-source", "")
    if "->" not in rest:
        return None
    src, _, tgt = rest.partition("->")
    src = src.strip().strip('"\'')
    tgt = tgt.strip().strip('"\'')
    if not src or not tgt:
        return None
    return src, tgt, do_apply, delete_source


def _list_spec_files(spec_dir: str) -> list:
    """[(leaf_id, sec_index, filename), ...] for every spec_<leaf_id>.<i>.md
    in spec_dir. leaf_id is dotted and captured greedily (correct even
    though it contains dots itself, since sec_index is always the LAST
    dot-segment, a plain integer, right before the .md extension)."""
    if not _os.path.isdir(spec_dir):
        return []
    hits = []
    for fname in _os.listdir(spec_dir):
        m = _SPEC_FILE_RE.match(fname)
        if m:
            hits.append((m.group(1), int(m.group(2)), fname))
    return hits


def _parse_full_tasks(tasks_path: str) -> dict:
    """id -> (title, tags) for every existing row, title included — unlike
    _dtask._parse_existing_tasks, which drops titles on purpose since
    deepagent_task.py always refreshes them from a backing item/spec file.
    Needed here because a row typed directly into tasks.md by hand (no
    item_<id>.md or spec_<id>.<i>.md behind it) has no "current source" to
    refresh a title from, and _collect_node_ids can't see it either — so
    both the offset calculation and the tasks.md rewrite need to know about
    these ids from tasks.md itself, not just from the file tree."""
    result = {}
    if not _os.path.isfile(tasks_path):
        return result
    with open(tasks_path, encoding="utf-8") as f:
        for line in f:
            m = _dtask._ROW_RE.match(line.strip())
            if not m:
                continue
            task_id = m.group(2)
            rest = m.group(3)
            tm = _dtask._TAG_START_RE.search(rest)
            if tm:
                title, tags = rest[:tm.start(1)].strip(), rest[tm.start(1):].strip()
            else:
                title, tags = rest.strip(), ""
            result[task_id] = (title, tags)
    return result


def _parse_spec_title_body(path: str):
    """(title, body) from an existing spec file — title is everything after
    the first ':' on the title line (same loose match as deepagent_task.py's
    own _SPEC_TITLE_RE), body is everything between the title line and the
    trailing 'SOURCE:' line, matching _write_spec's own template exactly so
    the round trip is faithful."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    first_line = lines[0] if lines else ""
    tm = _dtask._SPEC_TITLE_RE.match(first_line)
    title = tm.group(1).strip() if tm else first_line.lstrip("#").strip()
    body_lines = lines[1:]
    while body_lines and (not body_lines[-1].strip() or body_lines[-1].startswith("SOURCE:")):
        body_lines.pop()
    return title, "\n".join(body_lines).strip()


def run(chat, args: str):
    """Merge one deepagent_md/spec/task plan tree into another, renumbering top-level ids to continue after the target's own — dry-run by default."""
    parsed = _parse_args(args)
    if not parsed:
        print("usage: /flow deepagent_merge <source_plan> -> <target_plan> [--apply] [--delete-source]")
        return
    source_name, target_name, do_apply, delete_source = parsed

    source_dir = _resolve_plan_dir(source_name)
    target_dir = _resolve_plan_dir(target_name)
    if not _os.path.isdir(source_dir):
        print(f"[deepagent_merge] source plan not found: {source_dir}")
        return
    if not _os.path.isdir(target_dir):
        print(f"[deepagent_merge] target plan not found: {target_dir}")
        return
    if _os.path.normcase(_os.path.normpath(source_dir)) == _os.path.normcase(_os.path.normpath(target_dir)):
        print("[deepagent_merge] source and target resolve to the same plan — nothing to merge")
        return

    source_ids = _dmd._collect_node_ids(source_dir)
    target_ids = _dmd._collect_node_ids(target_dir)
    if not source_ids:
        print(f"[deepagent_merge] no nodes found in source plan: {source_dir}")
        return

    # offset must clear the highest top-level id used ANYWHERE in the
    # target — not just ids backed by an item_<id>.md, but also any row
    # typed directly into the target's tasks.md by hand (_collect_node_ids
    # can't see those, since there's no file for them). Missing this let a
    # real merge land a shifted "4.x" right on top of a manually-added "4"/
    # "4.1" row that should have pushed the offset to include it.
    target_tasks_full = _parse_full_tasks(_tasks_path_readonly(target_dir))
    offset_candidates = [int(i) for i in _dmd._top_level_ids(target_ids)]
    offset_candidates += [int(tid.split(".")[0]) for tid in target_tasks_full]
    offset = max(offset_candidates, default=0)
    id_pairs = sorted(
        ((old, _shift_id(old, offset)) for old in source_ids),
        key=lambda p: tuple(int(x) for x in p[0].split(".")),
    )

    collisions = [new_id for _old, new_id in id_pairs if new_id in target_ids]
    if collisions:
        print(f"[deepagent_merge] refusing to proceed — id collision(s) detected: {collisions}")
        return

    source_spec_dir = _dspec._default_spec_dir(source_dir)
    target_spec_dir = _dspec._default_spec_dir(target_dir)
    spec_files = _list_spec_files(source_spec_dir)

    source_tasks_path = _tasks_path_readonly(source_dir)
    source_tags = _dtask._parse_existing_tasks(source_tasks_path)
    source_refs = _dmd._load_refs(source_dir)

    target_rows_preview = _dtask._build_rows(target_dir, target_spec_dir)
    target_row_ids_preview = {tid for tid, _t, _d in target_rows_preview}
    manual_orphans = sorted(
        (tid for tid in target_tasks_full if tid not in target_row_ids_preview),
        key=lambda x: tuple(int(p) for p in x.split(".")),
    )

    print(f"[deepagent_merge] source : {source_dir}")
    print(f"[deepagent_merge] target : {target_dir}")
    print(f"[deepagent_merge] offset : +{offset} (target's highest top-level id, incl. manual tasks.md rows)")
    print(f"[deepagent_merge] items  : {len(id_pairs)} node(s)")
    for old_id, new_id in id_pairs:
        print(f"[deepagent_merge]   {old_id} -> {new_id}")
    print(f"[deepagent_merge] specs  : {len(spec_files)} file(s)")
    print(f"[deepagent_merge] refs   : {len(source_refs)} entry(ies){' (none)' if not source_refs else ''}")
    print(f"[deepagent_merge] tags   : {len(source_tags)} existing task row(s) will carry their status/tags over")
    if manual_orphans:
        print(f"[deepagent_merge] manual : {len(manual_orphans)} tasks.md row(s) with no backing item/spec file will be preserved as-is: {manual_orphans}")

    if not do_apply:
        print("[deepagent_merge] DRY RUN — no changes written. Re-run with --apply to perform this merge.")
        return

    try:
        # 1. item_<id>.md — pure copy + rename, content never contains an id
        for old_id, new_id in id_pairs:
            _shutil.copyfile(_dmd._item_path(source_dir, old_id), _dmd._item_path(target_dir, new_id))

        # 2. refs.json — remap "node" fields, append into target's own list
        if source_refs:
            remapped = [{**r, "node": _shift_id(r["node"], offset)} for r in source_refs]
            merged_refs = _dmd._load_refs(target_dir) + remapped
            with open(_dmd._refs_path(target_dir), "w", encoding="utf-8") as f:
                f.write(_json.dumps(merged_refs, ensure_ascii=False, indent=2))

        # 3. spec_<id>.<i>.md — re-written via deepagent_spec's own writer
        if spec_files:
            _os.makedirs(target_spec_dir, exist_ok=True)
            project = _dspec._project_name(target_dir)
            for leaf_id, sec_index, fname in spec_files:
                title, body = _parse_spec_title_body(_os.path.join(source_spec_dir, fname))
                new_leaf_id = _shift_id(leaf_id, offset)
                source_ref = f"item_{new_leaf_id}.md#{sec_index}"
                _dspec._write_spec(target_spec_dir, project, new_leaf_id, sec_index, title, body, source_ref)

        # 4. tasks.md — regenerated the same way deepagent_task.py's own
        #    run() does on every re-run, seeded with source's tags remapped
        #    onto their new ids so status/assignee/due/done survive, PLUS
        #    any target row with no backing item/spec file (typed directly
        #    into tasks.md by hand) re-appended as-is — _build_rows alone
        #    has no way to know those ids exist, so without this step they
        #    would silently vanish every time this rewrite runs.
        target_tasks_path = _dtask._tasks_path(target_dir)   # creates .1bcoder/tasks/<target>/ as needed
        _dtask._ensure_colors_file(target_dir)
        target_full = _parse_full_tasks(target_tasks_path)
        target_existing = {tid: tags for tid, (_title, tags) in target_full.items()}
        source_existing_shifted = {_shift_id(k, offset): v for k, v in source_tags.items()}
        merged_existing = {**source_existing_shifted, **target_existing}

        rows = _dtask._build_rows(target_dir, target_spec_dir)
        row_ids = {tid for tid, _t, _d in rows}
        orphan_ids = [tid for tid in target_full if tid not in row_ids]
        for tid in orphan_ids:
            orphan_title, _tags = target_full[tid]
            rows.append((tid, orphan_title, tid.count(".") + 1))
        rows.sort(key=lambda r: tuple(int(p) for p in r[0].split(".")))

        lines = []
        new_count = 0
        for task_id, title, depth in rows:
            if task_id in merged_existing:
                tags = merged_existing[task_id]
            else:
                tags = _dtask._DEFAULT_TAGS
                new_count += 1
            mark = "x" if (m := _dtask._DONE_RE.search(tags)) and m.group(1) else " "
            indent = "  " * (depth - 1)
            safe_title = _dtask._sanitize_title(title)
            lines.append(f"{indent}[{mark}] {target_name}-{task_id}. {safe_title} {tags}".rstrip())
        with open(target_tasks_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[deepagent_merge] error during apply: {e}")
        return

    print(f"[deepagent_merge] merged {len(id_pairs)} node(s), {len(spec_files)} spec file(s) into {target_dir}")
    print(f"[deepagent_merge] tasks.md rewritten: {target_tasks_path}")
    if orphan_ids:
        print(f"[deepagent_merge] preserved {len(orphan_ids)} manual tasks.md row(s) with no backing file: {sorted(orphan_ids)}")

    if delete_source:
        source_tasks_dir = _os.path.dirname(source_tasks_path)
        removed = []
        for d in (source_dir, source_spec_dir, source_tasks_dir):
            if _os.path.isdir(d):
                _shutil.rmtree(d)
                removed.append(d)
        if removed:
            print("[deepagent_merge] --delete-source: removed")
            for d in removed:
                print(f"[deepagent_merge]   {d}")
        else:
            print("[deepagent_merge] --delete-source: nothing to delete (source folders already absent)")
