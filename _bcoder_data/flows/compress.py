"""compress — annotate and remove redundant words/phrases.

Four modes:
  --mode rules   pure rule-based (fast, deterministic, covers common patterns)
  --mode list    LLM outputs a PHRASE → reason list, then annotate (default for hybrid step)
  --mode inline  LLM rewrites text with <phrase|reason> tags inline
  --mode hybrid  rules first, then list-model for remaining text (default)

Usage:
  /flow compress <text>
  /flow compress $                         (compress last LLM reply)
  /flow compress file: notes.txt
  /flow compress --strip <text>            (output compressed only, no annotations)
  /flow compress --mode rules <text>
  /flow compress --mode list <text>
  /flow compress --mode inline <text>
"""
import re as _re
import os as _os

# ── rule-based patterns ───────────────────────────────────────────────────────

_RULES = [
    # hedges
    (r"\bI think,?\s*",           "hedge"),
    (r"\bI believe,?\s*",         "hedge"),
    (r"\bI feel,?\s*",            "hedge"),
    (r"\bin my opinion,?\s*",     "hedge"),
    (r"\bit seems(?: that)?,?\s*","hedge"),
    (r"\bperhaps\s+",             "hedge"),
    (r"\bmaybe\s+",               "hedge"),
    (r"\bprobably\s+",            "hedge"),
    (r"\bgenerally\s+",           "hedge"),
    (r"\busually\s+",             "hedge"),
    (r"\btypically\s+",           "hedge"),
    # filler intensifiers
    (r"\bvery\s+",                "filler"),
    (r"\bquite\s+",               "filler"),
    (r"\brather\s+",              "filler"),
    (r"\breally\s+",              "filler"),
    (r"\bextremely\s+",           "filler"),
    (r"\babsolutely\s+",          "filler"),
    (r"\bcompletely\s+",          "filler"),
    (r"\btotally\s+",             "filler"),
    (r"\bsomewhat\s+",            "filler"),
    # empty phrases
    (r"\bthe fact that\s+",       "filler"),
    (r"\bit is worth noting that\s+",    "filler"),
    (r"\bit should be noted that\s+",    "filler"),
    (r"\bit is important to note that\s+",  "filler"),
    (r"\bit is worth mentioning that\s+",   "filler"),
    (r"\bit is necessary to note that\s+",  "filler"),
    (r"\bnote that\s+",                     "filler"),
    (r"\bin order to\s+",                              "filler"),
    (r"\bI would like to take this opportunity to mention\s+that\s+", "hedge"),
    (r"\bI would like to take this opportunity to\s+",               "hedge"),
    (r"\btake this opportunity to\s+",                               "hedge"),
    (r"\bdue to the fact that\s+","filler"),
    (r"\bas a matter of fact,?\s*","filler"),
    (r"\bbasically\s+",           "filler"),
    (r"\bessentially\s+",         "filler"),
    (r"\bfundamentally\s+",       "filler"),
    # weak verb clusters
    (r"\btends? to\s+",           "weaken"),
    (r"\bseems? to\s+",           "weaken"),
    (r"\bappears? to\s+",         "weaken"),
    # redundant quantifiers
    (r"\beach and every\b",       "redundant"),
    (r"\bfirst and foremost\b",   "redundant"),
    (r"\bat this point in time\b","redundant"),
    (r"\bin the event that\b",    "redundant"),
]

_STRIP_RE  = _re.compile(r'<([^|>]+)\|[^>]+>')
_INLINE_RE = _re.compile(r'<([^|>]+)\|([a-z]+)>')

_PROMPT_LIST = """\
Find redundant words and phrases in the text below.
Output ONLY a list, one per line: PHRASE → reason
Reasons: hedge, filler, redundant, weaken

Rules:
- PHRASE must be copied exactly from the text
- Never mark: technical terms, subjects, main verbs, key nouns
- Only mark words that can be removed without changing the core meaning

Example:
Text: I think cats are very nice animals that people generally tend to like a lot
I think → hedge
very → filler
that people generally tend to like a lot → redundant

Text: {text}
"""

