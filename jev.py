"""jev — adapter for decision models (Ollama /v1/systemone, Jev API).

(c) 2026 Stanislav Zholobetskyi
Institute for Information Recording, National Academy of Sciences of Ukraine, Kyiv
Створено в рамках аспірантського дослідження на тему:
"Інтелектуальна технологія підтримки розробки та супроводу програмних продуктів"

LLM generates -> Jev perceives -> Python decides.

A decision model never writes text: it answers closed questions (noul / choice /
score) and returns probabilities. This module compiles rubrics (YAML/JSON files
of criteria) into the structured `questions` dict, calls the model, and turns the
readings into a verdict with thresholds that live in Python, not in the model.

Deliberately does NOT import chat.py — flows and other tools can import it.
See concepts/JEV.md in the simrgl repo for the design.
"""

import json
import os
import re

import requests


class JevError(Exception):
    """Infrastructure failure: connection refused, timeout, 5xx, malformed reply."""


class JevConfigError(JevError):
    """Configuration problem: model unset/unsupported, bad or missing rubric, bad setting."""


# ── settings ──────────────────────────────────────────────────────────────────

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".1bcoder", "jev.json")

DEFAULTS = {
    "model": "",
    "host": "",
    "timeout": 30,
    "keep_alive": "5m",
    "threshold": 0.5,
    "batch": 64,
    "on_error": "fail",
}

RESERVED = {"list", "show", "model", "param", "on", "gate", "off", "help"}

MAX_QUESTIONS = 64
# Ollama rejects a larger /v1/systemone body with HTTP 413. requests sends
# json= with ensure_ascii=True, so Cyrillic costs 6 bytes per character.
MAX_BODY_BYTES = 64 * 1024

# rubric locations — module globals so tests can monkeypatch them.
# _bcoder_data/rubrics is never read at runtime: it is only the wheel's seed,
# copied into ~/.1bcoder/rubrics by chat._bootstrap_global_dir().
HOME_RUBRICS_DIR = os.path.join(os.path.expanduser("~"), ".1bcoder", "rubrics")
RUBRIC_EXTS = (".yaml", ".yml", ".json")

_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_JEV_TYPES = ("noul", "choice", "score")


def load_settings(path=None) -> dict:
    """Return DEFAULTS merged with the settings file (missing file = defaults)."""
    path = path or SETTINGS_FILE
    out = dict(DEFAULTS)
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise JevConfigError(f"cannot read jev params {path}: {e}")
    if not isinstance(data, dict):
        raise JevConfigError(f"jev params {path}: expected a JSON object")
    out.update(data)
    return out


def save_settings(d, path=None) -> None:
    path = path or SETTINGS_FILE
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


_EMPTY_WORDS = {"", '""', "''", "-", "none", "default"}


def coerce_setting(key, raw):
    """Validate and convert a raw (string) value for settings key `key`."""
    if key not in DEFAULTS:
        raise JevConfigError(f"unknown jev setting '{key}' — valid: {', '.join(DEFAULTS)}")
    s = str(raw).strip() if raw is not None else ""
    if key in ("model", "host"):
        if s.lower() in _EMPTY_WORDS:
            return ""
        if key == "host" and s.startswith("openai://"):
            raise JevConfigError("jev host must be an Ollama host — /v1/systemone is not "
                                 "part of the OpenAI-compatible API")
        return s
    if key == "timeout":
        try:
            v = float(s)
        except ValueError:
            raise JevConfigError(f"timeout must be a number of seconds, got '{s}'")
        if v <= 0:
            raise JevConfigError("timeout must be > 0")
        return int(v) if v == int(v) else v
    if key == "keep_alive":
        if not s:
            raise JevConfigError("keep_alive must not be empty (e.g. 5m, 30s, 0, -1)")
        return int(s) if re.fullmatch(r"-?\d+", s) else s
    if key == "threshold":
        try:
            v = float(s)
        except ValueError:
            raise JevConfigError(f"threshold must be a number in 0..1, got '{s}'")
        if not 0.0 <= v <= 1.0:
            raise JevConfigError(f"threshold must be in 0..1, got {v}")
        return v
    if key == "batch":
        try:
            v = int(s)
        except ValueError:
            raise JevConfigError(f"batch must be an integer 1..{MAX_QUESTIONS}, got '{s}'")
        if not 1 <= v <= MAX_QUESTIONS:
            raise JevConfigError(f"batch must be in 1..{MAX_QUESTIONS}, got {v}")
        return v
    if key == "on_error":
        if s.lower() not in ("fail", "pass"):
            raise JevConfigError(f"on_error must be 'fail' or 'pass', got '{s}'")
        return s.lower()
    return s  # pragma: no cover


