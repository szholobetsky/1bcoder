"""deepagent_spec — transform an existing deepagent_md tree into atomic specs.

`deepagent_md` produces deepened prose; it never produces something a person
or a small local model can execute checkbox by checkbox (see
concepts/DEEPAGENT_SPEC.md in simrgl). This flow is the second, separate pass
that closes that gap: it never generates a new tree of its own — it walks an
*existing* deepagent_md plan_dir, finds the tree's real leaves (nodes with no
children), splits each leaf's own `## N.` sections, and writes one atomic
WHO/WHAT/WHEN + SMART + INVEST + acceptance-criteria + boundaries spec per
section. Depth/decomposition is entirely deepagent_md's concern (§2.3 of the
concept doc — depth is external, never a generation-time decision here
either); this flow only ever looks at whatever leaves already exist.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  /flow deepagent_spec <plan_name> [--focus "<priority note>"] [--profile name]
                        [--ctx N] [--output <path>] [--predictable]

  <plan_name>      a deepagent_md plan_dir — absolute path, or a name under
                    .1bcoder/planMD/ (e.g. "plan5")
  --focus "<text>" your own priority/emphasis note, e.g. "prioritize data
                    integrity over UI polish" — appended to every per-section
                    prompt, NOT the system prompt (keeps the system prompt
                    fixed-size regardless of note length)
  --profile name    /parallel profile — distributes the flat list of
                    (leaf, section) units round-robin across workers, same
                    convention as deepagent_md's own --profile
  --ctx N           recent conversation context (K messages) appended
                    alongside --focus (default: 0, off)
  --output <path>   write spec_*.md directly into this directory instead of
                    the default location (created if missing); use for e.g.
                    "--output C:\\myfolder" or "--output /home/me/specs"
  --predictable     fill temperature=0.3/num_predict=2000 if not already set
                    on chat.params. OFF by default — the right values are a
                    separate research question that depends on whichever
                    model you're actually running; this flow never guesses
                    at them unless you explicitly ask it to. Either way,
                    whatever chat.params held before this run is restored
                    exactly once generation finishes (or is interrupted).

Root task resolution: prefers _deepagent_meta.yaml (persisted by deepagent_md
runs that postdate the `continue` subcommand). Falls back to index.md's own
title line (every deepagent_md tree writes "# {task}" as line 1 of index.md,
regardless of whether meta persistence existed at generation time) — so
older plan_dirs work too. Used ONLY to sanity-check that plan_dir is really
a deepagent_md tree; NOT injected into generation prompts (see _build_prompt's
own comment — it was written for `continue`'s different consistency need,
and confirmed for real to leak instructions from an unrelated generation
step into spec output when reused here).

Output: spec_<leaf_id>.<section_index>.md. Default location mirrors plan_dir
under .1bcoder/spec/<plan_name>/ (sibling to .1bcoder/planMD/<plan_name>/,
NOT mixed in alongside item_<id>.md — same top-level-artifact-folder
convention as .1bcoder/glossary/<project>/). Override with --output for an
arbitrary path. Title and SOURCE: are appended deterministically by this
flow, not asked of the model — only the WHO/WHAT/DEPENDS ON/DESCRIPTION/
ACCEPTANCE CRITERIA/BOUNDARIES body is generated (see _SPEC_SYSTEM's own
comment for the field-by-field history, why this isn't literally
"SMART"/"INVEST" fields, and why DESCRIPTION is deliberately unrestricted
in format — markdown, tables, code, whatever the model finds natural —
instead of forcing everything into single-line fields).

Example:
  /flow deepagent_spec plan5 --focus "backend correctness over UI polish"
  /flow deepagent_spec plan5 --profile phones
  /flow deepagent_spec plan5 --output C:\\myfolder\\AnimalAlert-specs
"""
import os as _os
import re as _re


# ── reuse deepagent_md's tree-reading machinery ─────────────────────────────

def _load_deepagent_md():
    import importlib.util as _iu
    here = _os.path.dirname(_os.path.abspath(__file__))
    path = _os.path.join(here, "deepagent_md.py")
    spec = _iu.spec_from_file_location("_dmd", path)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_dmd = _load_deepagent_md()


