#!/usr/bin/env python3
"""
map_index.py — standalone project scanner.

Scans a directory tree and produces a language-agnostic map of:
  - defined identifiers (classes, functions, endpoints, tables, …)
  - cross-file references with relationship types (import / call / ref / expr)
  - optionally variables and function parameters (depth 3)

Usage:
    python map_index.py [path] [depth] [--out FILE] [--stdout]

    path    directory to scan (default: current directory)
    depth   2 = definitions + links (default)
            3 = also variables and function parameters
    --out   output file (default: .1bcoder/map.txt)
    --stdout  print map to stdout instead of / in addition to file

Examples:
    python map_index.py .
    python map_index.py src/ 3
    python map_index.py . 3 --out my_map.txt
    python map_index.py . --stdout
"""

import re
import os
import sys
import argparse

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, **_):  # silent fallback
        return it

# ── file filtering ─────────────────────────────────────────────────────────────

MAX_FILE_KB = 200

SCAN_EXTENSIONS = {
    # code
    '.py', '.js', '.ts', '.java', '.cs', '.go', '.rs', '.cpp', '.c', '.h',
    '.rb', '.php', '.kt', '.scala', '.swift', '.lua', '.r', '.m',
    # web
    '.html', '.htm', '.css', '.jsx', '.tsx', '.vue', '.svelte',
    # query / db
    '.sql', '.plsql', '.pls', '.pkb', '.pks',
    # config / infra
    '.yaml', '.yml', '.toml', '.ini', '.env', '.conf', '.cfg', '.tf', '.hcl',
    # markup / data
    '.xml', '.json',
    # scripts
    '.sh', '.bat', '.ps1',
}

SKIP_DIRS = {
    '.git', '.svn', '.hg', 'node_modules', '__pycache__', '.pytest_cache',
    '.venv', 'venv', 'env', '.env', 'dist', 'build', 'target', 'out',
    '.gradle', '.idea', '.vscode', '.1bcoder',
}

# ── definition patterns (group 1 = name) ──────────────────────────────────────

DEFINE_PATTERNS = [
    r'(?:def|function|func|fn|sub|procedure)\s+(\w+)',
    r'(?:class|interface|type|struct|enum|record)\s+(\w+)',
    r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|PROCEDURE|FUNCTION|PACKAGE(?:\s+BODY)?|TRIGGER)\s+(?:\w+\.)?(\w+)',
    r'resource\s+"[^"]+"\s+"(\w+)"',           # Terraform
    r'module\s+"(\w+)"',                        # Terraform module
    r'(?:id|name)\s*=\s*["\'](\w[\w-]+)["\']', # HTML/XML id/name attrs
    r'@(?:app|router|Blueprint|api)\.\w+\s*\(\s*["\']([^"\']+)',  # endpoints
    r'^(\w[\w-]{3,})\s*[:=][^=]',              # YAML/TOML/ini top-level keys
]

VAR_PATTERNS = [
    r'^(\w+)\s*=\s*[^=\n]',   # module-level assignments
]

STOP_WORDS = {
    'true', 'false', 'null', 'none', 'self', 'this', 'return', 'super',
    'import', 'from', 'class', 'function', 'interface', 'struct', 'enum',
    'public', 'private', 'protected', 'static', 'final', 'abstract',
    'void', 'bool', 'int', 'str', 'float', 'list', 'dict', 'tuple',
    'string', 'number', 'object', 'array', 'type', 'with', 'async', 'await',
    'pass', 'break', 'continue', 'raise', 'yield', 'lambda', 'global',
    'args', 'kwargs', 'cls',
    # keywords ≥3 chars that appear in assignment lines
    'for', 'not', 'and', 'try', 'del', 'def', 'elif', 'else', 'none',
}

# ── assignment-line identifier scan ───────────────────────────────────────────

_ASSIGN_RE = re.compile(r'(?<![=!<>])=(?!=)')   # bare = (not ==, !=, <=, >=)
_WORD_RE   = re.compile(r'\b([A-Za-z_]\w*)\b')  # every identifier token

# ── relationship classification ────────────────────────────────────────────────

_IMPORT_KW = re.compile(
    r'\b(import|from|include|require|uses|use|using|load|needs)\b', re.IGNORECASE
)


