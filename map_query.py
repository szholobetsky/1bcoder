#!/usr/bin/env python3
"""
map_query.py — query a 1bcoder map file.

Modes:
  find   — filter file blocks by filename and/or child-line content
  trace  — follow call chain backwards from a defined identifier (BFS)
             annotated with SYMMETRY@K per depth level
  sym    — ASYMMETRY_SCORE and COHESION health report
  idiff  — ORPHAN_DRIFT + GHOST ALERT between two map snapshots

Usage:
    python map_query.py find [tokens ...] [--map FILE]
    python map_query.py trace <identifier>  [--map FILE]
    python map_query.py sym   [--k N]       [--map FILE]
    python map_query.py idiff --prev FILE   [--map FILE]

Filter token syntax for find:
    term    filename contains term
    !term   exclude if filename contains term
    \\term  include block if any child line contains term
    \\!term exclude entire block if any child contains term
    -term   show ONLY child lines containing term
    -!term  hide child lines containing term

    (no tokens → print full map)

Examples:
    python map_query.py find register
    python map_query.py find \\register !mock
    python map_query.py find auth \\UserService -!deprecated
    python map_query.py trace insertEmail
    python map_query.py trace register --map path/to/map.txt
    python map_query.py sym --k 10
    python map_query.py idiff --prev .1bcoder/map.prev.txt
"""

import re
import os
import sys
import argparse

DEFAULT_MAP = os.path.join('.1bcoder', 'map.txt')


# ── parse ───────────────────────────────────────────────────────────────────────

def parse_map(map_path: str) -> tuple:
    """Parse map.txt → (defines_map, links_map).

    defines_map : { rel_file → { name → lineno } }
    links_map   : { caller_rel → { target_rel → { name → kind } } }
    """
    with open(map_path, encoding='utf-8') as f:
        content = f.read()

    defines_map  = {}
    links_map    = {}
    current_file = None

    for line in content.splitlines():
        if line.startswith('#') or not line.strip():
            continue
        if not line.startswith(' '):
            current_file = line.strip()
            defines_map.setdefault(current_file, {})
            links_map.setdefault(current_file, {})
        elif current_file and 'defines :' in line:
            items_str = line.split('defines :', 1)[1].strip()
            for item in items_str.split(', '):
                m = re.match(r'(\w[\w-]*)\(ln:(\d+)\)', item.strip())
                if m:
                    defines_map[current_file][m.group(1)] = int(m.group(2))
        elif current_file and 'links  →' in line:
            m = re.match(r'\s+links\s+→\s+(\S+)\s+\((.+)\)', line)
            if m:
                target   = m.group(1)
                refs_str = m.group(2)
                refs = {}
                for item in refs_str.split(', '):
                    if ':' in item:
                        kind, name = item.split(':', 1)
                        refs[name.strip()] = kind.strip()
                links_map[current_file].setdefault(target, {}).update(refs)

    return defines_map, links_map


# ── symmetry helpers ─────────────────────────────────────────────────────────────

def compute_asymmetry(defines_map: dict, links_map: dict) -> tuple:
    """Return (all_defines, orphans, called_names).

    all_defines : { name → defining_file }
    orphans     : { name → defining_file }  — defines with no internal caller
    called_names: set of names referenced in any links → block
    """
    all_defines = {}
    for frel, defs in defines_map.items():
        for name in defs:
            all_defines[name] = frel

    called_names = set()
    for targets in links_map.values():
        for refs in targets.values():
            called_names.update(refs.keys())

    orphans = {name: frel for name, frel in all_defines.items()
               if name not in called_names}

    return all_defines, orphans, called_names


