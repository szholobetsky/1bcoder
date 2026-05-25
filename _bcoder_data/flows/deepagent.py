"""Recursive tree decomposition — expands a task into a hierarchical plan level by level.
Usage: /deepagent <task> plan: l1, l2, l3 [list: lens1, lens2] [file: output.md]
       /deepagent write article about local LLMs plan: thesis, arguments, evidence
       /deepagent Shakespeare is not a hero plan: claims, supporting points, evidence list: pro argument, counter-argument, edge case, real example file: essay.md
"""
import os as _os
import re as _re


# ── tree node ──────────────────────────────────────────────────────────────────

class _Node:
    def __init__(self, nid: str, text: str):
        self.id   = nid
        self.text = text

    @property
    def depth(self) -> int:
        return len(self.id.split('.'))

    @property
    def parent_id(self) -> str | None:
        parts = self.id.split('.')
        return '.'.join(parts[:-1]) if len(parts) > 1 else None


# ── concreteness detector (Smetana principle) ──────────────────────────────────

_CONCRETE = [
    r'\b(import|from|def|class|return|assert|raise|yield)\b',
    r'\b\w+\.\w+\(',
    r'\d+\.\d+',
    r'O\([^)]+\)',
    r'\b(реалізовано|обрано|пройшов|готово|implemented|done|complete|deployed)\b',
    r'```',
    r'\$\s*\w+',
]

def _is_concrete(text: str) -> bool:
    return any(_re.search(p, text, _re.IGNORECASE) for p in _CONCRETE)


# ── LLM output parser ──────────────────────────────────────────────────────────

_SKIP_PREFIXES = (
    'here are', 'the following', 'these are', 'below are',
    'note:', 'example:', 'output:', 'step ', 'sub-step',
    'sure', 'okay', 'of course',
    # prompt leakage — model echoing output rules back into tree
    'provide only', 'output exactly', 'do not include', 'each item',
    'no numbering', 'no bullets', 'no markdown', 'no explanation',
    'expand ', 'refine ', 'evaluate relevance',
)

def _parse_items(raw: str, lenses: list[str] | None = None) -> list[str]:
    lens_tags = {l.lower().strip() for l in lenses} if lenses else set()
    items = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # tagged item: [lens name] text — keep as-is after light cleaning
        tag_m = _re.match(r'^\[([^\]]+)\]\s*(.+)', line)
        if tag_m:
            tag, body = tag_m.group(1).strip(), tag_m.group(2).strip()
            body = _re.sub(r'\*{1,2}(.+?)\*{1,2}', r'\1', body).rstrip('.,;:')
            if len(body) >= 4:
                items.append(f'[{tag}] {body}')
                if len(items) == 5:
                    break
            continue
        # untagged item — normal pipeline
        line = _re.sub(r'^[\d]+[.)]\s*', '', line)
        line = _re.sub(r'^[-•*#>]+\s*', '', line)
        line = _re.sub(r'\*{1,2}(.+?)\*{1,2}', r'\1', line)
        line = _re.sub(r'`(.+?)`', r'\1', line)
        for sep in (' — ', ' - ', ': ', ' (', ' because', ' in order', ' so that', ' which'):
            if sep in line:
                line = line[:line.index(sep)]
        line = line.strip().rstrip('.,;:')
        if len(line) < 4:
            continue
        if any(line.lower().startswith(p) for p in _SKIP_PREFIXES):
            continue
        items.append(line)
        if len(items) == 5:
            break
    return items


# ── tree I/O ───────────────────────────────────────────────────────────────────

