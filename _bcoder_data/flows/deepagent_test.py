"""Generate unit tests for a joined code file produced by deepagent_code.

For each function in joined.py, generates a separate test file with optional
think-first planning, syntax checking, test execution, and a summary report.

Usage:
  /flow deepagent_test code14/joined.py --lang py
  /flow deepagent_test code14/joined.py --lang py --think --syntax --run
  /flow deepagent_test join code14      ← join test files into joined_test.py
  /flow deepagent_test report code14    ← show test results report

Flags:
  --lang py|js      target language (required)
  --think           plan test cases in text before writing code
  --syntax          check syntax of each test file, retry on error
  --run             execute each test file after generation
  --retries N       max retries on syntax/run failure (default 2)
"""
import os as _os
import re as _re
import subprocess as _sp


# ── reuse from deepagent_code ────────────────────────────────────────────────

def _load_deepagent_code():
    import importlib.util as _iu
    here = _os.path.dirname(_os.path.abspath(__file__))
    path = _os.path.join(here, "deepagent_code.py")
    spec = _iu.spec_from_file_location("_dac", path)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_dac = _load_deepagent_code()
_extract_code = _dac._extract_code
_syntax_check = _dac._syntax_check
_generate_with_retry = _dac._generate_with_retry
_generate_worker = _dac._generate_worker
_load_profile = _dac._load_profile
_StopGeneration = _dac._StopGeneration
LANGS = _dac.LANGS
_INTERNAL_PARAMS = _dac._INTERNAL_PARAMS


# ── prompts ──────────────────────────────────────────────────────────────────

_THINK_TEST_SYSTEM = (
    "You are a QA engineer planning tests for a function.\n"
    "Describe in plain text WHAT to test. Do NOT write code.\n\n"
    "Cover:\n"
    "- What does the function do? What are its inputs and outputs?\n"
    "- Happy path: what should work normally?\n"
    "- Edge cases: empty input, None, wrong types, boundary values\n"
    "- Error conditions: what should raise exceptions?\n"
    "- If the function calls helpers, what happens when helpers fail?\n\n"
    "Be specific. List concrete test cases with expected results. 5-15 sentences."
)

_TEST_SYSTEM = (
    "You are writing a unit test file for a Python function.\n"
    "The function is defined in 'joined.py' in the same directory.\n"
    "Import it with: from joined import function_name\n\n"
    "Rules:\n"
    "- Use pytest style: plain functions, assert statements\n"
    "- Use unittest.mock.patch to mock any helper functions called inside\n"
    "- Test happy path, edge cases, and error conditions\n"
    "- Each test function should test ONE thing\n"
    "- Output ONLY the code in a single code fence"
)

_FIX_SYSTEM = (
    "Fix the test code. The error is shown below.\n"
    "Output ONLY the corrected code in a single code fence."
)


# ── function extractor ───────────────────────────────────────────────────────

def _extract_functions(code: str) -> list:
    """Extract (name, full_function_code) pairs from a Python file."""
    lines = code.splitlines()
    functions = []
    current_name = None
    current_lines = []
    current_indent = 0

    for line in lines:
        m = _re.match(r'^(def\s+(\w+)\s*\()', line)
        if m:
            if current_name:
                functions.append((current_name, "\n".join(current_lines)))
            current_name = m.group(2)
            current_lines = [line]
            current_indent = 0
        elif current_name is not None:
            stripped = line.strip()
            if stripped == "" or line.startswith(" ") or line.startswith("\t"):
                current_lines.append(line)
            elif stripped.startswith("#"):
                current_lines.append(line)
            elif stripped.startswith("@"):
                current_lines.append(line)
            else:
                functions.append((current_name, "\n".join(current_lines)))
                current_name = None
                current_lines = []
                m2 = _re.match(r'^(def\s+(\w+)\s*\()', line)
                if m2:
                    current_name = m2.group(2)
                    current_lines = [line]

    if current_name:
        functions.append((current_name, "\n".join(current_lines)))

    return functions


# ── test generation ──────────────────────────────────────────────────────────

def _build_test_prompt(func_name: str, func_code: str, think_text: str) -> str:
    parts = [
        f"Write tests for this function:\n",
        f"```python\n{func_code}\n```\n",
        f"Import with: from joined import {func_name}",
    ]
    if think_text:
        parts.append(f"\nTest plan:\n{think_text}")
    return "\n".join(parts)


