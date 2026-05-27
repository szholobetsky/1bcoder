"""assist — before-proc: ask a second LLM for a one-sentence search hint each turn.

Reads the last agent reply, builds a focused "what to search next?" prompt,
and dispatches it to a parallel LLM profile. The hint is injected as [context]
into the next agent turn — helping the agent stay on track.

This is a meta-cognitive aid: a fast cheap LLM watches the main agent's
reasoning and suggests course corrections before the next turn begins.

Arguments:
  profile: <name>   1bcoder profile to use for the hint LLM (default: short)
                    Recommended: short, nemotron-mini, qwen3:0.6b (fast and cheap)

Usage in agent file:
  before =
      assist                        # uses built-in "short" profile
      assist profile: ten_experts   # custom profile

Usage from session:
  /proc before on assist
  /proc before on assist profile: ten_experts

Output per turn:
  CAPTURE: <hint prompt>
  ACTION:  /parallel $  profile: <name>  ctx: last

Examples:
  > /proc before on assist
  > /agent run research.txt
  # → before each turn, a second LLM suggests one focused search direction
  # → reduces turns wasted on tangents in long agent runs

  > /proc before on assist profile: short
  # → use the built-in "short" profile (fast single-turn LLM)

  Tip: combine with action-required gate for focused, efficient agents:
    before = assist
    gates  = action-required
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
