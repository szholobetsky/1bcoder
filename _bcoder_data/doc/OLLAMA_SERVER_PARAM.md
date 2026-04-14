# Ollama Server Parameters

Quick reference for the most useful Ollama environment variables.
Set before starting `ollama serve`.

**Windows (PowerShell):**
```powershell
$env:OLLAMA_HOST = "0.0.0.0:11434"
ollama serve
```
**Windows (persistent):** System → Advanced system settings → Environment Variables

**Linux / macOS:**
```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
# or add to /etc/systemd/system/ollama.service [Service] section
```

---

## Top 5 — covers 90% of needs

| Variable | Default | What it does |
|---|---|---|
| `OLLAMA_HOST` | `127.0.0.1:11434` | Bind address. Set `0.0.0.0:11434` to allow network access from other machines. |
| `OLLAMA_MODELS` | `~/.ollama/models` | Where model files are stored. Move to a larger/faster drive: `D:\ollama\models` |
| `OLLAMA_KV_CACHE_TYPE` | `f16` | KV cache quantization. `q8_0` saves ~50% VRAM, `q4_0` saves ~75%. Minor quality loss. |
| `OLLAMA_MAX_LOADED_MODELS` | `1` (GPU) / `3` (CPU) | How many models stay loaded in VRAM simultaneously. Set `2`–`3` for fast model switching. |
| `OLLAMA_KEEP_ALIVE` | `5m` | How long a model stays in VRAM after the last request. `0` = unload immediately. `-1` = never unload. `30m`, `2h` also valid. |

---

## Next 10 — performance and control

| Variable | Default | What it does |
|---|---|---|
| `OLLAMA_NUM_PARALLEL` | `1` | Max simultaneous requests per model. Increase for serving multiple clients. Uses more VRAM per extra slot. |
| `OLLAMA_FLASH_ATTENTION` | `0` | Set `1` to enable flash attention. Reduces VRAM usage, speeds up long contexts. Recommended on. |
| `OLLAMA_GPU_OVERHEAD` | `0` | VRAM (bytes) to reserve for OS/other processes before Ollama loads models. E.g. `500000000` = 500 MB. |
| `OLLAMA_MAX_QUEUE` | `512` | Max requests queued before server returns 503. Lower if you want fast fail instead of waiting. |
| `OLLAMA_SCHED_SPREAD` | `false` | Set `1` to spread model layers across multiple GPUs evenly instead of filling one first. |
| `OLLAMA_NOPRUNE` | `false` | Set `1` to skip pruning unused model blobs on startup. Faster start, uses more disk. |
| `OLLAMA_TMPDIR` | system temp | Directory for temporary files during model load. Set to fast SSD if default is slow. |
| `OLLAMA_ORIGINS` | `localhost` | Comma-separated allowed CORS origins. Set `*` to allow all (useful for local web UIs). |
| `OLLAMA_DEBUG` | `0` | Set `1` for verbose logging — shows model load details, GPU layer counts, errors. |
| `OLLAMA_RUNNERS_DIR` | built-in | Path to custom llama.cpp runner binaries. Rarely needed. |

---

## KV cache quantization — detail

Controls memory used by the context (key/value attention cache).
`OLLAMA_KV_CACHE_TYPE` sets both K and V to the same type.

| Type | VRAM vs f16 | Quality |
|---|---|---|
| `f16` | baseline | full |
| `q8_0` | ~50% | near-lossless |
| `q4_0` | ~25% | slight degradation on long contexts |

```powershell
$env:OLLAMA_KV_CACHE_TYPE = "q8_0"
ollama serve
```

For llama.cpp server directly (separate K and V):
```
llama-server -ctk q8_0 -ctv q4_0 ...
```

---

## Practical recipes

```powershell
# Network server with KV compression (6 GB VRAM machine)
$env:OLLAMA_HOST           = "0.0.0.0:11434"
$env:OLLAMA_KV_CACHE_TYPE  = "q8_0"
$env:OLLAMA_FLASH_ATTENTION = "1"
$env:OLLAMA_KEEP_ALIVE     = "30m"
ollama serve

# Keep two models hot for fast switching
$env:OLLAMA_MAX_LOADED_MODELS = "2"
$env:OLLAMA_KEEP_ALIVE        = "-1"
ollama serve

# CI / batch processing — unload immediately after each request
$env:OLLAMA_KEEP_ALIVE = "0"
ollama serve
```

---

## Android / Termux

Ollama runs on ARM64 Android via Termux. `OLLAMA_HOST` is mandatory — the phone is a remote worker, accessed from PC via network.

```bash
pkg update && pkg upgrade
pkg install ollama
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Add the phone as a worker in 1bcoder: `/parallel profile add <name>` → host `192.168.x.x:11434`

---

### Context size — the main RAM variable on phones

KV cache grows linearly with context. Cutting `num_ctx` from 4096 to 512 can free 300–700 MB.
Set in 1bcoder: `/ctx 512`  or in Modelfile: `PARAMETER num_ctx 512`

---

### Performance tiers — by processor and RAM

**12nm ARM (Helio G9x class), 6–8 GB RAM**
Models: `qwen2.5:0.5b`, `lfm2.5:350m`, `smollm:360m`, `gemma3:270m`
Max ctx: ~2048–3000 tokens in practice (4000 is the hard ceiling before OOM)
Threads: 4 physical cores max — beyond that triggers thermal throttle

**8nm ARM (Snapdragon 7xx class), 6 GB RAM**
Models: `qwen3:1.7b`, `lfm2.5:1.2b`, `gemma3:1b`, `llama3.2:1b`
Max ctx: 2048–4096 tokens
Noticeably faster per token than 12nm tier

**4nm ARM (Dimensity 7xxx / Snapdragon 8 Gen class), 12 GB RAM**
Models: up to 4b quant (e.g. `nemotron-mini:4b`)
Max ctx: still limited to ~4000 tokens — more RAM shifts the ceiling, architecture limit stays
Some Snapdragon 8 Gen 2+ support Vulkan GPU offload: try `-ngl 10` → `20` → `32`

---

### Ollama env vars for phone

```bash
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_KEEP_ALIVE=0
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_FLASH_ATTENTION=1
```

---

### llama.cpp direct flags

| Flag | Recommended | Notes |
|---|---|---|
| `-c` / `--ctx-size` | `512`–`2048` | Context size — primary RAM knob |
| `-t` / `--threads` | physical cores only | More threads = thermal throttle, not speed |
| `-ngl` | `0` (most), `10–32` (Snapdragon 8 Gen 2+) | GPU layer offload via Vulkan |
| `-b` / `--batch-size` | `128`–`256` | Reduce if OOM during prompt ingestion |
| `-ctk q4_0 -ctv q4_0` | recommended | KV cache quant — saves ~75% KV RAM |
| `--mlock` | **on** | Locks model in physical RAM. If any part swaps to disk, inference speed degrades catastrophically |

---

### Tips

- Keep a Termux wake lock: `termux-wake-lock` — screen-off can kill background processes
- Charge during inference on long jobs — phone inference is power-intensive

---

### When is it actually useful?

Small models on phones (350m–1.7b) can participate in `/parallel` to enrich context with known facts about different aspects of a task — which allows the main model to be more accurate when patching or editing code.

However, the small context window requires surgical precision in context preparation. This limits these models to explanation and analysis only: small logs, error messages, individual functions, or file trees shallow in depth. Such models often follow instructions poorly — they cannot edit files or act as agents. But they enrich context well and work effectively as a team alongside larger models.
