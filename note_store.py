"""/note — titled notes: definitions and facts written by hand or taken from the
last reply ($), kept in .1bcoder/note/<term>.md and found again by words.

Pure helpers; chat.py's _cmd_note is the command. One file per note:

    # booklify

    KEYWORDS: books, exchange, flask

    DEFINITION:
    - a flask app that tracks book exchange in a small company [llm: qwen3:4b · 2026-10-16]

    FACTS:
    - booklify uses sqlite as its database engine [llm: qwen3:4b · 2026-10-16]
    - booklify has 4 types of users [user · 2026-10-16]

An entry may span lines (a whole reply): the lines after the first are indented
by two spaces, so a markdown viewer shows one list item.
"""
import os
import re

SECTIONS = {"def": "definitions", "fact": "facts"}
_HEADERS = {"definitions": "DEFINITION:", "facts": "FACTS:"}
_RULE = "-" * 18


def norm(term: str) -> str:
    """'Order Service' -> 'order-service'; letters of any script are kept."""
    return re.sub(r"[^\w]+", "-", (term or "").strip().lower()).strip("-_")


def note_path(notes_dir: str, term: str) -> str:
    return os.path.join(notes_dir, norm(term) + ".md")


def empty(term: str) -> dict:
    return {"term": norm(term), "keywords": [], "definitions": [], "facts": []}


def parse(text: str, term: str = "") -> dict:
    note = empty(term)
    section = None
    for line in (text or "").splitlines():
        if line.startswith("# ") and not note["term"]:
            note["term"] = norm(line[2:])
        elif line.startswith("KEYWORDS:"):
            note["keywords"] = [k.strip() for k in line[len("KEYWORDS:"):].split(",") if k.strip()]
            section = None
        elif line.strip() == "DEFINITION:":
            section = "definitions"
        elif line.strip() == "FACTS:":
            section = "facts"
        elif section and line.startswith("- "):
            note[section].append(line[2:])
        elif section and line.startswith("  ") and note[section]:
            note[section][-1] += "\n" + line[2:]
    return note


def render(note: dict) -> str:
    lines = [f"# {note['term']}", ""]
    if note["keywords"]:
        lines += ["KEYWORDS: " + ", ".join(note["keywords"]), ""]
    for key in ("definitions", "facts"):
        lines.append(_HEADERS[key])
        for entry in note[key]:
            first, *rest = entry.split("\n")
            lines.append(f"- {first}")
            lines += [f"  {r}" for r in rest]
        lines.append("")
    return "\n".join(lines)


def read(notes_dir: str, term: str):
    path = note_path(notes_dir, term)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return parse(f.read(), term)


def write(notes_dir: str, note: dict) -> str:
    os.makedirs(notes_dir, exist_ok=True)
    path = note_path(notes_dir, note["term"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(note))
    return path


def strip_tag(entry: str) -> str:
    """The entry without its trailing [who · date] tag."""
    return re.sub(r"\s*\[[^\[\]]*·[^\[\]]*\]\s*$", "", entry or "").strip()


def add(notes_dir: str, term: str, kind: str, text: str, tag: str) -> bool:
    """Append a definition (kind 'def') or a fact ('fact'). False when the same text is already there."""
    if not norm(term):
        raise ValueError("the term is empty")
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to write")
    note = read(notes_dir, term) or empty(term)
    key = SECTIONS[kind]
    if any(strip_tag(e) == text for e in note[key]):
        return False
    note[key].append(f"{text} {tag}".rstrip())
    write(notes_dir, note)
    return True


def add_keywords(notes_dir: str, term: str, keywords: list) -> list:
    """Merge keywords (case-insensitive) into the note, creating it if needed; returns them all."""
    if not norm(term):
        raise ValueError("the term is empty")
    note = read(notes_dir, term) or empty(term)
    have = {k.lower() for k in note["keywords"]}
    for k in keywords:
        k = k.strip()
        if k and k.lower() not in have:
            note["keywords"].append(k)
            have.add(k.lower())
    write(notes_dir, note)
    return note["keywords"]


def all_notes(notes_dir: str) -> list:
    if not os.path.isdir(notes_dir):
        return []
    notes = []
    for name in sorted(os.listdir(notes_dir)):
        if name.endswith(".md"):
            with open(os.path.join(notes_dir, name), encoding="utf-8") as f:
                notes.append(parse(f.read(), name[:-3]))
    return notes


def query_words(query: str) -> list:
    """Words of a query, lowercased; "a phrase" stays one word."""
    return [(a or b).lower() for a, b in re.findall(r'"([^"]+)"|(\S+)', query or "")]


def search(notes_dir: str, query: str) -> list:
    """Notes holding every word of the query (in the name, keywords, definitions or facts).
    Ranked: a word in the name, then in keywords, then in a definition, then only in facts.
    Each hit: {"note", "facts" (only those with a word), "keyword_hit"}."""
    words = query_words(query)
    if not words:
        return []
    hits = []
    for note in all_notes(notes_dir):
        name = note["term"].replace("-", " ")
        fields = [(0, [name, note["term"]]), (1, note["keywords"]),
                  (2, note["definitions"]), (3, note["facts"])]
        rank = 4
        found_all = True
        for w in words:
            ranks = [r for r, texts in fields if any(w in t.lower() for t in texts)]
            if not ranks:
                found_all = False
                break
            rank = min(rank, min(ranks))
        if not found_all:
            continue
        facts = [f for f in note["facts"] if any(w in f.lower() for w in words)]
        keyword_hit = any(w in k.lower() for w in words for k in note["keywords"])
        hits.append({"note": note, "facts": facts, "keyword_hit": keyword_hit, "rank": rank})
    hits.sort(key=lambda h: (h["rank"], -len(h["facts"]), h["note"]["term"]))
    return hits


def _block(note: dict, facts: list, show_keywords: bool) -> list:
    term = note["term"]
    pad = " " * (len(term) + 2)
    lines = []
    defs = note["definitions"] or ["(no definition)"]
    for i, d in enumerate(defs):
        for j, part in enumerate(d.split("\n")):
            lines.append((f"{term}: " if i == 0 and j == 0 else pad) + part)
    if show_keywords and note["keywords"]:
        lines.append("keywords: " + ", ".join(note["keywords"]))
    for f in facts:
        first, *rest = f.split("\n")
        lines.append(f"fact: {first}")
        lines += ["      " + r for r in rest]
    return lines


def format_hits(hits: list) -> str:
    out = []
    for i, h in enumerate(hits, 1):
        out.append(f"--[ {i} ]{_RULE}")
        out += _block(h["note"], h["facts"], h["keyword_hit"])
    return "\n".join(out)


def format_note(note: dict) -> str:
    """The whole note: every definition, keyword and fact."""
    return "\n".join(_block(note, note["facts"], True))


def format_list(notes: list) -> str:
    rows = []
    for n in notes:
        kw = f"  keywords: {', '.join(n['keywords'])}" if n["keywords"] else ""
        rows.append(f"  {n['term']}  ({len(n['definitions'])} def, {len(n['facts'])} facts){kw}")
    return "\n".join(rows)


def parse_numbers(spec: str) -> list:
    """'2,5' / '2 5' / '2-4' -> [2, 5] / [2, 5] / [2, 3, 4]."""
    nums = []
    for part in re.split(r"[,\s]+", (spec or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            nums += list(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            nums.append(int(part))
        else:
            raise ValueError(f"not a number: {part}")
    return nums