def resolve_host(settings, fallback) -> str:
    """Jev host from settings, or `fallback` (the main host) when unset."""
    h = (settings.get("host") or "").strip() or (fallback or "").strip()
    if not h:
        raise JevConfigError("no jev host — set one: /jev param host localhost:11434")
    if h.startswith("openai://"):
        raise JevConfigError("jev host must be an Ollama host, not openai:// — "
                             "set one: /jev param host localhost:11434")
    if h.startswith("ollama://"):
        h = "http://" + h[len("ollama://"):]
    elif not h.startswith(("http://", "https://")):
        h = "http://" + h
    return h.rstrip("/")


# ── models ────────────────────────────────────────────────────────────────────

def _name_looks_decision(name: str) -> bool:
    base = name.split("/")[-1].lower()
    return base.startswith("tev") or base.startswith("nimble")


def list_models(host) -> list:
    """All models on the Ollama host: [{"name", "decision", "details"}].

    decision = True/False from the "capabilities" list in /api/tags, or None when
    the server does not report capabilities (older Ollama)."""
    try:
        resp = requests.get(f"{host}/api/tags", timeout=5)
        resp.raise_for_status()
        data = json.loads(resp.content.decode("utf-8"))
    except requests.exceptions.ConnectionError as e:
        raise JevError(f"cannot connect to {host}: {e.__class__.__name__}")
    except requests.exceptions.RequestException as e:
        raise JevError(f"cannot list models on {host}: {e}")
    except ValueError as e:
        raise JevError(f"bad /api/tags reply from {host}: {e}")
    out = []
    for m in data.get("models", []) or []:
        caps = m.get("capabilities")
        decision = ("decision" in caps) if isinstance(caps, list) else None
        out.append({"name": m.get("name", ""), "decision": decision,
                    "details": m.get("details") or {}})
    return out


def decision_models(models) -> list:
    """Filter list_models() output to decision models.

    Verified ones (capability "decision") first; if the server reports no
    capabilities, fall back to a name match (tev*, nimble*) — "unverified"."""
    return [m for m in models
            if m["decision"] is True or (m["decision"] is None and _name_looks_decision(m["name"]))]


def show_model(host, name) -> dict:
    """/api/show for one model (details, capabilities, model_info)."""
    try:
        resp = requests.post(f"{host}/api/show", json={"model": name}, timeout=10)
    except requests.exceptions.RequestException as e:
        raise JevError(f"cannot reach {host}: {e}")
    try:
        data = json.loads(resp.content.decode("utf-8"))
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        msg = data.get("error") if isinstance(data, dict) else None
        raise JevConfigError(msg or f"HTTP {resp.status_code} from /api/show")
    return data


# ── rubrics ───────────────────────────────────────────────────────────────────

def _scan_rubric_dir(d) -> list:
    """[(stem, path)] in sorted order; first extension wins for a duplicate stem."""
    if not d or not os.path.isdir(d):
        return []
    seen, out = set(), []
    for f in sorted(os.listdir(d)):
        stem, ext = os.path.splitext(f)
        if ext.lower() not in RUBRIC_EXTS or stem in seen:
            continue
        p = os.path.join(d, f)
        if os.path.isfile(p):
            seen.add(stem)
            out.append((stem, p))
    return out


def list_rubrics(workdir) -> list:
    """[(scope, name, path)] — global ("g") first, then local ("l").

    Global = ~/.1bcoder/rubrics, local = <workdir>/.1bcoder/rubrics.
    A local rubric with the same stem hides the global one.
    The 1-based position in this list is the rubric's number for /jev N."""
    glob = {}
    for stem, p in _scan_rubric_dir(HOME_RUBRICS_DIR):
        glob.setdefault(stem, p)
    local = _scan_rubric_dir(os.path.join(workdir, ".1bcoder", "rubrics")) if workdir else []
    local_stems = {s for s, _ in local}
    out = [("g", s, glob[s]) for s in sorted(glob) if s not in local_stems]
    out += [("l", s, p) for s, p in local]
    return out