def _build_think_prompt(func_name: str, func_code: str) -> str:
    return (
        f"Function to test:\n\n"
        f"```python\n{func_code}\n```\n\n"
        f"Plan the test cases for '{func_name}'."
    )


def _run_test(test_path: str, lang_cfg: dict) -> tuple:
    """Run a test file. Returns (passed: bool, output: str)."""
    ext = lang_cfg["ext"]
    if ext == ".py":
        try:
            result = _sp.run(
                ["pytest", test_path, "-v", "--tb=short", "--no-header", "-q"],
                capture_output=True, text=True, timeout=60
            )
            output = (result.stdout + result.stderr).strip()
            return result.returncode == 0, output[:1000]
        except FileNotFoundError:
            try:
                result = _sp.run(
                    ["python", "-m", "pytest", test_path, "-v", "--tb=short", "-q"],
                    capture_output=True, text=True, timeout=60
                )
                output = (result.stdout + result.stderr).strip()
                return result.returncode == 0, output[:1000]
            except Exception as e:
                return False, f"pytest not found: {e}"
        except Exception as e:
            return False, str(e)[:500]
    return False, "test execution not supported for this language"


# ── interactive review gate (--review) ───────────────────────────────────────
# Test units are flat/independent (one per function, no parent/child), so
# this mirrors deepagent_spec.py's simplified [y]es/[n]o/[a]ll menu -- no
# [b]ranch. No live_index/dedup hook existed in this file before -- reuses
# the same LivePlanIndex class deepagent_md's --dedup uses, seeded from
# on-disk test_*.<ext> siblings. Note: generated tests for different
# functions are expected to share substantial boilerplate, so this
# similarity signal is noisier here than for prose -- shipped anyway for
# consistency, revisit threshold tuning later if it proves too noisy.

def _make_review_index(test_dir: str, use_review: bool):
    if not use_review:
        return None
    try:
        from radogast.live_index import LivePlanIndex
    except ImportError:
        print("[review] radogast not installed -- skipped (pip install radogast[embed])")
        return None
    live_index = LivePlanIndex()
    seeded = 0
    if _os.path.isdir(test_dir):
        for fname in sorted(_os.listdir(test_dir)):
            if not fname.startswith("test_") or fname.endswith(".md"):
                continue
            func_name = fname[len("test_"):].rsplit(".", 1)[0]
            text = open(_os.path.join(test_dir, fname), encoding="utf-8").read()
            live_index.add(func_name, text)
            seeded += 1
    print(f"[review] on (radogast dedup check, seeded {seeded} existing unit(s))")
    return live_index


def _review_prompt() -> str:
    import sys as _sys
    if not _sys.stdin.isatty():
        print("  [review] no interactive terminal -- auto-continuing")
        return 'y'
    while True:
        ans = input("  Continue? [y]es [n]o [a]ll: ").strip().lower()
        if ans in ("y", "n", "a", ""):
            return ans or 'y'
        print("  please enter y, n, or a")


def _review_feedback_regen(chat, func_name, func_code, test_path):
    try:
        feedback = input("  what should change? ").strip()
    except (EOFError, KeyboardInterrupt):
        feedback = ""
    if not feedback:
        print("  [review] no feedback given -- keeping as-is")
        return None
    prompt = _build_test_prompt(func_name, func_code, "") + f"\n\nAdditional instruction: {feedback}"
    raw = _generate_with_retry(chat, _TEST_SYSTEM, prompt, func_name)
    if not raw:
        print("  [review] regeneration produced nothing -- keeping previous content")
        return None
    test_code, _ = _extract_code(raw)
    with open(test_path, "w", encoding="utf-8") as f:
        f.write(test_code)
    print(f"  -> {_os.path.basename(test_path)} regenerated")
    return test_code


def _run_review_gate(chat, review_state, review_index, func_name, content, func_code, test_path):
    match = review_index.check(func_name, content) if review_index is not None else None
    while True:
        if match is not None:
            print(f"  [radogast] {func_name}: overlaps {match[0]} (similarity {match[1]:.2f})")
        else:
            print(f"  [radogast] {func_name}: clean")
        if review_state["mode"] == "all":
            print("  [review] auto-continuing (all)")
            break
        ans = _review_prompt()
        if ans == 'y':
            break
        if ans == 'a':
            review_state["mode"] = "all"
            break
        new_content = _review_feedback_regen(chat, func_name, func_code, test_path)
        if new_content is None:
            continue
        content = new_content
        match = review_index.check(func_name, content) if review_index is not None else None
    if review_index is not None:
        review_index.add(func_name, content)
    return content


