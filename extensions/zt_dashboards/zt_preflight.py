"""ZT add-on: operator pre-flight checks for the /zt page (``GET /zt/preflight``).

Each check answers "is this part of the stack ready for a real test?" with
``OK``, ``WARN`` or ``FAIL``, a one-line summary, the evidence it looked at and,
when it is not OK, how to fix it. A check that crashes is reported as ``FAIL``
with the exception; it never takes the endpoint down.

Read-only by construction. The checks read the process environment, VT's own
config, files under VIBE_TRADING_HOME and the canonical project, and the price
lake (through DuckDB, in memory). The only network use is loopback HTTP to a
local Ollama server with a short timeout; a non-loopback OLLAMA_BASE_URL is not
probed. Nothing is written, no OAuth token is refreshed, and no secret (API key,
OAuth token, account id) is ever returned.

Unlike ``zt_core``, this module may import Vibe-Trading lazily: it runs inside
the VT server process and reads VT's effective configuration.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import ntpath
import os
import shutil
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

SUBSCRIPTION_PROVIDERS = ("ollama", "openai-codex")
DEFAULT_OLLAMA_URL = "http://localhost:11434"
EXPECTED_OLLAMA_CONTEXT = 65536      # bin\start_ollama.ps1 sets OLLAMA_CONTEXT_LENGTH
# VT sends every tool schema on every call and TOKEN_THRESHOLD (its auto-compaction
# trigger) counts only the messages. Measured 2026-09-29 with o200k: 107 local tools
# = 35.2k tokens as JSON, 24.4k in gpt-oss's Harmony rendering; the system prompt is
# 11.9k (inside the messages). The budgets add the ZT MCP tools and leave room for the
# reply (reasoning included), so TOKEN_THRESHOLD <= context - tools - reply.
TOOL_TOKENS = {"gpt-oss": 30000}
TOOL_TOKENS_DEFAULT = 40000          # models whose template renders tools as JSON
REPLY_RESERVE = 6000
MIN_OLLAMA_CONTEXT = 49152
OLLAMA_TIMEOUT_S = 1.5
AUDIT_MAX_AGE = timedelta(hours=1)   # pit-actor-sim simulation gate
INDEX_MAX_AGE = timedelta(hours=36)
DISK_FAIL_GIB = 5.0
DISK_WARN_GIB = 20.0
READ_TABLES = ("dim_source", "dim_security", "dim_security_alias",
               "dim_series", "fact_price_eod", "fact_observation")
SYNC_RECEIPT = "src_sync_receipt.json"
FIX_RESTART = "Restart VT (bin\\stop_vt_ui.ps1, then VT Web.cmd) to apply .env changes."


@dataclass
class Check:
    id: str
    label: str
    status: str
    summary: str
    fix: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "status": self.status,
                "summary": self.summary, "fix": self.fix if self.status != OK else "",
                "detail": self.detail}


@dataclass
class Context:
    """Everything a check reads; tests replace any of it."""

    now: datetime
    environ: Mapping[str, str]
    runtime_root: Path
    project_root: Path | None
    vt_root: Path
    settings: dict[str, Any]
    dotenv: dict[str, str]
    dotenv_path: Path | None
    app_state: Any = None
    http_get: Callable[[str, float], Any] | None = None
    windows: bool = os.name == "nt"
    server_port: int | None = None


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _runtime_root(environ: Mapping[str, str]) -> Path:
    raw = (environ.get("VIBE_TRADING_HOME") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".vibe-trading"


def _project_root(environ: Mapping[str, str]) -> Path | None:
    raw = (environ.get("INVESTMENT_AI_PROJECT_ROOT") or "").strip()
    return Path(raw) if raw else None


def parse_dotenv(text: str) -> dict[str, str]:
    """KEY=VALUE lines as python-dotenv reads them (quotes, ``export``, comments)."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def _dotenv_candidates(runtime_root: Path) -> list[Path]:
    try:
        from src.providers.llm import _ENV_CANDIDATES  # VT's own lookup order

        return [Path(p) for p in _ENV_CANDIDATES]
    except Exception:
        return [Path.home() / ".vibe-trading" / ".env", runtime_root / ".env"]


def _read_dotenv(runtime_root: Path) -> tuple[dict[str, str], Path | None]:
    for candidate in _dotenv_candidates(runtime_root):
        try:
            if candidate.is_file():
                return parse_dotenv(candidate.read_text(encoding="utf-8-sig")), candidate
        except OSError:
            continue
    return {}, None


