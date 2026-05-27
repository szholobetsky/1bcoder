"""Restore obfuscated text back to original business terms using a glossary.

Reverses what /flow obfuscate did — maps neutral terms back to real ones.
Use this after getting a response from a cloud LLM to restore original terminology.
Safe for any text size: text is passed as a Python string, never expanded inline.

── Usage ──────────────────────────────────────────────────────────────────────

  /flow deobfuscate --glossary <name>
      Decode $ (last output — paste the cloud LLM response as a message first).

  /flow deobfuscate --var <varname> --glossary <name>
      Decode a named session variable.

  /flow deobfuscate --glossary <name> -> clear_solution
      Capture decoded result into a variable.

  /flow deobfuscate --glossary <name> --profile <name>
      Use a specific 1bcoder profile (model) for the LLM call.
      Same model as used for obfuscation gives best results.

  /flow deobfuscate --glossary <name> --force
      Skip LLM — replace every occurrence by direct string substitution (reversed).
      Useful when the cloud LLM preserved terms exactly and no paraphrasing occurred.

── Typical workflow ───────────────────────────────────────────────────────────

  Step 1 — obfuscate and send to cloud:
    > describe the task -> task_text
    > /flow obfuscate --var task_text --glossary myproject
    [copy obfuscated text → paste in ChatGPT / Claude / Gemini → get answer]

  Step 2 — paste response and decode:
    > <paste cloud LLM response here as a plain message> -> cloud_answer
    > /flow deobfuscate --var cloud_answer --glossary myproject -> clear_solution
    > /var get clear_solution

  Shortcut (using $ = last output):
    > <paste cloud LLM response>
    > /flow deobfuscate --glossary myproject

── Notes ──────────────────────────────────────────────────────────────────────

  - Decoding runs in an isolated context — current conversation is not affected.
  - The glossary is reversed automatically (obfuscated → real).
  - If the cloud LLM slightly changed the obfuscated terms (e.g. "vessels" instead
    of "vessel"), the LLM decoder will still recover them correctly — unlike simple
    string replacement which would miss variations.
  - Use /flow obfuscate --glossary-new <name> to create a new glossary template.
  - For the full guided workflow, use /flow external_help instead.
"""
from __future__ import annotations
import re as _re
import os as _os

# ── reuse helpers from obfuscate ──────────────────────────────────────────────

def _force_replace(text: str, glossary: dict[str, str]) -> str:
    """Case-preserving direct substitution — no LLM, no context awareness."""
    for real, neutral in glossary.items():
        if not real or not neutral:
            continue
        def _make_rep(n: str):
            def _rep(m: "_re.Match") -> str:
                f = m.group(0)
                if f[0].isupper():
                    return n[0].upper() + n[1:]
                return n[0].lower() + n[1:]
            return _rep
        text = _re.sub(_re.escape(real), _make_rep(neutral), text, flags=_re.IGNORECASE)
    return text

def _find_glossary(name: str) -> str | None:
    if _os.sep in name or "/" in name:
        return name if _os.path.exists(name) else None
    candidates = [
        _os.path.join(".1bcoder", "glossaries", f"{name}.yaml"),
        _os.path.join(_os.path.expanduser("~"), ".1bcoder", "glossaries", f"{name}.yaml"),
    ]
    for p in candidates:
        if _os.path.exists(p):
            return p
    return None


def _load_glossary(name: str) -> dict[str, str]:
    path = _find_glossary(name)
    if not path:
        return {}
    try:
        import yaml as _yaml
        with open(path, encoding="utf-8") as f:
            data = _yaml.safe_load(f)
        return {str(k): str(v) for k, v in (data or {}).items()}
    except ImportError:
        pass
    result = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                k, _, v = line.partition(":")
                k, v = k.strip().strip('"\''), v.strip().strip('"\'')
                if k and v:
                    result[k] = v
    return result


