"""ladder.py — offline ladder detector for 1bcoder agent sessions.

Reads an autosave .txt (or agent_ctx JSON) and checks how far the agent
progressed on the generator-verifier ladder defined in §1.2 of
AUTOMATICAL_AGENTS_LOGICAL_EXTERNAL_APPROACH.

Four rungs:
  1. Presence     — task term appeared anywhere in context
  2. Witnessed    — task term appeared inside a successful tool result
                    (exit code 0); failed tools count as weak evidence only
  3. Co-occurred  — 2+ priority terms met in the same successful tool result
  4. Articulated  — 2+ priority terms present in the final assistant reply

PASS = rung 3 reached (co-occurrence in a real tool result).
FAIL = anything below rung 3, with a specific hint.

Usage — offline analysis:
  python ladder.py session.txt term1 term2 term3 ...
  python ladder.py session.txt --terms-from task.txt
  python ladder.py session.txt            (auto-extracts from task turn)
  python ladder.py --json ctx.json term1 term2

Usage — gate proc (reads BCODER_AGENT_CTX_FILE env var):
  python ladder.py --gate                 (auto-extracts terms from context)
  python ladder.py --gate term1 term2     (explicit terms)
  /proc gate on ladder                    (1bcoder usage)
"""
import sys
import os
import re
import json
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ── turn parsing ──────────────────────────────────────────────────────────────

TOOL_HDR_RE = re.compile(r'^\[(run|file|read|tool|web|webask|webfetch|search|fetch)[:\s]', re.IGNORECASE)
EXIT_OK_RE  = re.compile(r'\(exit code 0\)')
EXIT_ERR_RE = re.compile(r'\(exit code ([1-9]\d*)\)')


@dataclass
class Turn:
    role: str         # "user" | "assistant"
    content: str
    is_tool: bool     # user turn that carries a tool result
    tool_ok: bool     # True = exit code 0 (or [file:] which has no exit code)


def _make_turn(role: str, content: str) -> Turn:
    stripped = content.lstrip()
    is_tool = role == "user" and bool(TOOL_HDR_RE.match(stripped))
    if not is_tool:
        return Turn(role, content, False, False)
    # [file: ...] always ok; [run: ...] ok only if exit code 0
    if stripped.startswith("[file") or stripped.startswith("[read"):
        tool_ok = True
    else:
        header = stripped[:120]
        tool_ok = bool(EXIT_OK_RE.search(header)) or not bool(EXIT_ERR_RE.search(header))
    return Turn(role, content, is_tool, tool_ok)


def parse_autosave(path: str) -> List[Turn]:
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    blocks = re.split(r'^=== (user|assistant) ===\s*$', text, flags=re.MULTILINE)
    turns = []
    i = 1
    while i + 1 < len(blocks):
        role = blocks[i].strip()
        content = blocks[i + 1].strip()
        turns.append(_make_turn(role, content))
        i += 2
    return turns


def parse_json_ctx(path: str) -> List[Turn]:
    with open(path, encoding="utf-8") as f:
        messages = json.load(f)
    return [_make_turn(m.get("role", "user"), m.get("content", "")) for m in messages]


# ── term extraction ───────────────────────────────────────────────────────────

# Words never useful as task terms
_STOP = {
    "the","a","an","in","on","at","to","of","is","are","was","be","for","and",
    "or","with","this","that","it","as","by","from","not","but","when","if",
    "then","will","can","should","we","i","you","they","have","has","do","does",
    "no","so","there","also","please","get","use","make","what","how","why",
    "would","could","need","want","about","after","before","which","where",
    "these","those","their","them","been","being","some","more","other","like",
    "just","only","very","any","all","each","both","such","into","than","here",
    "our","your","my","his","her","its","might","must","shall","let","set",
    "put","take","give","know","think","look","mean","seem","find","say","tell",
    "show","help","work","call","keep","turn","start","another","window","signed",
    "reload","github","https","http","content","version","using","type","name",
    "value","list","code","file","class","method","function","object","string",
    "number","return","result","error","issue","problem","change","current",
    "expected","actual","behavior","example","used","added","fixed","updated",
    "created","removed","when","style","format","check","test","spec","line",
    "lines","config","default","should","without","please","output","input",
}


