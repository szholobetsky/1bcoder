"""Clarifying Q&A before implementation: LLM generates questions, user picks answers.

Usage:
  /flow interview 5 "implement Syryn BT beacon"
  /flow interview 10 "implement the system described above" --file plan.md
  /flow interview 5 "implement the system described above" --ctx 5
  /flow interview 5 "implement Syryn" --out ctx
  /flow interview 5 "REST API" --file requirements.md --out my-answers.md

Output saved to .1bcoder/interview/interview_result_N.md by default.
Use the output as task file for code generation:
  /flow deepagent_code "..." --file .1bcoder/interview/interview_result_1.md

With --out ctx: results are injected into conversation context (no file created).
  /flow interview 5 "implement the system above" --out ctx
  /flow deepagent_code "..." --ctx 2

Flags:
  --file file   inject file content as extra context for question generation
  --ctx N       inject last N conversation messages into question generation
  --out file    save to custom path (default: .1bcoder/interview/interview_result_N.md)
  --out ctx     inject results into conversation context, no file created
"""
import re as _re
import json as _json
import os as _os


def run(chat, args: str):
    args = (args or "").strip()

    # -- parse --out (override) or auto-number in .1bcoder/interview/
    m = _re.search(r'--out\s+(\S+)', args)
    if m:
        out_file = m.group(1)
        args = (args[:m.start()] + args[m.end():]).strip()
    else:
        interview_dir = _os.path.join(_os.getcwd(), ".1bcoder", "interview")
        _os.makedirs(interview_dir, exist_ok=True)
        n_out = 1
        while _os.path.exists(_os.path.join(interview_dir, f"interview_result_{n_out}.md")):
            n_out += 1
        out_file = _os.path.join(interview_dir, f"interview_result_{n_out}.md")

    # -- parse --ctx N
    ctx_n = 0
    m = _re.search(r'--ctx\s+(\d+)', args)
    if m:
        ctx_n = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    # -- parse --file
    ctx_text = ""
    m = _re.search(r'--file\s+(\S+)', args)
    if m:
        ctx_file = m.group(1)
        args = (args[:m.start()] + args[m.end():]).strip()
        try:
            with open(ctx_file, encoding="utf-8") as f:
                ctx_text = f.read()
            print(f"[interview] file loaded: {ctx_file} ({len(ctx_text)} chars)")
        except Exception as e:
            print(f"[interview] warning: could not read {ctx_file}: {e}")

    # -- parse N and task
    m = _re.match(r'(\d+)\s+"([^"]+)"', args)
    if not m:
        m = _re.match(r'(\d+)\s+(.+)', args)
    if not m:
        print('Usage: /flow interview N "task description" [--file plan.md] [--ctx N]')
        return

    n = int(m.group(1))
    task = m.group(2).strip().strip('"')

    # -- serialize conversation context
    chat_ctx = _serialize_ctx(getattr(chat, "messages", []), ctx_n)
    if chat_ctx:
        print(f"[interview] chat ctx: {ctx_n} msgs ({len(chat_ctx)} chars)")

    # -- generate questions
    ctx_section = f"\n\nAdditional context:\n{ctx_text[:3000]}" if ctx_text else ""
    if chat_ctx:
        ctx_section += f"\n\n{chat_ctx}"
    system = (
        "You are a requirements analyst. Given a task, generate clarifying questions "
        "that must be answered before implementation begins. "
        f"Generate exactly {n} questions. For each question provide 2-3 concrete answer options. "
        "Output ONLY a valid JSON array — no markdown fences, no explanation. Format:\n"
        '[{"q": "Question?", "options": ["Option A", "Option B", "Option C"]}, ...]'
    )
    user = f"Task: {task}{ctx_section}"

    print(f"[interview] generating {n} questions for: {task[:70]}...")
    raw = _generate_local(chat, system, user)
    if not raw:
        print("[interview] no response from model")
        return

    questions = _parse_questions(raw)
    if not questions:
        print("[interview] could not parse questions from model output:")
        print(raw[:800])
        return

    questions = questions[:n]
    print(f"[interview] got {len(questions)} questions\n")

    # -- interactive Q&A
    answers = []
    for i, q_data in enumerate(questions):
        q = q_data.get("q", "").strip()
        opts = q_data.get("options", [])
        if not q:
            continue

        print(f"[{i+1}/{len(questions)}] {q}")
        for j, opt in enumerate(opts, 1):
            print(f"  {j}. {opt}")
        other_idx = len(opts) + 1
        print(f"  {other_idx}. Other")

        answer = ""
        while not answer:
            raw_in = input("> ").strip()
            if not raw_in:
                continue
            if raw_in.isdigit():
                idx = int(raw_in)
                if 1 <= idx <= len(opts):
                    answer = opts[idx - 1]
                elif idx == other_idx:
                    answer = input("Your answer: ").strip()
                else:
                    print(f"  Enter 1-{other_idx}")
            else:
                answer = raw_in

        answers.append((q, answer))
        print(f"  -> {answer}\n")

    # -- build result
    lines = [f"# Interview Results\n", f"**Task**: {task}\n"]
    if ctx_text:
        lines.append(f"**File**: {ctx_file}\n")
    lines.append("")
    for i, (q, a) in enumerate(answers, 1):
        lines.append(f"## Q{i}: {q}")
        lines.append(f"**Answer**: {a}\n")

    content = "\n".join(lines)

    if out_file == "ctx":
        chat.messages.append({"role": "user",      "content": f"[interview results]\n{content}"})
        chat.messages.append({"role": "assistant",  "content": "Interview results recorded. Ready to proceed."})
        print(f"[interview] added to conversation context ({len(content)} chars)")
        print(f"[interview] next: /flow deepagent_code \"...\" --ctx 2")
    else:
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[interview] saved to {out_file}")
        print(f"[interview] next: /flow deepagent_code \"...\" --file {out_file}")


