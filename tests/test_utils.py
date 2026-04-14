"""Tests for pure utility functions in chat.py — no model, no network required."""
import sys
import os
import pytest

# chat.py is a single-file module in the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from chat import (
    _fts_rank,
    _extract_code_block,
    _extract_all_code_blocks,
    _split_identifier,
    _apply_params,
    _find_in_lines,
    _next_suffix_path,
    _fmt_size,
    _fmt_ctx,
)


# ── _fts_rank ─────────────────────────────────────────────────────────────────

def test_fts_rank_basic():
    files = {
        "auth.py": "def login(user, password): pass",
        "utils.py": "def helper(): pass",
    }
    results = _fts_rank(["login"], files, top_k=5)
    assert results[0][0] == "auth.py"


def test_fts_rank_multiple_terms():
    files = {
        "a.py": "login user password authentication",
        "b.py": "helper utility misc",
    }
    results = _fts_rank(["login", "password"], files, top_k=5)
    assert results[0][0] == "a.py"


def test_fts_rank_no_match():
    files = {"a.py": "hello world"}
    results = _fts_rank(["nonexistent_xyz"], files, top_k=5)
    assert results == [] or results[0][1] == 0


# ── _extract_code_block ───────────────────────────────────────────────────────

def test_extract_code_block_python():
    text = "Some text\n```python\ndef foo():\n    pass\n```\nMore text"
    result = _extract_code_block(text)
    assert "def foo():" in result


def test_extract_code_block_no_fence():
    result = _extract_code_block("no code here")
    assert result == "" or result is None or "no code" in result


def test_extract_all_code_blocks_multiple():
    text = "```python\ndef a(): pass\n```\n\n```python\ndef b(): pass\n```"
    blocks = _extract_all_code_blocks(text)
    assert len(blocks) == 2


# ── _split_identifier ─────────────────────────────────────────────────────────

def test_split_identifier_camel_case():
    parts = _split_identifier("getUserById")
    assert "user" in [p.lower() for p in parts]
    assert "id" in [p.lower() for p in parts]


def test_split_identifier_snake_case():
    parts = _split_identifier("get_user_by_id")
    assert len(parts) >= 3


def test_split_identifier_single_word():
    parts = _split_identifier("login")
    assert len(parts) >= 1


# ── _apply_params ─────────────────────────────────────────────────────────────

def test_apply_params_replaces():
    result = _apply_params("/read {{file}}", {"file": "main.py"})
    assert "main.py" in result
    assert "{{file}}" not in result


def test_apply_params_multiple():
    result = _apply_params("{{a}} and {{b}}", {"a": "foo", "b": "bar"})
    assert result == "foo and bar"


def test_apply_params_no_placeholders():
    result = _apply_params("/run tests", {})
    assert result == "/run tests"


# ── _find_in_lines ────────────────────────────────────────────────────────────

def test_find_in_lines_found():
    lines = ["def login():", "    return True", "def logout():"]
    idx = _find_in_lines(lines, "def login")
    assert idx is not None


def test_find_in_lines_not_found():
    lines = ["def login():", "    return True"]
    idx = _find_in_lines(lines, "nonexistent_xyz")
    # returns None, -1, or a tuple with None as first element when not found
    assert idx is None or idx == -1 or (isinstance(idx, tuple) and idx[0] is None)


# ── _next_suffix_path ─────────────────────────────────────────────────────────

def test_next_suffix_path_creates_suffix(tmp_path):
    base = tmp_path / "ctx.txt"
    base.write_text("original")
    result = _next_suffix_path(str(base))
    assert result != str(base)
    assert "ctx" in result


# ── _fmt_size / _fmt_ctx ──────────────────────────────────────────────────────

def test_fmt_size_bytes():
    result = _fmt_size(512)
    assert isinstance(result, str) and len(result) > 0


def test_fmt_size_mb():
    result = _fmt_size(2 * 1024 * 1024)
    assert "M" in result or "m" in result.lower()


def test_fmt_ctx_tokens():
    result = _fmt_ctx(1000)
    assert isinstance(result, str)
    assert len(result) > 0