def resolve_rubric(name_or_index, workdir):
    """(scope, name, path) for a rubric name, list number, or file path."""
    key = str(name_or_index).strip()
    if not key:
        raise JevConfigError("no rubric given — /jev list shows available rubrics")
    rubrics = list_rubrics(workdir)
    if key.isdigit():
        i = int(key)
        if not 1 <= i <= len(rubrics):
            raise JevConfigError(f"no rubric #{i} — /jev list shows {len(rubrics)} rubric(s)")
        return rubrics[i - 1]
    for r in rubrics:
        if r[1] == key:
            return r
    low = key.lower()
    for r in rubrics:
        if r[1].lower() == low:
            return r
    # explicit path to a rubric file
    if os.path.splitext(key)[1].lower() in RUBRIC_EXTS:
        p = key if os.path.isabs(key) else os.path.join(workdir or "", key)
        if os.path.isfile(p):
            return ("f", os.path.splitext(os.path.basename(p))[0], p)
    raise JevConfigError(f"rubric not found: '{key}' — /jev list shows available rubrics")


def _read_rubric_file(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        raise JevConfigError(f"cannot read rubric {path}: {e}")
    if path.lower().endswith(".json"):
        try:
            return json.loads(text)
        except ValueError as e:
            raise JevConfigError(f"rubric {os.path.basename(path)}: invalid JSON: {e}")
    try:
        import yaml
    except ImportError:
        raise JevConfigError("PyYAML is required for .yaml rubrics — pip install pyyaml")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise JevConfigError(f"rubric {os.path.basename(path)}: invalid YAML: {e}")


def _as_threshold(v, where):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise JevConfigError(f"{where}: threshold must be a number in 0..1, got {v!r}")
    if not 0.0 <= f <= 1.0:
        raise JevConfigError(f"{where}: threshold must be in 0..1, got {f}")
    return f


def _as_bool(v, where, field):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "yes", "no"):
        return v.strip().lower() in ("true", "yes")
    raise JevConfigError(f"{where}: {field} must be true or false, got {v!r}")


def _key_str(k):
    """YAML turns unquoted true/false/1 keys into bool/int — normalize to str."""
    if isinstance(k, bool):
        return "true" if k else "false"
    return str(k)


_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


def _normalize_criterion(c, idx, rname, seen):
    where = f"rubric {rname}, criterion #{idx}"
    if not isinstance(c, dict):
        raise JevConfigError(f"{where}: expected a mapping, got {type(c).__name__}")
    cid = c.get("id")
    if cid is None or not _ID_RE.match(str(cid)):
        raise JevConfigError(f"{where}: 'id' is required and must match [A-Za-z0-9_]+ (got {cid!r})")
    cid = str(cid)
    where = f"rubric {rname}, criterion '{cid}'"
    if cid in seen:
        raise JevConfigError(f"rubric {rname}: duplicate criterion id '{cid}'")
    seen.add(cid)
    check = str(c.get("check", "jev")).strip().lower()
    out = {"id": cid, "check": check,
           "message": str(c["message"]) if c.get("message") is not None else "",
           "threshold": _as_threshold(c.get("threshold"), where)}

    if check == "regex":
        pat = c.get("pattern")
        if not isinstance(pat, str) or not pat:
            raise JevConfigError(f"{where}: regex criterion needs a non-empty 'pattern'")
        fail_if = str(c.get("fail_if", "match")).strip().lower()
        if fail_if not in ("match", "nomatch"):
            raise JevConfigError(f"{where}: fail_if must be 'match' or 'nomatch', got {fail_if!r}")
        flags_s = str(c.get("flags") or "")
        flags = 0
        for ch in flags_s.lower():
            if ch not in _FLAG_MAP:
                raise JevConfigError(f"{where}: unknown regex flag '{ch}' (use i, m, s, x)")
            flags |= _FLAG_MAP[ch]
        try:
            compiled = re.compile(pat, flags)
        except re.error as e:
            raise JevConfigError(f"{where}: bad regex: {e}")
        out.update({"kind": "regex", "pattern": pat, "fail_if": fail_if,
                    "flags": flags_s, "_re": compiled})
        return out

    if check != "jev":
        raise JevConfigError(f"{where}: check must be 'jev' or 'regex', got {check!r}")

    typ = str(c.get("type", "")).strip().lower()
    if typ not in _JEV_TYPES:
        raise JevConfigError(f"{where}: type must be one of noul, choice, score (got {c.get('type')!r})")
    instr = c.get("instructions")
    if not isinstance(instr, str) or not instr.strip():
        raise JevConfigError(f"{where}: 'instructions' is required")
    out.update({"kind": typ, "type": typ, "instructions": instr.strip()})

    if typ == "noul":
        crit = c.get("criteria")
        if crit is not None:
            if not isinstance(crit, dict):
                raise JevConfigError(f"{where}: noul 'criteria' must be a mapping {{true: ..., false: ...}}")
            crit = {_key_str(k): str(v) for k, v in crit.items() if v is not None}
            bad = set(crit) - {"true", "false"}
            if bad:
                raise JevConfigError(f"{where}: noul 'criteria' keys must be true/false, got {sorted(bad)}")
            out["criteria"] = crit or None
        else:
            out["criteria"] = None
        out["pass_when"] = _as_bool(c.get("pass_when", True), where, "pass_when")
    elif typ == "score":
        levels = c.get("levels")
        if not isinstance(levels, list) or len(levels) < 2:
            raise JevConfigError(f"{where}: score needs 'levels' as a list of at least 2 descriptions (lowest first)")
        out["levels"] = [str(x) for x in levels]
    else:  # choice
        levels = c.get("levels")
        if not isinstance(levels, dict) or not levels:
            raise JevConfigError(f"{where}: choice needs 'levels' as a non-empty mapping {{key: description}}")
        out["levels"] = {_key_str(k): (None if v is None else str(v)) for k, v in levels.items()}
        pas = c.get("pass")
        if pas is not None:
            if isinstance(pas, (str, int, bool)):
                pas = [pas]
            if not isinstance(pas, list):
                raise JevConfigError(f"{where}: 'pass' must be a list of level keys")
            pas = [_key_str(k) for k in pas]
            unknown = [k for k in pas if k not in out["levels"]]
            if unknown:
                raise JevConfigError(f"{where}: 'pass' keys not in levels: {unknown}")
        out["pass"] = pas
    return out


