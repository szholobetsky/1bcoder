"""Ask a cloud LLM for help without revealing sensitive business terms.

Guides you through the full privacy-safe workflow:
  1. Obfuscate your text (local model replaces real terms with neutral ones)
  2. You manually copy the result to any cloud LLM (ChatGPT, Claude, Gemini, etc.)
  3. You paste the cloud response back
  4. Deobfuscate the response (local model restores real terminology)

No cloud credentials required. No LiteLLM. Works with any service — even manual
web browser usage. The cloud provider never sees your real business terms.

── Usage ──────────────────────────────────────────────────────────────────────

  /flow external_help --dict <name>
      Obfuscate $ (last LLM output) and print step-by-step instructions.

  /flow external_help --var <varname> --dict <name>
      Obfuscate a named session variable.

  /flow external_help --dict <name> --profile <name>
      Use a specific local model profile for obfuscation.
      Recommended for best quality: qwen3:4b, nemotron-mini, phi4-mini.

  /flow external_help --dict <name> --cloud-profile <name>
      Full automatic pipeline: obfuscate → send to cloud LLM → deobfuscate.
      Use when you have a cloud LLM configured as a 1bcoder profile.

  /flow external_help --dict <name> --force
      Use direct string substitution for obfuscation instead of LLM.
      Guarantees every occurrence is replaced, including camelCase identifiers.
      Combine with --cloud-profile for fully automatic pipeline with force obfuscation.

  /flow external_help --dict <name> --decode --var cloud_answer
      Deobfuscate a cloud response (shortcut for /flow deobfuscate).

── Full example session (automatic — cloud profile configured) ────────────────

  > describe the linear optimisation problem -> task_text
  > /flow external_help --var task_text --dict tanker \
        --profile local_nemotron --cloud-profile claude_api

  [obfuscates locally → sends to cloud → deobfuscates locally]
  → clear solution with real terminology

── Full example session (manual — no cloud profile) ──────────────────────────

  > describe the linear optimisation problem -> task_text
  > /flow external_help --var task_text --dict tanker --profile local_nemotron

  ── OBFUSCATED ────────────────────────────────
  Optimize vessel routing for liquid cargo delivery to loading terminals...
  ── END ───────────────────────────────────────

  ══ NEXT STEPS ════════════════════════════════
  1. Copy the obfuscated text above
  2. Paste it into ChatGPT / Claude.ai / Gemini (any cloud LLM)
  3. Get the answer from the cloud LLM
  4. Come back here and paste the answer as a plain message:
       > <paste cloud response here> -> cloud_answer
  5. Then decode it:
       > /flow deobfuscate --var cloud_answer --dict tanker

── Dictionary management ───────────────────────────────────────────────────────

  Create a new dictionary template:
    /flow obfuscate --dict-new <name>

  Dictionary location:
    .1bcoder/dictionaries/<name>.yaml     ← project-local
    ~/.1bcoder/dictionaries/<name>.yaml   ← global (shared across projects)

  Dictionary format:
    tanker:    vessel
    oil:       liquid cargo
    port:      loading terminal
    pipeline:  distribution network

── Why this approach works ────────────────────────────────────────────────────

  Cloud LLMs solve the STRUCTURE of your problem, not the CONTENT.
  A linear optimisation problem about "vessels and liquid cargo" has
  the same mathematical structure as one about "tankers and oil".
  The cloud model returns a structurally correct solution — your local
  model restores the real terminology. Neither side sees the full picture.

── Notes ──────────────────────────────────────────────────────────────────────

  - Small models (< 3B) may fail to follow dictionary instructions correctly.
    Use --profile to specify a 4B+ model for better results.
  - Use /flow obfuscate and /flow deobfuscate for a more granular workflow
    where you control each step separately.
  - Save this workflow as a script: /script save external_help
"""
from __future__ import annotations
import re as _re
import os as _os


# ── helpers (same as obfuscate/deobfuscate) ────────────────────────────────────

def _find_dict(name: str) -> str | None:
    if _os.sep in name or "/" in name:
        return name if _os.path.exists(name) else None
    for p in [
        _os.path.join(".1bcoder", "dictionaries", f"{name}.yaml"),
        _os.path.join(_os.path.expanduser("~"), ".1bcoder", "dictionaries", f"{name}.yaml"),
    ]:
        if _os.path.exists(p):
            return p
    return None


def _load_dict(name: str) -> dict[str, str]:
    path = _find_dict(name)
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


def _run_llm(chat, system_prompt: str, user_prompt: str,
             profile_name: str | None) -> str | None:
    orig_model = getattr(chat, "_model", None)
    orig_host  = getattr(chat, "_host", None)
    switched   = False

    if profile_name:
        phost, pmodel = _load_profile_first(profile_name)
        if phost and pmodel:
            chat._host  = phost
            chat._model = pmodel
            switched    = True
            print(f"[external_help] profile '{profile_name}': {pmodel}")
        else:
            print(f"[external_help] profile '{profile_name}' not found — using current model")

    temp_msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]
    reply = chat._stream_chat(temp_msgs)

    if switched:
        chat._model = orig_model
        chat._host  = orig_host

    return reply


# ── entry point ────────────────────────────────────────────────────────────────

def _force_replace(text: str, term_map: dict[str, str]) -> str:
    """Case-preserving direct substitution — no LLM, no context awareness."""
    for real, neutral in term_map.items():
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