# ── main flow ────────────────────────────────────────────────────────────────

def _process_one_test(func_name, func_code, i, total, test_dir, ext,
                      lang_cfg, use_think, use_syntax, use_run, max_retries,
                      call_llm_fn):
    """Process one function: think → generate → syntax → run. Returns result dict."""
    test_fname = f"test_{func_name}{ext}"
    test_path = _os.path.join(test_dir, test_fname)

    if _os.path.isfile(test_path):
        print(f"  [{i}/{total}] {func_name} [skip] already exists")
        return {"name": func_name, "status": "skipped", "file": test_fname}

    print(f"\n[{i}/{total}] {func_name}")

    # ── think step ────────────────────────────────────────────
    think_text = ""
    if use_think:
        md_path = _os.path.join(test_dir, f"test_{func_name}.md")
        if _os.path.isfile(md_path):
            think_text = open(md_path, encoding="utf-8").read()
        else:
            print(f"  [think] planning tests...")
            think_prompt = _build_think_prompt(func_name, func_code)
            think_text = call_llm_fn(_THINK_TEST_SYSTEM, think_prompt)
            if think_text:
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write(f"# Test plan: {func_name}\n\n{think_text}")
                print(f"  [think] -> {_os.path.basename(md_path)}")

    # ── generate test ─────────────────────────────────────────
    print(f"  [gen] writing test...")
    test_prompt = _build_test_prompt(func_name, func_code, think_text)
    raw = call_llm_fn(_TEST_SYSTEM, test_prompt)
    if not raw:
        print(f"  [error] empty reply")
        return {"name": func_name, "status": "error", "file": ""}

    test_code, _ = _extract_code(raw)
    with open(test_path, "w", encoding="utf-8") as f:
        f.write(test_code)
    print(f"  [gen] -> {test_fname}")

    # ── syntax check + retry ──────────────────────────────────
    if use_syntax:
        for attempt in range(max_retries):
            err = _syntax_check(test_path, lang_cfg)
            if not err:
                break
            print(f"  [syntax] error: {err}")
            fix_prompt = (
                f"This test code has a syntax error:\n\n"
                f"```\n{test_code}\n```\n\n"
                f"Error: {err}\n\nFix it."
            )
            raw = call_llm_fn(_FIX_SYSTEM, fix_prompt)
            if raw:
                test_code, _ = _extract_code(raw)
                with open(test_path, "w", encoding="utf-8") as f:
                    f.write(test_code)
        else:
            err = _syntax_check(test_path, lang_cfg)
            if err:
                print(f"  [syntax] FAILED after {max_retries} retries")
                return {"name": func_name, "status": "syntax_error",
                        "file": test_fname, "error": err}

    # ── run test ──────────────────────────────────────────────
    if use_run:
        for attempt in range(max_retries + 1):
            passed, output = _run_test(test_path, lang_cfg)
            if passed:
                print(f"  [run] PASSED")
                return {"name": func_name, "status": "passed", "file": test_fname}
            if attempt < max_retries:
                print(f"  [run] FAILED (retry {attempt + 1}/{max_retries})")
                fix_prompt = (
                    f"This test failed:\n\n"
                    f"```\n{test_code}\n```\n\n"
                    f"Error output:\n{output}\n\n"
                    f"The function being tested:\n```\n{func_code}\n```\n\n"
                    f"Fix the test. Output ONLY corrected code in a code fence."
                )
                raw = call_llm_fn(_FIX_SYSTEM, fix_prompt)
                if raw:
                    test_code, _ = _extract_code(raw)
                    with open(test_path, "w", encoding="utf-8") as f:
                        f.write(test_code)
            else:
                print(f"  [run] FAILED after {max_retries} retries")
                return {"name": func_name, "status": "failed",
                        "file": test_fname, "error": output[:300]}

    return {"name": func_name, "status": "generated", "file": test_fname}


