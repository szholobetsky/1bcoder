"""Obfuscate sensitive text by replacing business terms with neutral equivalents.

Uses a glossary YAML file to define term mappings, then asks the current LLM
to rewrite the text — handling plurals, case, and grammar automatically.
Safe for any text size: text is passed as a Python string, never expanded inline.

── Usage ──────────────────────────────────────────────────────────────────────

  /flow obfuscate --glossary <name>
      Obfuscate $ (last LLM output) using the named glossary.

  /flow obfuscate --var <varname> --glossary <name>
      Obfuscate a named session variable.

  /flow obfuscate --glossary <name> -> obf_text
      Capture obfuscated result into a variable.

  /flow obfuscate --glossary <name> --profile <name>
      Use a specific 1bcoder profile (model) for the LLM call.
      Recommended: a fast local 4B model (qwen3, nemotron-nano).

  /flow obfuscate --glossary <name> --force
      Skip LLM entirely — replace every occurrence by direct string substitution.
      Case-preserving: Oil→Cola, oil→cola, NewOilClass→NewColaClass.
      Catches terms inside camelCase identifiers that LLMs sometimes skip.
      Note: no plurals or grammar awareness — purely mechanical replacement.

── Glossary file ──────────────────────────────────────────────────────────────

  Location (searched in order):
    .1bcoder/glossaries/<name>.yaml     ← project-local
    ~/.1bcoder/glossaries/<name>.yaml   ← global

  Format:
    tanker:   vessel
    oil:      liquid cargo
    port:     loading terminal
    crew:     operational staff

  Create with: /flow obfuscate --glossary-new <name>  (opens editor template)

── Typical workflow ───────────────────────────────────────────────────────────

  > describe the optimisation task we need to solve -> task_text
  > /flow obfuscate --var task_text --glossary myproject -> obf_text
  [copy obf_text output → paste into ChatGPT / Gemini / Claude]
  [paste the response back as a plain message] -> cloud_answer
  > /flow deobfuscate --var cloud_answer --glossary myproject

  Or save the whole workflow:
  > /script save external_help

── Notes ──────────────────────────────────────────────────────────────────────

  - Translation runs in an isolated context — current conversation is not affected.
  - Some small models (< 3B) ignore instructions and output garbage. If that happens,
    use --profile to specify a smarter local model.
  - After obfuscation, use /flow deobfuscate to restore the cloud LLM's answer.
  - For the full guided workflow (obfuscate + instructions + remind deobfuscate),
    use /flow external_help instead.
"""
from __future__ import annotations
import re as _re
import os as _os


# ── shared helpers ─────────────────────────────────────────────────────────────

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
    # fallback: parse "key: value" lines without pyyaml
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
    """Return (host, model) for the first worker in a profile."""
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


def _obfuscate_prompt(text: str, glossary: dict[str, str]) -> str:
    pairs = "\n".join(f"  {k} → {v}" for k, v in glossary.items())
    return (
        "Your task is to rewrite the following text by replacing specific terms "
        "with neutral equivalents according to the glossary provided. "
        "Keep all technical meaning intact. Handle plurals and grammatical forms naturally. "
        "Do not add explanations or commentary. Output only the rewritten text.\n\n"
        f"Glossary:\n{pairs}\n\n"
        f"Text to rewrite:\n{text}"
    )


# ── entry point ────────────────────────────────────────────────────────────────

def run(chat, args: str):
    # ── parse args ──
    var_m      = _re.search(r'--var\s+(\w+)', args)
    glossary_m = _re.search(r'--glossary(?:-[a-z]+)?\s+(\S+)', args)
    profile_m  = _re.search(r'--profile\s+(\S+)', args)
    new_mode   = "--glossary-new" in args
    force      = "--force" in args

    # ── glossary-new: create template ──
    if new_mode and glossary_m:
        gname = glossary_m.group(1)
        gdir  = _os.path.join(".1bcoder", "glossaries")
        _os.makedirs(gdir, exist_ok=True)
        gpath = _os.path.join(gdir, f"{gname}.yaml")
        if _os.path.exists(gpath):
            print(f"[obfuscate] glossary already exists: {gpath}")
        else:
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(f"# Glossary: {gname}\n# real term: obfuscated term\n\n")
                f.write("# example:\n# tanker: vessel\n# oil: liquid cargo\n# port: loading terminal\n")
            print(f"[obfuscate] created: {gpath}  — edit it, then run /flow obfuscate --glossary {gname}")
        return

    if not glossary_m:
        print(__doc__)
        return

    gname    = glossary_m.group(1)
    glossary = _load_glossary(gname)

    if not glossary:
        gpath = _find_glossary(gname)
        if not gpath:
            print(f"[obfuscate] glossary '{gname}' not found.")
            print(f"  Create it: /flow obfuscate --glossary-new {gname}")
            print(f"  Expected:  .1bcoder/glossaries/{gname}.yaml")
        else:
            print(f"[obfuscate] glossary '{gname}' is empty: {gpath}")
        return

    # ── get text ──
    if var_m:
        text = chat._vars.get(var_m.group(1), "")
        if not text:
            print(f"[obfuscate] variable '{var_m.group(1)}' is empty or not set")
            print(f"  Use /var get to list available variables")
            return
    else:
        text = chat._last_output
        if not text:
            print("[obfuscate] nothing to obfuscate — no last output and no --var specified")
            return

    # ── force mode: direct string substitution, no LLM ──
    if force:
        print(f"[obfuscate] FORCE mode — {len(glossary)} terms, direct substitution, text: {len(text)} chars")
        chat._sep("OBFUSCATED")
        reply = _force_replace(text, glossary)
        print(reply)
        chat.last_reply   = reply
        chat._last_output = reply
        print(f"\n[obfuscate] done (force) — {len(reply)} chars")
        print( "[obfuscate] next step: copy the text above → paste into ChatGPT / Claude / Gemini")
        print( "[obfuscate] after you get the response, paste it back here and run:")
        print(f"  /flow deobfuscate --glossary {gname}")
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
            print(f"[obfuscate] using profile '{profile_m.group(1)}': {pmodel}")
        else:
            print(f"[obfuscate] profile '{profile_m.group(1)}' not found — using current model")

    # ── run LLM in isolated context ──
    prompt    = _obfuscate_prompt(text, glossary)
    temp_msgs = [
        {"role": "system", "content": "You are a precise text rewriter. Follow glossary instructions exactly."},
        {"role": "user",   "content": prompt},
    ]

    print(f"[obfuscate] glossary: {gname} ({len(glossary)} terms)  text: {len(text)} chars")
    chat._sep("OBFUSCATED")
    reply = chat._stream_chat(temp_msgs)

    if switched:
        chat._model = orig_model
        chat._host  = orig_host

    if reply:
        chat.last_reply    = reply
        chat._last_output  = reply

        print(f"\n[obfuscate] done — {len(reply)} chars")
        print( "[obfuscate] next step: copy the text above → paste into ChatGPT / Claude / Gemini")
        print( "[obfuscate] after you get the response, paste it back here and run:")
        print(f"  /flow deobfuscate --glossary {gname}")
        print( "  or for the full guided flow:  /flow external_help --glossary <name>")
