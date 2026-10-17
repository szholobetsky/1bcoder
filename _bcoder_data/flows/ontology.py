"""ontology — strict knowledge base of triples with contradiction checks.

(c) 2026 Stanislav Zholobetskyi
Institute for Information Recording, National Academy of Sciences of Ukraine, Kyiv
Створено в рамках аспірантського дослідження на тему:
"Інтелектуальна технологія підтримки розробки та супроводу програмних продуктів"

Like /flow glossary, but the unit of knowledge is a triple, not a sentence:

    woman :: can :: take maternity leave

LLM generates -> Jev perceives -> Python decides. The LLM extracts candidate
triples from text chunks; a decision model (if set) checks each one is really
stated in its chunk (rubric triple_grounded); a deterministic checker refuses
any triple that contradicts the base. See concepts/ONTOLOGY.md in the simrgl repo.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow ontology index <path> [--project P] [--chunk N] [--strict] [--no-jev]
  /flow ontology add "S :: P :: O [| condition]" [--project P] [--strict]
  /flow ontology remove "S :: P :: O" [--project P]
  /flow ontology check [--project P]
  /flow ontology conflicts [--project P]
  /flow ontology show <entity> [ctx] [--project P]
  /flow ontology ask "S :: P :: O" [--project P]      ? = any, e.g. "man :: can :: ?"
  /flow ontology export [--project P]                  writes ontology.ttl (Turtle)
  /flow ontology sparql "SELECT ..." [--project P]     needs: pip install rdflib
      prefixes ready: e: (entities, spaces as _), p: (modal/domain predicates),
      rdf: rdfs: owl:  — is_a = rdfs:subClassOf, instance_of = rdf:type,
      disjoint_with = owl:disjointWith, same_as = owl:sameAs
      e.g. "SELECT ?x WHERE { ?x rdfs:subClassOf+ e:user }"
           "SELECT ?o WHERE { e:жінка rdfs:subClassOf* ?c . ?c p:can ?o }"

Predicates:
  structural   is_a, instance_of, part_of, disjoint_with, same_as
  modal        can / cannot, must (conflicts with cannot), is / is_not,
               has / has_not, does / does_not
  domain verbs any short verb; negate with "not <verb>" (bank :: not issues :: loans)

Conditions become classes:
  user :: can :: take maternity leave | woman
      -> woman :: is_a :: user ;  woman :: can :: take maternity leave
  user :: can :: take maternity leave | gender = female
      -> user[gender=female] :: is_a :: user ; ... — classes with the same
         attribute and different values never share members.

Contradictions (two statements, opposite polarity, same object):
  RED     same subject, one subject is a kind of the other, or a known shared
          sub-class/instance  -> NOT recorded, written to the journal
  YELLOW  nothing says whether the subjects overlap (declare disjoint_with to
          settle it) -> recorded with a warning; --strict holds it in the journal
  Also RED: is_a cycles, and disjoint classes that share a member.

Reality is not consistent, so a contradiction is knowledge too: the journal
keeps every fact that was NOT recorded, where it was found, and which recorded
facts it contradicts. Rows are never deleted — a decision only sets a status:
  condition  narrow the new fact by a condition (| woman), then record it
  known      a real contradiction in the sources — record nothing, keep the row
  reject     the new fact is wrong            override  record it, drop the old one
  disjoint   (YELLOW) the subjects never overlap    accept  (YELLOW) record as is

  /flow ontology conflicts [--all] [--project P]   walk open rows / list all

Files (.1bcoder/ontology/<project>/; a host app may move the root — see ROOT):
  triples.txt      recorded triples, one per line — source of truth, editable
  conflicts.jsonl  the contradiction journal (machine)
  conflicts.md     the same journal for people, regenerated on every change
  nogood.txt       rejected triples — never proposed again
  ontology.ttl     export (generated)

Examples:
  /flow ontology add "man :: disjoint_with :: woman"
  /flow ontology add "user :: can :: take maternity leave | woman"
  /flow ontology add "man :: is_a :: user"
  /flow ontology add "user :: can :: take maternity leave"     -> RED
  /flow ontology ask "man :: can :: take maternity leave"
  /flow ontology index docs/hr --project hr
"""
import json as _json
import os as _os
import re as _re
import unicodedata as _ud
from urllib.parse import quote as _quote

# ── config ───────────────────────────────────────────────────────────────────

_DEFAULT_CHUNK_TOKENS = 1200     # Jev: chunk + question must stay under 2050 tokens
_CHARS_PER_TOKEN = 4
_PROSE_EXT = {"txt", "md", "rst", "log"}
_GROUND_RUBRIC = "triple_grounded"

STRUCTURAL = ("is_a", "instance_of", "part_of", "disjoint_with", "same_as")
_HIERARCHY = ("is_a", "instance_of")

# modal predicate -> (family, positive?)
_MODAL = {
    "can": ("can", True), "must": ("can", True), "cannot": ("can", False),
    "is": ("is", True), "is_not": ("is", False),
    "has": ("has", True), "has_not": ("has", False),
    "does": ("does", True), "does_not": ("does", False),
}

_ALIASES = {
    "is a": "is_a", "isa": "is_a", "is a kind of": "is_a", "kind of": "is_a",
    "subclass of": "is_a", "subclass_of": "is_a", "різновид": "is_a", "є різновидом": "is_a",
    "instance of": "instance_of", "is an instance of": "instance_of", "екземпляр": "instance_of",
    "part of": "part_of", "is part of": "part_of", "частина": "part_of", "є частиною": "part_of",
    "disjoint with": "disjoint_with", "disjoint": "disjoint_with", "не перетинається з": "disjoint_with",
    "same as": "same_as", "синонім": "same_as",
    "може": "can", "can not": "cannot", "can't": "cannot", "не може": "cannot",
    "must not": "cannot", "mustn't": "cannot", "не повинен": "cannot", "не має права": "cannot",
    "повинен": "must", "зобов'язаний": "must", "should": "must",
    "is not": "is_not", "isn't": "is_not",
    "has not": "has_not", "does not have": "has_not", "doesn't have": "has_not",
    "does not": "does_not", "doesn't": "does_not",
}

_ATTR_RE = _re.compile(r"^(.+)\[([^\[\]=]+)=([^\[\]]+)\]$")
_SRC_RE = _re.compile(r"\s+\[src: ([^\]]*)\]\s*$")


def _out(s=""):
    """print() that survives cp1251/cp1252 consoles (Cyrillic entities)."""
    try:
        print(s)
    except UnicodeEncodeError:
        import sys
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(s.encode(enc, errors="replace").decode(enc, errors="replace"))


# ── triples ──────────────────────────────────────────────────────────────────

def norm_entity(s: str) -> str:
    """Lowercase, NFC, collapsed spaces, no wrapping quotes/backticks/punctuation.
    Keeps Cyrillic (unlike glossary's _kebab) and attribute brackets."""
    s = _ud.normalize("NFC", s or "").strip().lower()
    s = s.strip("`'\"“”«»")
    s = _re.sub(r"\s+", " ", s)
    s = s.rstrip(".,;:!")
    return s.strip()


