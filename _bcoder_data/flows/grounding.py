"""Symbol grounding: extract codebase identifiers from task text, locate each progressively, summarize. Usage: /flow grounding <text>"""
import re as _re


def _parse_keywords(output: str) -> list[str]:
    """Extract keyword list from /map keyword extract -c output (CSV on one line)."""
    keywords = []
    for token in _re.split(r"[,\n]+", output):
        token = token.strip()
        token = _re.sub(r"\(\d+\)$", "", token).strip()  # strip count suffix like "Name(12)"
        if token and len(token) < 60 and " " not in token and not token.startswith("("):
            keywords.append(token)
    return keywords[:15]


def _has_result(output: str) -> bool:
    """Return True if the output contains a real match (not just a 'no matches' message)."""
    if not output or output == "(no output)":
        return False
    no_match_patterns = ["no matches", "no match", "not found", "nothing found"]
    low = output.lower()
    return not any(p in low for p in no_match_patterns)


def _search_keyword(chat, kw: str) -> str:
    """
    Progressive search for one keyword — stop as soon as we get a result:
      1. /map find {kw} -d 2        — kw in filename, definitions only (no links)
      2. /map find \\{kw} -d 2      — kw as identifier inside any block, definitions only
      3. /find {kw} -c              — grep fallback (filenames + line counts)
    """
    # step 1: keyword in filename
    out = chat._agent_exec(f"/map find {kw} -d 2", auto_apply=True).strip()
    if _has_result(out):
        return f"[filename match]\n{out}"

    # step 2: keyword as identifier inside blocks
    out = chat._agent_exec(f"/map find \\{kw} -d 2", auto_apply=True).strip()
    if _has_result(out):
        return f"[identifier match]\n{out}"

    # step 3: grep by filename
    out = chat._agent_exec(f"/find {kw} -f", auto_apply=True).strip()
    if _has_result(out):
        return f"[filename grep]\n{out}"

    # step 4: grep by content — last resort, may be large
    out = chat._agent_exec(f"/find {kw} -c", auto_apply=True).strip()
    if _has_result(out):
        return f"[content grep]\n{out}"

    return ""


def run(chat, args: str):
    if not args.strip():
        print("usage: /flow grounding <text or phrase>")
        return

    print(f"[grounding] extracting keywords from: {args}")
    kw_output = chat._agent_exec(f"/map keyword extract {args} -c", auto_apply=True)
    keywords = _parse_keywords(kw_output)
    match_mode = "exact"

    if not keywords:
        print(f"[grounding] no exact keywords — trying fuzzy match...")
        kw_output = chat._agent_exec(f"/map keyword extract {args} -f -c", auto_apply=True)
        keywords = _parse_keywords(kw_output)
        match_mode = "fuzzy"

    if not keywords:
        print(f"[grounding] no codebase keywords found — try /map keyword index first")
        print(f"[grounding] raw: {repr(kw_output[:200])}")
        return

    print(f"[grounding] found {len(keywords)} keyword(s) [{match_mode}]: {', '.join(keywords)}")

    sections = []
    for kw in keywords:
        result = _search_keyword(chat, kw)
        if result:
            sections.append(f"### {kw}\n{result}")
        else:
            print(f"[grounding] no hits for: {kw}")

    if not sections:
        print("[grounding] no hits found for any keyword")
        return

    combined = "\n\n".join(sections)
    prompt = (
        f"Task description: {args}\n\n"
        f"Below are keyword search results from the codebase.\n"
        f"Based on these results, list the specific files where the implementation "
        f"of this task most likely resides. For each file explain in one sentence why.\n\n"
        f"{combined}"
    )
    temp_msgs = [{"role": "system", "content": chat._role},
                 {"role": "user",   "content": prompt}]
    chat._sep("AI")
    reply = chat._stream_chat(temp_msgs)
    if reply:
        chat.last_reply = reply
        chat._last_output = reply
        chat.messages.append({"role": "user",      "content": f"[grounding: {args}]"})
        chat.messages.append({"role": "assistant", "content": reply})