def compute_cohesion(links_map: dict, k: int = 5) -> tuple:
    """Return (cohesion_score, top_k_names).

    COHESION = mean intra-module fraction for top-K identifiers by out-degree.
    Out-degree of name = number of distinct caller files that reference it.
    Intra-module = dirname(caller) == dirname(target).
    """
    # name → [(caller_file, target_file)]
    name_calls: dict = {}
    for caller, targets in links_map.items():
        for target, refs in targets.items():
            for name in refs:
                name_calls.setdefault(name, []).append((caller, target))

    if not name_calls:
        return 0.0, []

    out_degree = {name: len({c for c, _ in calls})
                  for name, calls in name_calls.items()}
    top_k = sorted(out_degree, key=lambda n: -out_degree[n])[:k]

    fractions = []
    for name in top_k:
        calls = name_calls[name]
        intra = sum(1 for c, t in calls
                    if os.path.dirname(c) == os.path.dirname(t))
        fractions.append(intra / len(calls))

    return sum(fractions) / len(fractions), top_k


# ── find ────────────────────────────────────────────────────────────────────────

def find_map(map_path: str, query: str) -> tuple:
    """Search map.txt with filter syntax.

    Returns (hits, rendered_string).
    hits  — list of matching block strings (empty list means full map returned).
    rendered_string — the text to display / inject.
    """
    with open(map_path, encoding='utf-8') as f:
        content = f.read()

    tokens     = query.split()
    pos_file   = []   # term   — filename must contain
    neg_file   = []   # !term  — filename must NOT contain
    pos_child  = []   # \term  — include block if any child line contains (all terms same line)
    neg_block  = []   # \!term — exclude block if any child line contains
    show_lines = []   # -term  — show ONLY child lines containing term (whitelist)
    hide_lines = []   # -!term — hide child lines containing term

    for t in tokens:
        if t.startswith('\\!') and len(t) > 2:
            neg_block.append(t[2:].lower())
        elif t.startswith('\\') and len(t) > 1:
            pos_child.append(t[1:].lower())
        elif t.startswith('-!') and len(t) > 2:
            hide_lines.append(t[2:].lower())
        elif t.startswith('-') and len(t) > 1:
            show_lines.append(t[1:].lower())
        elif t.startswith('!') and len(t) > 1:
            neg_file.append(t[1:].lower())
        else:
            pos_file.append(t.lower())

    # no criteria → return full map
    if not any([pos_file, neg_file, pos_child, neg_block, show_lines, hide_lines]):
        return [], content

    blocks = re.split(r'\n(?=\S)', content)

    def process_block(block):
        lines       = block.split('\n')
        fname       = lines[0].lower()
        child_lines = [l for l in lines[1:] if l.strip()]

        if pos_file and not all(t in fname for t in pos_file):
            return None
        if any(t in fname for t in neg_file):
            return None
        if pos_child:
            if not any(all(t in line.lower() for t in pos_child) for line in child_lines):
                return None
        if neg_block:
            children_text = '\n'.join(child_lines).lower()
            if any(t in children_text for t in neg_block):
                return None
        if show_lines:
            child_lines = [l for l in child_lines
                           if any(t in l.lower() for t in show_lines)]
        if hide_lines:
            child_lines = [l for l in child_lines
                           if not any(t in l.lower() for t in hide_lines)]

        return lines[0] + ('\n' + '\n'.join(child_lines) if child_lines else '')

    hits = [r for b in blocks
            if not b.startswith('#')
            for r in [process_block(b)] if r is not None]

    return hits, '\n'.join(hits)


# ── trace ───────────────────────────────────────────────────────────────────────