def norm_predicate(p: str):
    """-> (canonical predicate, error). Unknown verbs become domain predicates;
    'not <verb>' / 'не <verb>' become 'not_<verb>'."""
    raw = _re.sub(r"\s+", " ", _ud.normalize("NFC", p or "").strip().lower()).strip("`'\"")
    if not raw:
        return None, "empty predicate"
    if raw in _ALIASES:
        return _ALIASES[raw], None
    key = raw.replace(" ", "_")
    if key in STRUCTURAL or key in _MODAL:
        return key, None
    neg = False
    for pre in ("not ", "not_", "не "):
        if raw.startswith(pre):
            neg, raw = True, raw[len(pre):].strip()
            break
    if not raw or len(raw) > 40 or len(raw.split()) > 4:
        return None, f"predicate too long or empty: {p!r}"
    verb = raw.replace(" ", "_")
    if verb in _MODAL or verb in STRUCTURAL:
        return None, f"write the negative form instead of 'not {verb}': cannot / is_not / has_not / does_not"
    return ("not_" + verb if neg else verb), None


def family(pred: str):
    """(family, positive) for modal and domain predicates; None for structural."""
    if pred in STRUCTURAL:
        return None
    if pred in _MODAL:
        return _MODAL[pred]
    if pred.startswith("not_"):
        return pred[4:], False
    return pred, True


class Triple:
    __slots__ = ("s", "p", "o", "src", "auto")

    def __init__(self, s, p, o, src="", auto=False):
        self.s, self.p, self.o, self.src, self.auto = s, p, o, src, auto

    @property
    def key(self):
        return (self.s, self.p, self.o)

    def text(self):
        return f"{self.s} :: {self.p} :: {self.o}"

    def line(self):
        return self.text() + (f"  [src: {self.src}]" if self.src else "")

    def to_list(self):
        return [self.s, self.p, self.o, self.src]

    @classmethod
    def from_list(cls, x):
        return cls(x[0], x[1], x[2], x[3] if len(x) > 3 else "")

    def __repr__(self):
        return f"Triple({self.text()!r})"


def _condition_class(subject: str, cond: str):
    """'| woman' -> 'woman'; '| gender = female' -> 'user[gender=female]';
    '| if the user is a woman' -> 'woman'."""
    c = cond.strip()
    c = _re.sub(r"^(if|when|only if|якщо|коли)\s+", "", c, flags=_re.I).strip()
    m = _re.match(r"^([^=]+?)\s*=\s*(.+)$", c)
    if m:
        return f"{subject}[{norm_entity(m.group(1))}={norm_entity(m.group(2))}]"
    m = _re.match(r"^.+?\s+(?:is an?|is|є)\s+(.+)$", c, flags=_re.I)
    if m:
        c = m.group(1)
    return norm_entity(c)


def parse_triple(line: str, src: str = ""):
    """'S :: P :: O [| condition]' -> ([Triple, ...], error). A condition adds
    an automatic 'condition-class :: is_a :: S' triple before the statement."""
    line = _re.sub(r"^\s*(?:[-*]\s*)?(?:TRIPLE:)?\s*", "", line or "", flags=_re.I).strip()
    cond = None
    if " | " in line:
        line, cond = line.split(" | ", 1)
    parts = [x.strip() for x in line.split("::")]
    if len(parts) > 3 and all(parts):
        # small models split a verb phrase: "intern :: cannot :: sign :: the contract"
        parts = parts[:2] + [" ".join(parts[2:])]
    if len(parts) != 3 or not all(parts):
        return [], "expected 'subject :: predicate :: object'"
    s, o = norm_entity(parts[0]), norm_entity(parts[2])
    p, err = norm_predicate(parts[1])
    if err:
        return [], err
    if not s or not o or "?" in (s, o):
        return [], "empty subject or object"
    if "?" in s or "?" in o:
        # "small language model? or turing machine?" — a doubt, not a fact
        return [], "subject or object is a question, not a fact"
    out = []
    if cond and cond.strip():
        cls = _condition_class(s, cond)
        if cls and cls != s:
            out.append(Triple(cls, "is_a", s, src, auto=True))
            s = cls
    if s == o and p in ("is_a", "instance_of", "part_of", "disjoint_with", "same_as"):
        return [], f"'{s} :: {p} :: {o}' relates an entity to itself"
    out.append(Triple(s, p, o, src))
    return out, None


# predicates that are one token with "_" — safe to split on when a model drops
# the "::" separators ("entity map is_a ontological knowledge graph")
_UNDERSCORE_PREDS = ("is_a", "instance_of", "part_of", "disjoint_with", "same_as",
                     "is_not", "has_not", "does_not")


def _repair_separators(line):
    """'TRIPLE: S is_a O' -> 'TRIPLE: S :: is_a :: O' when exactly one
    underscore predicate occurs; else the line is left as is (and rejected).
    Free verbs ('codeXport builds X') stay rejected: where S ends is unknowable."""
    if "::" in line:
        return line
    head, _, body = line.partition(":")
    hits = [p for p in _UNDERSCORE_PREDS if _re.search(rf"\s{p}\s", f" {body} ")]
    if len(hits) != 1:
        return line
    s, _, o = f" {body} ".partition(f" {hits[0]} ")
    if not s.strip() or not o.strip():
        return line
    return f"{head}: {s.strip()} :: {hits[0]} :: {o.strip()}"


# thinking models (gpt-oss, qwen3) draft triples in their reasoning, mid-sentence:
# "TRIPLE: x :: is_a :: y? actually ... so" — the reasoning is not the answer.
# An unclosed block (reply cut off while thinking) is dropped to the end.
_THINK_RE = _re.compile(r"<think>.*?(?:</think>|\Z)", _re.S | _re.I)


def parse_llm_output(raw: str, src: str = ""):
    """TRIPLE: lines only — anything else in the reply (and the model's
    <think> block) is ignored."""
    triples, errors = [], []
    for ln in _THINK_RE.sub("", raw or "").splitlines():
        if not _re.match(r"^\s*(?:[-*]\s*)?TRIPLE:", ln, flags=_re.I):
            continue
        ln = _repair_separators(ln)
        ts, err = parse_triple(ln, src)
        if err:
            errors.append((ln.strip(), err))
        triples.extend(ts)
    return triples, errors


def statement(t: Triple) -> str:
    """Plain-English reading of a triple, for the grounding question."""
    s, o = t.s, t.o
    m = _ATTR_RE.match(s)
    if m:
        s = f"{m.group(1)} whose {m.group(2)} is {m.group(3)}"
    fixed = {
        "is_a": f"{s} is a kind of {o}", "instance_of": f"{s} is a {o}",
        "part_of": f"{s} is part of {o}", "disjoint_with": f"nothing is both {s} and {o}",
        "same_as": f"{s} and {o} are the same thing", "can": f"{s} can {o}",
        "cannot": f"{s} cannot {o}", "must": f"{s} must {o}", "is": f"{s} is {o}",
        "is_not": f"{s} is not {o}", "has": f"{s} has {o}", "has_not": f"{s} does not have {o}",
        "does": f"{s} {o}", "does_not": f"{s} does not {o}",
    }
    if t.p in fixed:
        return fixed[t.p]
    if t.p.startswith("not_"):
        return f"{s} does not {t.p[4:].replace('_', ' ')} {o}"
    return f"{s} {t.p.replace('_', ' ')} {o}"


