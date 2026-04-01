"""assist — before-proc that reads the last agent reply and asks another LLM
for a one-sentence hint on where to go next.

Outputs ACTION: /parallel $ profile: <name> ctx: last
$ is set to the hint prompt before the action is dispatched by the before-proc handler.

Usage in agent file:
    before =
        assist
        assist profile: ten_experts

Usage from session:
    /proc before on assist
    /proc before on assist profile: ten_experts
"""
import sys, os

profile = "short"
for arg in sys.argv[1:]:
    if arg.startswith("profile:"):
        profile = arg[8:].strip() or profile
    elif not arg.startswith("-"):
        profile = arg   # bare name also accepted

reply = sys.stdin.read().strip()

if not reply:
    sys.exit(0)

excerpt = reply[-500:] if len(reply) > 500 else reply
task    = os.environ.get("BCODER_TASK", "")

if task:
    prompt = (
        f"Task: {task}\n\n"
        f"Last reasoning:\n{excerpt}\n\n"
        "Suggest ONE next search direction in one sentence. No explanation, just the direction."
    )
else:
    prompt = (
        "Read this reasoning and suggest ONE next search direction in one sentence. "
        f"No explanation, just the direction:\n\n{excerpt}"
    )

# Output the prompt on a CAPTURE: line so the before-proc handler sets self._last_output = prompt.
# Then ACTION: /parallel $ expands $ to the prompt without any shell-quoting issues.
print(f"CAPTURE: {prompt}")
print(f"ACTION: /parallel $  profile: {profile}  ctx: last")