def _generate_tests(chat, joined_path: str, test_dir: str, lang_cfg: dict,
                    lang: str, workers: list, use_think: bool, use_syntax: bool,
                    use_run: bool, max_retries: int, stats: dict,
                    review_state=None, review_index=None):
    ext = lang_cfg["ext"]

    code = open(joined_path, encoding="utf-8").read()
    functions = _extract_functions(code)

    if not functions:
        print("[error] no functions found in joined file")
        return []

    total = len(functions)
    print(f"[deepagent_test] found {total} functions")
    n_workers = len(workers) if workers else 1

    if workers and len(functions) > 1:
        # ── parallel mode: each worker handles one function ───────
        from concurrent.futures import ThreadPoolExecutor, as_completed
        print(f"[deepagent_test] parallel: {n_workers} workers")

        def _make_worker_llm(worker_idx):
            h, m, _ = workers[worker_idx % len(workers)]
            def _call(sys_prompt, user_prompt):
                return _generate_worker(h, m, sys_prompt, user_prompt,
                                        chat.num_ctx, chat.params)
            return _call

        results = []
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = {}
            for i, (func_name, func_code) in enumerate(functions, 1):
                llm_fn = _make_worker_llm(i)
                fut = pool.submit(
                    _process_one_test, func_name, func_code, i, total,
                    test_dir, ext, lang_cfg, use_think, use_syntax,
                    use_run, max_retries, llm_fn
                )
                futs[fut] = func_name
            for fut in as_completed(futs):
                r = fut.result()
                results.append(r)
                if r["status"] == "generated":
                    stats["generated"] += 1
                elif r["status"] == "passed":
                    stats["generated"] += 1
                    stats["passed"] += 1
                elif r["status"] == "failed":
                    stats["generated"] += 1
                    stats["failed"] += 1
        return results

    # ── sequential mode ───────────────────────────────────────────
    results = []
    for i, (func_name, func_code) in enumerate(functions, 1):

        def _call_llm(sys_prompt, user_prompt, _fn=func_name):
            return _generate_with_retry(chat, sys_prompt, user_prompt, _fn)

        r = _process_one_test(func_name, func_code, i, total,
                              test_dir, ext, lang_cfg, use_think, use_syntax,
                              use_run, max_retries, _call_llm)
        results.append(r)
        if review_state is not None and r.get("file") and r["status"] not in ("skipped", "error"):
            test_path = _os.path.join(test_dir, r["file"])
            content = open(test_path, encoding="utf-8").read()
            _run_review_gate(chat, review_state, review_index, func_name, content,
                             func_code, test_path)
        if r["status"] == "generated":
            stats["generated"] += 1
        elif r["status"] == "passed":
            stats["generated"] += 1
            stats["passed"] += 1
        elif r["status"] == "failed":
            stats["generated"] += 1
            stats["failed"] += 1

    return results


# ── report ───────────────────────────────────────────────────────────────────

def _write_report(test_dir: str, results: list, stats: dict):
    lines = ["# Test Report\n"]
    lines.append(f"| Function | Status | File |")
    lines.append(f"|----------|--------|------|")
    for r in results:
        status = r["status"]
        icon = {"passed": "PASS", "failed": "FAIL", "generated": "GEN",
                "skipped": "SKIP", "error": "ERR",
                "syntax_error": "SYN"}.get(status, status)
        err = r.get("error", "")
        name_col = r["name"]
        if err:
            name_col += f" — {err[:60]}"
        lines.append(f"| {name_col} | {icon} | {r.get('file', '')} |")

    lines.append(f"\n## Summary\n")
    lines.append(f"- Generated: {stats['generated']}")
    if stats.get("passed"):
        lines.append(f"- Passed: {stats['passed']}")
    if stats.get("failed"):
        lines.append(f"- Failed: {stats['failed']}")

    report_path = _os.path.join(test_dir, "report.md")
    content = "\n".join(lines) + "\n"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"\n{content}")
    print(f"[report] -> {report_path}")
    return report_path