def _load_profile_first(profile_name: str) -> tuple[str | None, str | None]:
    for pfile in [
        _os.path.join(".1bcoder", "profiles.txt"),
        _os.path.join(_os.path.expanduser("~"), ".1bcoder", "profiles.txt"),
    ]:
        if not _os.path.exists(pfile):
            continue
        with open(pfile, encoding="utf-8") as f:
            content = f.read()
        m = _re.search(
            rf'^{_re.escape(profile_name)}:\s*\n((?:[ \t]+\S.*\n?)+)',
            content, _re.MULTILINE
        )
        if not m:
            continue
        for line in m.group(1).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split("|")
                if len(parts) >= 2:
                    return parts[0].strip(), parts[1].strip()
    return None, None


def _deobfuscate_prompt(text: str, glossary: dict[str, str]) -> str:
    # reverse: obfuscated → real
    pairs = "\n".join(f"  {v} → {k}" for k, v in glossary.items())
    return (
        "Your task is to restore the following text by replacing neutral/obfuscated terms "
        "back to their original business terminology according to the glossary provided. "
        "Keep all technical meaning intact. Handle plurals and grammatical forms naturally. "
        "Do not add explanations or commentary. Output only the restored text.\n\n"
        f"Glossary (obfuscated → original):\n{pairs}\n\n"
        f"Text to restore:\n{text}"
    )


# ── entry point ────────────────────────────────────────────────────────────────

def run(chat, args: str):
    var_m      = _re.search(r'--var\s+(\w+)', args)
    glossary_m = _re.search(r'--glossary\s+(\S+)', args)
    profile_m  = _re.search(r'--profile\s+(\S+)', args)
    force      = "--force" in args

    if not glossary_m:
        print(__doc__)
        return

    gname    = glossary_m.group(1)
    glossary = _load_glossary(gname)

    if not glossary:
        gpath = _find_glossary(gname)
        if not gpath:
            print(f"[deobfuscate] glossary '{gname}' not found.")
            print(f"  Create it: /flow obfuscate --glossary-new {gname}")
        else:
            print(f"[deobfuscate] glossary '{gname}' is empty: {gpath}")
        return

    # ── get text ──
    if var_m:
        text = chat._vars.get(var_m.group(1), "")
        if not text:
            print(f"[deobfuscate] variable '{var_m.group(1)}' is empty or not set")
            print( "  Paste the cloud LLM response as a message, capture it:")
            print( "  > <paste response> -> cloud_answer")
            print(f"  > /flow deobfuscate --var cloud_answer --glossary {gname}")
            return
    else:
        text = chat._last_output
        if not text:
            print("[deobfuscate] nothing to decode — paste the cloud LLM response first:")
            print("  > <paste response here>")
            print(f"  > /flow deobfuscate --glossary {gname}")
            return

    # ── force mode: direct string substitution, reversed glossary ──
    if force:
        rev_glossary = {v: k for k, v in glossary.items()}
        print(f"[deobfuscate] FORCE mode — {len(rev_glossary)} terms, direct substitution, text: {len(text)} chars")
        chat._sep("DECODED")
        reply = _force_replace(text, rev_glossary)
        print(reply)
        chat.last_reply   = reply
        chat._last_output = reply
        print(f"\n[deobfuscate] done (force) — original terminology restored")
        return

    # ── switch profile if requested ──
    orig_model = getattr(chat, "_model", None)
    orig_host  = getattr(chat, "_host", None)
    switched   = False

    if profile_m:
        phost, pmodel = _load_profile_first(profile_m.group(1))
        if phost and pmodel:
            chat._host  = phost
            chat._model = pmodel
            switched    = True
            print(f"[deobfuscate] using profile '{profile_m.group(1)}': {pmodel}")
        else:
            print(f"[deobfuscate] profile '{profile_m.group(1)}' not found — using current model")

    # ── run LLM in isolated context ──
    prompt    = _deobfuscate_prompt(text, glossary)
    temp_msgs = [
        {"role": "system", "content": "You are a precise text rewriter. Follow glossary instructions exactly."},
        {"role": "user",   "content": prompt},
    ]

    print(f"[deobfuscate] glossary: {gname} ({len(glossary)} terms, reversed)  text: {len(text)} chars")
    chat._sep("DECODED")
    reply = chat._stream_chat(temp_msgs)

    if switched:
        chat._model = orig_model
        chat._host  = orig_host

    if reply:
        chat.last_reply   = reply
        chat._last_output = reply
        print(f"\n[deobfuscate] done — original terminology restored")