def trace_deps(map_path: str, identifier: str, max_depth: int = 8, leaves_only: bool = False) -> str:
    """BFS forward through the dependency graph from a defined identifier.

    Shows what the identifier's file depends on (outgoing links).
    leaves_only=True: show only leaf files (no further outgoing deps).

    Returns a rendered string, or None if identifier not found.
    """
    defines_map, links_map = parse_map(map_path)

    # resolve identifier to file
    start_file, start_ln = None, None
    for frel, defs in defines_map.items():
        if identifier in defs:
            start_file = frel
            start_ln   = defs[identifier]
            break
    if not start_file:
        # try substring file match
        matches = [f for f in defines_map if identifier.replace("\\", "/") in f.replace("\\", "/")]
        if matches:
            start_file = sorted(matches, key=len)[0]

    if not start_file:
        return None

    # forward BFS: start_file → what it depends on
    # parent[file] = (parent_file, name, kind) or None for root
    parent  = {start_file: None}
    depth_of = {start_file: 0}
    queue   = [start_file]
    order   = [start_file]   # BFS visit order for tree rendering

    while queue:
        current = queue.pop(0)
        d = depth_of[current]
        if d >= max_depth:
            continue
        for target, refs in links_map.get(current, {}).items():
            if target not in parent:
                name = next(iter(refs))
                kind = refs[name]
                parent[target]   = (current, name, kind)
                depth_of[target] = d + 1
                queue.append(target)
                order.append(target)

    visited = set(order)

    if leaves_only:
        # leaf = reachable file with no outgoing links to other reachable files
        leaves = [f for f in order
                  if not any(t in visited for t in links_map.get(f, {}))]
        start_label = f"{identifier}(ln:{start_ln})" if start_ln else identifier
        lines_out = [f"deps (leaves): {start_label}  [{start_file}]", ""]
        for leaf in sorted(leaves):
            defs = defines_map.get(leaf, {})
            def_str = ", ".join(sorted(defs)[:6]) if defs else ""
            lines_out.append(f"  {leaf}" + (f"  [{def_str}]" if def_str else ""))
        return '\n'.join(lines_out)

    # full tree render
    start_label = f"{identifier}(ln:{start_ln})" if start_ln else identifier
    lines_out = [f"deps: {start_label}", f"{start_file}"]

    for frel in order[1:]:
        entry = parent[frel]
        if entry is None:
            continue
        _, name, kind = entry
        indent = "  " * depth_of[frel]
        lines_out.append(f"{indent}→ {kind}:{name}  {frel}")

    return '\n'.join(lines_out)


def trace_map(map_path: str, identifier: str, max_depth: int = 8) -> str:
    """BFS backwards through the call graph from a defined identifier.

    Returns a rendered string, or None if the identifier is not found in defines.
    """
    defines_map, links_map = parse_map(map_path)

    # locate the defining file
    start_file = None
    start_ln   = None
    for frel, defs in defines_map.items():
        if identifier in defs:
            start_file = frel
            start_ln   = defs[identifier]
            break

    if not start_file:
        return None

    # reverse index: target_file → [(caller_file, name, kind)]
    incoming = {}
    for caller, targets in links_map.items():
        for target, refs in targets.items():
            for name, kind in refs.items():
                incoming.setdefault(target, []).append((caller, name, kind))

    MAX_DEPTH = max_depth
    lines_out = [
        f'trace: {identifier}',
        f'{start_file}  [defines {identifier}(ln:{start_ln})]',
    ]
    visited = {start_file}
    queue   = [(start_file, 1)]

    while queue:
        current, depth = queue.pop(0)
        if depth > MAX_DEPTH:
            break
        indent  = '  ' * depth
        callers = incoming.get(current, [])
        for caller, name, kind in sorted(callers, key=lambda x: x[0]):
            lines_out.append(f'{indent}← {kind}:{name}  {caller}')
            if caller not in visited:
                visited.add(caller)
                queue.append((caller, depth + 1))

    return '\n'.join(lines_out)


def _resolve_id(token: str, defines_map: dict):
    """Resolve an identifier or file substring to (file, name, lineno)."""
    for frel, defs in defines_map.items():
        if token in defs:
            return frel, token, defs[token]
    matches = [f for f in defines_map if token.replace("\\", "/") in f.replace("\\", "/")]
    if not matches:
        return None, None, None
    matches.sort(key=len)
    if len(matches) > 1:
        # return all matches so caller can warn
        return matches[0], None, None
    return matches[0], None, None


