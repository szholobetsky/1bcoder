"""Tests for loop_stream.py — pure helpers of /loop (no Ollama, no network)."""
import csv
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import loop_stream as ls  # noqa: E402


def write(path, text, encoding="utf-8", newline=None):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding=encoding, newline=newline) as f:
        f.write(text)


# ── tokenizer / parser ────────────────────────────────────────────────────────

def test_tokenize_quoted_prefix_and_bare():
    toks = ls.tokenize_args('dir:concepts/ file:*.md split:"## " --llm "випиши все про MCP"')
    assert toks == [("dir:concepts/", False), ("file:*.md", False), ("split:## ", True),
                    ("--llm", False), ("випиши все про MCP", True)]


def test_tokenize_keeps_backslashes_in_regex():
    toks = ls.tokenize_args(r'split:re:"^## \d+\." last')
    assert toks[0] == (r"split:re:^## \d+\.", True)
    assert toks[1] == ("last", False)


def test_tokenize_escaped_quote():
    assert ls.tokenize_args(r'--llm "say \"hi\""') == [("--llm", False), ('say "hi"', True)]


def test_parse_jev_quoted_vs_bare():
    a = ls.parse_loop_args('last --jev "Чи це код?"')
    assert a["jev"] == "Чи це код?" and a["jev_is_question"] is True
    b = ls.parse_loop_args("last --jev tree_rules")
    assert b["jev"] == "tree_rules" and b["jev_is_question"] is False
    c = ls.parse_loop_args("last --jev 2")
    assert c["jev"] == "2" and not c["jev_is_question"]


def test_parse_sources_and_flags():
    a = ls.parse_loop_args('dir:~/logs file:*.log -r split:tokens:3000 --keep fail --ctx 2 '
                           '--max-tokens 500 --out ans/x.csv --fresh -y')
    assert a["source"] == "dir" and a["path"] == "~/logs" and a["glob"] == "*.log"
    assert a["recursive"] and a["cutter"] == "tokens:3000" and a["keep"] == "fail"
    assert a["ctx"] == 2 and a["max_tokens"] == 500 and a["out"] == "ans/x.csv"
    assert a["fresh"] and a["yes"]
    f = ls.parse_loop_args("file:big.txt split:blank")
    assert f["source"] == "file" and f["path"] == "big.txt" and f["glob"] is None
    c = ls.parse_loop_args('cmd:"/find _confirm -c" split:line')
    assert c["source"] == "cmd" and c["cmd"] == "/find _confirm -c"


@pytest.mark.parametrize("bad", [
    "", "--llm x", "last dir:x", "last cmd:\"/find x\"", "last --keep maybe",
    "last split:tokens:abc", 'last split:re:"("', "last --bogus", "file:a file:b",
    "last -r", "last --rg-min 0.5", 'last "stray"', "last --llm",
])
def test_parse_errors(bad):
    with pytest.raises(ls.LoopError):
        ls.parse_loop_args(bad)


def test_action_sig_changes_with_prompt_not_with_out():
    a = ls.parse_loop_args('last --llm "a" --out x.md')
    b = ls.parse_loop_args('last --llm "a" --out y.md -y')
    c = ls.parse_loop_args('last --llm "b" --out x.md')
    assert ls.action_sig(a) == ls.action_sig(b) != ls.action_sig(c)


# ── cutters ───────────────────────────────────────────────────────────────────

def test_est_tokens_cyrillic_heavier():
    assert ls.est_tokens("abcdefgh") == 2
    assert ls.est_tokens("абвгдеєж") == 4


def test_split_heading_prefix_with_title_preamble():
    text = "# Заголовок\n\n## 1. Перша\nтекст один\n\n## 2. Друга\nтекст два\n"
    parts = ls.split_text(text, "## ")
    assert len(parts) == 2
    assert parts[0].startswith("# Заголовок") and "## 1. Перша" in parts[0]
    assert parts[1].startswith("## 2. Друга")


def test_split_heading_real_preamble_is_own_element():
    text = "Вступ з реальним текстом.\n\n## A\nx\n## B\ny"
    assert ls.split_text(text, "## ") == ["Вступ з реальним текстом.", "## A\nx", "## B\ny"]


