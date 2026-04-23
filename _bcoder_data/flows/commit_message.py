"""Generate a git commit message from staged or unstaged changes. Usage: /flow commit_message"""


def run(chat, args: str):
    import subprocess as _sp

    def _git(cmd: list[str]) -> str:
        try:
            r = _sp.run(["git"] + cmd, capture_output=True, text=True, timeout=15)
            return r.stdout.strip()
        except Exception as e:
            return f"(git error: {e})"

    # prefer staged diff; fall back to unstaged
    diff = _git(["diff", "--staged"])
    diff_source = "staged changes"
    if not diff:
        diff = _git(["diff", "HEAD"])
        diff_source = "unstaged changes"
    if not diff:
        print("[commit_message_generator] no changes found — run 'git add' to stage files first")
        return

    print(f"[commit_message_generator] generating from {diff_source} ({len(diff)} chars)")

    prompt = (
        f"Diff ({diff_source}):\n{diff[:6000]}\n\n"
        f"Write a one-line git commit message describing what changed and why."
    )
    temp_msgs = [{"role": "system", "content": chat._role},
                 {"role": "user",   "content": prompt}]
    chat._sep("AI")
    reply = chat._stream_chat(temp_msgs)
    if reply:
        chat.last_reply = reply
        chat._last_output = reply
        chat.messages.append({"role": "user",      "content": f"[commit_message: {diff_source}]"})
        chat.messages.append({"role": "assistant", "content": reply})