# ── system prompt (kept <=700 chars) ────────────────────────────────────────
# v1 (475 chars) hinted every SMART sub-item but only 2 of 6 INVEST ones —
# INVEST came back as bare echoed label words, "Testable" dropped entirely.
# v2 (655 chars) hinted every SMART+INVEST sub-item individually — real
# content appeared for both acronyms, but the model started echoing the
# HINT WORDING itself back verbatim ("WHO: role of Alert Scoring Mechanism",
# "Testable: Link to criteria below") instead of writing new content, and
# "Testable" was still dropped in one of two real outputs.
#
# v3: SMART/INVEST were never fields to ask the model to fill in — they're
# principles a *human* judges a finished spec against, not content
# categories. Asking a small model to write a section literally titled
# "SMART:" makes it either parrot the acronym words or the hint text, both
# observed for real. Each principle is satisfied structurally instead,
# never named in the output template (Specific -> the template itself;
# Measurable/Testable -> ACCEPTANCE CRITERIA; Achievable/Independent ->
# DEPENDS ON; Time-boxed/Estimable/Small -> num_predict, not a text field;
# Negotiable -> dropped, meaningful at story/epic level, not an atomic leaf).
#
# v4: v3 fixed the acronym-parroting problem but created a new one —
# WHO/WHAT/WHY are abstract summary fields, and NOTHING in the template
# asked the model to carry over concrete technical facts from the source
# section. Confirmed directly against real output (plan5, spec_1.1.1.1.md):
# the source section explicitly named `POST /api/alerts/submit` and seven
# exact DB fields — the generated spec said only "WHAT: submit an alert for
# a danger animal", every concrete detail summarized away. Not a
# small-model failure — a template design gap: a spec with no field for
# "the actual technical contract" is useless to whoever implements it
# regardless of format compliance. Added DETAILS:, replacing WHY (the
# least implementation-critical field, mapped from SMART's
# Relevant/INVEST's Valuable) — instruction made explicit: copy facts,
# don't paraphrase.
#
# v5 (this one): v4's DETAILS made facts survive, but ACCEPTANCE CRITERIA's
# two identical "concrete check" hints biased toward happy-path only —
# confirmed against real output (plan5, spec_1.1.1.2.md): 2 checks, both
# happy path, no error/invalid-input/edge case coverage at all. Split the
# hint into three distinct kinds explicitly. Also widened DETAILS to name
# request AND response fields separately (was just "fields", ambiguous
# about which side). Deliberately NOT adding a dedicated
# performance/non-functional field — at atomic-leaf granularity most units
# genuinely have none, and forcing the field would produce empty
# boilerplate ("Performance: N/A") on most specs; a real perf constraint
# in the source section should already survive via DETAILS's "copy exact
# values" instruction instead.

# v6: v5's plain-English hints ("happy path check", "error or invalid-input
# check", "edge case check") were confirmed FOR REAL to be copied verbatim
# as the actual answer, in BOTH real files that existed (plan5,
# spec_1.1.1.1.md and spec_1.1.1.2.md — 2/2, not occasional). They read as
# plausible, well-formed content on their own, so a weak model just echoes
# them instead of substituting real content. Same root cause as v1/v2's
# SMART/INVEST hint-echoing, recurring in a new field. Switched every hint
# to <angle-bracket> placeholders and added a code-level backstop
# (_looks_like_template_echo, below) that retries once on literal survival.
#
# v7 (this one): v6 fixed the echo problem but the user pointed out the
# deeper mistake underneath all of v1-v6 — "no markdown, no code fences,
# follow this exact one-line-per-field template" is a self-inflicted
# constraint. Small models write BETTER with room to think, not less; the
# rigid format was fighting the model instead of using it, and was tighter
# than the deepagent_md SOURCE content itself (which freely uses headers,
# tables, code-style backticks). Split into two layers: a still-compact
# top (WHO/WHAT/DEPENDS ON — quick-scan only) plus a genuinely free-form
# DESCRIPTION field with NO format restriction — markdown, tables, code,
# whatever the model finds natural — where the real technical depth
# belongs. DETAILS is gone; DESCRIPTION replaces and expands it.
_SPEC_SYSTEM = (
    "Write ONE atomic implementation spec for the given section, using "
    "facts from the source text - never invent unstated details. Output "
    "EXACTLY these fields, in this order:\n\n"
    "WHO: <role>\n"
    "WHAT: <one action, one result>\n"
    'DEPENDS ON: <other specs needed first, or "none">\n\n'
    "DESCRIPTION:\n"
    "<Free-form technical write-up, your own words and format - markdown, "
    "tables, code, whatever helps. Cover: implementation approach, key "
    "details from the source (endpoints, fields, values, codes), "
    "integration with the rest of the system, edge cases, error "
    "handling.>\n\n"
    "ACCEPTANCE CRITERIA:\n"
    "- <exact success condition and response>\n"
    "- <exact invalid-input condition and response>\n\n"
    "BOUNDARIES:\n"
    "- <scope excluded>"
)