def test_split_regex_unnumbered_first_heading_is_section_1():
    text = "# Title\n\n## Огляд\nперший\n\n## 2. Друга\nдва\n\n## 3. Третя\nтри"
    parts = ls.split_text(text, r"re:^## \d+\.")
    assert len(parts) == 3
    assert "## Огляд" in parts[0] and "перший" in parts[0]
    assert parts[1].startswith("## 2. Друга") and parts[2].startswith("## 3. Третя")


def test_split_prefix_does_not_cut_deeper_headings():
    parts = ls.split_text("## A\n### a1\ntext\n## B\n", "## ")
    assert parts == ["## A\n### a1\ntext", "## B"]


def test_split_blank_crlf():
    text = "абзац один\r\nрядок два\r\n\r\n  \r\nабзац два\r\n\r\n"
    assert ls.split_text(text, "blank") == ["абзац один\nрядок два", "абзац два"]


def test_split_line_drops_blank():
    assert ls.split_text("a\n\n  \nb  \n", "line") == ["a", "b"]


def test_split_tokens_on_line_boundaries():
    lines = [f"line {i} " + "word " * 10 for i in range(40)]
    text = "\n".join(lines)
    chunks = ls.split_text(text, "tokens:50")
    assert len(chunks) > 1
    for c in chunks:
        for l in c.split("\n"):
            assert l in lines            # never cut inside a line
    assert "\n".join(chunks) == text


def test_split_tokens_overlong_line_cut_on_words():
    line = " ".join(f"слово{i}" for i in range(200))
    chunks = ls.split_text(line, "tokens:40")
    assert len(chunks) > 1
    words = " ".join(chunks).split()
    assert words == line.split()


def test_no_cutter_whole_text():
    assert ls.split_text("abc\n", None) == ["abc\n"]
    assert ls.split_text("   ", None) == []


def test_build_elements_ids_and_crumbs():
    big = "## 1. Мала\nкоротко\n## 2. Велика\n" + "\n".join("рядок " * 20 for _ in range(30))
    els = ls.build_elements([("docs/a.md", big), ("docs/b.md", "## X\nx")], "## ", max_tokens=100)
    ids = [e.id for e in els]
    assert ids[0] == "docs/a.md#1"
    assert ids[1].startswith("docs/a.md#2@1")
    k = sum(1 for i in ids if i.startswith("docs/a.md#2@"))
    assert k > 1
    assert ids[-1] == "docs/b.md#1"
    assert els[1].crumb == f"docs/a.md, section 2 of 2, part 1 of {k}"
    assert [e.index for e in els] == list(range(1, len(els) + 1))
    assert all(e.total == len(els) for e in els)
    assert all(ls.est_tokens(e.text) <= 100 for e in els)


def test_build_elements_tokens_and_no_cutter_ids():
    els = ls.build_elements([("f.txt", "a\n" * 400)], "tokens:30", max_tokens=1000)
    assert els[0].id == "f.txt@1" and els[1].id == "f.txt@2"
    one = ls.build_elements([("f.txt", "hello")], None, max_tokens=1000)
    assert one[0].id == "f.txt" and one[0].crumb == "f.txt"
    over = ls.build_elements([("f.txt", "word " * 2000)], None, max_tokens=100)
    assert over[0].id == "f.txt@1" and "part 1 of" in over[0].crumb


# ── collecting files ──────────────────────────────────────────────────────────

def test_collect_dir_glob_recursive_sorted_binary_bom(tmp_path):
    root = tmp_path / "plan"
    write(root / "item_2.10.md", "десять")
    write(root / "item_2.9.md", "дев'ять")
    write(root / "item_1.md", "﻿з BOM", encoding="utf-8")
    write(root / "notes.txt", "not md")
    with open(root / "bin.md", "wb") as f:
        f.write(b"ab\x00cd")
    write(root / "sub" / "item_3.md", "глибоко")
    write(root / ".hidden" / "item_4.md", "сховано")
    write(root / "__pycache__" / "item_5.md", "кеш")

    flat = ls.collect_dir(str(root), "*.md", False, str(tmp_path))
    assert [b for b, _ in flat] == ["plan/item_1.md", "plan/item_2.9.md", "plan/item_2.10.md"]
    assert flat[0][1] == "з BOM"
    rec = ls.collect_dir(str(root), "item_*.md", True, str(tmp_path))
    assert [b for b, _ in rec] == ["plan/item_1.md", "plan/item_2.9.md", "plan/item_2.10.md",
                                   "plan/sub/item_3.md"]
    # identical ids on two runs
    assert ls.collect_dir(str(root), "*.md", True, str(tmp_path)) == \
        ls.collect_dir(str(root), "*.md", True, str(tmp_path))