# ── knowledge base ───────────────────────────────────────────────────────────

class Conflict:
    __slots__ = ("level", "kind", "triples", "reason")

    def __init__(self, level, kind, triples, reason):
        self.level, self.kind, self.triples, self.reason = level, kind, triples, reason

    @property
    def key(self):
        return (self.level, self.kind, frozenset(t.key for t in self.triples))


class KB:
    """Accepted triples + derived structure. Rebuilt on every change: small
    bases, correctness over speed."""

    def __init__(self, triples=()):
        self.triples = []
        self._keys = set()
        for t in triples:
            if t.key not in self._keys:
                self.triples.append(t)
                self._keys.add(t.key)
        self._build()

    def __contains__(self, t):
        return t.key in self._keys

    def with_triple(self, t):
        return KB(self.triples + [t])

    # same_as union-find
    def _root(self, e):
        while self._uf.get(e, e) != e:
            e = self._uf[e]
        return e

    def canon(self, e):
        return self._root(e)

    def _build(self):
        self._uf = {}
        for t in self.triples:
            if t.p == "same_as":
                a, b = self._root(t.s), self._root(t.o)
                if a != b:
                    self._uf[max(a, b)] = min(a, b)
        c = self.canon
        self.parents = {}          # child -> [(parent, triple)]
        self.disjoint = []         # (a, b, triple)
        self.instances = set()
        self.entities = set()
        self.assertions = []       # (subject, family, positive, object, triple)
        for t in self.triples:
            s, o = c(t.s), c(t.o)
            self.entities.update((s, o))
            if t.p in _HIERARCHY:
                self.parents.setdefault(s, []).append((o, t))
                if t.p == "instance_of":
                    self.instances.add(s)
            elif t.p == "disjoint_with":
                self.disjoint.append((s, o, t))
            elif t.p in ("part_of", "same_as"):
                pass
            else:
                fam, pos = family(t.p)
                self.assertions.append((s, fam, pos, o, t))
        self._anc = {}

    def ancestors(self, e):
        """{ancestor: [triples on the is_a/instance_of path from e]} (BFS, shortest)."""
        e = self.canon(e)
        if e in self._anc:
            return self._anc[e]
        seen, frontier = {}, [(e, [])]
        while frontier:
            nxt = []
            for node, path in frontier:
                for par, t in self.parents.get(node, ()):
                    if par not in seen:
                        seen[par] = path + [t]
                        nxt.append((par, path + [t]))
            frontier = nxt
        self._anc[e] = seen
        return seen

    def descendants(self, e):
        e = self.canon(e)
        return {x for x in self.entities if e in self.ancestors(x)}

    def disjoint_reason(self, a, b):
        """Triples (possibly none) proving a and b share no members, or None."""
        a, b = self.canon(a), self.canon(b)
        if a != b and a in self.instances and b in self.instances:
            return []          # two distinct instances (unique names)
        up_a = {a: []}
        up_a.update(self.ancestors(a))
        up_b = {b: []}
        up_b.update(self.ancestors(b))
        for x, y, t in self.disjoint:
            for p, q in ((x, y), (y, x)):
                if p in up_a and q in up_b:
                    return up_a[p] + [t] + up_b[q]
        for x, px in up_a.items():
            mx = _ATTR_RE.match(x)
            if not mx:
                continue
            for y, py in up_b.items():
                my = _ATTR_RE.match(y)
                if my and (mx.group(1), mx.group(2)) == (my.group(1), my.group(2)) \
                        and mx.group(3) != my.group(3):
                    return px + py
        return None

    def overlap(self, a, b):
        """('red'|'none'|'yellow', explaining triples): can a and b share a member?"""
        a, b = self.canon(a), self.canon(b)
        if a == b:
            return "red", []
        anc_a, anc_b = self.ancestors(a), self.ancestors(b)
        if b in anc_a:
            return "red", anc_a[b]
        if a in anc_b:
            return "red", anc_b[a]
        common = sorted(self.descendants(a) & self.descendants(b))
        if common:
            anc_c = self.ancestors(common[0])
            return "red", anc_c[a] + anc_c[b]
        d = self.disjoint_reason(a, b)
        if d is not None:
            return "none", d
        return "yellow", []

    def conflicts(self):
        out = []
        # is_a cycles
        seen_cycles = set()
        for e in sorted(self.entities):
            anc = self.ancestors(e)
            if e in anc:
                k = frozenset(t.key for t in anc[e])
                if k not in seen_cycles:
                    seen_cycles.add(k)
                    out.append(Conflict("red", "cycle", anc[e],
                                        f"is_a cycle through '{e}'"))
        # disjoint classes that share a member
        for x, y, t in self.disjoint:
            lvl, why = self.overlap(x, y)
            if lvl == "red":
                out.append(Conflict("red", "disjoint", [t] + why,
                                    f"'{x}' and '{y}' are declared disjoint but share members"))
        # opposite statements about the same object
        groups = {}
        for s, fam, pos, o, t in self.assertions:
            groups.setdefault((fam, o), ([], []))[0 if pos else 1].append((s, t))
        for (fam, o), (pos, neg) in sorted(groups.items()):
            for s1, t1 in pos:
                for s2, t2 in neg:
                    lvl, why = self.overlap(s1, s2)
                    if lvl == "none":
                        continue
                    if lvl == "red":
                        reason = (f"'{t1.s}' and '{t2.s}' share members — "
                                  f"'{t1.p}' and '{t2.p}' about '{o}' contradict")
                    else:
                        reason = (f"nothing says whether '{t1.s}' and '{t2.s}' overlap — "
                                  f"'{t2.s} :: is_a :: {t1.s}' would make it a contradiction, "
                                  f"'{t1.s} :: disjoint_with :: {t2.s}' would settle it")
                    out.append(Conflict(lvl, "polarity", [t1, t2] + why, reason))
        return out

    def check(self, t, before=None):
        """Conflicts the base would gain by adding t (RED first)."""
        if before is None:
            before = {c.key for c in self.conflicts()}
        new = [c for c in self.with_triple(t).conflicts() if c.key not in before]
        return sorted(new, key=lambda c: c.level != "red")

    # ── queries ──────────────────────────────────────────────────────────────

    def about(self, e):
        """(direct triples mentioning e, inherited assertions [(triple, via path)])."""
        e = self.canon(e)
        direct = [t for t in self.triples if e in (self.canon(t.s), self.canon(t.o))]
        inherited = []
        for anc, path in sorted(self.ancestors(e).items()):
            for s, fam, pos, o, t in self.assertions:
                if s == anc:
                    inherited.append((t, path))
        return direct, inherited

    def ask(self, s, p, o):
        """Pattern query; '?' or '' = any. Statements apply to sub-classes and
        instances. -> [(triple, via path)]."""
        any_s, any_o = s in ("", "?"), o in ("", "?")
        s = None if any_s else self.canon(norm_entity(s))
        o = None if any_o else self.canon(norm_entity(o))
        res = []
        if p in ("", "?"):
            preds = None
        else:
            pc, err = norm_predicate(p)
            if err:
                raise ValueError(err)
            preds = {pc}
        if preds and preds & set(_HIERARCHY):
            subjects = [s] if s else sorted(self.entities)
            for x in subjects:
                for anc, path in sorted(self.ancestors(x).items()):
                    if o is None or anc == o:
                        res.append((Triple(x, "is_a", anc), path))
            return res
        fam = family(next(iter(preds))) if preds and not (preds & set(STRUCTURAL)) else None
        for t in self.triples:
            ts, to = self.canon(t.s), self.canon(t.o)
            if o is not None and to != o:
                continue
            if fam is not None:
                f = family(t.p)
                if not f or f[0] != fam[0]:
                    continue
            elif preds and t.p not in preds:
                continue
            if s is None or ts == s:
                res.append((t, []))
            elif t.p not in STRUCTURAL:
                anc = self.ancestors(s)
                if ts in anc:
                    res.append((t, anc[ts]))
        return res