# Literal survival of any of these (minus their brackets) means the model
# echoed the hint instead of replacing it — checked verbatim against the
# exact wording above, so keep these two in sync if _SPEC_SYSTEM changes.
# Deliberately excludes DESCRIPTION's own hint sentence: it's long,
# instruction-shaped prose, not a short plausible-looking phrase, so it
# was never the failure mode this detector targets — see v6/v7 history.
_HINT_ECHO_PHRASES = (
    "one action, one result",
    "other specs needed first",
    "exact success condition and response",
    "exact invalid-input condition and response",
    "scope excluded",
    "<role>",
)


def _looks_like_template_echo(text: str) -> bool:
    """True if generated text contains our own system-prompt hint wording
    verbatim — confirmed for real (plan5, spec_1.1.1.1.md/spec_1.1.1.2.md,
    2/2 files): a small model can copy a hint phrase as if it were the
    answer instead of writing real content from it. Cheap, high-confidence
    detector for exactly that failure — used to trigger one automatic
    retry rather than silently accepting broken output."""
    return any(p in text for p in _HINT_ECHO_PHRASES)


# ── helpers ──────────────────────────────────────────────────────────────────

def _project_name(plan_dir: str) -> str:
    """Derive a project slug from the repo folder three levels up from
    plan_dir (.../<Project>/.1bcoder/planMD/planN), matching the
    ANIMALALERT-style prefix used throughout DEEPAGENT_SPEC.md."""
    up3 = _os.path.dirname(_os.path.dirname(_os.path.dirname(plan_dir)))
    name = _os.path.basename(up3) or "PROJECT"
    return _re.sub(r'[^A-Za-z0-9]+', '', name).upper() or "PROJECT"


def _clean_heading(heading_line: str) -> str:
    """'## 2. Alert Confirmation and Validation\\n' -> 'Alert Confirmation and Validation'"""
    m = _re.match(r'^#{1,4}\s+(?:\d+[.)]\s+)?(.+)', heading_line.strip())
    return m.group(1).strip() if m else heading_line.strip()


def _leaf_title(plan_dir: str, leaf_id: str) -> str:
    return _dmd._read_file_title(plan_dir, leaf_id)


def _root_task(plan_dir: str, meta) -> str:
    """meta's task if present; otherwise index.md's own title line — every
    deepagent_md tree writes "# {task}" as its first line regardless of
    whether meta persistence existed when it was generated."""
    if meta and meta.get("task"):
        return meta["task"]
    index_path = _os.path.join(plan_dir, "index.md")
    if _os.path.isfile(index_path):
        first_line = open(index_path, encoding="utf-8").readline()
        return first_line.lstrip("#").strip()
    return ""


# _split_sections only splits at "## N." — real deepagent_md leaves often
# have a further "###" level *inside* one "## N." section (confirmed
# directly, two different leaves, two different heading styles:
# item_1.1.1.2.md's section 2 uses NUMBERED sub-headings — "### 2.1 New
# Alert Creation" / "### 2.2 Score Management Logic"; item_1.1.1.1.md's
# sections use UNNUMBERED ones — "### Request Body Specification" /
# "### Response Handling and Immediate Feedback" — same composite-content
# problem either way, but a numbered-only regex silently misses the second
# style entirely, which is exactly what happened: item_1.1.1.1's specs were
# never actually re-split, still one blob per "## N." section, still
# dropping ACCEPTANCE CRITERIA/BOUNDARIES for the same reason as before this
# fix existed. Match ANY level-3-or-deeper heading, numbered or not — the
# number, if present, is just discarded from the captured title.
#
# Handing a still-composite block to the spec writer as "one atomic unit"
# is a category error the same way an un-split "## N." leaf was — asked for
# ONE spec, the model reacted inconsistently: sometimes truncating early
# (unsure how to compress two things into one), sometimes breaking the
# "one" instruction and writing multiple WHO/WHAT blocks. Not a num_predict
# issue — confirmed empirically, both failure modes were far under the
# token cap either way.
_SUBHEAD_RE = _re.compile(r'^#{3,6}\s+(?:\d+(?:\.\d+)+\.?\s+)?(.+)', _re.MULTILINE)