def test_make_id_base_inside_and_outside(tmp_path):
    wd = tmp_path / "proj"
    os.makedirs(wd)
    inside = wd / "a" / "b.md"
    assert ls.make_id_base(str(inside), str(wd)) == "a/b.md"
    outside = tmp_path / "other" / "c.md"
    got = ls.make_id_base(str(outside), str(wd))
    assert got == str(outside).replace("\\", "/") and "\\" not in got


def test_collect_dir_missing(tmp_path):
    with pytest.raises(ls.LoopError):
        ls.collect_dir(str(tmp_path / "nope"), None, False, str(tmp_path))


def test_read_text_crlf_and_binary(tmp_path):
    p = tmp_path / "t.txt"
    with open(p, "wb") as f:
        f.write("﻿рядок\r\nдва".encode("utf-8"))
    assert ls.read_text(str(p)) == "рядок\nдва"
    b = tmp_path / "b.bin"
    with open(b, "wb") as f:
        f.write(b"\x00\x01")
    assert ls.read_text(str(b)) is None


# ── jsonl log / resume ────────────────────────────────────────────────────────

def test_resume_truncated_last_line_and_errors(tmp_path):
    jl = str(tmp_path / "out" / "r.md.jsonl")
    ls.append_jsonl(jl, {"id": "a#1", "index": 1, "kept": True, "sig": "s1", "llm": "одна"})
    ls.append_jsonl(jl, {"id": "a#2", "index": 2, "kept": False, "sig": "s1", "error": "jev: down"})
    with open(jl, "a", encoding="utf-8") as f:
        f.write('{"id": "a#3", "index": 3, "ke')          # crash mid-write
    assert ls.read_done_ids(jl) == {"a#1"}               # errored a#2 is retried
    assert ls.log_sigs(jl) == {"s1"}
    ls.append_jsonl(jl, {"id": "a#2", "index": 2, "kept": True, "sig": "s1", "llm": "дві"})
    assert ls.read_done_ids(jl) == {"a#1", "a#2"}
    latest = ls.latest_records(ls.read_log(jl))
    assert [r["id"] for r in latest] == ["a#1", "a#2"] and latest[1]["llm"] == "дві"


def test_read_log_missing_file(tmp_path):
    assert ls.read_log(str(tmp_path / "none.jsonl")) == []
    assert ls.read_done_ids(str(tmp_path / "none.jsonl")) == set()


# ── rendering ─────────────────────────────────────────────────────────────────

def _records():
    rub = {"rubric": "tree_rules", "passed": False, "criteria": [
        {"id": "no_bare_headings", "kind": "regex", "value": 0, "passed": True},
        {"id": "ceiling", "kind": "noul", "value": 0.81, "passed": False}]}
    return [
        {"id": "p/item_1.md", "source": "p/item_1.md", "index": 1, "kept": True, "jev": rub,
         "rg": {"sim": 0.71234, "oos": {"DNS": 2}}, "text": "Текст, з комою"},
        {"id": "p/item_2.md", "source": "p/item_2.md", "index": 2, "kept": False,
         "jev": dict(rub, passed=True)},
        {"id": "p/item_3.md", "source": "p/item_3.md", "index": 3, "kept": True,
         "jev": {"question": "q", "kind": "noul", "value": 0.9, "passed": True}, "llm": "Відповідь\nдва рядки"},
    ]