def classify_ref(name: str, text: str) -> str:
    """Return relationship type(s) for name in text: import, call, ref, expr."""
    escaped = re.escape(name)
    word = re.compile(r'\b' + escaped + r'\b')
    call = re.compile(escaped + r'\s*\(')
    ref  = re.compile(r'[\(\[,]\s*' + escaped + r'\s*[\)\],]')
    types = set()
    for line in text.splitlines():
        if not word.search(line):
            continue
        if _IMPORT_KW.search(line):
            types.add('import')
        elif call.search(line):
            types.add('call')
        elif ref.search(line):
            types.add('ref')
        else:
            types.add('expr')
    return ','.join(t for t in ('import', 'call', 'ref', 'expr') if t in types) or 'ref'


# ── core scanner ───────────────────────────────────────────────────────────────

def collect_files(root: str) -> list:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SCAN_EXTENSIONS:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                if os.path.getsize(fpath) > MAX_FILE_KB * 1024:
                    continue
            except OSError:
                continue
            files.append(fpath)
    return files


def extract_definitions(text: str, depth: int) -> tuple:
    """Return (defs_dict, vars_dict) where values are line numbers."""
    def lineno(m):
        return text[:m.start()].count('\n') + 1

    defs = {}
    for pat in DEFINE_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
            name = m.group(1).strip()
            if len(name) >= 4 and name.lower() not in STOP_WORDS and name not in defs:
                defs[name] = lineno(m)

    vars_dict = {}
    if depth >= 3:
        for pat in VAR_PATTERNS:
            for m in re.finditer(pat, text, re.MULTILINE):
                name = m.group(1).strip()
                if len(name) >= 3 and name.lower() not in STOP_WORDS \
                        and name not in defs and name not in vars_dict:
                    vars_dict[name] = lineno(m)
        for m in re.finditer(r'def\s+\w+\s*\(([^)]*)\)', text, re.IGNORECASE):
            for param in m.group(1).split(','):
                name = re.sub(r'[:\*=\[].*', '', param).strip().lstrip('*')
                if len(name) >= 3 and name.lower() not in STOP_WORDS \
                        and name not in defs and name not in vars_dict:
                    vars_dict[name] = lineno(m)
        # scan every assignment line — extract all identifier tokens from both
        # sides, treating dotted names (a.b.c) as separate identifiers
        for lno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith('#'):
                continue
            if not _ASSIGN_RE.search(line):
                continue
            for m in _WORD_RE.finditer(line):
                name = m.group(1)
                if len(name) >= 3 and name.lower() not in STOP_WORDS \
                        and name not in defs and name not in vars_dict:
                    vars_dict[name] = lno

    return defs, vars_dict


def _parse_existing_map(text: str) -> tuple:
    """Parse map.txt → (cached_blocks, cached_defs).

    cached_blocks : rel → [content lines]  (defines/links/vars, not the filename)
    cached_defs   : rel → (defs_dict, vars_dict)  name → line_number
    """
    cached_blocks = {}
    cached_defs   = {}

    current_rel   = None
    current_lines = []
    current_defs  = {}
    current_vars  = {}

    def _flush():
        if current_rel:
            cached_blocks[current_rel] = current_lines[:]
            cached_defs[current_rel]   = (dict(current_defs), dict(current_vars))

    for line in text.splitlines():
        if line.startswith('#'):
            continue
        if not line.strip():
            continue
        if not line.startswith(' ') and not line.startswith('\t'):
            _flush()
            current_rel   = line.strip()
            current_lines = []
            current_defs  = {}
            current_vars  = {}
        else:
            current_lines.append(line)
            s = line.strip()
            if s.startswith('defines :'):
                for item in s[len('defines :'):].strip().split(','):
                    m = re.match(r'(\w+)\(ln:(\d+)\)', item.strip())
                    if m:
                        current_defs[m.group(1)] = int(m.group(2))
            elif re.match(r'vars\s+:', s):
                for item in re.sub(r'^vars\s+:\s*', '', s).split(','):
                    m = re.match(r'(\w+)\(ln:(\d+)\)', item.strip())
                    if m:
                        current_vars[m.group(1)] = int(m.group(2))
    _flush()
    return cached_blocks, cached_defs