def _write_tree(nodes: list[_Node], filepath: str, target: str):
    ordered = sorted(nodes, key=lambda n: tuple(int(x) for x in n.id.split('.')))
    lines = [f'# {target}\n']
    for node in ordered:
        indent = '  ' * (node.depth - 1)
        lines.append(f'{indent}{node.id} {node.text}')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def _read_tree(filepath: str) -> list[_Node]:
    nodes = []
    with open(filepath, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip()
            if not line or line.startswith('#'):
                continue
            m = _re.match(r'\s*([\d.]+)\s+(.*)', line)
            if m:
                nodes.append(_Node(m.group(1), m.group(2).strip()))
    return nodes


def _get_leaves(nodes: list[_Node]) -> list[_Node]:
    parent_ids = {n.parent_id for n in nodes}
    return [n for n in nodes if n.id not in parent_ids]


def _count_text(text: str, nodes: list[_Node]) -> int:
    t = text.lower().strip()
    return sum(1 for n in nodes if n.text.lower().strip() == t)


def _check_cycle(new_items: list[str], nodes: list[_Node], parent: '_Node') -> tuple[list[str], bool]:
    """Filter duplicates and parent echoes. Returns (filtered, cycle_detected)."""
    parent_norm = parent.text.lower().strip()
    filtered = []
    cycle = False
    for item in new_items:
        norm = item.lower().strip()
        if _count_text(item, nodes) >= 2:
            cycle = True
            continue
        if norm == parent_norm:
            cycle = True
            continue
        filtered.append(item)
    return filtered, cycle


# phrases from the CORRECT example — if model outputs these, it's parroting the prompt
_EXAMPLE_PHRASES = {'prepare ingredients', 'mix dry components', 'add liquid base', 'shape and bake'}
_PROMPT_TOKENS   = 35  # approximate size of the expansion prompt in tokens

def _is_parroting(items: list[str]) -> bool:
    normalized = {i.lower().strip() for i in items}
    return bool(normalized & _EXAMPLE_PHRASES)


# ── expansion prompts ──────────────────────────────────────────────────────────

_PROMPT = '''Expand "{text}" into 2–5 children that cover it from distinct angles. Each item must address a different aspect — no redundancy. One item per line. No bullets, numbers, or explanations.

Task: {target}
Focus: {level}'''

_PROMPT_WITH_ASPECTS = '''Expand "{text}" into 2–5 children through the lenses below. Each lens must produce a genuinely different angle — no redundancy. Prefix each item with [LensName]. Omit lenses that do not apply. One item per line. No bullets, numbers, or explanations.

Task: {target}
Focus: {level}
Lenses:
{aspects}'''


# ── interruption handler ──────────────────────────────────────────────────────

def _on_interrupt(leaf: '_Node', nodes: list, new_nodes: int) -> tuple[bool, int]:
    """Called when _stream_chat returns None (Ctrl+C). Returns (stop, new_nodes)."""
    try:
        ans = input(
            f'\n  [Enter] continue   '
            f's <note> = mark node done   '
            f'q = quit and save: '
        ).strip()
    except (EOFError, KeyboardInterrupt):
        ans = 'q'

    if ans.lower() == 'q':
        print('[deepagent] stopped — tree saved')
        return True, new_nodes

    if ans.lower().startswith('s'):
        note = ans[1:].strip()
        suffix = f' [{note}]' if note else ' [done]'
        for n in nodes:
            if n.id == leaf.id:
                n.text += suffix
                break
        print(f'    {leaf.id}{suffix}')

    return False, new_nodes


# ── main flow ──────────────────────────────────────────────────────────────────

def run(chat, args: str):
    # ── parse args ─────────────────────────────────────────────────────────────
    plan_m = _re.search(r'\bplan:\s*(.+?)(?:\s+(?:list|file):|$)', args, _re.IGNORECASE)
    list_m = _re.search(r'\blist:\s*(.+?)(?:\s+file:|$)', args, _re.IGNORECASE)
    file_m = _re.search(r'\bfile:\s*(\S+)', args, _re.IGNORECASE)

    anchor = min(m.start() for m in [plan_m, list_m, file_m] if m) if any([plan_m, list_m, file_m]) else len(args)
    task = args[:anchor].strip().strip('"\'')

    plan_labels = [l.strip() for l in plan_m.group(1).split(',') if l.strip()] if plan_m else ['overview', 'detail', 'implementation']
    lenses     = [l.strip() for l in list_m.group(1).split(',') if l.strip()] if list_m else []
    output_file = file_m.group(1) if file_m else 'deepplan.md'

    if not task:
        print('usage: /deepagent <task> plan: l1, l2, l3 [list: lens1, lens2] [file: output.md]')
        return

    print(f'[deepagent] target : {task}')
    print(f'[deepagent] plan   : {" → ".join(plan_labels)}')
    if lenses:
        print(f'[deepagent] list   : {", ".join(lenses)}')
    print(f'[deepagent] file   : {output_file}')

    # ── boost creativity (save/restore params) ────────────────────────────────
    _saved_params = dict(chat.params)
    _injected = {}
    if 'temperature' not in chat.params:
        _injected['temperature'] = 0.8
    if 'top_k' not in chat.params:
        _injected['top_k'] = 40
    if _injected:
        chat.params.update(_injected)
        print(f'[deepagent] params : {_injected}  (restore on exit)')

    # ── init or resume ─────────────────────────────────────────────────────────
    if _os.path.isfile(output_file):
        nodes = _read_tree(output_file)
        print(f'[deepagent] resuming: {len(nodes)} nodes loaded')
    else:
        nodes = [_Node('1', task)]
        _write_tree(nodes, output_file, task)
        print(f'[deepagent] initialized root node')

    # ── expansion loop ─────────────────────────────────────────────────────────
    stop = False
    for level_label in plan_labels:
        if stop:
            break

        leaves = [n for n in _get_leaves(nodes) if not _is_concrete(n.text)]

        if not leaves:
            print(f'\n[deepagent] all leaves concrete — stopping before "{level_label}"')
            break

        print(f'\n[deepagent] ── level "{level_label}" ── {len(leaves)} leaf node(s)')

        new_nodes = 0
        for leaf in leaves:
            if stop:
                break
            if _is_concrete(leaf.text):
                print(f'  {leaf.id} [concrete, skip]')
                continue

            display = leaf.text[:60] + '…' if len(leaf.text) > 60 else leaf.text
            print(f'  {leaf.id} {display}')

            if lenses:
                aspects = '\n'.join(f'- {l}' for l in lenses)
                prompt = _PROMPT_WITH_ASPECTS.format(target=task, text=leaf.text, level=level_label, aspects=aspects)
            else:
                prompt = _PROMPT.format(target=task, text=leaf.text, level=level_label)
            ctx = [m for m in chat.messages if m.get('role') in ('user', 'assistant')][-6:]
            msgs = (
                [{'role': 'system', 'content': 'You are a planning assistant. Follow output rules strictly.'}]
                + ctx
                + [{'role': 'user', 'content': prompt}]
            )

            raw = chat._stream_chat(msgs)
            if raw is None:                          # Ctrl+C sentinel
                stop, new_nodes = _on_interrupt(leaf, nodes, new_nodes)
                continue
            if raw == "":                            # empty reply / network error
                print(f'    [skip] empty reply')
                continue

            items = _parse_items(raw, lenses)
            if not items:
                print(f'    [skip] no items parsed')
                continue

            if _is_parroting(items):
                ctx_tokens = sum(len(m.get('content','').split()) * 4 // 3
                                 for m in chat.messages if m.get('role') in ('user','assistant'))
                sufficient = ctx_tokens >= _PROMPT_TOKENS * 2
                print(f'\n  [deepagent] model is parroting the prompt example.')
                print(f'  Expansion prompt: ~{_PROMPT_TOKENS} tok  |  task context: ~{ctx_tokens} tok')
                if not sufficient:
                    print(f'  Context is too small (need > {_PROMPT_TOKENS*2} tok).')
                    print(f'  Ask the model to describe your task before running /deepagent:')
                    print(f'  e.g.  tell me everything about {task}')
                else:
                    print(f'  Context is sufficient but model still parrots — model too small.')
                    print(f'  Recommendation: use a larger model (1.7b+ recommended for /deepagent).')
                    print(f'  Switch with:  /model <name>  then re-run /deepagent.')
                stop = True
                break

            items, cycle = _check_cycle(items, nodes, leaf)
            if cycle:
                print(f'    [cycle] duplicate/parent-echo filtered — try: /param repeat_penalty 1.2')
            if not items:
                print(f'    [skip] all items were duplicates')
                continue

            existing = sum(1 for n in nodes if n.parent_id == leaf.id)
            for i, item in enumerate(items, existing + 1):
                nodes.append(_Node(f'{leaf.id}.{i}', item))
                new_nodes += 1

        _write_tree(nodes, output_file, task)
        print(f'[deepagent] level "{level_label}" done: +{new_nodes} nodes → {output_file}')

    # ── restore params ─────────────────────────────────────────────────────────
    chat.params = _saved_params

    # ── summary ────────────────────────────────────────────────────────────────
    leaves_final = _get_leaves(nodes)
    print(f'\n[deepagent] complete: {len(nodes)} total nodes, {len(leaves_final)} leaves')
    print(f'[deepagent] /agent file: {output_file}  ← execute the plan')
