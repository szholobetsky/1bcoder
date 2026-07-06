"""ctxtimer — Measure maximum safe context length for your model and hardware.

Uses binary or sequential search to find the largest context window where the model
still manages to generate the first token before timeout. Results depend on:
- Your specific model (and quantization level)
- Your specific hardware (CPU/GPU, VRAM, bandwidth)
- Your timeout setting in 1bcoder
- The inference framework (Ollama, llama.cpp, etc.)

Usage:
  /flow ctxtimer                              sequential search from 1000 tokens, step 1000
  /flow ctxtimer --start 500 --step 500 --seq sequential with custom start and step
  /flow ctxtimer --start 1000 --end 10000 --bin binary search between 1000 and 10000 tokens
  /flow ctxtimer --bin                        binary search (auto-detects reasonable bounds)
  /flow ctxtimer --full                       wait for the ENTIRE response, not just first token

  /flow ctxtimer report                       show all test results as table
  /flow ctxtimer report --model <name>        show results for specific model only
  /flow ctxtimer report --csv                 show raw CSV format
  /flow ctxtimer report clear                 delete report.csv (asks for confirmation)

Results are saved to: .1bcoder/ctxtimer/report.csv

Parameters:
  --start N       Start testing from N tokens (default: 1000)
  --end N         Max tokens to test (for binary search; default: auto-detect)
  --step N        Step size for sequential search (default: 1000)
  --seq           Sequential search mode: test start, start+step, start+2*step, ... (default)
  --bin           Binary search mode: efficient search between start and end
  --full          Success requires the whole response to complete, not just the
                  first token (see "Two test modes" below)

Output:
  Table with context sizes and pass/fail results
  Conclusion: "Maximum safe context: N tokens" + CSV entry saved

Report columns: timestamp, model, provider, timeout_s, max_context_tokens, search_mode

Tokenization: 1 token ≈ 4 characters (same as 1bcoder estimate)

Two test modes:
  probe (default) — forces num_predict=1 so the model stops after exactly
    one output token. Fast, and isolates time-to-first-token from
    decode-phase slowdown or long <think> preambles. Cannot detect a model
    that starts fine but dies partway through a longer response (e.g. an
    issue that only shows up deeper into generation, like growing KV-cache
    memory pressure) — with num_predict=1 there's no "deeper" to reach.
  --full — no num_predict limit; waits for the entire response. Success
    requires the WHOLE response to complete with no error at any point —
    a fast start that later dies still counts as FAIL. Slower, and mixes
    decode-phase dynamics into the result, but catches what probe mode
    cannot: a model that streams several tokens at a healthy rate and then
    dies partway through, unrelated to prefill.
"""

import os as _os
import re as _re


def _get_report_path() -> str:
    """Get path to report CSV file. Creates directory if needed."""
    # Try project-local first
    local_dir = _os.path.join(".1bcoder", "ctxtimer")
    if _os.path.isdir(local_dir) or not _os.path.exists(_os.path.expanduser("~/.1bcoder")):
        _os.makedirs(local_dir, exist_ok=True)
        return _os.path.join(local_dir, "report.csv")

    # Fall back to user global
    global_dir = _os.path.join(_os.path.expanduser("~"), ".1bcoder", "ctxtimer")
    _os.makedirs(global_dir, exist_ok=True)
    return _os.path.join(global_dir, "report.csv")


def _save_result(model: str, provider: str, timeout: int, max_tokens: int, mode: str,
                 start: int, end: int) -> None:
    """Append result to report.csv."""
    import csv as _csv
    from datetime import datetime as _datetime

    report_path = _get_report_path()

    # Check if file exists to decide if we need to write header
    file_exists = _os.path.isfile(report_path)

    timestamp = _datetime.now().isoformat()

    try:
        with open(report_path, "a", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)

            # Write header if file is new
            if not file_exists:
                writer.writerow([
                    "timestamp", "model", "provider", "timeout_s",
                    "max_context_tokens", "search_mode", "start_tokens", "end_tokens"
                ])

            # Write result row
            writer.writerow([
                timestamp, model, provider, timeout,
                max_tokens, mode.upper(), start, end or "-"
            ])
    except Exception as e:
        print(f"[ctxtimer] warning: could not save result to CSV: {e}")


def _load_base_prompt() -> str:
    """Load base_prompt.txt from ctxtimer data directory."""
    for base_dir in (
        _os.path.join(".1bcoder", "ctxtimer"),
        _os.path.join(_os.path.expanduser("~"), ".1bcoder", "ctxtimer"),
        _os.path.join(_os.path.dirname(__file__), "ctxtimer"),
    ):
        path = _os.path.join(base_dir, "base_prompt.txt")
        if _os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                print(f"[ctxtimer] warning: could not read {path}: {e}")
    return ""