_PROMPT_INLINE = """\
Rewrite the text below with redundant words and phrases wrapped in <word|reason> tags.
Reasons: hedge, filler, redundant, weaken
Keep ALL other words exactly as they are. Do not add, remove, or reorder any words outside the tags.

Rules:
- Never mark: technical terms, subjects, main verbs, key nouns
- Only mark what can be removed without changing the core meaning

Example:
Input:  I think cats are very nice animals that people generally tend to like a lot
Output: <I think|hedge> cats are <very|filler> nice animals <that people generally tend to like a lot|redundant>

Input:  {text}
Output:"""


# ── core functions ────────────────────────────────────────────────────────────

def _apply_rules(text: str) -> tuple:
    """Returns (annotated_text, [(phrase, reason), ...]).
    Searches all patterns in the ORIGINAL text, deduplicates, then annotates once."""
    candidates = []
    for pattern, reason in _RULES:
        m = _re.search(pattern, text, _re.IGNORECASE)
        if m:
            candidates.append((m.group(0).rstrip(), reason))

    # deduplicate: keep longest, remove any phrase that is a substring of a longer one
    found = []
    for phrase, reason in sorted(candidates, key=lambda x: -len(x[0])):
        if not any(phrase.lower() in p.lower() for p, _ in found):
            found.append((phrase, reason))

    result = _annotate(text, found)
    return _re.sub(r'  +', ' ', result).strip(), found


_VALID_REASONS = {'hedge', 'filler', 'redundant', 'weaken'}

def _parse_model_list(raw: str) -> list:
    pairs = []
    for line in raw.splitlines():
        line = line.strip()
        # skip lines without a separator — they're prose/thinking output
        sep_found = None
        for sep in (' → ', ' -> ', ' — ', ': '):
            if sep in line:
                sep_found = sep
                break
        if not sep_found:
            continue
        phrase, _, reason = line.partition(sep_found)
        # clean up list markers and stray quotes from phrase
        phrase = phrase.strip()
        phrase = _re.sub(r'^[-*•]\s*', '', phrase)   # leading - * •
        phrase = phrase.strip('"\'`')                  # surrounding quotes/backticks
        phrase = phrase.strip()
        reason = reason.strip().split()[0].lower().rstrip('.,;')
        # only accept known reasons, skip malformed/thinking lines
        if phrase and reason in _VALID_REASONS:
            pairs.append((phrase, reason))
    # deduplicate — keep first occurrence of each phrase
    seen = set()
    return [(p, r) for p, r in pairs if not (p in seen or seen.add(p))]


def _annotate(text: str, pairs: list) -> str:
    result = text
    for phrase, reason in sorted(pairs, key=lambda x: -len(x[0])):
        result = _re.sub(_re.escape(phrase), f"<{phrase}|{reason}>", result, count=1)
    return result


def _compress(annotated: str) -> str:
    # strip annotations, handle nested cases by repeating until stable
    text = annotated
    for _ in range(5):
        prev = text
        text = _STRIP_RE.sub('', text)
        # also clean up any leftover |reason> fragments from nesting
        text = _re.sub(r'\|[a-z]+>', '', text)
        text = _re.sub(r'<[^>]*>', '', text)   # catch any remaining < > fragments
        if text == prev:
            break
    text = _re.sub(r'  +', ' ', text).strip()
    # clean up orphan punctuation at start: ". text" → "text"
    text = _re.sub(r'^[.,;:\-–—]+\s*', '', text)
    return text


def _model_annotate_list(chat, text: str) -> tuple:
    prompt = _PROMPT_LIST.format(text=text)
    msgs = [
        {"role": "system", "content": "You are a text editor. Output only the redundancy list."},
        {"role": "user",   "content": prompt},
    ]
    raw = chat._stream_chat(msgs) or ""
    print()
    pairs = _parse_model_list(raw)
    return _annotate(text, pairs), pairs


