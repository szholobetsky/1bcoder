# 1bcoder — Model Parameter Reference

All parameters are set with `/param <key> <value>` and sent with every request.
View current values: `/param`
Reset all: `/param clear`

---

## Built-in parameters (1bcoder-internal)

These are handled by 1bcoder directly and are not forwarded to the model API.

| Parameter | Default | Description |
|---|---|---|
| `timeout` | 120 | HTTP read timeout in seconds. Increase for slow/large-context models. |
| `num_ctx` | 4096 | Context window size (tokens). Set via `/ctx <n>` or `/param num_ctx <n>` is not forwarded — use `/ctx`. |
| `think_exclude` | `true` | Strip `<think>…</think>` blocks from context (blocks are shown in terminal only). Set `false` to keep reasoning in context for multi-turn chains. |
| `ask_limit` | 8000 | Max characters of `/ask` tool output kept in context. |
| `ask_show` | 500 | Characters shown in terminal when output is truncated. |

---

## Sampling parameters

Control how the model selects the next token.

| Parameter | Range | Default | Description |
|---|---|---|---|
| `temperature` | 0.0 – 2.0 | 0.8 | Randomness. Lower = more deterministic. 0.0 = greedy. Good range: 0.2–0.8 for code. |
| `top_p` | 0.0 – 1.0 | 0.9 | Nucleus sampling. Only consider tokens whose cumulative probability ≤ top_p. Lower = less diversity. |
| `top_k` | 0 – ∞ | 40 | Limit sampling pool to top-K tokens. 0 = disabled. Lower = less diverse. |
| `min_p` | 0.0 – 1.0 | 0.0 | Minimum probability threshold relative to the top token. Filters weak candidates. |
| `typical_p` | 0.0 – 1.0 | 1.0 | Locally typical sampling. 1.0 = disabled. Can improve coherence for long outputs. |
| `seed` | integer | -1 | RNG seed. -1 = random. Set a fixed value for reproducible outputs. |

---

## Repetition control

These are the most useful parameters for preventing looping/repetitive output.

| Parameter | Range | Default | Description |
|---|---|---|---|
| `repeat_penalty` | 0.0 – ∞ | 1.1 | Penalise tokens that already appeared. >1.0 = penalise, <1.0 = encourage repetition. **1.1–1.3** is the practical range. |
| `repeat_last_n` | -1 – ∞ | 64 | How many recent tokens to check for repetition. -1 = full context. Larger = stricter de-loop. |
| `frequency_penalty` | -2.0 – 2.0 | 0.0 | Penalise proportional to how often a token appeared (OpenAI-style). Positive reduces repetition. |
| `presence_penalty` | -2.0 – 2.0 | 0.0 | Flat penalty for any token that appeared at all (OpenAI-style). Encourages topic diversity. |

**Recommended anti-loop recipe:**
```
/param repeat_penalty 1.15
/param repeat_last_n 128
```
For severe looping: `repeat_penalty 1.3`. Above 1.5 quality degrades.

---

## Output length

| Parameter | Range | Default | Description |
|---|---|---|---|
| `num_predict` | -1 – ∞ | -1 | Max tokens to generate. -1 = unlimited (until stop token or context full). |
| `stop` | string | — | Stop sequence. Generation halts when this string is produced. e.g. `/param stop </tool>` |

---

## Thinking / reasoning (extended models)

For models with explicit reasoning chains (DeepSeek-R1, Qwen3, QwQ, etc.).

| Parameter | Values | Description |
|---|---|---|
| `enable_thinking` | `true` / `false` | Enable/disable the extended `<think>` chain. Set `false` to skip reasoning and get faster, shorter replies. |
| `thinking_budget` | integer (tokens) | Max tokens allocated to the thinking chain before the visible reply starts. Model-specific. |
| `think_exclude` | `true` / `false` | **1bcoder-internal.** Strip `<think>` blocks from context (default: `true`). Does not affect terminal display or the model API call. |
| `think_show` | `true` / `false` | **1bcoder-internal.** Show `<think>` blocks in terminal during streaming (default: `true`). Independent of `think_exclude`. |

**Note:** `enable_thinking false` tells the model not to think at all.
`think_exclude` and `think_show` are independent — any combination works:
- default: show in terminal, strip from context
- `/think hide` + `/think exclude`: silent, no context cost
- `/think show` + `/think include`: full reasoning visible and passed to next turn

---

## Mirostat (adaptive sampling)

An alternative to top_p/top_k that targets a specific perplexity level.

| Parameter | Values | Default | Description |
|---|---|---|---|
| `mirostat` | 0, 1, 2 | 0 | 0 = disabled, 1 = Mirostat v1, 2 = Mirostat v2 (recommended if used). |
| `mirostat_tau` | float | 5.0 | Target entropy. Lower = more focused/coherent. |
| `mirostat_eta` | float | 0.1 | Learning rate. How fast the algorithm adapts. |

---

## Tail-free sampling

| Parameter | Range | Default | Description |
|---|---|---|---|
| `tfs_z` | 0.0 – 1.0 | 1.0 | Tail-free sampling. 1.0 = disabled. Removes low-probability tail tokens. |

---

## Penalise newlines

| Parameter | Values | Default | Description |
|---|---|---|---|
| `penalize_newline` | `true` / `false` | `false` | Apply repeat_penalty to newline tokens. Prevents newline-only loops. |

---

## Examples

```
# Reproducible output
/param seed 42
/param temperature 0.2

# Prevent looping
/param repeat_penalty 1.2
/param repeat_last_n 128

# Fast answer, no thinking chain
/param enable_thinking false

# Long generation
/param num_predict 4096
/param timeout 300

# Keep reasoning in context for chained agents
/param think_exclude false

# Reset everything
/param clear
```

---

## Model compatibility

| Backend | Supports |
|---|---|
| Ollama | All parameters except OpenAI-style `frequency_penalty`/`presence_penalty` |
| OpenAI-compatible | `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `seed`, `stop`, `max_tokens` (= `num_predict`) |
| LM Studio | Same as Ollama |
| vLLM | OpenAI-compatible subset |

Parameters not understood by the backend are silently ignored by the server.