def _chars_to_tokens(num_chars: int) -> int:
    """Convert character count to approximate token count (1 token ≈ 4 chars)."""
    return num_chars // 4


def _tokens_to_chars(num_tokens: int) -> int:
    """Convert token count to approximate character count (1 token ≈ 4 chars)."""
    return num_tokens * 4


def _test_context(chat, prompt_text: str, context_tokens: int, full_mode: bool = False) -> tuple:
    """
    Test if model can generate output with given context size.

    Two modes:
    - probe (full_mode=False, default): forces num_predict=1 so the model
      stops after exactly one output token. Isolates time-to-first-token
      from decode-phase slowdown or long <think> preambles. Fast, and
      matches the original "did prefill succeed" question. But it CANNOT
      detect a model that starts fine, streams a handful of tokens at a
      healthy rate, and then dies partway through a longer response (e.g.
      an OOM or KV-cache growth issue triggered only deeper into
      generation) — with num_predict=1 there is no "deeper into
      generation" to reach.
    - full (full_mode=True): no num_predict limit; waits for the entire
      response. Success requires the WHOLE response to complete with no
      error at any point — a fast start that later dies still counts as
      FAIL. Slower and mixes decode-phase dynamics into the result, but
      catches the failure mode probe mode is blind to.

    Returns (success: bool, error_msg: str)

    IMPORTANT: chat._stream_chat() never lets timeouts or Ctrl-C escape as
    Python exceptions — it catches them internally:
      - requests.exceptions.RequestException (read timeout, connection
        error, etc.) -> prints "error: <msg>" itself and returns ""
      - KeyboardInterrupt                    -> prints "[interrupted]"
        itself and returns None
    So a plain try/except around the call can never see these. We capture
    stdout instead and inspect both the return value and what was printed
    to tell a real timeout apart from a genuinely empty reply.
    """
    import sys as _sys
    import io as _io

    # Convert token count to character count for slicing
    char_count = _tokens_to_chars(context_tokens)

    # Slice the prompt to the desired size
    context = prompt_text[:char_count]
    if not context:
        return False, "context empty after slicing"

    # Create a simple request: ask model to summarize the context
    request_text = f"{context}\n\n---\n\nSummarize the above text in 1-2 sentences:"

    # Call the LLM through chat._stream_chat
    messages = [
        {"role": "user", "content": request_text}
    ]

    # In probe mode, force the model to stop after exactly 1 output token
    # (see docstring above for why). In full mode, leave num_predict alone
    # so the model runs to natural completion.
    _had_num_predict = "num_predict" in chat.params
    _old_num_predict = chat.params.get("num_predict")
    if not full_mode:
        chat.params["num_predict"] = 1

    old_stdout = _sys.stdout
    captured = _io.StringIO()
    _sys.stdout = captured
    try:
        reply = chat._stream_chat(messages)
    finally:
        _sys.stdout = old_stdout
        if not full_mode:
            if _had_num_predict:
                chat.params["num_predict"] = _old_num_predict
            else:
                chat.params.pop("num_predict", None)

    captured_text = captured.getvalue()

    # _stream_chat swallowed a Ctrl-C internally (returns None as a sentinel).
    # Surface it here as a real KeyboardInterrupt so the search loop actually
    # stops immediately instead of silently treating it as "no response".
    if reply is None:
        if captured_text.strip():
            print(captured_text.strip())
        raise KeyboardInterrupt()

    if reply == "":
        # "" is what _stream_chat returns BOTH for a genuinely empty model
        # reply AND for a caught timeout/connection error — the only way to
        # tell them apart is what it printed while doing so.
        if "error:" in captured_text:
            # Always surface the real error message — do not hide it.
            print(captured_text.strip())

            if not full_mode:
                error_idx = captured_text.find("error:")
                text_before_error = captured_text[:error_idx].strip()
                if text_before_error:
                    # Some content was already streamed before the connection
                    # died mid-generation — prefill succeeded, so per our
                    # success criterion (first token received) this counts as OK.
                    return True, ""
            # full_mode: any error anywhere in the response is a failure,
            # even if plenty of content streamed first — we require the
            # ENTIRE response to complete cleanly.

            lowered = captured_text.lower()
            if "timeout" in lowered or "timed out" in lowered:
                return False, "timeout"
            return False, "error"

        # No error was printed — a real (if unusual) empty reply; the
        # request completed cleanly within the timeout either way.
        return True, ""

    return True, ""