def _split_subsections(text: str):
    """Split a section's body by ### (or deeper) sub-headings, numbered or
    not, if any. Returns a list of (clean_title, body_text) — one per
    sub-heading, with any preamble before the first sub-heading folded into
    its body so context isn't dropped. Returns None if the section has no
    sub-headings at all (caller keeps it as a single unit, unchanged from
    before)."""
    matches = list(_SUBHEAD_RE.finditer(text))
    if not matches:
        return None
    parts = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = m.group(1).strip()
        body = text[m.end():end].strip()
        if i == 0:
            preamble = text[:m.start()].strip()
            if preamble:
                body = preamble + "\n\n" + body
        parts.append((title, body))
    return parts


def _leaf_sections(plan_dir: str, leaf_id: str) -> list:
    """Read item_<leaf_id>.md, strip its own title line, split into
    (unit_index, clean_title, body_text) — unit_index is positional
    (1, 2, 3...) by order encountered across the WHOLE leaf, not the
    model's own (possibly inconsistent) numbers, to avoid filename
    collisions/gaps. A "## N." section with its own "### N.N" sub-headings
    expands into multiple units here; one without stays a single unit —
    same flat-positional-ID principle applied one level deeper."""
    fpath = _dmd._item_path(plan_dir, leaf_id)
    if not _os.path.isfile(fpath):
        return []
    lines = open(fpath, encoding="utf-8").read().splitlines(keepends=True)
    body = "".join(lines[1:])   # drop "# {title}" line
    _preamble, sections = _dmd._split_sections(body)

    result = []
    idx = 1
    for _num, head, text in sections:
        text = text.strip()
        sub = _split_subsections(text)
        if sub is None:
            result.append((idx, _clean_heading(head), text))
            idx += 1
        else:
            for sub_title, sub_body in sub:
                result.append((idx, sub_title, sub_body))
                idx += 1
    return result


def _default_spec_dir(plan_dir: str) -> str:
    """Mirror plan_dir's location under .1bcoder/spec/<plan_name>/ instead of
    .1bcoder/planMD/<plan_name>/ — sibling to planMD, same convention as
    .1bcoder/glossary/<project>/. plan_dir's own basename is the plan name
    regardless of how deep .1bcoder/planMD/ actually is."""
    dot1bcoder = _os.path.dirname(_os.path.dirname(plan_dir))   # .../.1bcoder
    plan_name = _os.path.basename(plan_dir)
    return _os.path.join(dot1bcoder, "spec", plan_name)


def _spec_path(spec_dir: str, leaf_id: str, sec_index: int) -> str:
    return _os.path.join(spec_dir, f"spec_{leaf_id}.{sec_index}.md")


def _build_prompt(section_title: str, section_body: str, leaf_title: str,
                  focus: str, chat_ctx: str) -> str:
    # root_task deliberately does NOT appear here. It exists for
    # deepagent_md's `continue` subcommand, where re-seeding a growing tree
    # genuinely needs the exact original task for consistency. deepagent_spec
    # never extends the tree — it only transforms sections that already
    # exist — so that reason doesn't apply. Worse, root_task was written to
    # frame a DIFFERENT generation step and can carry instructions that
    # actively fight this one: confirmed for real, plan5's root_task said
    # "never mentioning backend, database, or implementation details" (to
    # keep deepagent_md's epic/story pass user-facing), and even after
    # explicitly relabeling it "background only, not an instruction" the
    # model still isn't guaranteed to respect that distinction reliably —
    # only verified once, not a structural guarantee. Safer to remove the
    # risk than keep patching around it. leaf_title + section_title already
    # give local topical context; section_body (via DETAILS) carries the
    # real technical facts — nothing here needed whole-product framing.
    parts = [
        f"Leaf topic: {leaf_title}",
        f"Section: {section_title}",
        f"\nSection text:\n{section_body}",
    ]
    if focus:
        parts.append(f"\nPriority note from the requester: {focus}")
    if chat_ctx:
        parts.append(f"\n{chat_ctx}")
    return "\n".join(parts)


# ── local (sequential) generation with Y/n/q retry ──────────────────────────

_ECHO_RETRY_NOTE = (
    "\n\nYour previous answer copied the template's own placeholder "
    "wording instead of real content. Replace every field with actual, "
    "specific content this time."
)