def _vt_settings(environ: Mapping[str, str]) -> dict[str, Any]:
    """The effective values the running VT process uses (VT's accessor, else env)."""
    try:
        from src.config.accessor import get_env_config

        cfg = get_env_config()
        return {
            "provider": cfg.llm.langchain_provider,
            "model": cfg.llm.langchain_model_name,
            "timeout_seconds": cfg.llm.timeout_seconds,
            "token_threshold": cfg.agent_tuning.token_threshold,
            "scheduler_enabled": bool(cfg.agent_tuning.vibe_trading_enable_scheduler),
            "playbook_dir": cfg.paths.vibe_trading_playbook_dir,
            "source": "vt_config",
        }
    except Exception:
        def flag(name: str) -> bool:
            return (environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")

        def number(name: str, default: int) -> int:
            try:
                return int(str(environ.get(name) or default).strip())
            except ValueError:
                return default

        return {
            "provider": (environ.get("LANGCHAIN_PROVIDER") or "openai"),
            "model": environ.get("LANGCHAIN_MODEL_NAME") or "",
            "timeout_seconds": number("TIMEOUT_SECONDS", 120),
            "token_threshold": number("TOKEN_THRESHOLD", 40000),
            "scheduler_enabled": flag("VIBE_TRADING_ENABLE_SCHEDULER"),
            "playbook_dir": environ.get("VIBE_TRADING_PLAYBOOK_DIR") or "",
            "source": "environment",
        }


def build_context(*, now: datetime | None = None, app_state: Any = None,
                  environ: Mapping[str, str] | None = None,
                  server_port: int | None = None) -> Context:
    env = os.environ if environ is None else environ
    runtime = _runtime_root(env)
    dotenv, dotenv_path = _read_dotenv(runtime)
    return Context(now=now or _utc_now(), environ=env, runtime_root=runtime,
                   project_root=_project_root(env), vt_root=REPO_ROOT.parent,
                   settings=_vt_settings(env), dotenv=dotenv, dotenv_path=dotenv_path,
                   app_state=app_state, server_port=server_port)


def _normalize_provider(value: str) -> str:
    return (value or "").strip().lower().replace("_", "-")


def _restart_note(ctx: Context, key: str, running: str) -> str | None:
    """A note when the .env on disk says something else than the running process."""
    on_disk = ctx.dotenv.get(key)
    if on_disk is None or on_disk.strip() == (running or "").strip():
        return None
    return f"{key} in .env differs from the running server. {FIX_RESTART}"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def check_llm_provider(ctx: Context) -> Check:
    provider = _normalize_provider(ctx.settings.get("provider", ""))
    model = (ctx.settings.get("model") or "").strip()
    detail = {"provider": provider or None, "model": model or None,
              "config_source": ctx.settings.get("source"),
              "dotenv": str(ctx.dotenv_path) if ctx.dotenv_path else None}
    add_lines = ("Run bin\\set_llm_default.ps1 (writes the Ollama default: LANGCHAIN_PROVIDER=ollama, "
                 "LANGCHAIN_MODEL_NAME=gpt-oss:20b, OLLAMA_BASE_URL) and restart VT; after "
                 "bin\\vt_cli.cmd provider login openai-codex, bin\\set_llm_default.ps1 -Provider "
                 "openai-codex switches to the ChatGPT subscription.")
    note = _restart_note(ctx, "LANGCHAIN_PROVIDER", ctx.settings.get("provider", "")) or \
        _restart_note(ctx, "LANGCHAIN_MODEL_NAME", model)
    if note:
        detail["note"] = note
    if not model:
        return Check("llm.provider", "LLM provider", FAIL,
                     "No LLM model configured (LANGCHAIN_MODEL_NAME is empty); the agent cannot run.",
                     note or add_lines, detail)
    if provider not in SUBSCRIPTION_PROVIDERS:
        return Check("llm.provider", "LLM provider", FAIL,
                     f"{provider or 'openai'} · {model}: not a subscription-only provider.",
                     "ZT rule: only local Ollama or the ChatGPT subscription (openai-codex). " + add_lines,
                     detail)
    if note:
        return Check("llm.provider", "LLM provider", WARN, f"{provider} · {model} (restart pending)",
                     note, detail)
    return Check("llm.provider", "LLM provider", OK, f"{provider} · {model}", "", detail)


def _is_loopback(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ollama_root(base_url: str) -> str:
    root = (base_url or DEFAULT_OLLAMA_URL).strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    if "://" not in root:
        root = "http://" + root
    return root


def _default_http_get(url: str, timeout: float) -> Any:
    # Loopback only, and never through a proxy (HTTP(S)_PROXY would reroute 127.0.0.1).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}),
                     timeout=timeout) as response:
        return json.loads(response.read(2_000_000).decode("utf-8"))


def _model_matches(configured: str, name: str) -> bool:
    configured, name = configured.strip().lower(), (name or "").strip().lower()
    if not configured or not name:
        return False
    if ":" not in configured:
        configured += ":latest"
    if ":" not in name:
        name += ":latest"
    return configured == name


def check_ollama(ctx: Context) -> Check:
    provider = _normalize_provider(ctx.settings.get("provider", ""))
    active = provider == "ollama"
    model = (ctx.settings.get("model") or "").strip() if active else "gpt-oss:20b"
    base = ctx.environ.get("OLLAMA_BASE_URL") or ctx.dotenv.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_URL
    root = ollama_root(base)
    detail: dict[str, Any] = {"base_url": root, "active_provider": active, "model": model}
    start = ("Start it with bin\\start_ollama.ps1 (models on E:\\OllamaModels, 64k context) and "
             "register the logon task once with bin\\register_ollama_logon_task.ps1.")
    if not _is_loopback(root):
        return Check("llm.ollama", "Ollama", WARN if active else OK,
                     f"{root} is not a loopback address, so it was not probed.",
                     "Point OLLAMA_BASE_URL at http://127.0.0.1:11434.", detail)
    get = ctx.http_get or _default_http_get
    try:
        version = get(f"{root}/api/version", OLLAMA_TIMEOUT_S)
        tags = get(f"{root}/api/tags", OLLAMA_TIMEOUT_S)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        detail["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        if active:
            return Check("llm.ollama", "Ollama", FAIL, f"Not reachable at {root}.", start, detail)
        return Check("llm.ollama", "Ollama", OK,
                     "Not running (not the active provider; fallback unavailable).", start, detail)
    names = [str(m.get("name") or m.get("model") or "") for m in (tags or {}).get("models", [])
             if isinstance(m, dict)]
    detail.update(version=(version or {}).get("version"), models=names[:20])
    present = any(_model_matches(model, name) for name in names)
    context_length = None
    try:
        running = get(f"{root}/api/ps", OLLAMA_TIMEOUT_S)
        for entry in (running or {}).get("models", []):
            if isinstance(entry, dict) and _model_matches(model, str(entry.get("name") or entry.get("model"))):
                context_length = entry.get("context_length")
    except (OSError, ValueError, urllib.error.URLError):
        pass
    detail["loaded_context_length"] = context_length
    if not active:
        return Check("llm.ollama", "Ollama", OK,
                     f"Reachable (v{detail['version']}), fallback model {'present' if present else 'not pulled'}; "
                     "not the active provider.", "", detail)
    if not present:
        return Check("llm.ollama", "Ollama", FAIL, f"Reachable, but {model} is not pulled.",
                     f"Run: ollama pull {model} (with OLLAMA_MODELS=E:\\OllamaModels). If the model is "
                     "already on E:, another Ollama (the tray app, models on C:) owns port 11434: quit "
                     "it and run bin\\start_ollama.ps1 -Restart.", detail)
    threshold = int(ctx.settings.get("token_threshold") or 0)
    window = int(context_length or EXPECTED_OLLAMA_CONTEXT)
    tools = next((n for family, n in TOOL_TOKENS.items() if model.lower().startswith(family)),
                 TOOL_TOKENS_DEFAULT)
    budget = window - tools - REPLY_RESERVE
    detail.update(token_threshold=threshold, context_window_checked=window,
                  context_basis="loaded model" if context_length else "expected OLLAMA_CONTEXT_LENGTH",
                  tool_schema_tokens_assumed=tools, token_threshold_budget=budget)
    overflow = ("Ollama silently drops the start of an overlong prompt, which holds VT's system prompt "
                "and tool schemas, and the agent then misbehaves without an error.")
    if context_length and int(context_length) < MIN_OLLAMA_CONTEXT:
        return Check("llm.ollama", "Ollama", WARN,
                     f"{model} is loaded with a {context_length}-token context; VT's tool schemas and "
                     f"system prompt alone take about {tools + 12000} tokens.",
                     "Restart Ollama with bin\\start_ollama.ps1 -Restart (OLLAMA_CONTEXT_LENGTH=65536). "
                     + overflow, detail)
    if threshold > budget:
        return Check("llm.ollama", "Ollama", WARN,
                     f"TOKEN_THRESHOLD={threshold} lets the transcript outgrow the {window}-token context "
                     f"(budget {budget} after ~{tools} tokens of tool schemas and the reply).",
                     f"Set TOKEN_THRESHOLD={max(8000, budget // 1000 * 1000)} in home\\.env "
                     "(bin\\set_llm_default.ps1 does). " + overflow + " " + FIX_RESTART, detail)
    return Check("llm.ollama", "Ollama", OK,
                 f"Reachable (v{detail['version']}), {model} present"
                 + (f", loaded with {context_length}-token context." if context_length else "."),
                 "", detail)


def check_openai_codex(ctx: Context) -> Check:
    provider = _normalize_provider(ctx.settings.get("provider", ""))
    active = provider == "openai-codex"
    path = ctx.runtime_root / "auth" / "openai-codex.json"
    detail: dict[str, Any] = {"token_file": str(path), "active_provider": active}
    login = "Run bin\\vt_cli.cmd provider login openai-codex (ChatGPT subscription), then restart VT."
    payload: Any = None
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
    usable = isinstance(payload, dict) and all(
        isinstance(payload.get(key), str) and payload.get(key) for key in ("access", "refresh"))
    detail["token_present"] = bool(usable)
    if usable:
        try:
            expires = datetime.fromtimestamp(int(payload.get("expires")) / 1000, timezone.utc)
            detail["access_expires_at"] = _iso(expires)
            detail["refreshable"] = True
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        return Check("llm.openai_codex", "ChatGPT login (openai-codex)", OK,
                     "Login present" + ("" if active else " (not the active provider)") + ".", "", detail)
    if path.exists():
        return Check("llm.openai_codex", "ChatGPT login (openai-codex)", FAIL if active else WARN,
                     "Token file exists but is unreadable.", login, detail)
    if active:
        return Check("llm.openai_codex", "ChatGPT login (openai-codex)", FAIL,
                     "openai-codex is the provider but no ChatGPT login is stored.", login, detail)
    return Check("llm.openai_codex", "ChatGPT login (openai-codex)", OK,
                 "Not logged in (optional; Ollama is the default).", login, detail)


# ---------------------------------------------------------------------------
# PIT receipts and the price lake
# ---------------------------------------------------------------------------


def lake_root(ctx: Context) -> Path | None:
    raw = (ctx.environ.get("PITDB_LAKE") or "").strip()
    if raw:
        return Path(raw)
    if ctx.project_root is None:
        return None
    return ctx.project_root / "implementation" / "pit_warehouse" / "lake"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _signature_drift(ctx: Context, signature: Any, tables: tuple[str, ...] | None) -> list[str]:
    """Tables whose (size, mtime_ns) differ from a receipt's lake signature."""
    root = lake_root(ctx)
    if not isinstance(signature, dict) or root is None:
        return ["(no lake signature)"]
    drift = []
    recorded_root = str(signature.get("lake_root") or "")
    try:
        current_root = str(root.resolve())
    except OSError:
        current_root = str(root)
    if _norm(recorded_root) != _norm(current_root):
        drift.append("lake_root")
    files = signature.get("files") if isinstance(signature.get("files"), dict) else {}
    for table in (tables or tuple(files)):
        path = root / table / "data.parquet"
        try:
            stat = path.stat()
            current: Any = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            current = None
        if files.get(table) != current:
            drift.append(table)
    return drift


def _read_receipt(path: Path) -> tuple[dict | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        data = path.read_bytes()
        payload = json.loads(data.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, f"unreadable ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return None, "not a JSON object"
    payload["_sha256"] = hashlib.sha256(data).hexdigest()
    return payload, None


def _age_hours(ctx: Context, raw: Any) -> tuple[datetime | None, float | None]:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp, round((ctx.now - stamp).total_seconds() / 3600, 2)


PIT_ENV = ("the VT environment (vt_env.ps1 dot-sourced, PITDB_INDEX=E:\\pitdb\\pit.duckdb, "
           "INVESTMENT_AI_PROJECT_ROOT set)")


def check_audit_receipt(ctx: Context) -> Check:
    path = ctx.runtime_root / "pit_audit_receipt.json"
    refresh = ("Before a simulation or a formation-mode pitdb backtest run: venv\\Scripts\\python.exe "
               f"src\\extensions\\pit_actor_sim\\server.py --refresh-audit, in {PIT_ENV}.")
    receipt, error = _read_receipt(path)
    detail: dict[str, Any] = {"path": str(path)}
    if receipt is None:
        return Check("pit.audit_receipt", "PIT audit receipt", WARN,
                     f"Receipt {error or 'missing'}; simulations and formation-mode backtests refuse to run.",
                     refresh, detail)
    stamp, age = _age_hours(ctx, receipt.get("audited_at_utc"))
    drift = _signature_drift(ctx, receipt.get("signature"), None)
    detail.update(audited_at=_iso(stamp), age_hours=age, receipt_status=receipt.get("status"),
                  checks=receipt.get("checks"), checks_passed=receipt.get("checks_passed"),
                  lake_changed=drift, sha256=receipt["_sha256"])
    if receipt.get("status") != "PASS":
        return Check("pit.audit_receipt", "PIT audit receipt", FAIL,
                     f"Last audit status is {receipt.get('status')!r}, not PASS.", refresh, detail)
    if stamp is None or age is None or age < -0.1:
        return Check("pit.audit_receipt", "PIT audit receipt", WARN,
                     "Audit time is missing or in the future.", refresh, detail)
    if drift:
        return Check("pit.audit_receipt", "PIT audit receipt", WARN,
                     f"PASS {age:g} h ago, but the lake changed since ({', '.join(drift[:4])}).",
                     refresh, detail)
    if ctx.now - stamp > AUDIT_MAX_AGE:
        return Check("pit.audit_receipt", "PIT audit receipt", WARN,
                     f"PASS, {age:g} h old: expired for simulations (1 h window), lake unchanged.",
                     refresh, detail)
    return Check("pit.audit_receipt", "PIT audit receipt", OK,
                 f"PASS, {age:g} h old, bound to the current lake ({receipt.get('checks')} checks).",
                 "", detail)


def check_index_receipt(ctx: Context) -> Check:
    path = ctx.runtime_root / "pit_index_receipt.json"
    refresh = ("Run venv\\Scripts\\python.exe src\\extensions\\pit_actor_sim\\server.py "
               f"--refresh-index in {PIT_ENV}.")
    receipt, error = _read_receipt(path)
    index = (ctx.environ.get("PITDB_INDEX") or "").strip()
    detail: dict[str, Any] = {"path": str(path), "pitdb_index": index or None}
    if index and index.lower() not in ("memory", ":memory:", "none", "local", "file", "disk"):
        detail["index_exists"] = Path(index).is_file()
    if receipt is None:
        return Check("pit.index_receipt", "PIT index receipt", WARN,
                     f"Receipt {error or 'missing'}; the pit-actor-sim tools refuse to read.", refresh, detail)
    stamp, age = _age_hours(ctx, receipt.get("refreshed_at_utc"))
    drift = _signature_drift(ctx, receipt.get("signature"), READ_TABLES)
    detail.update(refreshed_at=_iso(stamp), age_hours=age, rows=receipt.get("rows"),
                  lake_changed=drift, sha256=receipt["_sha256"])
    if detail.get("index_exists") is False:
        return Check("pit.index_receipt", "PIT index receipt", WARN,
                     f"The E: index {index} is missing.", refresh, detail)
    if drift:
        return Check("pit.index_receipt", "PIT index receipt", WARN,
                     f"Index is stale: the lake changed since the rebuild ({', '.join(drift[:4])}).",
                     refresh, detail)
    if stamp is None or age is None or ctx.now - stamp > INDEX_MAX_AGE:
        return Check("pit.index_receipt", "PIT index receipt", WARN,
                     "Index matches the lake but was rebuilt "
                     + (f"{age:g} h ago." if age is not None else "at an unknown time."),
                     refresh, detail)
    return Check("pit.index_receipt", "PIT index receipt", OK,
                 f"Mirrors the lake, rebuilt {age:g} h ago ({receipt.get('rows')} rows).", "", detail)


# US equity sessions (NYSE rules), so "latest price date" can be compared with
# the last session that has closed. Full-day closures only.
_EASTERN = None


def _eastern():
    global _EASTERN
    if _EASTERN is None:
        try:
            from zoneinfo import ZoneInfo

            _EASTERN = ZoneInfo("America/New_York")
        except Exception:  # pragma: no cover - host without tz database
            _EASTERN = timezone(timedelta(hours=-5))
    return _EASTERN


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date, saturday_to_friday: bool = True) -> date | None:
    if day.weekday() == 5:
        return day - timedelta(days=1) if saturday_to_friday else None
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


_SPECIAL_CLOSURES = frozenset({date(2018, 12, 5), date(2025, 1, 9)})


def us_market_holidays(year: int) -> frozenset[date]:
    days = {
        _observed(date(year, 1, 1), saturday_to_friday=False),
        _nth_weekday(year, 1, 0, 3),     # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),     # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),       # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),     # Labor Day
        _nth_weekday(year, 11, 3, 4),    # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    days |= {d for d in _SPECIAL_CLOSURES if d.year == year}
    return frozenset(d for d in days if d is not None)


def is_session(day: date) -> bool:
    return day.weekday() < 5 and day not in us_market_holidays(day.year)


def previous_session(day: date) -> date:
    day -= timedelta(days=1)
    while not is_session(day):
        day -= timedelta(days=1)
    return day


def last_closed_session(now: datetime) -> date:
    local = now.astimezone(_eastern())
    day = local.date()
    if is_session(day) and local.time() >= time(16, 0):
        return day
    return previous_session(day)


def due_session(now: datetime) -> date:
    """Latest session whose EOD prices the 07:40 ET pitdb run should have landed (by 09:00 ET next day)."""
    local = now.astimezone(_eastern())
    day = last_closed_session(now)
    while local < datetime.combine(day + timedelta(days=1), time(9, 0), _eastern()):
        day = previous_session(day)
    return day


def sessions_between(after: date, until: date) -> int:
    """Sessions in (after, until]."""
    count, day = 0, after + timedelta(days=1)
    while day <= until and count < 400:
        count += is_session(day)
        day += timedelta(days=1)
    return count


_PRICE_CACHE: dict[tuple, tuple] = {}
_PRICE_LOCK = threading.Lock()


def latest_price_date(path: Path) -> tuple[date | None, int]:
    """max(event_date) and row count of fact_price_eod, cached per file version."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _PRICE_LOCK:
        if key in _PRICE_CACHE:
            return _PRICE_CACHE[key]
    import duckdb

    con = duckdb.connect()
    try:
        latest, rows = con.execute(
            "SELECT max(event_date), count(*) FROM read_parquet(?)", [str(path)]).fetchone()
    finally:
        con.close()
    if isinstance(latest, datetime):
        latest = latest.date()
    result = (latest, int(rows or 0))
    with _PRICE_LOCK:
        _PRICE_CACHE.clear()
        _PRICE_CACHE[key] = result
    return result


def check_price_lake(ctx: Context) -> Check:
    root = lake_root(ctx)
    run_daily = ("Check the pitdb daily run (07:40 ET): implementation\\pit_warehouse\\logs\\pitdb_daily.log "
                 "and the pit-health section of the daily brief.")
    if root is None:
        return Check("pit.price_lake", "Latest price in the lake", FAIL,
                     "INVESTMENT_AI_PROJECT_ROOT is not set, so the lake cannot be found.",
                     "Start VT through bin\\start_vt_ui.ps1 or VT Web.cmd (vt_env.ps1 sets it).")
    path = root / "fact_price_eod" / "data.parquet"
    detail: dict[str, Any] = {"path": str(path)}
    if not path.is_file():
        return Check("pit.price_lake", "Latest price in the lake", FAIL,
                     "fact_price_eod/data.parquet is missing.", run_daily, detail)
    latest, rows = latest_price_date(path)
    closed = last_closed_session(ctx.now)
    due = due_session(ctx.now)
    detail.update(latest_event_date=latest.isoformat() if latest else None, rows=rows,
                  last_closed_session=closed.isoformat(), due_session=due.isoformat(),
                  rule="a session's EOD prices are due by 09:00 ET the next day (pitdb daily run 07:40 ET)")
    if latest is None:
        return Check("pit.price_lake", "Latest price in the lake", FAIL, "The price table is empty.",
                     run_daily, detail)
    if latest >= due:
        note = "" if latest >= closed else f"; {closed} closed and lands with the next daily run"
        return Check("pit.price_lake", "Latest price in the lake", OK,
                     f"Latest price {latest} (due session {due}){note}.", "", detail)
    behind = sessions_between(latest, due)
    detail["sessions_behind"] = behind
    return Check("pit.price_lake", "Latest price in the lake", FAIL if behind >= 3 else WARN,
                 f"Latest price {latest} is {behind} session(s) behind {due}.", run_daily, detail)


# ---------------------------------------------------------------------------
# Scheduler, kill switch, order approval
# ---------------------------------------------------------------------------


WEB_PORT = 8899


def _web_server_also_running(ctx: Context) -> bool:
    """True when this is not the 8899 web server and a VT server answers on 8899."""
    if not ctx.server_port or ctx.server_port == WEB_PORT:
        return False
    try:
        payload = (ctx.http_get or _default_http_get)(f"http://127.0.0.1:{WEB_PORT}/health",
                                                      OLLAMA_TIMEOUT_S)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return isinstance(payload, dict) and "status" in payload


def check_scheduler(ctx: Context) -> Check:
    enabled = bool(ctx.settings.get("scheduler_enabled"))
    path = ctx.runtime_root / "scheduled_research" / "scheduled_research_jobs.json"
    detail: dict[str, Any] = {"enabled": enabled, "store": str(path)}
    jobs: list[dict] = []
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
        except (OSError, ValueError, AttributeError) as exc:
            detail["error"] = f"{type(exc).__name__}"
    active = [j for j in jobs if j.get("status") in ("pending", "running")]
    failing = [j for j in active if int(j.get("consecutive_failures") or 0) > 0]
    detail.update(jobs=len(jobs), active=len(active), failing=len(failing),
                  active_titles=[str(j.get("title") or j.get("playbook_slug") or j.get("id"))[:60]
                                 for j in active[:10]])
    note = _restart_note(ctx, "VIBE_TRADING_ENABLE_SCHEDULER", "true" if enabled else "false")
    enable = ("Set VIBE_TRADING_ENABLE_SCHEDULER=true in home\\.env when the monitors should fire "
              "(a ZT decision), then restart VT.")
    if detail.get("error"):
        return Check("scheduler", "Scheduler", WARN, "The job store could not be read.",
                     "Open the Scheduled page; VT quarantines a corrupt store when it next loads it.",
                     detail)
    if not enabled:
        if active:
            return Check("scheduler", "Scheduler", WARN,
                         f"Off: {len(active)} active job(s) will not fire.", note or enable, detail)
        return Check("scheduler", "Scheduler", OK, "Off; no active scheduled jobs.", enable, detail)
    if _web_server_also_running(ctx):
        # VT's scheduler has no cross-process lock: two servers on one home both fire every job.
        detail["also_running"] = f"127.0.0.1:{WEB_PORT}"
        return Check("scheduler", "Scheduler", WARN,
                     f"On here and in the web server on {WEB_PORT}, which shares this runtime: "
                     "every scheduled job would fire twice.",
                     "Run one of the two: close the desktop app, or stop the web server with "
                     "bin\\stop_vt_ui.ps1.", detail)
    if failing:
        return Check("scheduler", "Scheduler", WARN,
                     f"On, {len(active)} active job(s); {len(failing)} failing.",
                     "Open the Scheduled page for the last error of each failing job.", detail)
    return Check("scheduler", "Scheduler", OK, f"On, {len(active)} active job(s).", "", detail)


def check_halt(ctx: Context) -> Check:
    live = ctx.runtime_root / "live"
    detail: dict[str, Any] = {"path": str(live / "HALT")}
    halted = []
    if (live / "HALT").exists():
        halted.append("*")
    if live.is_dir():
        for folder in sorted(live.iterdir()):
            if folder.is_dir() and (folder / "HALT").exists():
                halted.append(folder.name)
    detail["halted"] = halted
    if not halted:
        return Check("live.halt", "Live kill switch (live\\HALT)", OK, "Not tripped.", "", detail)
    meta: Any = {}
    target = live / "HALT" if "*" in halted else live / halted[0] / "HALT"
    try:
        meta = json.loads(target.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        meta = {}
    if isinstance(meta, dict):
        detail.update(tripped_at=meta.get("tripped_at"), by=meta.get("by"),
                      reason=str(meta.get("reason") or "")[:160])
    scope = "all brokers" if "*" in halted else ", ".join(halted)
    return Check("live.halt", "Live kill switch (live\\HALT)", WARN,
                 f"Tripped for {scope}: live orders are blocked.",
                 "Intended? Leave it. To trade again, clear it from the Portfolio page or with /resume "
                 "in bin\\vt_cli.cmd; every order still needs your approval.", detail)


def check_order_approval(ctx: Context) -> Check:
    running = (ctx.environ.get("VIBE_ORDER_APPROVAL") or "").strip()
    detail: dict[str, Any] = {"value": running or None, "dotenv_value": ctx.dotenv.get("VIBE_ORDER_APPROVAL")}
    fix = "Set VIBE_ORDER_APPROVAL=required in home\\.env, then restart VT."
    note = _restart_note(ctx, "VIBE_ORDER_APPROVAL", running)
    if running.lower() == "required":
        if note:
            return Check("orders.approval", "Order approval (VIBE_ORDER_APPROVAL)", WARN,
                         "required (the .env now says otherwise)", note, detail)
        return Check("orders.approval", "Order approval (VIBE_ORDER_APPROVAL)", OK,
                     "required: every order waits for your approval.", "", detail)
    if not running:
        return Check("orders.approval", "Order approval (VIBE_ORDER_APPROVAL)", WARN,
                     "Not set in this server's environment; the approval hook's default applies.",
                     note or fix, detail)
    return Check("orders.approval", "Order approval (VIBE_ORDER_APPROVAL)", FAIL,
                 f"{running!r}: ZT rule is that a human approves every order.", fix, detail)


# ---------------------------------------------------------------------------
# MCP servers, playbooks, disk, version, extension routes
# ---------------------------------------------------------------------------


def _agent_config_path(runtime_root: Path) -> Path | None:
    for name in ("agent.json", "agent.yaml", "agent.yml"):
        if (runtime_root / name).is_file():
            return runtime_root / name
    return None


def check_mcp_servers(ctx: Context) -> Check:
    path = _agent_config_path(ctx.runtime_root)
    detail: dict[str, Any] = {"path": str(path or ctx.runtime_root / "agent.json")}
    if path is None:
        return Check("mcp.servers", "MCP servers (agent.json)", WARN, "No agent.json: no MCP servers.",
                     "Add the zt-dashboards, pit-actor-sim and other ZT servers to home\\agent.json "
                     "(see INTEGRATION.md).", detail)
    try:
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix == ".json":
            raw = json.loads(text)
        else:
            import yaml

            raw = yaml.safe_load(text)
    except Exception as exc:  # parse errors of any kind are the finding
        detail["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return Check("mcp.servers", "MCP servers (agent.json)", FAIL,
                     f"{path.name} does not parse; VT falls back to no MCP servers.",
                     f"Fix the syntax in {path}, then restart VT.", detail)
    servers = {}
    if isinstance(raw, dict):
        servers = raw.get("mcpServers") or raw.get("mcp_servers") or {}
    rows, missing = [], []
    for name, spec in (servers.items() if isinstance(servers, dict) else []):
        if not isinstance(spec, dict):
            continue
        tools = spec.get("enabledTools", spec.get("enabled_tools", ["*"]))
        tools = tools if isinstance(tools, list) else ["*"]
        command = str(spec.get("command") or "")
        transport = spec.get("type") or ("stdio" if command else "http")
        row = {"name": name, "transport": transport,
               "enabled_tools": "all" if "*" in tools else len(tools)}
        paths = [command] + [str(a) for a in spec.get("args") or [] if str(a).lower().endswith(".py")]
        absent = [p for p in paths if p and (ntpath.isabs(p) or os.path.isabs(p)) and not Path(p).exists()]
        if absent:
            row["missing_files"] = absent
            missing.append(name)
        rows.append(row)
    detail.update(servers=rows, count=len(rows))
    if not rows:
        return Check("mcp.servers", "MCP servers (agent.json)", WARN,
                     f"{path.name} lists no MCP servers.", "Add the ZT servers to mcpServers.", detail)
    listing = ", ".join(f"{r['name']} ({r['enabled_tools']})" for r in rows)
    if missing:
        return Check("mcp.servers", "MCP servers (agent.json)", WARN,
                     f"{len(rows)} server(s); command or script missing for {', '.join(missing)}.",
                     "Fix the paths in agent.json (the runtime copy lives under src\\), then restart VT.",
                     detail)
    return Check("mcp.servers", "MCP servers (agent.json)", OK, f"{len(rows)} server(s): {listing}.",
                 "", detail)


def check_playbooks(ctx: Context) -> Check:
    configured = str(ctx.settings.get("playbook_dir") or "").strip()
    detail: dict[str, Any] = {"dir": configured or None}
    setup = ("Set VIBE_TRADING_PLAYBOOK_DIR='G:\\My Drive\\work\\Investment-AI-Drive-Research\\vt_addons"
             "\\playbooks' (single quotes) in home\\.env, then restart VT.")
    if not configured:
        return Check("playbooks", "Playbook directory", WARN,
                     "VIBE_TRADING_PLAYBOOK_DIR is not set: only VT's bundled playbooks are available.",
                     setup, detail)
    folder = Path(configured).expanduser()
    if not folder.is_dir():
        return Check("playbooks", "Playbook directory", FAIL, f"{folder} does not exist or is not a folder.",
                     setup, detail)
    try:
        files = sorted(p.name for p in folder.glob("*.md"))
    except OSError as exc:
        return Check("playbooks", "Playbook directory", FAIL, f"{folder} is not readable ({exc}).",
                     setup, detail)
    detail["files"] = files
    try:
        from src.scheduled_research.playbooks import list_playbooks

        detail["loadable"] = [p.slug for p in list_playbooks(folder)]
    except ImportError:
        pass
    except Exception as exc:  # PlaybookError names the broken file
        detail["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return Check("playbooks", "Playbook directory", FAIL, "A playbook in the folder does not parse.",
                     "Fix the file named in the details; VT refuses to list playbooks until then.", detail)
    if not files:
        return Check("playbooks", "Playbook directory", WARN, f"{folder} holds no .md playbooks.",
                     "Copy the ZT playbooks (vt_addons\\playbooks) there.", detail)
    return Check("playbooks", "Playbook directory", OK, f"{len(files)} playbook(s) readable.", "", detail)


def _drive(path: Path) -> str:
    return ntpath.splitdrive(str(path))[0].upper()


def check_disk(ctx: Context) -> Check:
    detail: dict[str, Any] = {"path": str(ctx.runtime_root)}
    target = ctx.runtime_root if ctx.runtime_root.exists() else ctx.vt_root
    if ctx.windows:
        drive = _drive(ctx.runtime_root)
        detail["drive"] = drive
        system = (ctx.environ.get("SystemDrive") or "C:").upper()
        if drive in ("C:", "D:", system):
            return Check("disk.runtime", "Runtime disk space", FAIL,
                         f"VIBE_TRADING_HOME is on {drive}: ZT rule is nothing on C: or D:.",
                         "Start VT through the E: launchers (vt_env.ps1 sets VIBE_TRADING_HOME).", detail)
        anchor = drive + "\\" if drive else str(target)
    else:
        anchor = str(target)
    usage = shutil.disk_usage(anchor)
    free = round(usage.free / 1024 ** 3, 1)
    detail.update(free_gib=free, total_gib=round(usage.total / 1024 ** 3, 1))
    label = detail.get("drive") or anchor
    fix = "Free space on E: (old logs, run folders, caches under E:\\Caches) before long runs."
    if free < DISK_FAIL_GIB:
        return Check("disk.runtime", "Runtime disk space", FAIL, f"{free} GiB free on {label}.", fix, detail)
    if free < DISK_WARN_GIB:
        return Check("disk.runtime", "Runtime disk space", WARN, f"{free} GiB free on {label}.", fix, detail)
    return Check("disk.runtime", "Runtime disk space", OK, f"{free} GiB free on {label}.", "", detail)


def _vt_version() -> str:
    host = sys.modules.get("api_server")
    if host is not None and getattr(host, "APP_VERSION", None):
        return str(host.APP_VERSION)
    try:
        from importlib.metadata import version

        return version("vibe-trading-ai")
    except Exception:
        pass
    try:
        import tomllib

        return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except Exception:
        return "unknown"


def sync_receipt_candidates(ctx: Context) -> list[Path]:
    override = (ctx.environ.get("ZT_SRC_SYNC_RECEIPT") or "").strip()
    if override:
        return [Path(override)]
    return [ctx.vt_root / SYNC_RECEIPT, ctx.vt_root / "logs" / SYNC_RECEIPT,
            REPO_ROOT / SYNC_RECEIPT, ctx.runtime_root / SYNC_RECEIPT]


def _pick(payload: dict, *needles: str) -> Any:
    for key, value in payload.items():
        lowered = str(key).lower()
        if any(needle in lowered for needle in needles) and isinstance(value, (str, int, float)):
            return value
    return None


def check_version(ctx: Context) -> Check:
    version = _vt_version()
    detail: dict[str, Any] = {"vt_version": version}
    for path in sync_receipt_candidates(ctx):
        receipt, error = _read_receipt(path)
        if receipt is None:
            continue
        commit = _pick(receipt, "commit", "head", "sha")
        synced = _pick(receipt, "synced", "completed", "finished", "time", "at_utc")
        detail.update(receipt=str(path), commit=str(commit)[:40] if commit else None,
                      synced_at=synced, branch=_pick(receipt, "branch", "ref"))
        short = str(commit)[:12] if commit else "commit not recorded"
        return Check("vt.version", "VT version", OK,
                     f"v{version}, runtime copy of {short}" + (f", synced {synced}" if synced else "") + ".",
                     "", detail)
    detail["searched"] = [str(p) for p in sync_receipt_candidates(ctx)]
    return Check("vt.version", "VT version", WARN,
                 f"v{version}; no {SYNC_RECEIPT}, so the fork commit of this runtime copy is unknown.",
                 "Run bin\\sync_src.ps1 (it records the synced commit).", detail)


def check_extension_routes(ctx: Context) -> Check:
    results = getattr(ctx.app_state, "vt_extension_routes", None) if ctx.app_state is not None else None
    if results is None:
        return Check("routes.extensions", "Extension routes", WARN,
                     "This server was not started through launch_api.py / vt-zt; extension routes "
                     "other than the ZT dashboards may be missing.",
                     "Start the web UI with VT Web.cmd and the desktop with VT Desktop.cmd.")
    detail = {"extensions": results}
    failed = [r["name"] for r in results if r.get("status") == "failed"]
    registered = [f"{r['name']} ({len(r.get('routes') or [])})" for r in results
                  if r.get("status") == "registered"]
    if failed:
        return Check("routes.extensions", "Extension routes", FAIL,
                     f"Failed to load: {', '.join(failed)}; registered: {', '.join(registered) or 'none'}.",
                     "See the [vt-extensions] lines in the server log (logs\\vt_server_*.err.log).", detail)
    return Check("routes.extensions", "Extension routes", OK,
                 f"Registered: {', '.join(registered) or 'none'}.", "", detail)


CHECKS: tuple[Callable[[Context], Check], ...] = (
    check_llm_provider,
    check_ollama,
    check_openai_codex,
    check_audit_receipt,
    check_index_receipt,
    check_price_lake,
    check_scheduler,
    check_halt,
    check_order_approval,
    check_mcp_servers,
    check_playbooks,
    check_disk,
    check_version,
    check_extension_routes,
)


def run_preflight(ctx: Context | None = None, *, app_state: Any = None,
                  now: datetime | None = None, server_port: int | None = None) -> dict[str, Any]:
    ctx = ctx or build_context(now=now, app_state=app_state, server_port=server_port)
    results = []
    for check in CHECKS:
        try:
            outcome = check(ctx)
        except Exception as exc:  # a crashing check is a finding, not an outage
            name = check.__name__.removeprefix("check_")
            outcome = Check(name, name.replace("_", " ").capitalize(), FAIL,
                            f"The check crashed: {type(exc).__name__}: {str(exc)[:200]}",
                            "Report this; the other checks are unaffected.")
        results.append(outcome.as_dict())
    counts = {status: sum(1 for r in results if r["status"] == status) for status in (OK, WARN, FAIL)}
    overall = max((r["status"] for r in results), key=_RANK.__getitem__, default=OK)
    return {
        "tool": "zt_preflight",
        "generated_at": _iso(ctx.now),
        "overall": overall,
        "counts": counts,
        "checks": results,
        "note": "Read-only checks of this server's configuration; no secret is returned.",
    }