def _model_annotate_inline(chat, text: str) -> tuple:
    prompt = _PROMPT_INLINE.format(text=text)
    msgs = [
        {"role": "system", "content": "You are a text editor. Output only the rewritten line with tags."},
        {"role": "user",   "content": prompt},
    ]
    raw = chat._stream_chat(msgs) or ""
    print()
    pairs = [(p, r) for p, r in _INLINE_RE.findall(raw) if r in _VALID_REASONS]
    seen: set = set()
    pairs = [(p, r) for p, r in pairs if not (p in seen or seen.add(p))]
    # raw is already annotated; clean up any stray leading/trailing prose
    annotated = raw.strip().splitlines()[-1] if raw.strip() else text
    return annotated, pairs


# ── main ──────────────────────────────────────────────────────────────────────

def run(chat, args: str):
    args = args.strip()

    strip_only = "--strip" in args
    if strip_only:
        args = args.replace("--strip", "").strip()

    mode = "hybrid"
    m = _re.search(r'--mode\s+(\S+)', args)
    if m:
        mode = m.group(1)
        args = (args[:m.start()] + args[m.end():]).strip()

    if mode not in ("rules", "list", "inline", "hybrid"):
        print(f"[compress] unknown mode '{mode}'. Use: rules, list, inline, hybrid"); return

    if args.startswith("file:"):
        fpath = args[5:].strip()
        if not _os.path.isabs(fpath):
            fpath = _os.path.join(_os.getcwd(), fpath)
        try:
            text = open(fpath, encoding="utf-8").read().strip()
        except OSError as e:
            print(f"[compress] {e}"); return
    elif args == "$":
        text = getattr(chat, "_last_output", "").strip()
        if not text:
            print("[compress] no last output"); return
    else:
        text = args

    if not text:
        print("usage: /flow compress [--mode rules|model|hybrid] [--strip] <text | $ | file: path>")
        return

    print(f"[compress] input  : {text}")
    print(f"[compress] mode   : {mode}\n")

    all_pairs    = []
    annotated    = text
    final_compressed = None

    if mode == "rules":
        annotated, all_pairs = _apply_rules(text)

    elif mode == "list":
        annotated, all_pairs = _model_annotate_list(chat, text)

    elif mode == "inline":
        annotated, all_pairs = _model_annotate_inline(chat, text)

    elif mode == "hybrid":
        # step 1: rules on original text
        annotated, rule_pairs = _apply_rules(text)
        all_pairs.extend(rule_pairs)
        if rule_pairs:
            print(f"[compress] rules  : {len(rule_pairs)} match(es)")
        # step 2: list-model on the clean (compressed) version of what rules left
        clean_after_rules = _compress(annotated)
        if clean_after_rules != _compress(text):   # rules found something
            model_input = clean_after_rules
        else:
            model_input = text
        model_annotated, model_pairs = _model_annotate_list(chat, model_input)
        model_pairs = [(p, r) for p, r in model_pairs if p not in {x[0] for x in rule_pairs}]
        all_pairs = rule_pairs + model_pairs
        # display: rules annotation on original text
        annotated, _ = _apply_rules(text)
        # compressed: sequential — rules clean first, then model strips on top
        seq = clean_after_rules
        for phrase, _ in model_pairs:
            seq = _re.sub(_re.escape(phrase), '', seq, count=1, flags=_re.IGNORECASE)
        seq = _re.sub(r'^[.,;:\-–—\s]+', '', _re.sub(r'  +', ' ', seq)).strip()
        final_compressed = seq

    if not all_pairs:
        print("[compress] no redundancies found")
        compressed = text
    else:
        print(f"[compress] found  : {len(all_pairs)} redundanc{'y' if len(all_pairs)==1 else 'ies'}")
        for phrase, reason in all_pairs:
            print(f"  '{phrase}' → {reason}")

        compressed = final_compressed if final_compressed is not None else _compress(annotated)
        ratio = round((1 - len(compressed) / max(len(text), 1)) * 100)

        if not strip_only:
            print(f"\n[compress] annotated:")
            print(f"  {annotated}")

        print(f"\n[compress] compressed (-{ratio}%):")
        print(f"  {compressed}")

    chat._last_output = compressed