def test_render_md_csv_txt_deterministic(tmp_path):
    jl = str(tmp_path / "o.md.jsonl")
    for r in _records():
        ls.append_jsonl(jl, r)
    md1, w = ls.render(jl, str(tmp_path / "o.md"))
    md2, _ = ls.render(jl, str(tmp_path / "o.md"))
    assert w is None and md1 == md2
    assert "## p/item_1.md" in md1 and "## p/item_2.md" not in md1     # not kept
    assert "> jev tree_rules: FAIL (ceiling)" in md1
    assert "> radogast sim=0.712 out-of-scope: DNS×2" in md1
    assert "Відповідь\nдва рядки" in md1

    csv_text, _ = ls.render(jl, str(tmp_path / "o.csv"))
    with open(tmp_path / "o.csv", "rb") as f:
        assert f.read(3) == b"\xef\xbb\xbf"                              # utf-8-sig for Excel
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0][:4] == ["id", "source", "index", "verdict"]
    assert "ceiling" in rows[0] and "ceiling:pass" in rows[0] and "p_true" in rows[0]
    assert len(rows) == 3
    r1 = dict(zip(rows[0], rows[1]))
    assert r1["ceiling"] == "0.810" and r1["ceiling:pass"] == "false" and r1["verdict"] == "false"
    assert r1["rg_oos"] == "DNS:2"
    assert r1["llm"] == "Текст, з комою"
    r3 = dict(zip(rows[0], rows[2]))
    assert r3["p_true"] == "0.900" and r3["llm"] == "Відповідь\nдва рядки"

    txt, _ = ls.render(jl, str(tmp_path / "o.txt"))
    assert txt == "Текст, з комою\n\nВідповідь\nдва рядки\n"

    other, warn = ls.render(jl, str(tmp_path / "o.log"))
    assert warn and other == txt


def test_summary_table_counts():
    s = ls.summary_table(_records())
    assert "3 element(s), kept 2, errors 0" in s
    assert "PASS    2   FAIL    1" in s
    assert "ceiling" in s and "radogast sim" in s


# ── radogast (fake vectors, real radogast helpers when installed) ─────────────

def test_rg_element_scores_and_oos():
    target = SimpleNamespace(goal="g", key_terms=["usage"], out_of_scope=["DNS", "TCP"])
    scores = ls.rg_element_scores(["dns over tcp, DNS again", "clean"],
                                  [[1.0, 0.0], [0.0, 1.0]], [1.0, 0.0], target)
    assert scores[0] == {"sim": 1.0, "oos": {"DNS": 2, "TCP": 1}}
    assert scores[1] == {"sim": 0.0, "oos": {}}


def test_rg_goal_text_strips_heading():
    t = SimpleNamespace(goal="# Design a system", key_terms=["usage", "threshold"])
    assert ls.rg_goal_text(t) == "Design a system usage threshold"


def test_rg_stream_report_fake_vectors():
    np = pytest.importorskip("numpy")
    pytest.importorskip("radogast.metrics.similarity")
    v = np.array([[1.0, 0.0, 0.0], [0.99, 0.141, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    # ids that would look like dotted ancestors to radogast — positional ids avoid that
    ids = ["plan/item_2.md", "plan/item_2.md#1", "plan/item_3.md", "plan/item_4.md"]
    texts = ["usage threshold", "usage", "restock here", "nothing"]
    target = SimpleNamespace(goal="g", key_terms=["usage", "restock", "MCP"], out_of_scope=[])
    rep = ls.rg_stream_report(ids, texts, v, np.array([1.0, 0.0, 0.0]), target, threshold=0.9)
    assert len(rep["clusters"]) == 1
    assert rep["clusters"][0]["ids"] == ["plan/item_2.md", "plan/item_2.md#1"]
    assert rep["clusters"][0]["max_sim"] > 0.98
    assert rep["absent_terms"] == ["MCP"]
    assert [i for i, _ in rep["farthest"]][:2] in (["plan/item_3.md", "plan/item_4.md"],
                                                  ["plan/item_4.md", "plan/item_3.md"])
    assert rep["top_pairs"][0][:2] == ("plan/item_2.md", "plan/item_2.md#1")
    text = ls.format_rg_report(rep)
    assert "duplicate clusters" in text and "most similar pairs" in text


def test_rg_load_missing_file(tmp_path):
    pytest.importorskip("radogast.target")
    with pytest.raises(ls.LoopError):
        ls.rg_load(str(tmp_path / "nope.yaml"))