def _display_report(args: str) -> None:
    """Display report.csv as a formatted table."""
    import csv as _csv

    report_path = _get_report_path()

    if not _os.path.isfile(report_path):
        print("[ctxtimer] No results yet. Run /flow ctxtimer to collect data.")
        return

    # Parse report args
    show_raw_csv = "--csv" in args
    model_filter = None
    m = _re.search(r'--model\s+(\S+)', args)
    if m:
        model_filter = m.group(1)

    try:
        with open(report_path, "r", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            rows = list(reader)
    except Exception as e:
        print(f"[ctxtimer] Error reading report: {e}")
        return

    if not rows:
        print("[ctxtimer] No results in report.")
        return

    # Filter by model if specified
    if model_filter:
        rows = [r for r in rows if model_filter.lower() in r["model"].lower()]
        if not rows:
            print(f"[ctxtimer] No results found for model: {model_filter}")
            return

    if show_raw_csv:
        # Show raw CSV
        print("timestamp,model,provider,timeout_s,max_context_tokens,search_mode,start_tokens,end_tokens")
        for row in rows:
            print(f"{row['timestamp']},{row['model']},{row['provider']},{row['timeout_s']},"
                  f"{row['max_context_tokens']},{row['search_mode']},{row['start_tokens']},{row['end_tokens']}")
    else:
        # Show formatted table
        print("\n[ctxtimer] Test Results Report")
        print("[ctxtimer] " + "=" * 100)
        print("[ctxtimer] "
              f"{'Model':<30} {'Provider':<10} {'Timeout':<10} {'Max Context':<15} {'Mode':<8} {'Range':<20}")
        print("[ctxtimer] " + "─" * 100)

        for row in rows:
            model = row["model"][:28]
            provider = row["provider"][:8]
            timeout = row["timeout_s"]
            max_ctx = row["max_context_tokens"]
            mode = row["search_mode"][:7]
            start = row["start_tokens"]
            end = row["end_tokens"]
            range_str = f"{start}–{end}" if end != "-" else f"{start}+"

            print(f"[ctxtimer] {model:<30} {provider:<10} {timeout:>8}s {max_ctx:>13} {mode:<8} {range_str:<20}")

        print("[ctxtimer] " + "=" * 100)
        print(f"[ctxtimer] Total runs: {len(rows)}")


def _clear_report() -> None:
    """Delete report.csv after user confirmation."""
    report_path = _get_report_path()

    if not _os.path.isfile(report_path):
        print("[ctxtimer] No report file to clear.")
        return

    answer = input(f"[ctxtimer] Delete {report_path}? [y/N] ").strip().lower()
    if answer != "y":
        print("[ctxtimer] Cancelled.")
        return

    try:
        _os.remove(report_path)
        print(f"[ctxtimer] Deleted: {report_path}")
    except Exception as e:
        print(f"[ctxtimer] Error deleting report: {e}")


def run(chat, args: str):
    """Main flow entry point."""
    stripped = args.strip()

    # Check if this is a report request
    if stripped.startswith("report"):
        report_args = stripped[len("report"):].strip()
        if report_args == "clear":
            _clear_report()
        else:
            _display_report(report_args)
        return

    try:
        _run_impl(chat, args)
    except KeyboardInterrupt:
        print("\n[ctxtimer] ✗ Interrupted by user (Ctrl-C)")
        return


def _run_impl(chat, args: str):
    """Implementation of ctxtimer flow."""
    args = args.strip()

    # Parse arguments
    mode = "seq"
    start_tokens = 1000
    end_tokens = None
    step_tokens = 1000

    # Parse --start
    m = _re.search(r'--start\s+(\d+)', args)
    if m:
        start_tokens = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    # Parse --end
    m = _re.search(r'--end\s+(\d+)', args)
    if m:
        end_tokens = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    # Parse --step
    m = _re.search(r'--step\s+(\d+)', args)
    if m:
        step_tokens = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    # Parse mode (--seq or --bin)
    if "--bin" in args:
        mode = "bin"
    elif "--seq" in args:
        mode = "seq"

    # Parse --full: wait for entire response instead of just first token
    full_mode = "--full" in args

    # Load base prompt
    base_prompt = _load_base_prompt()
    if not base_prompt:
        print("[ctxtimer] ERROR: could not load base_prompt.txt")
        print("[ctxtimer] expected in: .1bcoder/ctxtimer/ or ~/.1bcoder/ctxtimer/")
        return

    base_prompt_tokens = _chars_to_tokens(len(base_prompt))

    # Auto-detect end_tokens for binary search if not specified
    if mode == "bin" and end_tokens is None:
        end_tokens = min(base_prompt_tokens, start_tokens * 10)

    # Validate parameters
    if start_tokens > base_prompt_tokens:
        print(f"[ctxtimer] WARNING: --start ({start_tokens}) exceeds base_prompt size ({base_prompt_tokens})")
        start_tokens = base_prompt_tokens

    if mode == "bin" and end_tokens and end_tokens > base_prompt_tokens:
        print(f"[ctxtimer] WARNING: --end ({end_tokens}) exceeds base_prompt size ({base_prompt_tokens})")
        end_tokens = base_prompt_tokens

    print(f"[ctxtimer] =========================================")
    print(f"[ctxtimer] Base prompt size: {base_prompt_tokens:,} tokens ({len(base_prompt):,} chars)")
    print(f"[ctxtimer] Mode: {mode.upper()}" + (" (FULL — waits for entire response)" if full_mode else " (probe — first token only)"))
    if mode == "seq":
        print(f"[ctxtimer] Start: {start_tokens:,} tokens, Step: {step_tokens:,} tokens")
    else:
        print(f"[ctxtimer] Range: {start_tokens:,}–{end_tokens:,} tokens, Step: {step_tokens:,} tokens")
    print(f"[ctxtimer] =========================================\n")

    results = []  # list of (context_tokens, status_str)
    max_success_tokens = None

    if mode == "seq":
        # Sequential search
        current = start_tokens
        while current <= base_prompt_tokens:
            print(f"[ctxtimer] {current:6,} tokens  ", end="", flush=True)

            success, error_msg = _test_context(chat, base_prompt, current, full_mode)

            if success:
                status_str = "✓ OK"
                max_success_tokens = current
                print(status_str)
            else:
                status_str = f"✗ FAIL ({error_msg})"
                print(status_str)
                # In sequential mode, stop at first failure
                results.append((current, status_str))
                break

            results.append((current, status_str))
            current += step_tokens

    else:
        # Binary search
        low = start_tokens
        high = end_tokens or base_prompt_tokens

        print(f"[ctxtimer] Binary search in range [{low:,}, {high:,}]\n")

        tested = set()  # Track what we've already tested

        while high - low > step_tokens:
            mid = (low + high) // 2
            # Round mid to nearest multiple of step_tokens for cleaner results
            mid = (mid // step_tokens) * step_tokens
            if mid <= low:
                mid = low + step_tokens
            if mid in tested or mid > base_prompt_tokens:
                break

            print(f"[ctxtimer] {mid:6,} tokens  ", end="", flush=True)

            success, error_msg = _test_context(chat, base_prompt, mid, full_mode)
            tested.add(mid)

            if success:
                status_str = "✓ OK"
                max_success_tokens = mid
                print(status_str)
                results.append((mid, status_str))
                low = mid  # Success, try larger
            else:
                status_str = f"✗ FAIL"
                print(status_str)
                results.append((mid, status_str))
                high = mid  # Failure, try smaller

        # Test the final boundaries to narrow down exact threshold
        for test_size in [low, low + step_tokens, high - step_tokens, high]:
            if test_size <= 0 or test_size > base_prompt_tokens or test_size in tested:
                continue

            print(f"[ctxtimer] {test_size:6,} tokens  ", end="", flush=True)

            success, error_msg = _test_context(chat, base_prompt, test_size, full_mode)
            tested.add(test_size)

            if success:
                status_str = "✓ OK"
                max_success_tokens = max(max_success_tokens or 0, test_size)
                print(status_str)
            else:
                status_str = f"✗ FAIL"
                print(status_str)

            results.append((test_size, status_str))

    # Print results
    print(f"\n[ctxtimer] =========================================")
    print(f"[ctxtimer] RESULTS")
    print(f"[ctxtimer] =========================================\n")

    # Sort results by context size
    results = sorted(set(results), key=lambda x: x[0])

    # Print table
    print("  Context (tokens) | Status")
    print("  ─────────────────┼────────────────")
    for tokens, status in results:
        print(f"  {tokens:15,} | {status}")

    # Print conclusion
    print(f"\n[ctxtimer] =========================================")
    if max_success_tokens is not None:
        print(f"[ctxtimer] ✓ SUCCESS")
        print(f"[ctxtimer] Maximum safe context: {max_success_tokens:,} tokens")
        print(f"[ctxtimer] =========================================")
        print(f"\n[ctxtimer] Your model/hardware can handle up to {max_success_tokens:,} tokens")
        if full_mode:
            print(f"[ctxtimer] with the full response completing before timeout.")
        else:
            print(f"[ctxtimer] before hitting timeout on first token generation.")
            print(f"[ctxtimer] (probe mode — run with --full to also verify the whole response completes)")

        # Save result to CSV
        _save_result(
            model=chat.model,
            provider=chat.provider,
            timeout=chat.timeout,
            max_tokens=max_success_tokens,
            mode=mode + ("-FULL" if full_mode else ""),
            start=start_tokens,
            end=end_tokens
        )
        print(f"[ctxtimer] Result saved to: {_get_report_path()}")
    else:
        print(f"[ctxtimer] ✗ FAILURE")
        print(f"[ctxtimer] All tested context sizes exceeded timeout!")
        print(f"[ctxtimer] =========================================")
        print(f"\n[ctxtimer] Even {start_tokens:,} tokens is too much for this model.")
        print(f"[ctxtimer] Try reducing model size or enabling more aggressive quantization.")

    print()
