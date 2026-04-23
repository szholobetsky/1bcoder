"""Run simargl task retrieval, read top N matched files, summarize relevance. Usage: /flow simargl_files <task description> [-n N]"""
import re as _re
import os as _os


def _parse_file_paths(output: str) -> list[str]:
    """Extract file paths from simargl output lines like '0.82  src/foo/bar.py'."""
    paths = []
    for line in output.splitlines():
        line = line.strip()
        # match lines: optional score + path with extension
        m = _re.match(r"(?:[\d.]+\s+)?(\S+\.\w+)$", line)
        if m:
            paths.append(m.group(1))
    return paths


def run(chat, args: str):
    max_files = 5
    m = _re.search(r"-n\s+(\d+)", args)
    if m:
        max_files = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()
    task = args.strip()
    if not task:
        print('usage: /flow simargl_files <task description> [-n N]')
        return

    print(f"[simargl_files] querying simargl for: {task}")
    simargl_output = chat._agent_exec(f'/run simargl search --mode task --sort rank "{task}"', auto_apply=True)
    file_paths = _parse_file_paths(simargl_output)

    if not file_paths:
        print("[simargl_files] no file paths found in simargl output")
        return

    file_paths = file_paths[:max_files]
    print(f"[simargl_files] reading {len(file_paths)} file(s): {', '.join(file_paths)}")

    sections = []
    for path in file_paths:
        content = chat._agent_exec(f"/read {path}", auto_apply=True).strip()
        if content:
            sections.append(f"### {path}\n{content}")
        else:
            print(f"[simargl_files] could not read {path}")

    if not sections:
        print("[simargl_files] no file content retrieved")
        return

    combined = "\n\n".join(sections)
    prompt = (
        f"Given the following task description:\n{task}\n\n"
        f"And the following relevant source files:\n\n{combined}\n\n"
        f"Explain what changes would be needed and in which files."
    )
    temp_msgs = [{"role": "system", "content": chat._role},
                 {"role": "user",   "content": prompt}]
    chat._sep("AI")
    reply = chat._stream_chat(temp_msgs)
    if reply:
        chat.last_reply = reply
        chat._last_output = reply
        chat.messages.append({"role": "user",      "content": f"[simargl_files: {task}]"})
        chat.messages.append({"role": "assistant", "content": reply})