def build_map(root: str, depth: int = 2, map_path: str = None) -> str:
    """Scan root directory and return map as a string.

    If map_path points to an existing map.txt, files whose mtime is older than
    the map are skipped — their cached definitions and link lines are reused.
    Only changed files are re-scanned and re-linked.
    """
    root = os.path.abspath(root)
    files = collect_files(root)

    # ── load cache ──────────────────────────────────────────────────────────────
    map_mtime      = 0.0
    cached_blocks  = {}   # rel → [content lines]
    cached_defs    = {}   # rel → (defs_dict, vars_dict)
    if map_path and os.path.exists(map_path):
        map_mtime = os.path.getmtime(map_path)
        try:
            existing = open(map_path, encoding='utf-8', errors='ignore').read()
            cached_blocks, cached_defs = _parse_existing_map(existing)
        except OSError:
            pass

    # ── scan phase ───────────────────────────────────────────────────────────────
    file_defs    = {}   # rel → (defs_dict, vars_dict)
    file_content = {}   # rel → text   (only for changed files)
    skipped      = 0

    for fpath in tqdm(files, desc="scanning", unit="file", file=sys.stderr):
        rel = os.path.relpath(fpath, root)
        try:
            fmtime = os.path.getmtime(fpath)
        except OSError:
            continue
        if map_mtime and fmtime <= map_mtime and rel in cached_defs:
            file_defs[rel] = cached_defs[rel]
            skipped += 1
            continue
        try:
            text = open(fpath, encoding='utf-8', errors='ignore').read()
        except OSError:
            continue
        defs, vars_dict   = extract_definitions(text, depth)
        file_defs[rel]    = (defs, vars_dict)
        file_content[rel] = text

    changed = len(file_content)
    if skipped:
        print(f"[map] {skipped} unchanged (reused), {changed} changed (re-scanned)", file=sys.stderr)

    # ── global index ─────────────────────────────────────────────────────────────
    global_index = {}
    for rel, (defs, _) in file_defs.items():
        for name in defs:
            if name not in global_index:
                global_index[name] = rel
    for rel, (_, vars_dict) in file_defs.items():
        for name in vars_dict:
            if name not in global_index:
                global_index[name] = rel

    # ── link phase (changed files only) ──────────────────────────────────────────
    file_links = {}   # rel → { target_rel → { name → kind } }
    for rel, text in tqdm(file_content.items(), desc="linking", unit="file", file=sys.stderr):
        by_target = {}
        for name, target_rel in global_index.items():
            if target_rel == rel:
                continue
            if re.search(r'\b' + re.escape(name) + r'\b', text):
                kind = classify_ref(name, text)
                by_target.setdefault(target_rel, {})[name] = kind
        file_links[rel] = by_target

    # ── format output ────────────────────────────────────────────────────────────
    out = [f"# project map — {root}  depth:{depth}"]
    for rel in sorted(file_defs):
        out.append(f"\n{rel}")
        if rel in file_content:
            # freshly scanned — generate lines
            defs, vars_dict = file_defs[rel]
            if defs:
                items = ', '.join(
                    f"{n}(ln:{ln})" for n, ln in sorted(defs.items(), key=lambda x: x[1])
                )
                out.append(f"  defines : {items}")
            for target in sorted(file_links.get(rel, {})):
                refs  = file_links[rel][target]
                items = ', '.join(f"{kind}:{n}" for n, kind in sorted(refs.items()))
                out.append(f"  links  → {target} ({items})")
            if vars_dict:
                items = ', '.join(
                    f"{n}(ln:{ln})" for n, ln in sorted(vars_dict.items(), key=lambda x: x[1])
                )
                out.append(f"  vars    : {items}")
        elif rel in cached_blocks:
            # unchanged — reuse cached content lines verbatim
            out.extend(cached_blocks[rel])

    return '\n'.join(out)


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Scan a project and produce a language-agnostic identifier map.'
    )
    parser.add_argument('path', nargs='?', default='.',
                        help='Directory to scan (default: current directory)')
    parser.add_argument('depth', nargs='?', type=int, default=2,
                        help='Index depth: 2=definitions+links (default), 3=also vars/params')
    parser.add_argument('--out', metavar='FILE',
                        help='Output file (default: .1bcoder/map.txt)')
    parser.add_argument('--stdout', action='store_true',
                        help='Also print map to stdout')
    args = parser.parse_args()

    root  = os.path.abspath(args.path)
    depth = max(2, min(args.depth, 3))

    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    print(f"[map] scanning {root} (depth {depth}) ...", file=sys.stderr)
    map_text = build_map(root, depth, map_path=out_path)

    if args.out:
        out_path = args.out
    else:
        bcoder_dir = os.path.join(root, '.1bcoder')
        os.makedirs(bcoder_dir, exist_ok=True)
        out_path = os.path.join(bcoder_dir, 'map.txt')

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(map_text)

    lines = map_text.count('\n') + 1
    print(f"[map] {lines} lines → {out_path}", file=sys.stderr)

    if args.stdout:
        print(map_text)


if __name__ == '__main__':
    main()