def _code_terms(text: str) -> List[str]:
    """Extract terms from backtick-quoted and fenced code blocks — highest confidence."""
    terms = []
    # inline backtick: `identifier`
    for m in re.finditer(r'`([A-Za-z][A-Za-z0-9_:/.-]{2,})`', text):
        t = m.group(1)
        if "/" not in t and "." not in t:   # skip paths/urls in backticks
            terms.append(t)
    # fenced code blocks
    for block in re.findall(r'```[\w]*\n(.*?)```', text, re.DOTALL):
        for m in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', block):
            terms.append(m.group(1))
        for m in re.finditer(r'\b([a-z][a-z0-9]+(?:_[a-z0-9]+)+)\b', block):
            terms.append(m.group(1))
    return terms


def _camel_terms(text: str) -> List[str]:
    """CamelCase identifiers from plain text — high confidence."""
    return re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', text)


def _snake_terms(text: str) -> List[str]:
    """snake_case identifiers — high confidence."""
    return re.findall(r'\b([a-z][a-z0-9]+(?:_[a-z0-9]+)+)\b', text)


def _plain_terms(text: str) -> List[str]:
    """Long plain words from text — low confidence, heavily filtered."""
    result = []
    # remove URLs first
    text = re.sub(r'https?://\S+', ' ', text)
    for m in re.finditer(r'\b([a-zA-Z]{8,})\b', text):
        w = m.group(1)
        wl = w.lower()
        if wl in _STOP:
            continue
        if w == w.upper():          # ALL_CAPS — env vars, constants, skip
            continue
        if re.search(r'(.)\1{2,}', wl):   # tripled letter — likely typo
            continue
        result.append(w)
    return result


def extract_terms(text: str, max_terms: int = 15) -> List[str]:
    """Extract task terms in priority order: code > CamelCase > snake > plain."""
    seen: set = set()
    result: List[str] = []

    def add(tokens: List[str]) -> None:
        for t in tokens:
            k = t.lower()
            if k not in seen and k not in _STOP and len(t) >= 3:
                seen.add(k)
                result.append(t)

    add(_code_terms(text))
    add(_camel_terms(text))
    add(_snake_terms(text))
    if len(result) < 4:           # only add noisy plain terms when we have few good ones
        add(_plain_terms(text))

    return result[:max_terms]


def _auto_terms(turns: List[Turn], max_terms: int = 12) -> List[str]:
    """Extract task terms: prefer first non-tool user turn; fall back to first [file:] content."""
    # Pass 1: first non-tool user message (the actual task prompt)
    for t in turns:
        if t.role == "user" and not t.is_tool and len(t.content) > 30:
            terms = extract_terms(t.content, max_terms=max_terms)
            if terms:
                return terms
    # Pass 2: first [file:] tool result (the loaded task/plan file)
    for t in turns:
        if t.is_tool and t.content.lstrip().startswith("[file"):
            # strip the header line, extract from content body
            body = re.sub(r'^\[file:[^\]]+\]\s*```[^\n]*\n?', '', t.content.lstrip(), count=1)
            body = re.sub(r'```\s*$', '', body)
            terms = extract_terms(body, max_terms=max_terms)
            if terms:
                return terms
    return []


# ── ladder logic ──────────────────────────────────────────────────────────────

@dataclass
class Rung:
    reached: bool
    weak: bool = False          # reached only via failed tool (exit code != 0)
    evidence: str = ""
    source: str = ""