# ── storage ──────────────────────────────────────────────────────────────────

# Hooks for hosts other than the 1bcoder CLI (vyrii sets both once at startup):
#   ROOT      where projects live; None = <cwd>/.1bcoder/ontology
#   SRC_BASE  "[src: file: ...]" paths are relative to it; None = cwd
ROOT = None
SRC_BASE = None


def project_dir(project):
    root = ROOT or _os.path.join(_os.getcwd(), ".1bcoder", "ontology")
    d = _os.path.join(root, project or "default")
    _os.makedirs(d, exist_ok=True)
    return d


def list_projects():
    """[{project, triples, open}] for every project under the storage root."""
    root = ROOT or _os.path.join(_os.getcwd(), ".1bcoder", "ontology")
    if not _os.path.isdir(root):
        return []
    out = []
    for name in sorted(_os.listdir(root)):
        if not _os.path.isdir(_os.path.join(root, name)):
            continue
        rows = load_jsonl(name, JOURNAL)
        out.append({"project": name, "triples": len(load_triples(name)),
                    "open": sum(r["status"] == "open" for r in rows)})
    return out


def _src_path(fp):
    try:
        return _os.path.relpath(fp, SRC_BASE or _os.getcwd())
    except ValueError:   # another drive on Windows
        return fp


def _path(project, name):
    return _os.path.join(project_dir(project), name)


def load_triples(project):
    p = _path(project, "triples.txt")
    out = []
    if not _os.path.isfile(p):
        return out
    with open(p, encoding="utf-8") as f:
        for n, ln in enumerate(f, 1):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            src = ""
            m = _SRC_RE.search(ln)
            if m:
                src, ln = m.group(1), ln[:m.start()]
            ts, err = parse_triple(ln, src)
            if err:
                _out(f"[ontology] triples.txt:{n}: {err} — line ignored")
            out.extend(ts)
    return out


def save_triples(project, triples):
    with open(_path(project, "triples.txt"), "w", encoding="utf-8") as f:
        f.write("# subject :: predicate :: object  [src: ...]   — /flow ontology help\n")
        for t in triples:
            f.write(t.line() + "\n")


def append_triple(project, t):
    p = _path(project, "triples.txt")
    if not _os.path.isfile(p):
        save_triples(project, [])
    with open(p, "a", encoding="utf-8") as f:
        f.write(t.line() + "\n")


def load_jsonl(project, name):
    p = _path(project, name)
    if not _os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [_json.loads(l) for l in f if l.strip()]


