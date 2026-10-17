"""Tests for parametrized rubrics: {{name}} placeholders filled from /var (or asked)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import chat  # noqa: E402
import jev  # noqa: E402


PAY_YAML = """\
name: payment
description: "Check a payment against balance {{current_balance}}"
criteria:
  - id: mentions_date
    check: regex
    pattern: '{{previous_payment_date}}'
    fail_if: nomatch
    message: "must mention the previous payment of {{previous_payment_date}}"
  - id: within_balance
    type: noul
    instructions: "Is the paid amount at most {{current_balance}}?"
    criteria:
      "true": "Amount <= {{current_balance}}"
      "false": "Amount > {{current_balance}}"
"""


def _raw():
    import yaml
    return yaml.safe_load(PAY_YAML)


# ── jev.py ────────────────────────────────────────────────────────────────────

def test_rubric_params_finds_all_names():
    assert jev.rubric_params(_raw()) == ["current_balance", "previous_payment_date"]


def test_fill_substitutes_everywhere_and_escapes_regex():
    r = jev.normalize_rubric(_raw(), "payment",
                             params={"current_balance": "1200.50", "previous_payment_date": "2026.09.01"})
    c_re, c_jev = r["criteria"]
    assert c_re["pattern"] == r"2026\.09\.01"            # literal, the dots are escaped
    assert c_re["_re"].search("paid on 2026.09.01")
    assert not c_re["_re"].search("paid on 2026x09x01")
    assert c_re["message"] == "must mention the previous payment of 2026.09.01"
    assert c_jev["instructions"] == "Is the paid amount at most 1200.50?"
    assert c_jev["criteria"] == {"true": "Amount <= 1200.50", "false": "Amount > 1200.50"}
    assert r["description"] == "Check a payment against balance 1200.50"
    assert r["params"] == ["current_balance", "previous_payment_date"]


def test_missing_params_raise_and_name_all():
    with pytest.raises(jev.JevConfigError) as e:
        jev.normalize_rubric(_raw(), "payment", params={"current_balance": "", "x": "1"})
    msg = str(e.value)
    assert "{{current_balance}}" in msg and "{{previous_payment_date}}" in msg   # "" counts as unset


def test_template_mode_keeps_placeholders():
    r = jev.normalize_rubric(_raw(), "payment")
    assert "{{current_balance}}" in r["criteria"][1]["instructions"]


def test_load_rubric_requires_params_for_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(jev, "HOME_RUBRICS_DIR", str(tmp_path))
    (tmp_path / "payment.yaml").write_text(PAY_YAML, encoding="utf-8")
    with pytest.raises(jev.JevConfigError):
        jev.load_rubric("payment", str(tmp_path / "w"))
    r = jev.load_rubric("payment", str(tmp_path / "w"),
                        params={"current_balance": "5", "previous_payment_date": "d"})
    assert r["criteria"][1]["instructions"] == "Is the paid amount at most 5?"


# ── chat.py ───────────────────────────────────────────────────────────────────

@pytest.fixture
def cli(tmp_path, monkeypatch):
    home = tmp_path / "rubrics"
    home.mkdir()
    (home / "payment.yaml").write_text(PAY_YAML, encoding="utf-8")
    monkeypatch.setattr(jev, "HOME_RUBRICS_DIR", str(home))
    monkeypatch.setattr(jev, "SETTINGS_FILE", str(tmp_path / "jev.json"))
    monkeypatch.setattr(chat, "WORKDIR", str(tmp_path))
    c = object.__new__(chat.CoderCLI)
    c.messages, c.last_reply, c._last_output = [], "", ""
    c._vars, c._proc_active, c._proc_gates = {}, [], []
    c._proc_before, c._proc_gates_post = [], []
    c.provider, c.base_url = "ollama", "http://localhost:11434"
    return c


def test_existing_var_used_silently_missing_one_asked(cli, monkeypatch):
    cli._vars["current_balance"] = "300"
    asked = []
    monkeypatch.setattr(cli, "_prompt_input", lambda p: (asked.append(p), "2026-09-01")[1])
    r = cli._jev_load_rubric("payment")
    assert asked == ["  previous_payment_date = ? "]
    assert cli._vars["previous_payment_date"] == "2026-09-01"      # persisted like /script
    assert r["criteria"][1]["instructions"] == "Is the paid amount at most 300?"


def test_non_interactive_never_prompts(cli, monkeypatch):
    monkeypatch.setattr(cli, "_prompt_input", lambda p: pytest.fail("must not prompt"))
    with pytest.raises(jev.JevConfigError):
        cli._jev_load_rubric("payment", interactive=False)


def test_gate_binding_fails_with_message_when_var_missing(cli, monkeypatch):
    monkeypatch.setattr(cli, "_prompt_input", lambda p: pytest.fail("must not prompt"))
    out = cli._jev_bound("jev:payment --gate", input_text="paid 100")
    assert out.startswith("FAIL: payment") and "previous_payment_date" in out


def test_gate_binding_follows_current_var_values(cli, monkeypatch):
    seen = []
    monkeypatch.setattr(jev, "call_systemone",
                        lambda state, qs, s: (seen.append(qs["within_balance"]["instructions"]),
                                              {"within_balance": {"noul": 0.9}})[1])
    jev.save_settings({**jev.DEFAULTS, "model": "tev1:0.8b"})
    cli._vars.update(current_balance="100", previous_payment_date="D1")
    assert cli._jev_bound("jev:payment --gate", input_text="paid on D1") == ""
    cli._vars["current_balance"] = "999"
    cli._jev_bound("jev:payment --gate", input_text="paid on D1")
    assert seen == ["Is the paid amount at most 100?", "Is the paid amount at most 999?"]


def test_jev_param_set_show_clear(cli, capsys):
    cli._cmd_jev("/jev param timeout 300")
    assert jev.load_settings()["timeout"] == 300
    cli._cmd_jev("/jev param")
    assert "timeout" in capsys.readouterr().out
    cli._cmd_jev("/jev param clear")
    assert jev.load_settings() == jev.DEFAULTS


def test_param_is_a_reserved_rubric_name():
    with pytest.raises(jev.JevConfigError):
        jev.normalize_rubric({"criteria": [{"id": "a", "check": "regex", "pattern": "x"}]}, "param")


def test_cmd_jev_key_value_args_set_vars(cli, monkeypatch):
    monkeypatch.setattr(cli, "_prompt_input", lambda p: pytest.fail("must not prompt"))
    monkeypatch.setattr(jev, "call_systemone", lambda state, qs, s: {"within_balance": {"noul": 0.9}})
    jev.save_settings({**jev.DEFAULTS, "model": "tev1:0.8b"})
    cli._last_output = "paid 50 after the 2026-09-01 payment"
    cli._cmd_jev('/jev payment current_balance="1 200" previous_payment_date=2026-09-01')
    assert cli._vars == {"current_balance": "1 200", "previous_payment_date": "2026-09-01"}