def _generate_local(chat, prompt: str, label: str) -> str:
    msgs = [
        {"role": "system", "content": _SPEC_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    echo_retried = False
    while True:
        result = chat._stream_chat(msgs) or ""
        if result:
            if _looks_like_template_echo(result) and not echo_retried:
                echo_retried = True
                msgs[-1] = {"role": "user", "content": prompt + _ECHO_RETRY_NOTE}
                continue
            if _looks_like_template_echo(result):
                print(f"  [{label}] WARNING: still echoes template wording after retry — kept anyway, needs manual review")
            return result
        action = _dmd._on_interrupt(label)
        if action == "quit":
            raise _dmd._StopGeneration()
        if action == "skip":
            return ""
        hint = action.split(":", 1)[1] if ":" in action else ""
        if hint:
            msgs[-1] = {"role": "user", "content": prompt + f"\n\nAdditional instruction: {hint}"}


def _write_spec(spec_dir: str, project: str, leaf_id: str, sec_index: int,
                title: str, body: str, source_ref: str) -> None:
    spec_id = f"{leaf_id}.{sec_index}"
    content = (
        f"# {project}-{spec_id}: {title}\n\n"
        f"{body.strip()}\n\n"
        f"SOURCE: [src: {source_ref}]\n"
    )
    with open(_spec_path(spec_dir, leaf_id, sec_index), "w", encoding="utf-8") as f:
        f.write(content)


# ── entry point ──────────────────────────────────────────────────────────────

def run(chat, args: str):
    """Transform an existing deepagent_md tree's leaves into atomic specs."""
    args = args.strip()

    fm = _re.search(r'--focus\s+"([^"]*)"', args)
    focus = ""
    if fm:
        focus = fm.group(1)
        args = (args[:fm.start()] + args[fm.end():]).strip()

    pm = _re.search(r'--profile\s+(\S+)', args)
    profile_name = None
    if pm:
        profile_name = pm.group(1)
        args = (args[:pm.start()] + args[pm.end():]).strip()

    cm = _re.search(r'--ctx\s+(\d+)', args)
    ctx_n = int(cm.group(1)) if cm else 0
    if cm:
        args = (args[:cm.start()] + args[cm.end():]).strip()

    om = _re.search(r'--output\s+(?:"([^"]+)"|(\S+))', args)
    output_override = None
    if om:
        output_override = om.group(1) or om.group(2)
        args = (args[:om.start()] + args[om.end():]).strip()

    predictable = "--predictable" in args
    args = args.replace("--predictable", "").strip()

    plan_name = args.strip().strip('"\'')
    if not plan_name:
        print('usage: /flow deepagent_spec <plan_name> [--focus "<note>"] [--profile name] [--ctx N]')
        return

    base = _os.path.join(_os.getcwd(), ".1bcoder", "planMD")
    plan_dir = plan_name if _os.path.isabs(plan_name) else _os.path.join(base, plan_name)
    if not _os.path.isdir(plan_dir):
        print(f"[deepagent_spec] not found: {plan_dir}")
        return

    spec_dir = output_override if output_override else _default_spec_dir(plan_dir)
    _os.makedirs(spec_dir, exist_ok=True)

    meta = _dmd._load_meta(plan_dir)
    root_task = _root_task(plan_dir, meta)
    if not root_task:
        print(f"[deepagent_spec] no task found in {plan_dir} — no {_dmd._META_FILENAME} "
              f"and no readable index.md title. This doesn't look like a deepagent_md "
              f"plan_dir at all.")
        return
    if not (meta and meta.get("task")):
        print(f"[deepagent_spec] no {_dmd._META_FILENAME} — using index.md's title as root task")
    project = _project_name(plan_dir)

    all_ids = _dmd._collect_node_ids(plan_dir)
    if not all_ids:
        print(f"[deepagent_spec] no nodes found in {plan_dir}")
        return
    leaves = sorted(
        [nid for nid in all_ids if not _dmd._child_ids(nid, all_ids)],
        key=lambda x: tuple(int(p) for p in x.split("."))
    )
    if not leaves:
        print(f"[deepagent_spec] no leaves found in {plan_dir}")
        return

    # ── build the flat work list up front (leaf x section), skip existing ──
    work = []
    for leaf_id in leaves:
        title = _leaf_title(plan_dir, leaf_id)
        for sec_index, sec_title, sec_body in _leaf_sections(plan_dir, leaf_id):
            if _os.path.isfile(_spec_path(spec_dir, leaf_id, sec_index)):
                continue
            source_ref = f"item_{leaf_id}.md#{sec_index}"
            prompt = _build_prompt(sec_title, sec_body, title, focus,
                                   _dmd._serialize_ctx(getattr(chat, "messages", []), ctx_n))
            work.append((leaf_id, sec_index, sec_title, prompt, source_ref))

    if not work:
        print(f"[deepagent_spec] nothing to do — every leaf section in {plan_dir} already has a spec")
        return

    print(f"[deepagent_spec] plan     : {plan_dir}")
    print(f"[deepagent_spec] output   : {spec_dir}")
    print(f"[deepagent_spec] leaves   : {len(leaves)}")
    print(f"[deepagent_spec] units    : {len(work)} section(s) to spec")
    if focus:
        print(f"[deepagent_spec] focus    : {focus}")

    workers = None
    if profile_name:
        workers = _dmd._load_profile(profile_name)
        if workers:
            print(f"[deepagent_spec] profile  : {profile_name} ({len(workers)} workers)")
        else:
            print(f"[deepagent_spec] profile '{profile_name}' not found — running local")

    saved = dict(chat.params)
    if predictable:
        # temperature/num_predict values are our own guess, tuned against
        # one model (llama3.2:3b) during this tool's development — the
        # right values are a separate research question and depend heavily
        # on whichever model the user actually picks. Never applied by
        # default; only under explicit --predictable, and only filling gaps
        # the user hasn't already set themselves. Restored below regardless
        # of whether this branch ran.
        if "temperature" not in chat.params:
            chat.params["temperature"] = 0.3   # structured template, not open-ended prose
        if "num_predict" not in chat.params:
            # 400 (this tool's original default) was set before DESCRIPTION
            # existed, back when the whole spec was a handful of one-line
            # fields. Confirmed for real it now truncates DESCRIPTION
            # mid-sentence and starves ACCEPTANCE CRITERIA/BOUNDARIES
            # entirely — a free-form technical write-up genuinely needs
            # room, and there's no reason to ration it: this is still just
            # a ceiling the engine stops at if reached, not a target it
            # pads out to, so a generous cap costs nothing on a spec that
            # finishes earlier. Still a token cap the engine actually
            # honors, unlike a character-count instruction a small model
            # would just fail to follow accurately.
            chat.params["num_predict"] = 2000
        print(f"[deepagent_spec] predictable: temperature={chat.params['temperature']} "
              f"num_predict={chat.params['num_predict']}")
    done = 0

    try:
        if workers:
            from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

            def _one(indexed_item):
                idx, (leaf_id, sec_index, sec_title, prompt, source_ref) = indexed_item
                host, model, _f = workers[idx % len(workers)]
                content = _dmd._generate_worker(host, model, prompt, chat.num_ctx,
                                                chat.params, system=_SPEC_SYSTEM)
                if content and _looks_like_template_echo(content):
                    content = _dmd._generate_worker(host, model, prompt + _ECHO_RETRY_NOTE,
                                                    chat.num_ctx, chat.params, system=_SPEC_SYSTEM)
                    if content and _looks_like_template_echo(content):
                        print(f"  [{leaf_id}.{sec_index}] WARNING: still echoes template wording after retry — kept anyway, needs manual review")
                return leaf_id, sec_index, sec_title, content, source_ref

            with ThreadPoolExecutor(max_workers=len(workers)) as pool:
                futs = {pool.submit(_one, iw): iw for iw in enumerate(work)}
                for fut in _as_completed(futs):
                    leaf_id, sec_index, sec_title, content, source_ref = fut.result()
                    done += 1
                    if content:
                        _write_spec(spec_dir, project, leaf_id, sec_index, sec_title,
                                   content, source_ref)
                        print(f"  [{done}/{len(work)}] spec_{leaf_id}.{sec_index}.md")
                    else:
                        print(f"  [{done}/{len(work)}] spec_{leaf_id}.{sec_index} — empty, skipped")
        else:
            for leaf_id, sec_index, sec_title, prompt, source_ref in work:
                label = f"{leaf_id}.{sec_index}"
                content = _generate_local(chat, prompt, label)
                done += 1
                if content:
                    _write_spec(spec_dir, project, leaf_id, sec_index, sec_title,
                               content, source_ref)
                    print(f"  [{done}/{len(work)}] spec_{leaf_id}.{sec_index}.md")
                else:
                    print(f"  [{done}/{len(work)}] spec_{leaf_id}.{sec_index} — empty, skipped")
    except _dmd._StopGeneration:
        print("\n[deepagent_spec] stopped by user — partial output saved")

    chat.params = saved
    print(f"\n[deepagent_spec] done: {done}/{len(work)} unit(s) processed in {spec_dir}")