@dataclass
class LadderReport:
    terms: List[str]
    priority: List[str]         # first 5 terms — used for co-occurrence
    presence:   Rung
    witnessed:  Rung
    co_occurred: Rung
    articulated: Rung
    highest: int                # sequential rung count (0-4)

    def passed(self) -> bool:
        return self.highest >= 3 and not self.co_occurred.weak

    def fail_hint(self) -> str:
        if self.highest == 0:
            top = ", ".join(self.terms[:5])
            return (f"None of the task terms ({top}) appeared in context. "
                    "The agent has not started working on the task.")
        if self.highest == 1:
            top = ", ".join(self.terms[:5])
            return (f"Terms appeared only in model prose — no tool result confirmed them. "
                    f"The agent must call a tool (read/find/run) that returns "
                    f"evidence about: {top}")
        if self.highest == 2:
            p = self.priority[:4]
            return (f"Terms were witnessed separately but never co-occurred in one "
                    f"tool result. Need both in the same result: {p}")
        if self.co_occurred.weak:
            return ("Co-occurrence was found only in a failed tool result (exit code != 0). "
                    "The agent needs a successful tool call containing both term clusters.")
        return ""


def _snip(content: str, term: str) -> str:
    idx = content.lower().find(term.lower())
    if idx < 0:
        return ""
    s = max(0, idx - 25)
    e = min(len(content), idx + 100)
    return content[s:e].replace("\n", " ").strip()[:120]


def run_ladder(turns: List[Turn], terms: List[str]) -> LadderReport:
    priority = terms[:5]   # top terms used for co-occurrence

    # Rung 1: Presence — any term in any turn
    r1 = Rung(False)
    for i, t in enumerate(turns):
        for term in terms:
            if term.lower() in t.content.lower():
                r1 = Rung(True, evidence=_snip(t.content, term),
                          source=f"turn {i+1} ({t.role}{' [tool]' if t.is_tool else ''})")
                break
        if r1.reached:
            break

    # Rung 2: Witnessed — term in a tool result
    # Prefer tool_ok=True; fall back to failed tool as "weak"
    r2 = Rung(False)
    r2_weak = Rung(False)
    for i, t in enumerate(turns):
        if not t.is_tool:
            continue
        for term in terms:
            if term.lower() in t.content.lower():
                rung = Rung(True, weak=not t.tool_ok,
                            evidence=_snip(t.content, term),
                            source=f"turn {i+1} [tool{'!' if not t.tool_ok else ''}]")
                if t.tool_ok:
                    r2 = rung
                    break
                elif not r2_weak.reached:
                    r2_weak = rung
        if r2.reached:
            break
    if not r2.reached and r2_weak.reached:
        r2 = r2_weak   # weak fallback

    # Rung 3: Co-occurrence — 2+ priority terms in same tool result
    # Must be a successful tool (tool_ok=True) to count as strong
    r3 = Rung(False)
    r3_weak = Rung(False)
    for i, t in enumerate(turns):
        if not t.is_tool:
            continue
        hits = [term for term in priority if term.lower() in t.content.lower()]
        if len(hits) >= 2:
            rung = Rung(True, weak=not t.tool_ok,
                        evidence=f"{hits[:4]} @ {_snip(t.content, hits[0])}",
                        source=f"turn {i+1} [tool{'!' if not t.tool_ok else ''}]")
            if t.tool_ok:
                r3 = rung
                break
            elif not r3_weak.reached:
                r3_weak = rung
    if not r3.reached and r3_weak.reached:
        r3 = r3_weak

    # Rung 4: Articulated — 2+ priority terms in final assistant turn
    r4 = Rung(False)
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        if t.role == "assistant" and not t.is_tool:
            hits = [term for term in priority if term.lower() in t.content.lower()]
            if len(hits) >= 2:
                r4 = Rung(True, evidence=f"{hits[:4]} @ {_snip(t.content, hits[0])}",
                          source=f"turn {i+1} [assistant]")
            break

    # Sequential height — stop at first unreached rung
    rungs = [r1, r2, r3, r4]
    height = 0
    for r in rungs:
        if r.reached and not r.weak:
            height += 1
        else:
            break

    return LadderReport(
        terms=terms, priority=priority,
        presence=r1, witnessed=r2, co_occurred=r3, articulated=r4,
        highest=height,
    )


# ── report ────────────────────────────────────────────────────────────────────