# ── parameters ({{name}} placeholders, same syntax as /script) ────────────────

_PARAM_RE = re.compile(r"\{\{(\w+)\}\}")


def _walk_strings(obj, found):
    if isinstance(obj, str):
        found.update(_PARAM_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_strings(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_strings(v, found)


def rubric_params(data) -> list:
    """Sorted {{name}} placeholders used anywhere in the raw rubric's string values."""
    found = set()
    _walk_strings(data, found)
    return sorted(found)


def _fill(obj, params, escape=False):
    if isinstance(obj, str):
        def rep(m):
            v = str(params[m.group(1)])
            return re.escape(v) if escape else v
        return _PARAM_RE.sub(rep, obj)
    if isinstance(obj, dict):
        # a regex pattern matches the value literally, not as a sub-pattern
        is_regex = str(obj.get("check", "")).strip().lower() == "regex"
        return {k: _fill(v, params, escape or (is_regex and k == "pattern"))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, params, escape) for v in obj]
    return obj


def fill_params(data, params, name="rubric"):
    """Raw rubric data with every {{name}} replaced by params[name].

    Empty values count as unset (the /var NaN sentinel). Missing ones raise
    JevConfigError naming all of them."""
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    missing = [k for k in rubric_params(data) if k not in params]
    if missing:
        raise JevConfigError(
            f"rubric {name} needs {', '.join('{{' + k + '}}' for k in missing)} — "
            f"/var set {missing[0]} <value>, or /jev {name} {missing[0]}=<value>")
    return _fill(data, params)


def normalize_rubric(data, name, path="", scope="", params=None) -> dict:
    """Validate raw rubric data (already parsed) and return the normalized form.

    params=None keeps {{placeholders}} as written (for list/show); a dict fills
    them and raises JevConfigError if any is missing."""
    if name in RESERVED:
        raise JevConfigError(f"rubric name '{name}' is reserved (it is a /jev subcommand) — rename the file")
    if not isinstance(data, dict):
        raise JevConfigError(f"rubric {name}: expected a mapping at top level")
    param_names = rubric_params(data)
    if params is not None:
        data = fill_params(data, params, name)
    crits = data.get("criteria")
    if not isinstance(crits, list) or not crits:
        raise JevConfigError(f"rubric {name}: 'criteria' must be a non-empty list")
    seen = set()
    criteria = [_normalize_criterion(c, i, name, seen) for i, c in enumerate(crits, 1)]
    return {
        "name": name,
        "title": str(data.get("name") or name),
        "description": str(data.get("description") or ""),
        "threshold": _as_threshold(data.get("threshold"), f"rubric {name}"),
        "split": data.get("split"),   # parsed, not applied yet
        "criteria": criteria,
        "params": param_names,
        "path": path,
        "scope": scope,
    }


def read_rubric(name_or_index, workdir):
    """(scope, name, path, raw data) — found and parsed, not validated or filled."""
    scope, name, path = resolve_rubric(name_or_index, workdir)
    return scope, name, path, _read_rubric_file(path)


def load_rubric(name_or_index, workdir, params=None) -> dict:
    """Find (by name, list number or path), parse, fill and validate a rubric.

    A rubric with {{placeholders}} needs params covering all of them; one
    without placeholders ignores params."""
    scope, name, path, raw = read_rubric(name_or_index, workdir)
    if params is None and rubric_params(raw):
        params = {}   # template must be filled before it is judged with
    return normalize_rubric(raw, name, path, scope, params)


def compile_questions(criteria) -> dict:
    """Jev-checked criteria -> the `questions` dict of /v1/systemone."""
    qs = {}
    for c in criteria:
        if c.get("kind") == "regex" or c.get("check") == "regex":
            continue
        q = {"type": c["type"], "instructions": c["instructions"]}
        if c["type"] == "noul":
            if c.get("criteria"):
                q["criteria"] = dict(c["criteria"])
        elif c["type"] == "score":
            q["criteria"] = list(c["levels"])
        else:
            q["criteria"] = dict(c["levels"])
        qs[c["id"]] = q
    return qs


# ── transport ─────────────────────────────────────────────────────────────────

def _post_systemone(host, payload, timeout):
    url = f"{host}/v1/systemone"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        raise JevError(f"timeout after {timeout}s ({url})")
    except requests.exceptions.ConnectionError as e:
        raise JevError(f"cannot connect to {host}: {e.__class__.__name__}")
    except requests.exceptions.RequestException as e:
        raise JevError(f"request failed: {e}")
    try:
        data = json.loads(resp.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = None
    status = resp.status_code
    if status in (400, 404, 413):
        msg = data.get("error") if isinstance(data, dict) else None
        raise JevConfigError(msg or f"HTTP {status} from {url}")
    if status >= 400:
        msg = data.get("error") if isinstance(data, dict) else None
        raise JevError(f"HTTP {status} from {url}" + (f": {msg}" if msg else ""))
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise JevError(f"malformed reply from {url} (no 'answers')")
    return data["answers"]


def call_systemone(state, questions, settings) -> dict:
    """POST questions to /v1/systemone; returns the merged `answers` dict.

    Splits into chunks of settings["batch"] (max 64) questions per request.
    settings["host"] must already be resolved (see resolve_host)."""
    model = (settings.get("model") or "").strip()
    if not model:
        raise JevConfigError("no decision model set — run /jev model list, then /jev model set <name|N>")
    host = (settings.get("host") or "").strip()
    if not host:
        raise JevConfigError("no jev host — set one: /jev param host localhost:11434")
    if not isinstance(questions, dict) or not questions:
        raise JevConfigError("no questions to ask")
    for qid in questions:
        if not _ID_RE.match(str(qid)):
            raise JevConfigError(f"bad question id {qid!r} — must match [A-Za-z0-9_]+")
    try:
        batch = int(settings.get("batch") or MAX_QUESTIONS)
    except (TypeError, ValueError):
        batch = MAX_QUESTIONS
    batch = max(1, min(MAX_QUESTIONS, batch))
    timeout = settings.get("timeout") or DEFAULTS["timeout"]
    items = list(questions.items())
    answers = {}
    for i in range(0, len(items), batch):
        chunk = dict(items[i:i + batch])
        payload = {"model": model, "state": state or "", "questions": chunk,
                   "keep_alive": settings.get("keep_alive", DEFAULTS["keep_alive"])}
        size = len(json.dumps(payload).encode("utf-8"))
        if size > MAX_BODY_BYTES:
            raise JevConfigError(
                f"request is {size // 1024} KiB, /v1/systemone accepts at most "
                f"{MAX_BODY_BYTES // 1024} KiB — judge a smaller text, e.g. "
                f"/loop last split:\"## \" --jev <rubric>")
        answers.update(_post_systemone(host, payload, timeout))
    return answers


# ── verdict ───────────────────────────────────────────────────────────────────

def _thr(c, rubric, settings):
    for v in (c.get("threshold"), rubric.get("threshold"), settings.get("threshold")):
        if v is not None:
            return float(v)
    return DEFAULTS["threshold"]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _eval_regex(c, state, thr):
    matches = list(c["_re"].finditer(state or ""))
    n = len(matches)
    passed = (n == 0) if c["fail_if"] == "match" else (n > 0)
    first = matches[0].group(0)[:60] if matches else None
    return {"value": n, "passed": passed,
            "raw": {"matches": n, "first": first, "fail_if": c["fail_if"]}}


def _eval_jev(c, ans, thr):
    """Turn one answer into (value, passed, raw)."""
    kind = c["kind"]
    if not isinstance(ans, dict):
        return {"value": None, "passed": None, "raw": None, "message": "no answer"}
    probs = ans.get("probabilities") if isinstance(ans.get("probabilities"), dict) else None
    conf = _f(ans.get("confidence"))
    if kind == "noul":
        v = _f(ans.get("noul", ans.get("value")))
        if v is None:
            return {"value": None, "passed": None, "raw": ans, "message": "no noul value"}
        p = v if c.get("pass_when", True) else 1.0 - v
        return {"value": v, "passed": p >= thr,
                "raw": {"p_true": v, "confidence": conf, "pass_when": c.get("pass_when", True)}}
    if kind == "score":
        n = len(c["levels"])
        raw_score = _f(ans.get("score"))
        if probs:
            num = 0.0
            for k, p in probs.items():
                try:
                    num += int(k) * float(p)
                except (TypeError, ValueError):
                    pass
            norm = num / (n - 1)
        elif raw_score is not None:
            norm = raw_score / (n - 1)
        else:
            return {"value": None, "passed": None, "raw": ans, "message": "no score value"}
        norm = max(0.0, min(1.0, norm))
        return {"value": norm, "passed": norm >= thr,
                "raw": {"score": raw_score, "probabilities": probs, "confidence": conf,
                        "legend": ans.get("legend")}}
    # choice
    choice = ans.get("choice")
    choice = _key_str(choice) if choice is not None else None
    pas = c.get("pass")
    raw = {"probabilities": probs, "confidence": conf}
    if not pas:
        return {"value": choice, "passed": True, "raw": raw}
    if probs:
        s = sum(_f(probs.get(k)) or 0.0 for k in pas)
    else:
        s = 1.0 if choice in pas else 0.0
    raw["p_pass"] = s
    return {"value": choice, "passed": s >= thr, "raw": raw}


def run_rubric(state, rubric, settings, *, short_circuit=False) -> dict:
    """Apply a normalized rubric to `state`. Regex criteria run first; with
    short_circuit=True a regex failure skips the jev call entirely.

    JevConfigError propagates. JevError (infra) is recorded in result["error"]:
    on_error="fail" -> rubric fails, "pass" -> verdict from regex criteria only."""
    state = state or ""
    on_error = str(settings.get("on_error", "fail")).lower()
    entries = {}
    for c in rubric["criteria"]:
        entries[c["id"]] = {"id": c["id"], "kind": c["kind"], "value": None,
                            "threshold": _thr(c, rubric, settings), "passed": None,
                            "message": c.get("message", ""), "raw": None}

    regex_c = [c for c in rubric["criteria"] if c["kind"] == "regex"]
    jev_c = [c for c in rubric["criteria"] if c["kind"] != "regex"]
    for c in regex_c:
        entries[c["id"]].update(_eval_regex(c, state, entries[c["id"]]["threshold"]))

    result = {"rubric": rubric["name"], "on_error": on_error}
    if jev_c:
        regex_failed = any(entries[c["id"]]["passed"] is False for c in regex_c)
        if short_circuit and regex_failed:
            for c in jev_c:
                entries[c["id"]]["message"] = "skipped"
        else:
            questions = compile_questions(jev_c)
            try:
                answers = call_systemone(state, questions, settings)
            except JevConfigError:
                raise
            except JevError as e:
                result["error"] = str(e)
                for c in jev_c:
                    entries[c["id"]]["message"] = "jev unavailable"
            else:
                for c in jev_c:
                    e = entries[c["id"]]
                    r = _eval_jev(c, answers.get(c["id"]), e["threshold"])
                    msg = r.pop("message", None)
                    e.update(r)
                    if msg:
                        e["message"] = msg
    crit_list = [entries[c["id"]] for c in rubric["criteria"]]
    passed = not any(e["passed"] is False for e in crit_list)
    if "error" in result and on_error == "fail":
        passed = False
    result["passed"] = passed
    result["criteria"] = crit_list
    return result


def ask(state, question, options, settings) -> dict:
    """Quick question without a rubric file.

    No options -> one noul question; options -> one choice question with the
    option names as keys (description null). passed: noul -> p >= threshold,
    choice -> None (no pass list)."""
    question = (question or "").strip()
    if not question:
        raise JevConfigError("empty question")
    opts = []
    for o in options or []:
        o = str(o).strip()
        if o and o not in opts:
            opts.append(o)
    if opts:
        q = {"type": "choice", "instructions": question, "criteria": {o: None for o in opts}}
    else:
        q = {"type": "noul", "instructions": question}
    answers = call_systemone(state, {"q": q}, settings)
    ans = answers.get("q")
    if not isinstance(ans, dict):
        raise JevError("no answer for the question")
    thr = _f(settings.get("threshold"))
    thr = DEFAULTS["threshold"] if thr is None else thr
    if opts:
        choice = ans.get("choice")
        return {"question": question, "kind": "choice",
                "value": _key_str(choice) if choice is not None else None,
                "probabilities": ans.get("probabilities") or {},
                "confidence": _f(ans.get("confidence")), "passed": None}
    v = _f(ans.get("noul", ans.get("value")))
    if v is None:
        raise JevError("no noul value in answer")
    return {"question": question, "kind": "noul", "value": v,
            "probabilities": {"true": v, "false": round(1.0 - v, 6)},
            "confidence": _f(ans.get("confidence")), "passed": v >= thr}


# ── formatting ────────────────────────────────────────────────────────────────

def _fmt_num(v):
    return f"{v:.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def _detail(c) -> str:
    kind, v, thr, raw = c["kind"], c["value"], c["threshold"], c.get("raw") or {}
    if kind == "regex":
        fail_if = raw.get("fail_if", "match")
        if v:
            return f"matched {v}× (fail_if: {fail_if})"
        return f"no match (fail_if: {fail_if})"
    if v is None:
        return c.get("message") or "no reading"
    if kind == "noul":
        pw = raw.get("pass_when", True)
        s = f"p(true)={_fmt_num(v)}"
        if not pw:
            s += ", pass_when=false"
        return f"{s} (threshold {thr:g})"
    if kind == "score":
        return f"score={_fmt_num(v)} (threshold {thr:g})"
    # choice
    p_pass = raw.get("p_pass")
    if p_pass is None:
        return f"chose {v!r} (informational)"
    return f"chose {v!r}, p(pass)={_fmt_num(p_pass)} (threshold {thr:g})"


def fail_lines(result) -> list:
    """FAIL: lines for a gate — empty list means PASS."""
    name = result.get("rubric", "?")
    lines = []
    if result.get("error") and result.get("on_error", "fail") == "fail":
        lines.append(f"FAIL: {name} — jev unavailable: {result['error']}")
    for c in result.get("criteria", []):
        if c.get("passed") is not False:
            continue
        line = f"FAIL: {name}.{c['id']} — {_detail(c)}"
        if c.get("message") and c["message"] not in ("skipped", "jev unavailable"):
            line += f"; {c['message']}"
        lines.append(line)
    return lines


def format_table(result) -> list:
    """Human-readable table of a run_rubric() result."""
    crits = result.get("criteria", [])
    verdict = "PASS" if result.get("passed") else "FAIL"
    lines = [f"[jev] {result.get('rubric', '?')}: {verdict}"]
    if result.get("error"):
        lines.append(f"  jev unavailable: {result['error']} (on_error={result.get('on_error', 'fail')})")
    w = max([len(c["id"]) for c in crits] + [8])
    for c in crits:
        mark = {True: "PASS", False: "FAIL", None: " -- "}[c.get("passed")]
        detail = _detail(c) if c.get("value") is not None or c["kind"] == "regex" else (c.get("message") or "")
        lines.append(f"  {mark}  {c['id']:<{w}}  {c['kind']:<6}  {detail}")
        if c.get("passed") is False and c.get("message") and c["message"] not in ("skipped", "jev unavailable"):
            lines.append(f"        {'':<{w}}          {c['message']}")
    return lines