def _bfs_path(start_file: str, end_file: str, links_map: dict, blocked: set = None):
    """BFS returning parent dict, trying reverse graph then forward graph.

    blocked: set of intermediate file paths to skip (for finding alternative paths).
    Returns (parent_dict, found: bool).
    """
    blocked = blocked or set()

    # build reverse index: target → [(caller, name, kind)]
    incoming = {}
    for caller, targets in links_map.items():
        for target, refs in targets.items():
            for name, kind in refs.items():
                incoming.setdefault(target, []).append((caller, name, kind))

    def _bfs(adj_fn):
        parent = {start_file: None}
        queue  = [start_file]
        while queue:
            current = queue.pop(0)
            if current == end_file:
                return parent, True
            for neighbour, name, kind in adj_fn(current):
                if neighbour not in parent and neighbour not in blocked:
                    parent[neighbour] = (current, name, kind)
                    queue.append(neighbour)
        return parent, False

    def rev_adj(node):
        return incoming.get(node, [])

    def fwd_adj(node):
        return [(t, next(iter(r)), r[next(iter(r))]) for t, r in links_map.get(node, {}).items()]

    parent, found = _bfs(rev_adj)
    if not found:
        parent, found = _bfs(fwd_adj)
    return parent, found


def _reconstruct_path(parent: dict, end_file: str) -> list:
    """Walk parent dict back from end_file → list of (file, name, kind)."""
    path = []
    node = end_file
    while node is not None:
        entry = parent[node]
        if entry is None:
            path.append((node, None, None))
        else:
            prev_file, name, kind = entry
            path.append((node, name, kind))
        node = entry[0] if entry else None
    path.reverse()
    return path


def _render_path(path: list, start_label: str, end_label: str, idx: int = 1) -> str:
    prefix = f"path {idx}: " if idx > 1 else "path: "
    lines_out = [f"{prefix}{start_label} → {end_label}", ""]
    for i, (frel, name, kind) in enumerate(path):
        if i == 0:
            lines_out.append(f"  {frel}")
        else:
            lines_out.append(f"    ↓ {kind}:{name}")
            lines_out.append(f"  {frel}")
    return '\n'.join(lines_out)


def find_path(map_path: str, start_id: str, end_id: str, blocked: set = None, idx: int = 1):
    """Find shortest dependency path between two identifiers or file substrings.

    blocked: set of intermediate file paths to exclude (used for alternative paths).
    Returns (rendered_str, intermediate_files_set) or (error_str, None).
    """
    defines_map, links_map = parse_map(map_path)

    start_file, start_name, start_ln = _resolve_id(start_id, defines_map)
    end_file,   end_name,   end_ln   = _resolve_id(end_id,   defines_map)

    if not start_file:
        return f"[path] '{start_id}' not found in defines or file paths", None
    if not end_file:
        return f"[path] '{end_id}' not found in defines or file paths", None
    if start_file == end_file:
        label = f"{start_name or start_id}(ln:{start_ln})" if start_ln else start_file
        return f"[path] start and end are in the same file: {label}", None

    parent, found = _bfs_path(start_file, end_file, links_map, blocked)
    if not found:
        return f"[path] no path found between\n  {start_file}\n  {end_file}", None

    path = _reconstruct_path(parent, end_file)
    intermediates = {f for f, _, _ in path[1:-1]}  # exclude endpoints

    start_label = f"{start_name}(ln:{start_ln})" if start_ln else start_id
    end_label   = f"{end_name}(ln:{end_ln})"     if end_ln   else end_id
    return _render_path(path, start_label, end_label, idx), intermediates


# ── idiff ────────────────────────────────────────────────────────────────────────

def detect_ghosts(dm_prev: dict, lm_prev: dict, dm_curr: dict) -> dict:
    """Detect files deleted between snapshots that other files depended on.

    Returns { deleted_file → [name, ...] } where names were actively called
    in the previous snapshot and are now undefined.

    Why cross-snapshot: map_index.py builds links only for names in global_index
    (project-defined names). When a file is deleted, its names leave global_index,
    so links to them also disappear from the current map — making the deletion
    invisible to orphan counting. Cross-snapshot comparison catches this.
    """
    # files that were link targets in prev snapshot
    prev_targets: set = set()
    for targets in lm_prev.values():
        prev_targets.update(targets.keys())

    # which of those no longer exist in current map
    deleted = prev_targets - set(dm_curr.keys())

    ghosts: dict = {}
    for f in deleted:
        called_names = set()
        for targets in lm_prev.values():
            if f in targets:
                called_names.update(targets[f].keys())
        if called_names:
            ghosts[f] = sorted(called_names)

    return ghosts