def _parse_questions(text: str):
    text = text.strip()
    # strip markdown fences
    text = _re.sub(r'^```[a-z]*\n?', '', text)
    text = _re.sub(r'\n?```\s*$', '', text)
    text = text.strip()

    # strip JS // comments (but not :// in URLs) and # comments
    text = _re.sub(r'(?<!:)//[^\n]*', '', text)
    text = _re.sub(r'(?<!")#[^\n"]*', '', text)

    # fix trailing commas before ] or }
    text = _re.sub(r',(\s*[}\]])', r'\1', text)

    # attempt 1: direct parse
    try:
        data = _json.loads(text)
        if isinstance(data, list):
            return _normalize_questions(data)
    except Exception:
        pass

    # attempt 2: recover truncated JSON (close open brackets after last })
    fixed = _try_close(text)
    if fixed:
        try:
            data = _json.loads(fixed)
            if isinstance(data, list):
                return _normalize_questions(data)
        except Exception:
            pass

    # attempt 3: regex fallback — extract q/options pairs directly
    return _regex_extract(text)


def _try_close(text: str) -> str:
    """Close truncated JSON by trimming after last } and closing open [."""
    last = text.rfind('}')
    if last == -1:
        return None
    candidate = _re.sub(r',\s*$', '', text[:last + 1].strip())
    opens = candidate.count('[') - candidate.count(']')
    if opens > 0:
        candidate += ']' * opens
    return candidate


def _normalize_questions(data: list) -> list:
    """Normalize: ensure q is string, options are strings, skip empty."""
    result = []
    for item in data:
        if not isinstance(item, dict):
            continue
        q = str(item.get('q', '')).strip()
        if not q:
            continue
        opts = [str(o).strip() for o in item.get('options', []) if o is not None]
        result.append({'q': q, 'options': opts})
    return result or None


def _regex_extract(text: str) -> list:
    """Last resort: pull q/options pairs with regex, ignoring JSON structure."""
    results = []
    q_matches = list(_re.finditer(r'"q"\s*:\s*"([^"]+)"', text))
    for i, qm in enumerate(q_matches):
        q = qm.group(1)
        start = qm.end()
        end = q_matches[i + 1].start() if i + 1 < len(q_matches) else len(text)
        block = text[start:end]
        opts = [o for o in _re.findall(r'"([^"]{5,})"', block)
                if o not in ('options', 'q')]
        results.append({'q': q, 'options': opts[:4]})
    return results if results else None


def _serialize_ctx(messages: list, n: int) -> str:
    if not messages or n == 0:
        return ""
    recent = [m for m in messages if m.get("role") in ("user", "assistant")][-n:]
    if not recent:
        return ""
    lines = ["[Conversation context]"]
    for m in recent:
        role = "User" if m["role"] == "user" else "Assistant"
        lines.append(f"{role}: {m.get('content', '')[:800]}")
    return "\n".join(lines) + "\n"


def _generate_local(chat, system_prompt: str, user_prompt: str) -> str:
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]
    result = chat._stream_chat(msgs)
    return result or ""