def run(chat, args: str):
    var_m     = _re.search(r'--var\s+(\w+)', args)
    dict_m    = _re.search(r'--dict\s+(\S+)', args)
    profile_m = _re.search(r'--profile\s+(\S+)', args)
    cloud_m   = _re.search(r'--cloud-profile\s+(\S+)', args)
    decode    = "--decode" in args
    force     = "--force" in args

    if not dict_m:
        print(__doc__)
        return

    dname         = dict_m.group(1)
    term_map      = _load_dict(dname)
    local_profile = profile_m.group(1) if profile_m else None
    cloud_profile = cloud_m.group(1) if cloud_m else None

    if not term_map:
        dpath = _find_dict(dname)
        if not dpath:
            print(f"[external_help] dictionary '{dname}' not found.")
            print(f"  Create it: /flow obfuscate --dict-new {dname}")
        else:
            print(f"[external_help] dictionary '{dname}' is empty: {dpath}")
        return

    # ── get text ──
    if var_m:
        text = chat._vars.get(var_m.group(1), "")
        if not text:
            print(f"[external_help] variable '{var_m.group(1)}' is not set")
            print("  Use /var get to list available variables")
            return
    else:
        text = chat._last_output
        if not text:
            print("[external_help] no text to process — use --var <name> or run after an LLM response")
            return

    # ── decode mode (shortcut for deobfuscate) ──
    profile = local_profile  # used in decode/obfuscate single-step calls
    if decode:
        pairs = "\n".join(f"  {v} → {k}" for k, v in term_map.items())
        prompt = (
            "Restore the following text by replacing neutral terms back to their "
            "original business terminology according to the dictionary. "
            "Handle plurals and grammar naturally. Output only the restored text.\n\n"
            f"Dictionary (obfuscated → original):\n{pairs}\n\nText:\n{text}"
        )
        print(f"[external_help] decoding with dictionary '{dname}' ({len(term_map)} terms)")
        chat._sep("DECODED")
        reply = _run_llm(
            chat,
            "You are a precise text rewriter. Follow dictionary instructions exactly.",
            prompt, profile
        )
        if reply:
            chat.last_reply   = reply
            chat._last_output = reply
            print(f"\n[external_help] done — terminology restored")
        return

    # ── step 1: obfuscate (force = direct replacement, otherwise local LLM) ──
    if force:
        print(f"[external_help] step 1/3 — obfuscating ({len(term_map)} terms, FORCE/direct)")
        chat._sep("OBFUSCATED")
        obf_text = _force_replace(text, term_map)
        print(obf_text)
    else:
        pairs = "\n".join(f"  {k} → {v}" for k, v in term_map.items())
        obf_prompt = (
            "Rewrite the following text by replacing specific terms with neutral equivalents "
            "according to the dictionary. Keep all technical meaning intact. Handle plurals and "
            "grammar naturally. Do not add explanations. Output only the rewritten text.\n\n"
            f"Dictionary:\n{pairs}\n\nText:\n{text}"
        )
        print(f"[external_help] step 1/3 — obfuscating ({len(term_map)} terms, local model)")
        chat._sep("OBFUSCATED")
        obf_text = _run_llm(
            chat,
            "You are a precise text rewriter. Follow dictionary instructions exactly.",
            obf_prompt, local_profile
        )

    if not obf_text:
        return

    # ── step 2: cloud LLM ──
    if cloud_profile:
        # automatic: send to cloud profile
        phost, pmodel = _load_profile_first(cloud_profile)
        if not phost:
            print(f"[external_help] cloud profile '{cloud_profile}' not found in profiles.txt")
            print(f"  Available profiles: /parallel profile list")
            print(f"  Falling back to manual workflow ↓")
            cloud_profile = None  # fall through to manual
        else:
            print(f"\n[external_help] step 2/3 — asking cloud LLM ({pmodel})")
            chat._sep("CLOUD RESPONSE")
            cloud_reply = _run_llm(chat, "", obf_text, cloud_profile)

            if not cloud_reply:
                print("[external_help] cloud LLM returned no response")
                return

            # ── step 3: deobfuscate (local) ──
            rev_pairs = "\n".join(f"  {v} → {k}" for k, v in term_map.items())
            deobf_prompt = (
                "Restore the following text by replacing neutral terms back to their "
                "original business terminology according to the dictionary. "
                "Handle plurals and grammar naturally. Output only the restored text.\n\n"
                f"Dictionary (obfuscated → original):\n{rev_pairs}\n\nText:\n{cloud_reply}"
            )

            print(f"\n[external_help] step 3/3 — deobfuscating (local model)")
            chat._sep("DECODED")
            final = _run_llm(
                chat,
                "You are a precise text rewriter. Follow dictionary instructions exactly.",
                deobf_prompt, local_profile
            )

            if final:
                chat.last_reply   = final
                chat._last_output = final
                print(f"\n[external_help] done — full pipeline complete")
            return

    # ── manual workflow (no cloud profile) ──
    chat.last_reply   = obf_text
    chat._last_output = obf_text

    sep = "═" * 54
    print(f"\n{sep}")
    print("  NEXT STEPS  (manual cloud step)")
    print(sep)
    print("  1. Copy the obfuscated text above")
    print("  2. Paste it into any cloud LLM:")
    print("       ChatGPT  →  chat.openai.com")
    print("       Claude   →  claude.ai")
    print("       Gemini   →  gemini.google.com")
    print("  3. Get the answer from the cloud LLM")
    print("  4. Come back here — paste the answer as a plain message:")
    print("       > <paste cloud response here> -> cloud_answer")
    print("  5. Decode the response:")
    print(f"       > /flow deobfuscate --var cloud_answer --dict {dname}")
    print(sep)
    print(f"\n  Tip: if you have a cloud profile, use --cloud-profile <name> next time")
    print(f"       for a fully automatic pipeline without manual copy-paste")