def idiff_report(map_prev: str, map_curr: str) -> str:
    """Structural diff between two map snapshots.

    Reports:
    - ORPHAN_DRIFT: delta in orphan count (defined but never called)
    - GHOST alert: files deleted that other files depended on
    """
    dm_prev, lm_prev = parse_map(map_prev)
    dm_curr, lm_curr = parse_map(map_curr)

    _, orphans_prev, _ = compute_asymmetry(dm_prev, lm_prev)
    _, orphans_curr, _ = compute_asymmetry(dm_curr, lm_curr)

    delta       = len(orphans_curr) - len(orphans_prev)
    new_orphans = {n: f for n, f in orphans_curr.items() if n not in orphans_prev}
    healed      = {n: f for n, f in orphans_prev.items() if n not in orphans_curr}
    ghosts      = detect_ghosts(dm_prev, lm_prev, dm_curr)

    if delta > 0:
        label = 'DEGRADATION'
    elif delta < 0:
        label = 'HEALING'
    else:
        label = 'NEUTRAL'

    lines = [
        f'ORPHAN_DRIFT = {delta:+d}  [{label}]',
        f'  before: {len(orphans_prev)} orphans',
        f'  after:  {len(orphans_curr)} orphans',
    ]

    if new_orphans:
        lines.append(f'\nnew orphans (+{len(new_orphans)}):')
        for name in sorted(new_orphans):
            lines.append(f'  + {name:<40} ← {new_orphans[name]}')

    if healed:
        lines.append(f'\nhealed orphans (-{len(healed)}):')
        for name in sorted(healed):
            lines.append(f'  - {name:<40} ← {healed[name]}')

    if ghosts:
        lines.append(f'\n! GHOST ALERT — {len(ghosts)} deleted file(s) had active callers:')
        for f in sorted(ghosts):
            lines.append(f'  ! {f}')
            lines.append(f'    called: {", ".join(ghosts[f])}')

    return '\n'.join(lines)


# ── CLI entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Query a 1bcoder map file (find or trace identifiers).'
    )
    parser.add_argument('--map', metavar='FILE', default=DEFAULT_MAP,
                        help=f'Map file to query (default: {DEFAULT_MAP})')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_find = sub.add_parser('find', help='Filter map blocks by filename/content')
    p_find.add_argument('query', nargs='*',
                        help='Filter tokens (term, !term, \\term, \\!term, \\\\!term)')

    p_trace = sub.add_parser('trace', help='Follow call chain backwards from an identifier')
    p_trace.add_argument('identifier', help='Identifier name to trace')

    p_idiff = sub.add_parser('idiff', help='ORPHAN_DRIFT + GHOST ALERT between two map snapshots')
    p_idiff.add_argument('--prev', required=True, metavar='FILE',
                         help='Previous map snapshot (e.g. .1bcoder/map.prev.txt)')

    args = parser.parse_args()

    if not os.path.exists(args.map):
        print(f'error: map file not found: {args.map}', file=sys.stderr)
        print('hint:  run map_index.py first to build the map', file=sys.stderr)
        sys.exit(1)

    if args.cmd == 'find':
        query = ' '.join(args.query)
        hits, result = find_map(args.map, query)
        if not query:
            print(result)
        elif hits:
            print(result)
            print(f'\n[map] {len(hits)} match(es)', file=sys.stderr)
        else:
            print(f'[map] no matches for: {query}', file=sys.stderr)
            sys.exit(1)

    elif args.cmd == 'trace':
        result = trace_map(args.map, args.identifier)
        if result is None:
            print(f"[map] '{args.identifier}' not found in any defines", file=sys.stderr)
            print(f"hint:  try: python map_query.py find \\{args.identifier}", file=sys.stderr)
            sys.exit(1)
        print(result)

    elif args.cmd == 'idiff':
        if not os.path.exists(args.prev):
            print(f'error: prev map not found: {args.prev}', file=sys.stderr)
            sys.exit(1)
        print(idiff_report(args.prev, args.map))


if __name__ == '__main__':
    main()