def print_report(rep: LadderReport) -> None:
    labels = ["presence", "witnessed", "co-occurred", "articulated"]
    rungs  = [rep.presence, rep.witnessed, rep.co_occurred, rep.articulated]

    print("\n-- Ladder report -------------------------------------------")
    print(f"   Terms ({len(rep.terms)}): {', '.join(rep.terms[:8])}"
          f"{'...' if len(rep.terms) > 8 else ''}")
    print(f"   Priority: {rep.priority}")
    print()
    for i, (label, rung) in enumerate(zip(labels, rungs)):
        if rung.reached and rung.weak:
            icon = "~"
        elif rung.reached:
            icon = "+"
        else:
            icon = "-"
        print(f"  [{icon}]  Rung {i+1}: {label}{'  (weak: failed tool)' if rung.weak else ''}")
        if rung.reached and rung.evidence:
            ev = rung.evidence[:100].encode("ascii", errors="replace").decode("ascii")
            print(f"       @ {rung.source}")
            print(f"       > {ev}")
    print()
    print(f"  Highest clean rung: {rep.highest}/4")
    verdict = "PASS" if rep.passed() else "FAIL"
    print(f"  Verdict: {verdict}")
    if not rep.passed():
        hint = rep.fail_hint().encode("ascii", errors="replace").decode("ascii")
        print(f"\n  Hint: {hint}")
    print("------------------------------------------------------------\n")


# ── gate mode ─────────────────────────────────────────────────────────────────

def gate_mode(explicit_terms: List[str]) -> None:
    _reply = sys.stdin.read()  # consume — agent reply piped by 1bcoder; not used by ladder
    ctx_file = os.environ.get("BCODER_AGENT_CTX_FILE", "")
    if not ctx_file or not os.path.isfile(ctx_file):
        sys.exit(0)  # no context yet — pass silently

    try:
        turns = parse_json_ctx(ctx_file)
    except Exception as e:
        print(f"FAIL: [ladder] cannot read context: {e}")
        sys.exit(0)

    terms = list(explicit_terms)

    # auto-extract: first non-tool user turn, else first [file:] content
    if not terms:
        terms = _auto_terms(turns, max_terms=12)

    if not terms:
        sys.exit(0)  # can't determine terms — pass silently

    rep = run_ladder(turns, terms)
    if not rep.passed():
        print(f"FAIL: [ladder] {rep.fail_hint()}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    argv = sys.argv[1:]

    # gate mode
    if not argv or argv[0] == "--gate":
        explicit = argv[1:] if argv else []
        gate_mode(explicit)
        sys.exit(0)

    # --json flag
    use_json = False
    if argv[0] == "--json":
        use_json = True
        argv = argv[1:]

    if not argv:
        print(__doc__)
        sys.exit(1)

    session_path = argv[0]
    term_args    = argv[1:]

    # --terms-from <file>
    if len(term_args) == 2 and term_args[0] == "--terms-from":
        with open(term_args[1], encoding="utf-8", errors="replace") as f:
            raw = f.read()
        term_args = extract_terms(raw, max_terms=15)
        print(f"[ladder] extracted {len(term_args)} terms from {argv[1]}: {term_args}")

    if not os.path.isfile(session_path):
        print(f"[ladder] file not found: {session_path}")
        sys.exit(1)

    turns = parse_json_ctx(session_path) if use_json else parse_autosave(session_path)

    # auto-extract if no terms given
    if not term_args:
        term_args = _auto_terms(turns, max_terms=12)
        if term_args:
            print(f"[ladder] auto-extracted {len(term_args)} terms: {term_args}")

    if not term_args:
        print("[ladder] no terms — pass as args or use --terms-from <file>")
        sys.exit(1)

    rep = run_ladder(turns, term_args)
    print_report(rep)

    tool_ok  = sum(1 for t in turns if t.is_tool and t.tool_ok)
    tool_err = sum(1 for t in turns if t.is_tool and not t.tool_ok)
    asst     = sum(1 for t in turns if t.role == "assistant")
    print(f"  Session: {len(turns)} turns — "
          f"{tool_ok} tool-ok, {tool_err} tool-err, {asst} assistant")
    print()