def save_jsonl(project, name, rows):
    with open(_path(project, name), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(_json.dumps(r, ensure_ascii=False) + "\n")


def load_nogood(project):
    p = _path(project, "nogood.txt")
    if not _os.path.isfile(p):
        return set()
    with open(p, encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip() and not l.startswith("#")}


def add_nogood(project, t):
    with open(_path(project, "nogood.txt"), "a", encoding="utf-8") as f:
        f.write(t.text() + "\n")


# ── conflict journal ─────────────────────────────────────────────────────────
# Reality is not consistent: a contradiction found in the sources is knowledge
# too. Nothing is deleted from the journal — a decision only changes a row's
# status, so a person can always see what was NOT recorded, where it was
# found, and which recorded facts it contradicted.
#
#   open      waiting for a decision
#   rejected  the new fact was judged wrong (also in nogood.txt)
#   refined   the new fact was narrowed by a condition and then recorded
#   replaced  the new fact was recorded, the old statement it contradicted dropped
#   disjoint  the subjects were declared disjoint, then the fact was recorded
#   accepted  YELLOW held by --strict, accepted as is
#   known     a real contradiction in the sources — neither side is changed

JOURNAL = "conflicts.jsonl"
STATUSES = ("open", "rejected", "refined", "replaced", "disjoint", "accepted", "known")


def _keys(ts):
    return sorted(list(k) for k in {t.key for t in ts})


def journal_add(project, conflict, cand):
    """Record a refused (RED) or held (YELLOW) candidate. The same contradiction
    met again only adds its source to the open row."""
    rows = load_jsonl(project, JOURNAL)
    cand_keys = {t.key for t in cand}
    others = [t for t in conflict.triples if t.key not in cand_keys]
    new_k, with_k = _keys(cand), _keys(others)
    srcs = sorted({t.src for t in cand if t.src})
    for r in rows:
        if r["status"] == "open" and _keys(map(Triple.from_list, r["new"])) == new_k \
                and _keys(map(Triple.from_list, r["with"])) == with_k:
            r["found_in"] = sorted(set(r.get("found_in", [])) | set(srcs))
            save_jsonl(project, JOURNAL, rows)
            render_journal(project, rows)
            return r
    row = {"id": max([r["id"] for r in rows] or [0]) + 1, "status": "open",
           "level": conflict.level, "reason": conflict.reason,
           "new": [t.to_list() for t in cand], "found_in": srcs,
           "with": [t.to_list() for t in others], "note": ""}
    rows.append(row)
    save_jsonl(project, JOURNAL, rows)
    render_journal(project, rows)
    return row


def journal_set(project, row_id, status, note=""):
    rows = load_jsonl(project, JOURNAL)
    for r in rows:
        if r["id"] == row_id:
            r["status"] = status
            if note:
                r["note"] = note
    save_jsonl(project, JOURNAL, rows)
    render_journal(project, rows)


def render_journal(project, rows=None):
    """conflicts.md — the journal for people, regenerated on every change."""
    rows = load_jsonl(project, JOURNAL) if rows is None else rows
    order = {s: i for i, s in enumerate(STATUSES)}
    lines = [f"# Contradictions — project {project}", "",
             "Generated from conflicts.jsonl; resolve with `/flow ontology conflicts`.", ""]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines.append(" · ".join(f"{s}: {counts[s]}" for s in STATUSES if s in counts) or "empty")
    for r in sorted(rows, key=lambda r: (order.get(r["status"], 99), r["id"])):
        new = [Triple.from_list(x) for x in r["new"]]
        lines += ["", f"## #{r['id']} {r['level'].upper()} — {r['status']}", ""]
        verb = "NOT RECORDED" if r["status"] in ("open", "rejected", "known") else "recorded later"
        for t in new:
            lines.append(f"- {verb}: `{t.text()}`")
        if r.get("found_in"):
            lines.append(f"- found in: {'; '.join(r['found_in'])}")
        lines.append(f"- because: {r['reason']}")
        if r["with"]:
            lines.append("- contradicts recorded facts:")
            for x in r["with"]:
                t = Triple.from_list(x)
                lines.append(f"  - `{t.text()}`" + (f" — {t.src}" if t.src else ""))
        if r.get("note"):
            lines.append(f"- note: {r['note']}")
    with open(_path(project, "conflicts.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── decision: accept / hold / refuse ─────────────────────────────────────────

class Session:
    """KB + its current conflict set, kept in step as triples are accepted."""

    def __init__(self, project, strict=False):
        self.project, self.strict = project, strict
        self.kb = KB(load_triples(project))
        self.before = {c.key for c in self.kb.conflicts()}
        self.nogood = load_nogood(project)
        self.stats = {"accepted": 0, "duplicate": 0, "red": 0, "yellow": 0,
                      "held": 0, "nogood": 0, "ungrounded": 0, "invalid": 0}

    def offer(self, group, quiet=False, journal=True):
        """Offer a group (a statement plus its automatic condition is_a).
        -> 'accepted' | 'duplicate' | 'nogood' | 'red' | 'held'.
        journal=False: a refusal is not written (re-offer while resolving a row)."""
        group = [t for t in group if t not in self.kb]
        if not group:
            self.stats["duplicate"] += 1
            return "duplicate"
        if any(t.text() in self.nogood for t in group):
            self.stats["nogood"] += 1
            return "nogood"
        kb2 = self.kb
        for t in group:
            kb2 = kb2.with_triple(t)
        new = [c for c in kb2.conflicts() if c.key not in self.before]
        red = [c for c in new if c.level == "red"]
        yellow = [c for c in new if c.level == "yellow"]
        self.last_conflict = (red or yellow or [None])[0]
        if red:
            self.stats["red"] += 1
            if journal:
                journal_add(self.project, red[0], group)
            if not quiet:
                show_conflict(red[0], group)
            return "red"
        if yellow and self.strict:
            self.stats["held"] += 1
            if journal:
                journal_add(self.project, yellow[0], group)
            if not quiet:
                show_conflict(yellow[0], group, held=True)
            return "held"
        if yellow:
            self.stats["yellow"] += 1
            if not quiet:
                show_conflict(yellow[0], group, accepted=True)
        for t in group:
            append_triple(self.project, t)
        self.kb = kb2
        self.before = {c.key for c in kb2.conflicts()}
        self.stats["accepted"] += 1
        return "accepted"


def show_conflict(c, cand=(), held=False, accepted=False):
    tag = "RED" if c.level == "red" else "YELLOW"
    note = " (held: --strict)" if held else (" (accepted with warning)" if accepted else "")
    _out(f"  {tag}{note}  {c.reason}")
    cand_keys = {t.key for t in cand}
    for t in cand:
        _out(f"    new   {t.line()}")
    for t in c.triples:
        if t.key not in cand_keys:
            _out(f"    with  {t.line()}")


# ── grounding (Jev) ──────────────────────────────────────────────────────────

class Grounder:
    """Batches 'is this triple stated in the chunk?' questions into one
    /v1/systemone request per chunk. Disabled (None) without a decision model."""

    def __init__(self, jev, settings, raw):
        self.jev, self.settings, self.raw = jev, settings, raw

    @classmethod
    def setup(cls, chat, enabled=True):
        if not enabled:
            return None
        try:
            import jev
        except ImportError:
            _out("[ontology] jev module not found — grounding off")
            return None
        try:
            if hasattr(chat, "_jev_settings"):
                settings = chat._jev_settings()
            else:
                # another host (vyrii): an empty jev host means "the host this
                # chat talks to", as in 1bcoder
                settings = jev.load_settings()
                settings["host"] = jev.resolve_host(
                    settings, getattr(chat, "_base_url", "") or getattr(chat, "base_url", ""))
            if not (settings.get("model") or "").strip():
                _out("[ontology] no decision model — grounding off (/jev model set <name>, or --no-jev)")
                return None
            raw = jev.read_rubric(_GROUND_RUBRIC, _os.getcwd())[3]
        except Exception as e:
            _out(f"[ontology] jev unavailable — grounding off: {e}")
            return None
        return cls(jev, settings, raw)

    def judge(self, chunk, triples):
        """-> [True/False per triple], or None when the decision model failed."""
        jev = self.jev
        crits = []
        for i, t in enumerate(triples):
            r = jev.normalize_rubric(self.raw, _GROUND_RUBRIC, params={"statement": statement(t)})
            for c in r["criteria"]:
                c = dict(c)
                c["id"] = f"t{i}__{c['id']}"
                crits.append(c)
        rubric = {"name": _GROUND_RUBRIC, "threshold": r.get("threshold"), "criteria": crits}
        try:
            res = jev.run_rubric(chunk, rubric, self.settings)
        except jev.JevError as e:
            _out(f"    [jev] {e} — grounding skipped for this chunk")
            return None
        if res.get("error"):
            _out(f"    [jev] {res['error']} — grounding skipped for this chunk")
            return None
        ok = [True] * len(triples)
        for c in res["criteria"]:
            i = int(c["id"].split("__")[0][1:])
            if c.get("passed") is False:
                ok[i] = False
        return ok


# ── LLM extraction ───────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """You extract facts from text as triples.
Output one triple per line, exactly in this format:
TRIPLE: subject :: predicate :: object

Use these predicates when they fit:
  is_a           subject is a kind of object          TRIPLE: woman :: is_a :: user
  instance_of    subject is one concrete object       TRIPLE: alice :: instance_of :: user
  part_of        subject is a component of object     TRIPLE: wheel :: part_of :: car
  disjoint_with  nothing is both subject and object   TRIPLE: man :: disjoint_with :: woman
  can / cannot   permission or ability                TRIPLE: woman :: can :: take maternity leave
  must           obligation                           TRIPLE: employee :: must :: sign the contract
  is / is_not    property                             TRIPLE: contract :: is :: signed
  has / has_not  attribute                            TRIPLE: account :: has :: currency
Otherwise use a short verb, and "not <verb>" for a negation:
  TRIPLE: bank :: issues :: loans
  TRIPLE: bank :: not issues :: mortgages

If a statement holds only for some subjects, make that group the subject and add an is_a line:
  "A user can take maternity leave if the user is a woman" ->
  TRIPLE: woman :: is_a :: user
  TRIPLE: woman :: can :: take maternity leave

Write subjects and objects as short noun phrases in the singular ("man", not "men"),
in the language of the text.
Only write what the text states. No other text."""


def _prompt_examples():
    keys = set()
    for ln in _re.findall(r"TRIPLE:\s*(.+)", _EXTRACT_SYSTEM):
        ts, err = parse_triple(ln)
        if not err:
            keys |= {t.key for t in ts}
    return keys


_PROMPT_EXAMPLES = None


def is_prompt_echo(t, chunk):
    """Small models copy the format line or the examples of _EXTRACT_SYSTEM
    instead of reading the chunk. Such a triple is dropped unless its subject
    really occurs in the chunk."""
    global _PROMPT_EXAMPLES
    if _PROMPT_EXAMPLES is None:
        _PROMPT_EXAMPLES = _prompt_examples()
    if t.key not in _PROMPT_EXAMPLES:
        return False
    if (t.s, t.o) == ("subject", "object"):   # the format line itself
        return True
    return t.s.lower() not in chunk.lower()


def _llm(chat, chunk):
    # a web host can't send Ctrl+C to a worker thread: it sets this callable
    # instead (Stop button), checked before every LLM call
    should_cancel = getattr(chat, "_ontology_should_cancel", None)
    if should_cancel and should_cancel():
        raise _Stop()
    msgs = [{"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": chunk}]
    try:
        raw = chat._stream_chat(msgs)
    except KeyboardInterrupt:
        raw = None
    if raw is None:   # 1bcoder's _stream_chat returns None on Ctrl+C
        try:
            ans = input("\n[ontology] interrupted — [s]kip chunk / [q]uit: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "q"
        if ans.startswith("q"):
            raise _Stop()
        return ""
    return raw


class _Stop(Exception):
    pass


# ── chunking ─────────────────────────────────────────────────────────────────

def chunks_of(text, max_chars):
    """Paragraph-packed chunks -> [(chunk, start_line)]; a paragraph longer
    than max_chars is cut on line boundaries."""
    lines = text.splitlines()
    paras, cur, start = [], [], 1
    for i, ln in enumerate(lines, 1):
        if ln.strip():
            if not cur:
                start = i
            cur.append(ln)
        elif cur:
            paras.append(("\n".join(cur), start))
            cur = []
    if cur:
        paras.append(("\n".join(cur), start))
    out, buf, buf_start = [], "", 1
    for p, s in paras:
        pieces = [(p, s)]
        if len(p) > max_chars:
            pieces, acc, acc_start = [], "", s
            for j, ln in enumerate(p.split("\n")):
                if acc and len(acc) + len(ln) + 1 > max_chars:
                    pieces.append((acc, acc_start))
                    acc, acc_start = "", s + j
                acc = (acc + "\n" + ln) if acc else ln[:max_chars]
            if acc:
                pieces.append((acc, acc_start))
        for piece, ps in pieces:
            if buf and len(buf) + len(piece) + 2 > max_chars:
                out.append((buf, buf_start))
                buf = ""
            if not buf:
                buf_start = ps
            buf = (buf + "\n\n" + piece) if buf else piece
    if buf:
        out.append((buf, buf_start))
    return out


def _collect(path):
    if _os.path.isfile(path):
        return [path]
    out = []
    for root, _dirs, files in _os.walk(path):
        if ".1bcoder" in root.split(_os.sep) or ".git" in root.split(_os.sep):
            continue
        for f in sorted(files):
            if _os.path.splitext(f)[1].lstrip(".").lower() in _PROSE_EXT:
                out.append(_os.path.join(root, f))
    return out


def _group(triples):
    """Keep a statement together with its automatic condition is_a; structural
    groups first so condition classes exist before the statements about them."""
    groups, pending_auto = [], []
    for t in triples:
        if t.auto:
            pending_auto.append(t)
            continue
        groups.append(pending_auto + [t])
        pending_auto = []
    structural = [g for g in groups if g[-1].p in STRUCTURAL]
    return structural + [g for g in groups if g[-1].p not in STRUCTURAL]


# ── subcommands ──────────────────────────────────────────────────────────────

def _pop(rest, pattern):
    m = _re.search(pattern, rest)
    if not m:
        return None, rest
    val = m.group(1) if m.groups() else True
    return val, (rest[:m.start()] + rest[m.end():]).strip()


def _opts(rest):
    project, rest = _pop(rest, r"--project\s+(\S+)")
    strict, rest = _pop(rest, r"--strict\b")
    return project or "default", bool(strict), rest.strip()


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def cmd_index(chat, rest):
    project, strict, rest = _opts(rest)
    chunk_tokens, rest = _pop(rest, r"--chunk\s+(\d+)")
    no_jev, rest = _pop(rest, r"--no-jev\b")
    path = _unquote(rest)
    if not path or path.startswith("--"):
        _out("usage: /flow ontology index <path> [--project P] [--chunk N] [--strict] [--no-jev]")
        return
    files = _collect(path)
    if not files:
        _out(f"[ontology] no .md/.txt/.rst files at: {path}")
        return
    max_chars = int(chunk_tokens or _DEFAULT_CHUNK_TOKENS) * _CHARS_PER_TOKEN
    ses = Session(project, strict)
    grounder = Grounder.setup(chat, not no_jev)
    import time as _time
    model = getattr(chat, "model", "") or ""
    jev_model = grounder.settings.get("model", "") if grounder else ""
    _out(f"[ontology] project: {project}  files: {len(files)}  base: {len(ses.kb.triples)} triple(s)"
         f"  grounding: {'on (' + jev_model + ')' if grounder else 'off'}{'  --strict' if strict else ''}")
    if model:
        _out(f"[ontology] extraction model: {model} — one LLM call per chunk, then one Jev call per chunk")
    t_start = _time.time()
    try:
        for fi, fp in enumerate(files, 1):
            try:
                text = open(fp, encoding="utf-8", errors="ignore").read()
            except OSError as e:
                _out(f"[ontology] skip {fp}: {e}")
                continue
            chunks = [c for c in chunks_of(text, max_chars) if c[0].strip()]
            _out(f"\n[ontology] file {fi}/{len(files)}  {fp} — {len(chunks)} chunk(s)")
            for ci, (chunk, line) in enumerate(chunks, 1):
                src = f"file: {_src_path(fp)}:{line}"
                _out(f"  chunk {ci}/{len(chunks)} (line {line}, {len(chunk)} chars): extracting triples with the LLM…")
                t0 = _time.time()
                raw = _llm(chat, chunk)
                cands, errors = parse_llm_output(raw, src)
                echoes = [c for c in cands if is_prompt_echo(c, chunk)]
                if echoes:
                    cands = [c for c in cands if c not in echoes]
                    errors += [(c.text(), "copied from the instruction, not from the text") for c in echoes]
                ses.stats["invalid"] += len(errors)
                _out(f"    LLM: {len(cands)} candidate(s), {len(errors)} invalid line(s) — {_time.time() - t0:.0f}s")
                if len(errors) >= 3 and len(errors) > len(cands):
                    _out("    ! most lines break the 'S :: P :: O' format — this model does not follow it;"
                         " try another extraction model")
                for ln, err in errors:
                    _out(f"    invalid  {ln}  — {err}")
                groups = _group(cands)
                if grounder and groups:
                    heads = [g[-1] for g in groups]
                    _out(f"    Jev: checking {len(heads)} statement(s) against the chunk…")
                    t0 = _time.time()
                    ok = grounder.judge(chunk, heads)
                    if ok is not None:
                        for g, good in zip(groups, ok):
                            if not good:
                                ses.stats["ungrounded"] += 1
                                _out(f"    ungrounded  {g[-1].text()}")
                        groups = [g for g, good in zip(groups, ok) if good]
                        _out(f"    Jev: {len(groups)}/{len(heads)} grounded — {_time.time() - t0:.0f}s")
                n_acc = 0
                for g in groups:
                    if ses.offer(g) == "accepted":
                        n_acc += 1
                        for t in g:
                            _out(f"    +  {t.text()}")
                s = ses.stats
                _out(f"  chunk {ci}/{len(chunks)} done: {n_acc} accepted — base {len(ses.kb.triples)} triple(s),"
                     f" RED {s['red']}, YELLOW {s['yellow']}, elapsed {(_time.time() - t_start) / 60:.1f} min")
    except _Stop:
        _out("\n[ontology] stopped — accepted triples are already saved")
    s = ses.stats
    _out(f"\n[ontology] done — accepted {s['accepted']}, RED {s['red']}, YELLOW {s['yellow']}"
         f", held {s['held']}, duplicate {s['duplicate']}, ungrounded {s['ungrounded']}"
         f", invalid {s['invalid']}, nogood {s['nogood']}  — base: {len(ses.kb.triples)}")
    if s["red"] or s["held"]:
        _out(f"[ontology] review: /flow ontology conflicts --project {project}")


def cmd_add(chat, rest):
    project, strict, rest = _opts(rest)
    group, err = parse_triple(_unquote(rest), "manual")
    if err:
        _out(f"[ontology] {err}")
        return
    ses = Session(project, strict)
    r = ses.offer(group)
    if r == "accepted":
        for t in group:
            _out(f"[ontology] +  {t.text()}")
    elif r == "duplicate":
        _out("[ontology] already in the base")
    elif r == "nogood":
        _out("[ontology] rejected earlier (nogood.txt) — not added")
    elif r == "red":
        _out(f"[ontology] NOT RECORDED — written to the journal ({_path(project, 'conflicts.md')}); "
             f"resolve: /flow ontology conflicts --project {project}")
    elif r == "held":
        _out(f"[ontology] held (--strict) — resolve: /flow ontology conflicts --project {project}")


def cmd_remove(chat, rest):
    project, _strict, rest = _opts(rest)
    group, err = parse_triple(_unquote(rest))
    if err:
        _out(f"[ontology] {err}")
        return
    t = group[-1]
    triples = load_triples(project)
    left = [x for x in triples if x.key != t.key]
    if len(left) == len(triples):
        _out(f"[ontology] not in the base: {t.text()}")
        return
    save_triples(project, left)
    _out(f"[ontology] removed: {t.text()}")


def cmd_check(chat, rest):
    project, _strict, _rest = _opts(rest)
    kb = KB(load_triples(project))
    cs = kb.conflicts()
    red = [c for c in cs if c.level == "red"]
    yellow = [c for c in cs if c.level == "yellow"]
    _out(f"[ontology] {len(kb.triples)} triple(s), {len(kb.entities)} entit(ies) — "
         f"RED {len(red)}, YELLOW {len(yellow)}")
    for c in red + yellow:
        show_conflict(c)
    rows = load_jsonl(project, JOURNAL)
    n_open = sum(r["status"] == "open" for r in rows)
    n_known = sum(r["status"] == "known" for r in rows)
    if n_open or n_known:
        _out(f"[ontology] journal: {n_open} open, {n_known} known contradiction(s) in the sources"
             f" — {_path(project, 'conflicts.md')}")
    if n_open:
        _out(f"[ontology] resolve: /flow ontology conflicts --project {project}")


def _ask_choice(prompt, allowed):
    while True:
        try:
            a = input(prompt).strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            return "q"
        if a in allowed:
            return a


def _input(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def show_row(row):
    new = [Triple.from_list(x) for x in row["new"]]
    _out(f"\n  #{row['id']} {row['level'].upper()} — {row['status']}")
    for t in new:
        _out(f"    NOT RECORDED  {t.text()}")
    if row.get("found_in"):
        _out(f"    found in      {'; '.join(row['found_in'])}")
    _out(f"    because       {row['reason']}")
    for x in row["with"]:
        _out(f"    contradicts   {Triple.from_list(x).line()}")
    if row.get("note"):
        _out(f"    note          {row['note']}")


def resolve_row(project, row, choice, text=""):
    """Apply one decision to a journal row. -> (new status or None, message).
    None = nothing changed, the row stays open."""
    new = [Triple.from_list(x) for x in row["new"]]
    head = new[-1]
    others = [Triple.from_list(x) for x in row["with"]]
    if choice == "known":
        return "known", "kept as a real contradiction in the sources — nothing recorded"
    if choice == "reject":
        add_nogood(project, head)
        return "rejected", "rejected (nogood.txt)"
    ses = Session(project)
    if choice == "condition":
        group, err = parse_triple(f"{head.s} :: {head.p} :: {head.o} | {text}", head.src)
        if err:
            return None, err
        r = ses.offer(group, quiet=True, journal=False)
        if r in ("accepted", "duplicate"):
            return "refined", "recorded as " + "; ".join(t.text() for t in group)
        return None, f"still {r}: {ses.last_conflict.reason if ses.last_conflict else ''}"
    if choice == "override":
        olds = [t for t in others if t.p not in STRUCTURAL] or others[:1]
        drop = {t.key for t in olds}
        ses.kb = KB([x for x in ses.kb.triples if x.key not in drop])
        ses.before = {c.key for c in ses.kb.conflicts()}
        r = ses.offer(new, quiet=True, journal=False)
        if r != "accepted":
            return None, f"still {r} without the old statement — nothing changed"
        save_triples(project, ses.kb.triples)
        for t in olds:
            add_nogood(project, t)
        return "replaced", "recorded; dropped " + "; ".join(t.text() for t in olds)
    if choice == "disjoint":
        subj = [t.s for t in others if t.p not in STRUCTURAL and t.s != head.s]
        if not subj:
            return None, "no second subject to declare disjoint"
        dis = Triple(head.s, "disjoint_with", subj[0], "manual")
        r = ses.offer([dis] + new, quiet=True, journal=False)
        if r != "accepted":
            return None, f"still {r} — {ses.last_conflict.reason if ses.last_conflict else ''}"
        return "disjoint", f"recorded with {dis.text()}"
    if choice == "accept":
        r = ses.offer(new, quiet=True, journal=False)   # Session is not strict here
        if r in ("accepted", "duplicate"):
            return "accepted", "recorded"
        return None, f"still {r}"
    return None, f"unknown decision {choice!r}"


_CHOICES = {"c": "condition", "k": "known", "r": "reject", "o": "override",
            "d": "disjoint", "a": "accept"}


def cmd_conflicts(chat, rest):
    """Walk the open rows of the journal; --all lists every row with its status."""
    project, _strict, rest = _opts(rest)
    all_rows, rest = _pop(rest, r"--all\b")
    rows = load_jsonl(project, JOURNAL)
    if all_rows:
        for r in rows:
            show_row(r)
        _out(f"\n[ontology] {len(rows)} row(s) — {_path(project, 'conflicts.md')}")
        return
    open_rows = [r for r in rows if r["status"] == "open"]
    if not open_rows:
        _out(f"[ontology] nothing open — history: /flow ontology conflicts --all, or "
             f"{_path(project, 'conflicts.md')}")
        return
    _out(f"[ontology] {len(open_rows)} open contradiction(s)")
    for row in open_rows:
        show_row(row)
        yellow = row["level"] == "yellow"
        menu = ("  [c]ondition: narrow the new fact (e.g. '| woman')  [k]nown: a real contradiction"
                " in the sources\n  [r]eject the new fact  [o]verride: record it, drop the old statement")
        menu += ("\n  [d]isjoint: the subjects never overlap  [a]ccept as is" if yellow else "")
        _out(menu)
        while True:
            a = _input("  [s]kip / [q]uit, or a letter above: ").lower()[:1]
            if a in ("", "q"):
                _out(f"[ontology] stopped — {_path(project, 'conflicts.md')}")
                return
            if a == "s":
                break
            choice = _CHOICES.get(a)
            if not choice or (choice in ("disjoint", "accept") and not yellow):
                continue
            text = ""
            if choice == "condition":
                text = _input("  condition (a sub-class, or attribute = value): ")
                if not text:
                    continue
            if choice == "known":
                text = _input("  note (optional): ")
            status, msg = resolve_row(project, row, choice, text)
            _out(f"    {msg}")
            if status:
                note = text if choice in ("condition", "known") else ""
                journal_set(project, row["id"], status, note)
                break
    _out(f"[ontology] journal: {_path(project, 'conflicts.md')}")


def cmd_show(chat, rest):
    project, _strict, rest = _opts(rest)
    toks = rest.split()
    ctx = bool(toks) and toks[-1].lower() == "ctx"
    if ctx:
        toks = toks[:-1]
    e = norm_entity(_unquote(" ".join(toks)))
    if not e:
        _out("usage: /flow ontology show <entity> [ctx] [--project P]")
        return
    kb = KB(load_triples(project))
    direct, inherited = kb.about(e)
    if not direct and not inherited:
        _out(f"[ontology] nothing about '{e}' (project: {project})")
        return
    lines = [f"# {e}"]
    lines += [t.line() for t in direct]
    if inherited:
        lines.append("inherited:")
        for t, path in inherited:
            via = " <- ".join(p.text() for p in path)
            lines.append(f"  {t.text()}   (via {via})")
    text = "\n".join(lines)
    _out(text)
    if ctx:
        chat.messages.append({"role": "user", "content": f"[ontology: {e}]\n{text}"})
        _out(f"[ontology] added '{e}' to context")


def cmd_ask(chat, rest):
    project, _strict, rest = _opts(rest)
    parts = [x.strip() for x in _unquote(rest).split("::")]
    if len(parts) != 3:
        _out('usage: /flow ontology ask "S :: P :: O"   (? = any)')
        return
    kb = KB(load_triples(project))
    try:
        res = kb.ask(*parts)
    except ValueError as e:
        _out(f"[ontology] {e}")
        return
    if not res:
        _out("[ontology] unknown — nothing in the base says so (open world: not the same as 'no')")
        return
    lines = []
    for t, path in res:
        via = ("   via " + " <- ".join(p.text() for p in path)) if path else ""
        lines.append(f"  {t.line()}{via}")
    pol = {}
    for t, _ in res:
        f = family(t.p)
        if f:
            pol.setdefault((f[0], kb.canon(t.o)), set()).add(f[1])
    both = [o for (fam, o), ps in pol.items() if ps == {True, False}]
    if both:
        lines.append(f"  ! both answers are derivable for: {', '.join(both)} — run /flow ontology check")
    for ln in lines:
        _out(ln)
    if hasattr(chat, "_last_output"):
        chat._last_output = "\n".join(lines)


def _local(name):
    """IRI tail usable as a SPARQL prefixed name: spaces -> '_', letters of any
    script kept as is (e:жінка, e:take_maternity_leave), the rest %-escaped."""
    out = []
    for ch in name:
        if ch == " ":
            out.append("_")
        elif ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append(_quote(ch, safe=""))
    return "".join(out)


def _unlocal(tail):
    from urllib.parse import unquote
    return unquote(tail.replace("_", " "))


def to_turtle(triples):
    base = "http://1bcoder.local/ontology/"
    iri = lambda e: f"<{base}e/{_local(e)}>"
    rel = {"is_a": "rdfs:subClassOf", "instance_of": "rdf:type",
           "disjoint_with": "owl:disjointWith", "same_as": "owl:sameAs"}
    out = ["@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
           "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
           "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
           f"@prefix p: <{base}p/> .", ""]
    ents = sorted({t.s for t in triples} | {t.o for t in triples})
    for e in ents:
        out.append(f'{iri(e)} rdfs:label {_json.dumps(e, ensure_ascii=False)} .')
    out.append("")
    for t in triples:
        pred = rel.get(t.p) or f"<{base}p/{_local(t.p)}>"
        out.append(f"{iri(t.s)} {pred} {iri(t.o)} .")
    return "\n".join(out) + "\n"


def cmd_export(chat, rest):
    project, _strict, _rest = _opts(rest)
    triples = load_triples(project)
    p = _path(project, "ontology.ttl")
    with open(p, "w", encoding="utf-8") as f:
        f.write(to_turtle(triples))
    _out(f"[ontology] {len(triples)} triple(s) -> {p}")


def cmd_sparql(chat, rest):
    project, _strict, rest = _opts(rest)
    query = _unquote(rest)
    if not query:
        _out('usage: /flow ontology sparql "SELECT ?s WHERE { ?s ?p ?o }" [--project P]')
        return
    try:
        import rdflib
    except ImportError:
        _out("[ontology] sparql needs rdflib: pip install rdflib")
        return
    g = rdflib.Graph()
    g.parse(data=to_turtle(load_triples(project)), format="turtle")
    prefixes = ("PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
                "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
                "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
                "PREFIX e: <http://1bcoder.local/ontology/e/>\n"
                "PREFIX p: <http://1bcoder.local/ontology/p/>\n")
    try:
        rows = g.query(prefixes + query)
    except Exception as e:
        _out(f"[ontology] sparql error: {e}")
        return
    n = 0
    for row in rows:
        n += 1
        _out("  " + "  |  ".join(
            (_unlocal(str(v).rsplit("/", 1)[-1]) if str(v).startswith("http://1bcoder.local/")
             else str(v)) if v is not None else "-" for v in row))
    _out(f"[ontology] {n} row(s)")


def run(chat, args: str) -> None:
    """Strict triple knowledge base (S :: P :: O) with contradiction checks"""
    args = (args or "").strip()
    if not args or args in ("help", "-h", "--help"):
        _out(__doc__)
        return
    sub, _, rest = args.partition(" ")
    cmds = {"index": cmd_index, "add": cmd_add, "remove": cmd_remove, "check": cmd_check,
            "conflicts": cmd_conflicts, "show": cmd_show, "ask": cmd_ask,
            "export": cmd_export, "sparql": cmd_sparql}
    fn = cmds.get(sub)
    if not fn:
        _out(f"[ontology] unknown subcommand '{sub}' — /flow ontology help")
        return
    fn(chat, rest)