def _join_tests(test_dir: str, lang_cfg: dict):
    ext = lang_cfg["ext"]
    comment = lang_cfg["comment"]
    files = sorted(f for f in _os.listdir(test_dir)
                   if f.startswith("test_") and f.endswith(ext)
                   and f != f"joined_test{ext}")
    if not files:
        print(f"[error] no test files in {test_dir}")
        return

    seen_imports = set()
    parts = []
    for fname in files:
        fpath = _os.path.join(test_dir, fname)
        code = open(fpath, encoding="utf-8").read().strip()
        lines = []
        for line in code.splitlines():
            if line.strip().startswith("import ") or line.strip().startswith("from "):
                if line.strip() not in seen_imports:
                    seen_imports.add(line.strip())
                    lines.append(line)
            else:
                lines.append(line)
        parts.append(f"{comment} === {fname} ===\n" + "\n".join(lines))

    imports_block = "\n".join(sorted(seen_imports))
    body = "\n\n\n".join(parts)
    joined = f"{imports_block}\n\n\n{body}\n"

    out_path = _os.path.join(test_dir, f"joined_test{ext}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(joined)
    print(f"[join] {len(files)} test files -> {out_path}")


# ── entry point ──────────────────────────────────────────────────────────────

def run(chat, args: str):
    args = args.strip()

    # ── subcommand: join ──────────────────────────────────────────────────
    if args.startswith("join"):
        rest = args[4:].strip()
        lang = "py"
        m = _re.search(r'--lang\s+(\S+)', rest)
        if m:
            lang = m.group(1).lower()
            rest = (rest[:m.start()] + rest[m.end():]).strip()
        code_dir = rest.strip()
        if not code_dir:
            print("usage: /flow deepagent_test join <code_dir> [--lang py]")
            return
        base = _os.path.join(_os.getcwd(), ".1bcoder", "code")
        if not _os.path.isabs(code_dir):
            code_dir = _os.path.join(base, code_dir)
        test_dir = _os.path.join(code_dir, "tests")
        if not _os.path.isdir(test_dir):
            print(f"[error] no tests dir: {test_dir}")
            return
        _join_tests(test_dir, LANGS.get(lang, LANGS["py"]))
        return

    # ── subcommand: redo ──────────────────────────────────────────────────
    if args.startswith("redo"):
        # /flow deepagent_test redo <joined_path> <func_name> [--lang py] [--review]
        #
        # No subtree here -- test units are flat and independent (one per
        # function), so this just deletes and regenerates ONE test file
        # (+ its think-plan .md if present).
        rest = args[4:].strip()
        review_flag = bool(_re.search(r'--review\b', rest))
        if review_flag:
            rest = _re.sub(r'--review\b', '', rest).strip()
        lang = "py"
        m = _re.search(r'--lang\s+(\S+)', rest)
        if m:
            lang = m.group(1).lower()
            rest = (rest[:m.start()] + rest[m.end():]).strip()
        parts = rest.split()
        if len(parts) < 2:
            print("usage: /flow deepagent_test redo <joined_path> <func_name> [--lang py] [--review]")
            return
        joined_path, func_name = parts[0], parts[1]

        if not _os.path.isabs(joined_path):
            base = _os.path.join(_os.getcwd(), ".1bcoder", "code")
            joined_path = _os.path.join(base, joined_path)
        if not _os.path.isfile(joined_path):
            print(f"[error] file not found: {joined_path}")
            return
        if lang not in LANGS:
            print(f"[error] unsupported language: {lang}")
            return
        lang_cfg = LANGS[lang]
        ext = lang_cfg["ext"]

        code = open(joined_path, encoding="utf-8").read()
        functions = _extract_functions(code)
        func_code = next((fc for fn, fc in functions if fn == func_name), None)
        if func_code is None:
            print(f"[deepagent_test] function {func_name} not found in {joined_path}")
            return

        test_dir = _os.path.join(_os.path.dirname(joined_path), "tests")
        test_path = _os.path.join(test_dir, f"test_{func_name}{ext}")
        md_path = _os.path.join(test_dir, f"test_{func_name}.md")

        import time as _time
        import shutil as _shutil
        for p in (test_path, md_path):
            if _os.path.isfile(p):
                timestamp = _time.strftime("%Y%m%dT%H%M%S")
                backup_dir = _os.path.join(test_dir, "_superseded", f"{func_name}_{timestamp}")
                _os.makedirs(backup_dir, exist_ok=True)
                dst = _os.path.join(backup_dir, _os.path.basename(p))
                _shutil.move(p, dst)
                print(f"[deepagent_test] backed up -> {dst}")

        def _call_llm(sys_prompt, user_prompt, _fn=func_name):
            return _generate_with_retry(chat, sys_prompt, user_prompt, _fn)

        r = _process_one_test(func_name, func_code, 1, 1, test_dir, ext, lang_cfg,
                              False, False, False, 2, _call_llm)
        print(f"[deepagent_test] redo done: {r['status']} ({r.get('file', '')})")

        if review_flag and r.get("file"):
            review_index = _make_review_index(test_dir, True)
            review_state = {"mode": "ask"}
            regen_path = _os.path.join(test_dir, r["file"])
            content = open(regen_path, encoding="utf-8").read()
            _run_review_gate(chat, review_state, review_index, func_name, content,
                             func_code, regen_path)
        return

    # ── subcommand: report ────────────────────────────────────────────────
    if args.startswith("report"):
        rest = args[6:].strip()
        code_dir = rest.strip()
        if not code_dir:
            print("usage: /flow deepagent_test report <code_dir>")
            return
        base = _os.path.join(_os.getcwd(), ".1bcoder", "code")
        if not _os.path.isabs(code_dir):
            code_dir = _os.path.join(base, code_dir)
        report_path = _os.path.join(code_dir, "tests", "report.md")
        if _os.path.isfile(report_path):
            print(open(report_path, encoding="utf-8").read())
        else:
            print(f"[error] no report found: {report_path}")
        return

    # ── parse flags ───────────────────────────────────────────────────────
    lang = "py"
    m = _re.search(r'--lang\s+(\S+)', args)
    if m:
        lang = m.group(1).lower()
        args = (args[:m.start()] + args[m.end():]).strip()

    use_think = "--think" in args
    args = args.replace("--think", "").strip()

    use_syntax = "--syntax" in args
    args = args.replace("--syntax", "").strip()

    use_run = "--run" in args
    args = args.replace("--run", "").strip()

    max_retries = 2
    m = _re.search(r'--retries\s+(\d+)', args)
    if m:
        max_retries = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()

    profile_name = None
    m = _re.search(r'--profile\s+(\S+)', args)
    if m:
        profile_name = m.group(1)
        args = (args[:m.start()] + args[m.end():]).strip()

    use_review = bool(_re.search(r'--review\b', args))
    if use_review:
        args = _re.sub(r'--review\b', '', args).strip()

    joined_path = args.strip().strip('"').strip("'")
    if not joined_path:
        print("usage: /flow deepagent_test <path/to/joined.py> --lang py [--think] [--syntax] [--run]")
        return

    # resolve path
    if not _os.path.isabs(joined_path):
        base = _os.path.join(_os.getcwd(), ".1bcoder", "code")
        joined_path = _os.path.join(base, joined_path)
    if not _os.path.isfile(joined_path):
        print(f"[error] file not found: {joined_path}")
        return

    if lang not in LANGS:
        print(f"[error] unsupported language: {lang}")
        return
    lang_cfg = LANGS[lang]

    # workers
    workers = None
    if profile_name:
        workers = _load_profile(profile_name)
        if workers:
            print(f"[deepagent_test] profile: {profile_name} ({len(workers)} workers)")
        else:
            print(f"[deepagent_test] profile '{profile_name}' not found")

    if workers and use_review:
        print("error: --review is incompatible with --profile "
              "(parallel workers complete out of order, so a per-unit review "
              "gate can't cleanly hold work back). "
              "Drop --profile for a supervised run, or drop --review for an unattended one.")
        return

    # create test dir alongside joined file
    code_dir = _os.path.dirname(joined_path)
    test_dir = _os.path.join(code_dir, "tests")
    _os.makedirs(test_dir, exist_ok=True)

    print(f"[deepagent_test] source: {joined_path}")
    print(f"[deepagent_test] tests:  {test_dir}")
    flags = []
    if use_think: flags.append("think")
    if use_syntax: flags.append("syntax")
    if use_run: flags.append("run")
    if flags:
        print(f"[deepagent_test] flags:  {', '.join(flags)}")

    review_index = _make_review_index(test_dir, use_review)
    review_state = {"mode": "ask"} if use_review else None

    stats = {"generated": 0, "passed": 0, "failed": 0}
    try:
        results = _generate_tests(chat, joined_path, test_dir, lang_cfg, lang,
                                  workers, use_think, use_syntax, use_run,
                                  max_retries, stats,
                                  review_state=review_state, review_index=review_index)
    except _StopGeneration:
        results = [{"name": "interrupted", "status": "stopped", "file": ""}]

    # ── report ────────────────────────────────────────────────────────────
    if results:
        _write_report(test_dir, results, stats)

    chat.last_reply = f"Generated {stats['generated']} tests in {test_dir}"
    chat._last_output = chat.last_reply
