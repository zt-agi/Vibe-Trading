"""ZT add-on: read-only readers over ZT's Investment-AI-Drive-Research project.

Shared by the stdio MCP server (``server.py``) and the Web UI routes
(``api_routes.py``). Nothing in this module writes a file, opens a network
connection or imports Vibe-Trading. Every reader resolves paths inside the
canonical project folder named by ``INVESTMENT_AI_PROJECT_ROOT`` and returns a
plain JSON-able envelope:

    tool          reader name
    as_of         UTC instant the data describes (None when it cannot be told)
    as_of_basis   where as_of came from: a content field, or the file mtime
    retrieved_at  UTC instant of this read
    source_path   project-relative path of the primary source (file or folder)
    sha256        digest of exactly the bytes that were parsed (a manifest
                  digest over "path<TAB>sha256" lines when the source is a folder)
    pit_label     NON_PIT: none of these readers go through pitdb's as-of macros
    status        FRESH | STALE | MISSING, from as_of against a declared max age
    status_rule   the rule that produced status
    authority     monitoring context only; never an investment signal
    data          reader-specific payload

Portfolio exports are reduced to coverage and status: account numbers,
quantities and values never leave this module.
"""
from __future__ import annotations

import ast
import csv
import functools
import hashlib
import html
import io
import json
import math
import os
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path, PurePosixPath
from stat import S_ISREG
from typing import Any, Iterable

PROJECT_ENV = "INVESTMENT_AI_PROJECT_ROOT"
PROJECT_NAME = "Investment-AI-Drive-Research"
NON_PIT = "NON_PIT"
AUTHORITY = "READ_ONLY_MONITORING_CONTEXT_NOT_AN_INVESTMENT_SIGNAL"
FRESH, STALE, MISSING = "FRESH", "STALE", "MISSING"

# Declared freshness windows. A source is FRESH when its as_of is no older
# than this at read time. The windows follow each producer's own cadence.
MAX_AGE: dict[str, timedelta] = {
    "daily_snapshot": timedelta(hours=36),      # alpha_monitor exports, daily ~08:30 ET
    "source_families": timedelta(days=35),      # top-25 backfill coverage ledger
    "signal_state": timedelta(hours=36),        # ASM phase 1 daily state read
    "lane_status": timedelta(hours=36),         # ASM phase 2 lane board
    "test_ledger": timedelta(hours=36),         # appended by every monitor run
    "promotion_board": timedelta(days=8),       # ASM phase 3 weekly board
    "pit_inventory": timedelta(hours=36),       # pitdb daily run, 07:40 ET
    "world_model": timedelta(days=35),
    "project_reports": timedelta(days=35),      # PROJECT_HUB.json curation
}
AUDIT_RECEIPT_MAX_AGE = timedelta(hours=1)      # pit-actor-sim simulation gate
FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_READ_BYTES = 25 * 1024 * 1024
LOG_TAIL_BYTES = 256 * 1024
MAX_LIST_ROWS = 200

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HTML_SUFFIXES = (".html", ".htm")


class ProjectRootError(RuntimeError):
    """The project root is unset, missing, or not the canonical folder."""


class ReportNotFound(LookupError):
    """A report id is not whitelisted, or its file is missing."""


class UnreadableSource(ValueError):
    """A source file exists but could not be parsed."""

    def __init__(self, blob: "Blob", exc: Exception):
        super().__init__(f"{blob.rel} could not be parsed ({type(exc).__name__})")
        self.blob = blob


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _eastern():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # pragma: no cover - host without tz database
        return None


def today_et(now: datetime) -> date:
    zone = _eastern()
    return now.astimezone(zone).date() if zone else now.astimezone(timezone.utc).date()


def parse_instant(raw: Any, naive_is_utc: bool = True) -> tuple[datetime | None, str]:
    """Parse an ISO-like timestamp to UTC; returns (instant, precision).

    Date-only values mean midnight America/New_York (the producers' zone).
    Naive timestamps are the pitdb convention and are read as UTC.
    """
    text = str(raw or "").strip()
    if not text:
        return None, ""
    try:
        if _DATE_RE.match(text):
            zone = _eastern() or timezone.utc
            day = date.fromisoformat(text)
            return datetime.combine(day, time(0), zone).astimezone(timezone.utc), "date"
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, ""
    if value.tzinfo is None:
        if not naive_is_utc:
            return None, ""
        return value.replace(tzinfo=timezone.utc), "naive_utc"
    return value.astimezone(timezone.utc), "instant"


def _latest_instant(values: Iterable[Any]) -> tuple[datetime | None, str]:
    best: tuple[datetime | None, str] = (None, "")
    for value in values:
        instant, precision = parse_instant(value)
        if instant is not None and (best[0] is None or instant > best[0]):
            best = (instant, precision)
    return best


def _human(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:g}h" if hours < 72 else f"{hours / 24:g}d"


# ---------------------------------------------------------------------------
# Paths and files
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """Resolve and validate INVESTMENT_AI_PROJECT_ROOT (read-only use)."""
    raw = os.environ.get(PROJECT_ENV, "").strip()
    if not raw:
        raise ProjectRootError(f"{PROJECT_ENV} is required")
    try:
        root = Path(raw).resolve(strict=True)
    except OSError as exc:
        raise ProjectRootError(f"{PROJECT_ENV} does not exist: {raw}") from exc
    if not root.is_dir():
        raise ProjectRootError(f"{PROJECT_ENV} is not a folder: {raw}")
    if root.name != PROJECT_NAME or root.parent.name != "work":
        raise ProjectRootError(
            f"{PROJECT_ENV} must be the canonical work/{PROJECT_NAME} folder; got {raw}")
    return root


def _clean_parts(rel: str) -> tuple[str, ...]:
    text = str(rel).replace("\\", "/").strip()
    pure = PurePosixPath(text)
    if not text or pure.is_absolute():
        raise ValueError(f"not a project-relative path: {rel!r}")
    parts = tuple(part for part in pure.parts if part != ".")
    if not parts or any(part == ".." or ":" in part or "\x00" in part for part in parts):
        raise ValueError(f"not a project-relative path: {rel!r}")
    return parts


def inside(root: Path, rel: str) -> Path:
    """Join a project-relative path and refuse anything that escapes the root."""
    resolved = root.joinpath(*_clean_parts(rel)).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes the project root: {rel!r}")
    return resolved


def rel_of(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


class Blob:
    """Bytes read once; the digest always matches what was parsed."""

    __slots__ = ("rel", "path", "data", "mtime", "sha256")

    def __init__(self, rel: str, path: Path, data: bytes, mtime: datetime):
        self.rel, self.path, self.data, self.mtime = rel, path, data, mtime
        self.sha256 = hashlib.sha256(data).hexdigest()

    def text(self) -> str:
        return self.data.decode("utf-8-sig", errors="replace")

    def info(self) -> dict[str, Any]:
        return {"path": self.rel, "exists": True, "bytes": len(self.data),
                "mtime_utc": iso(self.mtime), "sha256": self.sha256}


def read_blob(root: Path, rel: str, max_bytes: int = MAX_READ_BYTES) -> Blob | None:
    path = inside(root, rel)
    if not path.is_file():
        return None
    stat = path.stat()
    if stat.st_size > max_bytes:
        raise ValueError(f"{rel} is larger than {max_bytes} bytes")
    return Blob(rel, path, path.read_bytes(), datetime.fromtimestamp(stat.st_mtime, timezone.utc))


def read_tail(root: Path, rel: str, limit: int = LOG_TAIL_BYTES) -> tuple[Blob | None, bool]:
    """Read the last ``limit`` bytes of a log; returns (blob, truncated)."""
    path = inside(root, rel)
    if not path.is_file():
        return None, False
    stat = path.stat()
    with path.open("rb") as handle:
        if stat.st_size > limit:
            handle.seek(stat.st_size - limit)
            data = handle.read()
            data = data[data.find(b"\n") + 1:]
            truncated = True
        else:
            data, truncated = handle.read(), False
    return Blob(rel, path, data, datetime.fromtimestamp(stat.st_mtime, timezone.utc)), truncated


def missing_info(rel: str) -> dict[str, Any]:
    return {"path": rel, "exists": False, "bytes": None, "mtime_utc": None, "sha256": None}


def manifest_digest(infos: Iterable[dict[str, Any]]) -> str | None:
    lines = sorted(f"{i['path']}\t{i['sha256']}\n" for i in infos if i.get("sha256"))
    if not lines:
        return None
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def csv_rows(blob: Blob) -> list[dict[str, str]]:
    rows = []
    try:
        for row in csv.DictReader(io.StringIO(blob.text())):
            rows.append({str(k).strip(): (v if v is not None else "")
                         for k, v in row.items() if k is not None})
    except csv.Error as exc:
        raise UnreadableSource(blob, exc) from exc
    return rows


def json_payload(blob: Blob) -> Any:
    try:
        return json.loads(blob.text())
    except ValueError as exc:
        raise UnreadableSource(blob, exc) from exc


def reader(tool: str):
    """Turn an unparseable primary source into an honest envelope, not a crash."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return fn(*args, **kwargs)
            except UnreadableSource as exc:
                return envelope(tool, now=kwargs.get("now") or utc_now(),
                                source_path=exc.blob.rel, sha256=exc.blob.sha256, exists=True,
                                as_of=None, as_of_basis=None, data={"error": str(exc)},
                                notes=["The source exists but could not be parsed."])
        return inner
    return wrap


def num(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text or text.lower() in ("nan", "none", "null", "n/a"):
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if math.isnan(number) or math.isinf(number):
        return None
    if number.is_integer() and abs(number) < 2**53 and not (
            isinstance(value, str) and "." in value):
        return int(number)
    return number


def flag(value: Any) -> bool | None:
    text = str(value).strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0"):
        return False
    return None


def counts(rows: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    tally: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) if row.get(key) not in (None, "") else "(blank)")
        tally[value] = tally.get(value, 0) + 1
    return dict(sorted(tally.items()))


def pick(row: dict[str, Any], fields: Iterable[str], numeric: Iterable[str] = ()) -> dict[str, Any]:
    numeric = set(numeric)
    return {field: (num(row.get(field)) if field in numeric else row.get(field, ""))
            for field in fields}


# Money amounts and account-number-like tokens are withheld from free text.
_MONEY_RE = re.compile(
    r"(?:US)?[$€£¥]\s?[-−]?\d[\d,]*(?:\.\d+)?(?:\s?(?:[kKmMbB]n?|million|billion|thousand)\b)?"
    r"|\b(?:USD|CNY|RMB|HKD|EUR|GBP|JPY|KRW)\s?[-−]?\d[\d,]*(?:\.\d+)?",
    re.IGNORECASE)
_ACCOUNT_RE = re.compile(
    r"\b[A-Z]{0,2}\d{2,5}[- ]\d{4,7}\b(?![-:.]\d)"
    r"|\b[A-Z]{1,2}\d{6,}\b"
    r"|\b\d{7,}\b")
VALUE_WITHHELD = "[value withheld]"
ACCOUNT_WITHHELD = "[account withheld]"


def redact_text(value: Any) -> str:
    text = str(value or "")
    text = _MONEY_RE.sub(VALUE_WITHHELD, text)
    return _ACCOUNT_RE.sub(ACCOUNT_WITHHELD, text)


def _path_list(value: Any) -> list[str]:
    text = str(value or "").strip()
    items: list[Any] = []
    if text.startswith("["):
        try:
            parsed = ast.literal_eval(text)
            items = list(parsed) if isinstance(parsed, (list, tuple)) else [text]
        except (ValueError, SyntaxError):
            items = [text]
    elif text:
        items = [text]
    return [redact_text(item) for item in items]


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def status_for(exists: bool, as_of: datetime | None, now: datetime,
               max_age: timedelta) -> tuple[str, float | None, str | None]:
    """Return (status, age_hours, note) for a source."""
    if not exists:
        return MISSING, None, None
    if as_of is None:
        return STALE, None, "as_of could not be determined, so the source is not claimed fresh"
    age = now - as_of
    hours = round(age.total_seconds() / 3600, 2)
    if age < -FUTURE_TOLERANCE:
        return STALE, hours, "as_of is in the future; treated as not fresh"
    return (FRESH if age <= max_age else STALE), hours, None


def envelope(tool: str, *, now: datetime, source_path: str, sha256: str | None, exists: bool,
             as_of: datetime | None, as_of_basis: str | None, data: Any,
             max_age: timedelta | None = None, files: list[dict[str, Any]] | None = None,
             notes: list[str] | None = None,
             supplementary: dict[str, Any] | None = None) -> dict[str, Any]:
    window = max_age or MAX_AGE[tool]
    computed, age_hours, note = status_for(exists, as_of, now, window)
    out: dict[str, Any] = {
        "tool": tool,
        "as_of": iso(as_of),
        "as_of_basis": as_of_basis,
        "retrieved_at": iso(now),
        "source_path": source_path,
        "sha256": sha256,
        "pit_label": NON_PIT,
        "pit_note": "Read from project files, not through pitdb as-of macros.",
        "status": computed,
        "status_rule": (f"FRESH when as_of is within {_human(window)} of retrieved_at; "
                        "STALE when older or undeterminable; MISSING when the source does not exist"),
        "age_hours": age_hours,
        "authority": AUTHORITY,
        "data": data,
    }
    all_notes = [n for n in ([note] if note else []) + list(notes or []) if n]
    if files is not None:
        out["files"] = files
    if supplementary:
        out["supplementary"] = supplementary
    if all_notes:
        out["notes"] = all_notes
    return out


def section(tool: str, blob: Blob | None, rel: str, now: datetime, data: Any,
            as_of: datetime | None, as_of_basis: str | None,
            max_age: timedelta | None = None) -> dict[str, Any]:
    """Provenance-carrying sub-section for supplementary sources."""
    window = max_age or MAX_AGE[tool]
    status, age_hours, note = status_for(blob is not None, as_of, now, window)
    out = {"source_path": rel, "sha256": blob.sha256 if blob else None,
           "as_of": iso(as_of), "as_of_basis": as_of_basis, "status": status,
           "age_hours": age_hours, "pit_label": NON_PIT,
           "status_rule": f"FRESH when as_of is within {_human(window)} of retrieved_at",
           "data": data}
    if note:
        out["note"] = note
    return out


def _mtime_basis(blob: Blob) -> tuple[datetime, str]:
    return blob.mtime, "file_mtime"


# ---------------------------------------------------------------------------
# daily_snapshot
# ---------------------------------------------------------------------------

EXPORTS_REL = "implementation/exports"
EXPORT_FILES = {
    "alerts": "alerts.csv",
    "source_health": "source_health.csv",
    "market_context": "market_context.csv",
    "macro_context": "macro_context.csv",
    "event_guardrails": "event_guardrails.csv",
    "signal_readiness": "signal_readiness.csv",
    "portfolio_context": "portfolio_context.json",
    "broker_connectivity": "ibkr_availability.json",
}
DAILY_STATUS_REL = "implementation/state/daily_update_status.json"
PORTFOLIO_POLICY = ("coverage and status only; account numbers, quantities and values "
                    "are withheld by the ZT add-on")


def _valid_day(text: str) -> bool:
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def export_dates(root: Path) -> list[str]:
    base = inside(root, EXPORTS_REL)
    try:
        # One listing: os.scandir carries the entry type on Windows, where a per-folder
        # is_dir() on Google Drive for desktop costs ~0.15 s (ZT add-on, 2026-09-29).
        with os.scandir(base) as entries:
            names = [e.name for e in entries if e.is_dir() and _DATE_RE.match(e.name) and _valid_day(e.name)]
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(names)


def resolve_snapshot_date(value: str | None, dates: list[str], now: datetime) -> str | None:
    text = (value or "latest").strip().lower()
    if text == "latest":
        return dates[-1] if dates else None
    if text == "today":
        return today_et(now).isoformat()
    if not _DATE_RE.match(text) or not _valid_day(text):
        raise ValueError("date must be 'latest', 'today' or YYYY-MM-DD")
    if date.fromisoformat(text) > today_et(now) + timedelta(days=1):
        raise ValueError("date cannot be in the future")
    return text


def _alerts(blob: Blob) -> dict[str, Any]:
    fields = ("alert_id", "category", "severity", "title", "summary", "scope", "symbol",
              "source_label", "as_of", "evidence_id", "action", "decision_posture")
    free_text = {"title", "summary", "action", "source_label"}
    rows = [{f: (redact_text(r.get(f)) if f in free_text else r.get(f, "")) for f in fields}
            for r in csv_rows(blob)]
    return {"count": len(rows), "by_severity": counts(rows, "severity"),
            "by_category": counts(rows, "category"), "rows": rows[:MAX_LIST_ROWS]}


def _source_health(blob: Blob) -> dict[str, Any]:
    rows = []
    for r in csv_rows(blob):
        row = pick(r, ("source_id", "label", "source_type", "status", "observation_date",
                       "observation_precision", "age_hours", "age_label", "cadence",
                       "file_count", "evidence_id"), numeric=("age_hours", "file_count"))
        row["latest_paths"] = _path_list(r.get("latest_paths"))
        rows.append(row)
    return {"count": len(rows), "by_status": counts(rows, "status"), "rows": rows[:MAX_LIST_ROWS]}


def _market_context(blob: Blob) -> dict[str, Any]:
    fields = ("symbol", "scope", "close", "return_1d_pct", "return_5d_pct", "return_20d_pct",
              "volume", "as_of", "price_status", "source_id", "source_path")
    rows = [pick(r, fields, numeric=("close", "return_1d_pct", "return_5d_pct",
                                     "return_20d_pct", "volume")) for r in csv_rows(blob)]
    return {"count": len(rows), "rows": rows[:MAX_LIST_ROWS]}


def _macro_context(blob: Blob) -> dict[str, Any]:
    fields = ("series", "label", "value", "as_of", "underlying_source", "status", "source_id",
              "source_path")
    rows = [pick(r, fields, numeric=("value",)) for r in csv_rows(blob)]
    return {"count": len(rows), "by_status": counts(rows, "status"), "rows": rows[:MAX_LIST_ROWS]}


def _event_guardrails(blob: Blob) -> dict[str, Any]:
    rows = [pick(r, ("category", "status", "source", "as_of", "note")) for r in csv_rows(blob)]
    return {"count": len(rows), "by_status": counts(rows, "status"), "rows": rows}


def _signal_readiness(blob: Blob) -> dict[str, Any]:
    rows = []
    for r in csv_rows(blob):
        row = pick(r, ("rank", "name", "readiness_status", "data_status",
                       "connected_source_statuses", "missing_requirements", "decision_posture"),
                   numeric=("rank",))
        row["alpha_ready"] = flag(r.get("alpha_ready"))
        rows.append(row)
    return {"count": len(rows), "by_readiness": counts(rows, "readiness_status"),
            "by_data_status": counts(rows, "data_status"),
            "alpha_ready_count": sum(1 for r in rows if r["alpha_ready"]),
            "rows": rows[:MAX_LIST_ROWS]}


def _portfolio_context(blob: Blob) -> dict[str, Any]:
    payload = json_payload(blob)
    payload = payload if isinstance(payload, dict) else {}
    return {
        "as_of": payload.get("as_of"),
        "source_id": payload.get("source_id"),
        "source_label": redact_text(payload.get("source_label")),
        "reconciliation_status": payload.get("reconciliation_status"),
        "pnl_status": payload.get("pnl_status"),
        "pnl_value_coverage_pct": num(payload.get("pnl_value_coverage_pct")),
        "note": redact_text(payload.get("note")),
        "policy": PORTFOLIO_POLICY,
    }


def _broker_connectivity(blob: Blob) -> dict[str, Any]:
    payload = json_payload(blob)
    payload = payload if isinstance(payload, dict) else {}
    return {key: redact_text(payload.get(key)) if key == "note" else payload.get(key)
            for key in ("status", "checked_at", "order_capability", "account_data",
                        "market_data_entitlements", "note")}


_SECTION_READERS = {
    "alerts": _alerts,
    "source_health": _source_health,
    "market_context": _market_context,
    "macro_context": _macro_context,
    "event_guardrails": _event_guardrails,
    "signal_readiness": _signal_readiness,
    "portfolio_context": _portfolio_context,
    "broker_connectivity": _broker_connectivity,
}


def _daily_update(root: Path, now: datetime, snapshot_date: str | None) -> dict[str, Any]:
    blob = read_blob(root, DAILY_STATUS_REL)
    if blob is None:
        return section("daily_snapshot", None, DAILY_STATUS_REL, now, None, None, None)
    try:
        payload = json_payload(blob)
    except UnreadableSource as exc:
        return section("daily_snapshot", blob, DAILY_STATUS_REL, now, {"error": str(exc)},
                       None, None)
    payload = payload if isinstance(payload, dict) else {}
    as_of, precision = parse_instant(payload.get("last_completed_at") or payload.get("last_started_at"))
    data = {key: payload.get(key) for key in (
        "status", "run_id", "last_started_at", "last_completed_at", "collection_exit_code",
        "snapshot_exit_code", "llm_analysis_status")}
    data["message"] = redact_text(payload.get("message"))
    started = str(payload.get("last_started_at") or "")[:10]
    data["matches_snapshot_date"] = bool(snapshot_date) and started == snapshot_date
    return section("daily_snapshot", blob, DAILY_STATUS_REL, now, data, as_of,
                   f"content:last_completed_at ({precision})" if as_of else None)


@reader("daily_snapshot")
def daily_snapshot(date_value: str = "latest", *, root: Path | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """Read one dated alpha_monitor export folder."""
    root = root or project_root()
    now = now or utc_now()
    dates = export_dates(root)
    resolved = resolve_snapshot_date(date_value, dates, now)
    latest = dates[-1] if dates else None
    rel_dir = f"{EXPORTS_REL}/{resolved}" if resolved else EXPORTS_REL
    base = {"requested": date_value, "resolved_date": resolved, "latest_available": latest,
            "available_dates": dates[-14:]}
    folder = inside(root, rel_dir) if resolved else None
    if folder is None or not folder.is_dir():
        return envelope("daily_snapshot", now=now, source_path=rel_dir, sha256=None, exists=False,
                        as_of=None, as_of_basis=None, data=base,
                        notes=[f"No export folder for {resolved or date_value!r}; "
                               f"latest available is {latest or 'none'}"])
    files: list[dict[str, Any]] = []
    data: dict[str, Any] = dict(base)
    missing_files: list[str] = []
    blobs: dict[str, Blob] = {}
    for key, name in EXPORT_FILES.items():
        rel = f"{rel_dir}/{name}"
        blob = read_blob(root, rel)
        if blob is None:
            files.append(missing_info(rel))
            missing_files.append(name)
            data[key] = None
            continue
        blobs[key] = blob
        files.append(blob.info())
        try:
            data[key] = _SECTION_READERS[key](blob)
        except ValueError as exc:
            data[key] = {"error": str(exc)}
    data["complete"] = not missing_files
    data["missing_files"] = missing_files
    as_of, basis = None, None
    guardrails = (data.get("event_guardrails") or {}).get("rows") or []
    if guardrails:
        as_of, precision = _latest_instant(row.get("as_of") for row in guardrails)
        basis = f"content:event_guardrails.as_of ({precision})" if as_of else None
    if as_of is None and data.get("broker_connectivity"):
        as_of, precision = parse_instant(data["broker_connectivity"].get("checked_at"))
        basis = f"content:ibkr_availability.checked_at ({precision})" if as_of else None
    if as_of is None and blobs:
        as_of, precision = parse_instant(resolved)
        basis = "folder date (date)" if as_of else None
    notes = []
    if missing_files:
        notes.append("Export folder is incomplete: " + ", ".join(missing_files))
    return envelope("daily_snapshot", now=now, source_path=rel_dir,
                    sha256=manifest_digest(files), exists=bool(blobs), as_of=as_of,
                    as_of_basis=basis, data=data, files=files, notes=notes,
                    supplementary={"daily_update": _daily_update(root, now, resolved)})


# ---------------------------------------------------------------------------
# source_families
# ---------------------------------------------------------------------------

BACKFILL_REL = "implementation/backfill"
TOP25_NAME = "top25_backfill_coverage.json"
DATA_SOURCE_LEDGER_REL = "implementation/pit_warehouse/ledgers/DATA_SOURCE_LEDGER.csv"
SOURCE_REGISTRY_REL = "implementation/config/source_registry.json"


def _top25_rel(root: Path) -> str:
    base = inside(root, BACKFILL_REL)
    candidates = []
    if base.is_dir():
        candidates = sorted(p.name for p in base.iterdir() if (p / TOP25_NAME).is_file())
    window = candidates[-1] if candidates else "2025-08-06_to_2026-08-06"
    return f"{BACKFILL_REL}/{window}/{TOP25_NAME}"


def _family_row(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items()
            if key != "artifacts" and not isinstance(value, (dict, list))}


def _pit_catalog_families(root: Path, now: datetime) -> dict[str, Any]:
    blob = read_blob(root, DATA_SOURCE_LEDGER_REL)
    if blob is None:
        return section("source_families", None, DATA_SOURCE_LEDGER_REL, now, None, None, None,
                       max_age=MAX_AGE["pit_inventory"])
    try:
        rows = csv_rows(blob)
    except UnreadableSource as exc:
        return section("source_families", blob, DATA_SOURCE_LEDGER_REL, now,
                       {"error": str(exc)}, None, None, max_age=MAX_AGE["pit_inventory"])
    families: dict[str, dict[str, Any]] = {}
    for row in rows:
        family = row.get("family") or "(blank)"
        entry = families.setdefault(family, {"sources": 0, "by_status": {}, "by_pit_class": {},
                                             "not_ok": []})
        entry["sources"] += 1
        for key, field in (("by_status", "status"), ("by_pit_class", "pit_class")):
            value = row.get(field) or "(blank)"
            entry[key][value] = entry[key].get(value, 0) + 1
        if (row.get("last_status") or "").lower() not in ("ok", ""):
            entry["not_ok"].append({"source_id": row.get("source_id"),
                                    "last_status": row.get("last_status"),
                                    "last_run": row.get("last_run")})
    as_of, precision = _latest_instant(row.get("last_run") for row in rows)
    data = {"family_count": len(families), "source_count": len(rows),
            "families": dict(sorted(families.items()))}
    return section("source_families", blob, DATA_SOURCE_LEDGER_REL, now, data, as_of,
                   f"content:max(last_run) ({precision})" if as_of else None,
                   max_age=MAX_AGE["pit_inventory"])


def _monitor_registry(root: Path, now: datetime) -> dict[str, Any]:
    blob = read_blob(root, SOURCE_REGISTRY_REL)
    if blob is None:
        return section("source_families", None, SOURCE_REGISTRY_REL, now, None, None, None)
    try:
        payload = json_payload(blob)
    except UnreadableSource as exc:
        return section("source_families", blob, SOURCE_REGISTRY_REL, now, {"error": str(exc)},
                       None, None)
    items = payload if isinstance(payload, list) else []
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        by_type.setdefault(str(item.get("source_type") or "(blank)"), []).append({
            "id": item.get("id"), "label": item.get("label"), "cadence": item.get("cadence"),
            "retention_class": item.get("retention_class"),
            "stale_after_hours": item.get("stale_after_hours")})
    as_of, basis = _mtime_basis(blob)
    data = {"source_count": sum(len(v) for v in by_type.values()),
            "source_types": dict(sorted(by_type.items()))}
    return section("source_families", blob, SOURCE_REGISTRY_REL, now, data, as_of, basis)


@reader("source_families")
def source_families(*, root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Top-25 PIT-aware source families, plus the pitdb catalog and monitor registry."""
    root = root or project_root()
    now = now or utc_now()
    rel = _top25_rel(root)
    blob = read_blob(root, rel)
    supplementary = {"pit_catalog_families": _pit_catalog_families(root, now),
                     "monitor_registry": _monitor_registry(root, now)}
    if blob is None:
        return envelope("source_families", now=now, source_path=rel, sha256=None, exists=False,
                        as_of=None, as_of_basis=None, data=None, supplementary=supplementary,
                        notes=["The top-25 backfill coverage ledger is not present; the "
                               "supplementary sections are separate sources, not a substitute."])
    payload = json_payload(blob)
    rows = payload.get("families", []) if isinstance(payload, dict) else payload
    rows = [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
    families = [_family_row(r) for r in rows]
    pit_counts: dict[str, int] = {}
    for row in families:
        key = str(row.get("pit_class") or "UNSPECIFIED")
        pit_counts[key] = pit_counts.get(key, 0) + 1
    data = {
        "family_count": len(families),
        "by_coverage_status": counts(families, "coverage_status"),
        "pit_counts": dict(sorted(pit_counts.items())),
        "record_count": sum(num(r.get("record_count")) or 0 for r in families),
        "file_count": sum(num(r.get("file_count")) or 0 for r in families),
        "bytes": sum(num(r.get("bytes")) or 0 for r in families),
        "families": families,
    }
    generated = payload.get("generated_at") if isinstance(payload, dict) else None
    as_of, precision = parse_instant(generated)
    basis = f"content:generated_at ({precision})" if as_of else None
    if as_of is None:
        as_of, basis = _mtime_basis(blob)
    notes = []
    if families and len(families) != 25:
        notes.append(f"Expected 25 families, found {len(families)}")
    return envelope("source_families", now=now, source_path=rel, sha256=blob.sha256,
                    exists=True, as_of=as_of, as_of_basis=basis, data=data,
                    supplementary=supplementary, notes=notes)


# ---------------------------------------------------------------------------
# Alpha signal monitor (ASM) boards
# ---------------------------------------------------------------------------

ASM_DATA_REL = "implementation/alpha_signal_monitor_v01/data"
SIGNAL_STATE_REL = f"{ASM_DATA_REL}/signal_state.csv"
LANE_STATUS_REL = f"{ASM_DATA_REL}/lane_status.csv"
TEST_LEDGER_REL = f"{ASM_DATA_REL}/hypothesis_test_ledger.csv"
PROMOTION_BOARD_REL = f"{ASM_DATA_REL}/promotion_board.csv"
ASM_BUILD_LOG_REL = f"{ASM_DATA_REL}/dashboard_build_log.csv"
PROMOTION_GATES = ("G0_prereg", "A1_baseline", "A2_sample_split", "A3_t_ge_3",
                   "A4_materiality", "A5_robustness", "A6_replication", "A7_decay_watch")


def _missing(tool: str, rel: str, now: datetime, note: str | None = None) -> dict[str, Any]:
    return envelope(tool, now=now, source_path=rel, sha256=None, exists=False, as_of=None,
                    as_of_basis=None, data=None, notes=[note] if note else None)


@reader("signal_state")
def _asm_basis(root: Path, blob: Blob) -> tuple[datetime, str, dict[str, Any] | None]:
    """Boards without their own timestamp use the ASM build log's data as-of."""
    log = read_blob(root, ASM_BUILD_LOG_REL)
    if log is not None:
        try:
            rows = csv_rows(log)
        except UnreadableSource:
            rows = []
        as_of, precision = _latest_instant(r.get("asof") for r in rows)
        if as_of is not None:
            return as_of, f"content:dashboard_build_log.asof ({precision})", log.info()
    as_of, basis = _mtime_basis(blob)
    return as_of, basis, None


def signal_state(*, root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """ASM phase-1 state read: one row per monitored signal (monitored, not admitted)."""
    root = root or project_root()
    now = now or utc_now()
    blob = read_blob(root, SIGNAL_STATE_REL)
    if blob is None:
        return _missing("signal_state", SIGNAL_STATE_REL, now,
                        "monitor.py writes this file; it is absent from the project copy")
    fields = ("signal_id", "family", "name", "value", "unit", "baseline", "baseline_value",
              "z", "state", "n_obs", "cadence", "asof", "available_from", "staleness_days",
              "stale_limit_days", "freshness", "attention_rank", "pit_class", "source",
              "paper_anchor", "falsifier", "admission_status", "computed_on")
    numeric = ("value", "baseline_value", "z", "n_obs", "staleness_days", "stale_limit_days",
               "attention_rank")
    raw = csv_rows(blob)
    rows = [pick(r, fields, numeric=numeric) for r in raw]
    as_of, precision = _latest_instant(r.get("computed_on") for r in raw)
    basis = f"content:max(computed_on) ({precision})" if as_of else None
    if as_of is None:
        as_of, basis, _ = _asm_basis(root, blob)
    data = {"count": len(rows), "by_freshness": counts(rows, "freshness"),
            "by_state": counts(rows, "state"), "by_admission": counts(rows, "admission_status"),
            "rows": rows[:MAX_LIST_ROWS]}
    return envelope("signal_state", now=now, source_path=SIGNAL_STATE_REL, sha256=blob.sha256,
                    exists=True, as_of=as_of, as_of_basis=basis, data=data,
                    notes=["Row pit_class values are the monitor's own labels."])


@reader("lane_status")
def lane_status(*, root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """ASM phase-2 lane board: collector state and freshness per lane."""
    root = root or project_root()
    now = now or utc_now()
    blob = read_blob(root, LANE_STATUS_REL)
    if blob is None:
        return _missing("lane_status", LANE_STATUS_REL, now)
    fields = ("lane_id", "signal_name", "cadence", "current_freshness", "current_age_days",
              "stale_limit_days", "collector_status", "collector_script", "scheduled",
              "b3_dry_run", "b3_three_clean_runs", "b3_induced_failure_alert",
              "consecutive_fresh_days", "source", "opened_on")
    rows = [pick(r, fields, numeric=("current_age_days", "stale_limit_days",
                                     "consecutive_fresh_days")) for r in csv_rows(blob)]
    as_of, basis, build_log = _asm_basis(root, blob)
    data = {"count": len(rows), "by_freshness": counts(rows, "current_freshness"),
            "by_collector_status": counts(rows, "collector_status"),
            "by_scheduled": counts(rows, "scheduled"),
            "max_consecutive_fresh_days": max((r["consecutive_fresh_days"] or 0 for r in rows),
                                              default=0),
            "rows": rows[:MAX_LIST_ROWS]}
    note = ("The lane file carries no timestamp; as_of is the ASM build log's data as-of."
            if build_log else "The lane file carries no timestamp and no ASM build log was "
            "found; as_of is the file modification time.")
    return envelope("lane_status", now=now, source_path=LANE_STATUS_REL, sha256=blob.sha256,
                    exists=True, as_of=as_of, as_of_basis=basis, data=data,
                    supplementary={"asm_build_log": build_log} if build_log else None,
                    notes=[note, "Row freshness is each lane's own label."])


def hlz_hurdle(n_tests: int) -> float:
    """Harvey-Liu-Zhu style hurdle: max(3.0, 2.57 + 0.5 * log10(n))."""
    return max(3.0, 2.57 + 0.5 * math.log10(max(1, int(n_tests))))


@reader("test_ledger")
def test_ledger(*, root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Multiple-testing ledger: cumulative test count and the rising t hurdle."""
    root = root or project_root()
    now = now or utc_now()
    blob = read_blob(root, TEST_LEDGER_REL)
    if blob is None:
        return _missing("test_ledger", TEST_LEDGER_REL, now)
    raw = csv_rows(blob)
    n = len(raw)
    hurdle = hlz_hurdle(n)
    as_of, precision = _latest_instant(r.get("run_date") for r in raw)
    basis = f"content:max(run_date) ({precision})" if as_of else None
    if as_of is None:
        as_of, basis = _mtime_basis(blob)
    data = {
        "cumulative_tests": n,
        "distinct_test_ids": len({r.get("test_id") for r in raw}),
        "distinct_signals": len({r.get("signal_id") for r in raw}),
        "hurdle_t": round(hurdle, 2),
        "hurdle_t_unrounded": hurdle,
        "hurdle_rule": "max(3.0, 2.57 + 0.5 * log10(cumulative_tests))",
        "by_run_date": counts(raw, "run_date"),
        "by_role": counts(raw, "role"),
        "by_hlz_hurdle_applies": counts(raw, "hlz_hurdle_applies"),
        "recent": [pick(r, ("test_id", "run_date", "signal_id", "family", "statistic",
                            "statistic_value", "n", "role"), numeric=("statistic_value", "n"))
                   for r in raw[-25:]],
    }
    notes = []
    if data["distinct_test_ids"] != n:
        notes.append("Some test_id values repeat; every row counts toward the cumulative "
                     "denominator, as in the phase-3 dashboard.")
    return envelope("test_ledger", now=now, source_path=TEST_LEDGER_REL, sha256=blob.sha256,
                    exists=True, as_of=as_of, as_of_basis=basis, data=data, notes=notes)


@reader("promotion_board")
def promotion_board(*, root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """ASM phase-3 promotion board: eligibility and the eight-gate battery."""
    root = root or project_root()
    now = now or utc_now()
    blob = read_blob(root, PROMOTION_BOARD_REL)
    if blob is None:
        return _missing("promotion_board", PROMOTION_BOARD_REL, now)
    raw = csv_rows(blob)
    rows = []
    gates_run = 0
    for r in raw:
        row = pick(r, ("rank_by_history", "signal_id", "signal_name", "family", "n_obs",
                       "cadence", "eligible", "status", "verdict", "verdict_date",
                       "selection_rule"), numeric=("rank_by_history", "n_obs"))
        row["gates"] = {gate: r.get(gate, "") for gate in PROMOTION_GATES}
        gates_run += sum(1 for value in row["gates"].values() if value not in ("", "NOT_RUN"))
        rows.append(row)
    as_of, precision = _latest_instant(r.get("verdict_date") for r in raw)
    basis = f"content:max(verdict_date) ({precision})" if as_of else None
    build_log = None
    if as_of is None:
        as_of, basis, build_log = _asm_basis(root, blob)
    data = {"count": len(rows), "by_eligible": counts(rows, "eligible"),
            "by_status": counts(rows, "status"), "by_verdict": counts(rows, "verdict"),
            "gates_run": gates_run, "gates_total": len(rows) * len(PROMOTION_GATES),
            "rows": rows[:MAX_LIST_ROWS]}
    return envelope("promotion_board", now=now, source_path=PROMOTION_BOARD_REL,
                    sha256=blob.sha256, exists=True, as_of=as_of, as_of_basis=basis, data=data,
                    supplementary={"asm_build_log": build_log} if build_log else None)


# ---------------------------------------------------------------------------
# pit_inventory
# ---------------------------------------------------------------------------

PIT_REL = "implementation/pit_warehouse"
PIT_LOG_REL = f"{PIT_REL}/logs/pitdb_daily.log"
PIT_LEDGERS = {
    "data_source_ledger": f"{PIT_REL}/ledgers/DATA_SOURCE_LEDGER.csv",
    "file_coverage_ledger": f"{PIT_REL}/ledgers/FILE_COVERAGE_LEDGER.csv",
    "ingest_ledger": f"{PIT_REL}/ledgers/INGEST_LEDGER.csv",
    "paper_ledger": f"{PIT_REL}/ledgers/PAPER_LEDGER.csv",
    "inventory": f"{PIT_REL}/ledgers/INVENTORY.json",
}
_RUN_HEADER_RE = re.compile(r"^--- pitdb daily run (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC ---\s*$",
                            re.MULTILINE)
_CHECK_RE = re.compile(r"^\s*\[(PASS|FAIL|WARN|SKIP)\]\s+(A\d+)\s+(.*?)\s*$", re.MULTILINE)
_OVERALL_RE = re.compile(r"OVERALL:\s*(PASS|FAIL)")
_EXIT_RE = re.compile(r"^exit=(-?\d+)", re.MULTILINE)
_FACT_RE = re.compile(r"^\s{2,}([a-z][A-Za-z ]*[A-Za-z])\s{2,}(\S.*?)\s*$")


def _parse_pit_runs(text: str) -> list[dict[str, Any]]:
    headers = list(_RUN_HEADER_RE.finditer(text))
    runs = []
    for index, match in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        chunk = text[match.end():end]
        started = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}:00+00:00")
        checks = [{"id": m.group(2), "result": m.group(1), "check": m.group(3)}
                  for m in _CHECK_RE.finditer(chunk)]
        overalls = _OVERALL_RE.findall(chunk)
        exits = _EXIT_RE.findall(chunk)
        facts: dict[str, Any] = {}
        marker = chunk.rfind("=== WAREHOUSE FACTS ===")
        if marker >= 0:
            for line in chunk[marker:].splitlines()[1:]:
                if line.startswith("==="):
                    break
                fact = _FACT_RE.match(line)
                if fact:
                    value = fact.group(2)
                    facts[fact.group(1)] = num(value) if num(value) is not None else value
        runs.append({"started_at": iso(started), "_started": started,
                     "overall": overalls[-1] if overalls else None,
                     "exit_code": int(exits[-1]) if exits else None,
                     "checks": checks, "checks_passed": sum(c["result"] == "PASS" for c in checks),
                     "checks_total": len(checks), "facts": facts})
    return runs


def _ledger_summary(key: str, blob: Blob) -> dict[str, Any]:
    if key == "inventory":
        payload = json_payload(blob)
        payload = payload if isinstance(payload, dict) else {}
        return {name: payload.get(name) for name in (
            "timeseries", "prices", "documents", "sources", "files", "storage",
            "by_pit_class") if name in payload}
    rows = csv_rows(blob)
    summary: dict[str, Any] = {"rows": len(rows)}
    if key == "data_source_ledger":
        summary.update({
            "by_status": counts(rows, "status"), "by_last_status": counts(rows, "last_status"),
            "by_pit_class": counts(rows, "pit_class"),
            "not_ok": [pick(r, ("source_id", "family", "status", "last_status", "last_run",
                                "hours_since_data"), numeric=("hours_since_data",))
                       for r in rows if (r.get("last_status") or "").lower() not in ("ok", "")
                       ][:50]})
    elif key == "file_coverage_ledger":
        by_status: dict[str, dict[str, float]] = {}
        for r in rows:
            entry = by_status.setdefault(r.get("status") or "(blank)", {"files": 0, "mb": 0.0})
            entry["files"] += num(r.get("files")) or 0
            entry["mb"] = round(entry["mb"] + float(num(r.get("mb")) or 0), 2)
        summary["by_status"] = dict(sorted(by_status.items()))
    elif key == "ingest_ledger":
        stamped = [(parse_instant(r.get("started_at"))[0], r) for r in rows]
        stamped = [(t, r) for t, r in stamped if t is not None]
        if stamped:
            latest = max(t for t, _ in stamped)
            window = [r for t, r in stamped if latest - t <= timedelta(hours=24)]
            summary.update({
                "latest_started_at": iso(latest),
                "last_24h_runs": len(window),
                "last_24h_by_status": counts(window, "status"),
                "last_24h_errors": [pick(r, ("connector", "source_id", "status", "error"))
                                    for r in window if (r.get("error") or "").strip()][:20]})
    elif key == "paper_ledger":
        summary.update({"by_status": counts(rows, "status"), "by_kind": counts(rows, "kind")})
    return summary


def _receipts(now: datetime) -> dict[str, Any]:
    home = os.environ.get("VIBE_TRADING_HOME", "").strip()
    out: dict[str, Any] = {}
    for key, name, field, window in (
            ("audit", "pit_audit_receipt.json", "audited_at_utc", AUDIT_RECEIPT_MAX_AGE),
            ("index", "pit_index_receipt.json", "refreshed_at_utc", MAX_AGE["pit_inventory"])):
        entry: dict[str, Any] = {"source_path": f"VIBE_TRADING_HOME/{name}", "pit_label": NON_PIT}
        path = Path(home) / name if home else None
        if path is None or not path.is_file():
            entry.update({"status": MISSING, "sha256": None,
                          "note": "VIBE_TRADING_HOME is not set" if not home else "receipt absent"})
            out[key] = entry
            continue
        data = path.read_bytes()
        try:
            payload = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        as_of, precision = parse_instant(payload.get(field))
        status, age_hours, note = status_for(True, as_of, now, window)
        entry.update({"sha256": hashlib.sha256(data).hexdigest(), "as_of": iso(as_of),
                      "as_of_basis": f"content:{field} ({precision})" if as_of else None,
                      "status": status, "age_hours": age_hours,
                      "status_rule": f"FRESH when {field} is within {_human(window)}",
                      "receipt_status": payload.get("status"),
                      "checks": payload.get("checks"), "rows": payload.get("rows")})
        if key == "audit":
            entry["note"] = ("pit-actor-sim also requires the receipt to match the current lake "
                             "signature; that binding is not re-checked here")
        elif note:
            entry["note"] = note
        out[key] = entry
    return out


@reader("pit_inventory")
def pit_inventory(*, root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """pitdb ledgers, the latest daily run and audit, and the E: receipts."""
    root = root or project_root()
    now = now or utc_now()
    files: list[dict[str, Any]] = []
    ledgers: dict[str, Any] = {}
    for key, rel in PIT_LEDGERS.items():
        blob = read_blob(root, rel)
        if blob is None:
            files.append(missing_info(rel))
            ledgers[key] = {"source_path": rel, "status": MISSING, "sha256": None}
            continue
        files.append(blob.info())
        try:
            summary = _ledger_summary(key, blob)
        except ValueError as exc:
            summary = {"error": str(exc)}
        ledgers[key] = {"source_path": rel, "sha256": blob.sha256,
                        "mtime_utc": iso(blob.mtime), "summary": summary}
    log, truncated = read_tail(root, PIT_LOG_REL)
    supplementary = {"ledgers": ledgers, "receipts": _receipts(now)}
    if log is None:
        return envelope("pit_inventory", now=now, source_path=PIT_LOG_REL, sha256=None,
                        exists=False, as_of=None, as_of_basis=None, data=None, files=files,
                        supplementary=supplementary)
    runs = _parse_pit_runs(log.text())
    latest = runs[-1] if runs else None
    data: dict[str, Any] = {
        "log_tail_only": truncated,
        "latest_run": {k: v for k, v in latest.items() if not k.startswith("_")} if latest else None,
        "recent_runs": [{"started_at": r["started_at"], "overall": r["overall"],
                         "exit_code": r["exit_code"]} for r in runs[-7:]],
    }
    notes = ["sha256 covers the log tail that was parsed" if truncated else ""]
    if latest and latest["exit_code"] is None:
        notes.append("The latest run has no exit= line yet (still running or interrupted).")
    if latest and latest["overall"] is None:
        notes.append("The latest run printed no OVERALL line.")
    return envelope("pit_inventory", now=now, source_path=PIT_LOG_REL, sha256=log.sha256,
                    exists=True, as_of=latest["_started"] if latest else None,
                    as_of_basis="content:run header (instant)" if latest else None,
                    data=data, files=files, supplementary=supplementary,
                    notes=[n for n in notes if n])


# ---------------------------------------------------------------------------
# world_model
# ---------------------------------------------------------------------------

WORLD_MODEL_ENV = "ZT_WORLD_MODEL_PATH"
WORLD_MODEL_CANDIDATES = (
    "implementation/world_model/world_model_state.json",
    "implementation/world_model/state.json",
    "world_model/world_model_state.json",
    "world_model/state.json",
)
WORLD_MODEL_REFERENCES = ("WORLD_MODEL_V06.html", "WORLD_MODEL_ENHANCEMENT_PLAN_2026-08-15.md")


@reader("world_model")
def world_model(*, root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Machine-readable world-model state, or an honest NOT_AVAILABLE."""
    root = root or project_root()
    now = now or utc_now()
    override = os.environ.get(WORLD_MODEL_ENV, "").strip()
    candidates = (override,) if override else WORLD_MODEL_CANDIDATES
    references = []
    for rel in WORLD_MODEL_REFERENCES:
        blob = read_blob(root, rel)
        references.append(blob.info() if blob else missing_info(rel))
    for rel in candidates:
        if not rel.lower().endswith((".json", ".csv")):
            raise ValueError(f"{WORLD_MODEL_ENV} must name a .json or .csv file")
        blob = read_blob(root, rel, max_bytes=2 * 1024 * 1024)
        if blob is None:
            continue
        payload: Any = json_payload(blob) if rel.lower().endswith(".json") else csv_rows(blob)
        generated = payload.get("generated_at") or payload.get("as_of") if isinstance(
            payload, dict) else None
        as_of, precision = parse_instant(generated)
        basis = f"content:generated_at ({precision})" if as_of else None
        if as_of is None:
            as_of, basis = _mtime_basis(blob)
        return envelope("world_model", now=now, source_path=rel, sha256=blob.sha256, exists=True,
                        as_of=as_of, as_of_basis=basis,
                        data={"availability": "AVAILABLE", "state": payload,
                              "human_readable_references": references},
                        notes=["Any probabilities in this file were set upstream; read them, "
                               "never re-estimate or alter them."])
    return envelope("world_model", now=now, source_path=candidates[0], sha256=None, exists=False,
                    as_of=None, as_of_basis=None,
                    data={"availability": "NOT_AVAILABLE",
                          "searched": list(candidates),
                          "human_readable_references": references,
                          "gap": ("No machine-readable world-model export exists. "
                                  "WORLD_MODEL_V06.html is rendered HTML only; the world-model "
                                  "builder would need to write a JSON export (stories, kernels, "
                                  "assertions, as-of) to close this gap.")},
                    notes=[f"Set {WORLD_MODEL_ENV} to a project-relative .json or .csv export "
                           "once one exists."])


# ---------------------------------------------------------------------------
# project_reports and the report viewer
# ---------------------------------------------------------------------------

HUB_REL = "PROJECT_HUB.json"
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _title_from_head(head: bytes) -> str | None:
    match = _TITLE_RE.search(head)
    if not match:
        return None
    title = " ".join(html.unescape(match.group(1).decode("utf-8", errors="replace")).split())
    return title[:200] or None


# ZT add-on (2026-09-29): report metadata cache. The project lives on Google Drive for
# desktop, where reading every dashboard to hash it (and to find its <title>) made
# /zt/reports take a minute on PC1 before the list could render. The list now answers
# from stat() and this cache, keyed by (path, size, mtime_ns) so an edited file is
# re-read; the viewer fills it for the report it serves and one background pass fills
# the rest (warm_report_cache).
_REPORT_META: dict[tuple[str, int, int], dict[str, Any]] = {}
_REPORT_META_LOCK = threading.Lock()
_REPORT_WARMUP: threading.Thread | None = None


def _report_key(path: Path, stat: os.stat_result) -> tuple[str, int, int]:
    return (str(path), int(stat.st_size), int(stat.st_mtime_ns))


def _regular_file_stat(path: Path) -> os.stat_result | None:
    """One stat() call: the stat of a regular file, else None."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat if S_ISREG(stat.st_mode) else None


def _remember_report(key: tuple[str, int, int], data: bytes) -> dict[str, Any]:
    meta = {"sha256": hashlib.sha256(data).hexdigest(), "title": _title_from_head(data[:65536])}
    with _REPORT_META_LOCK:
        _REPORT_META[key] = meta
    return meta


def warm_report_cache(root: Path | None = None) -> int:
    """Read, hash and title every listed report not cached yet; returns how many were read."""
    root = root or project_root()
    read = 0
    for entry in report_index(root).values():
        path: Path = entry["_file"]
        try:
            stat = entry.get("_stat") or _regular_file_stat(path)
            if stat is None or stat.st_size > MAX_READ_BYTES:
                continue
            key = _report_key(path, stat)
            if key in _REPORT_META:
                continue
            _remember_report(key, path.read_bytes())
            read += 1
        except OSError:
            continue
    return read


def _start_report_warmup(root: Path) -> None:
    global _REPORT_WARMUP
    with _REPORT_META_LOCK:
        if _REPORT_WARMUP is not None and _REPORT_WARMUP.is_alive():
            return

        def run() -> None:
            try:
                warm_report_cache(root)
            except Exception:  # noqa: BLE001 - a cache pass must never break the server
                pass

        _REPORT_WARMUP = threading.Thread(target=run, name="zt-report-warmup", daemon=True)
        _REPORT_WARMUP.start()


def _hub_items(root: Path) -> tuple[Blob | None, dict[str, Any], list[dict[str, Any]]]:
    blob = read_blob(root, HUB_REL)
    if blob is None:
        return None, {}, []
    try:
        payload = json_payload(blob)
    except json.JSONDecodeError:
        return blob, {"error": "PROJECT_HUB.json is not valid JSON"}, []
    payload = payload if isinstance(payload, dict) else {}
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    items = [i for i in payload.get("items", []) if isinstance(i, dict) and i.get("path")]
    return blob, status, items


def _root_listing(root: Path) -> dict[str, tuple[Path, os.stat_result | None]]:
    """Root entries from one directory listing: name -> (path, stat of a regular file or None).

    ZT add-on (2026-09-29): on Google Drive for desktop every stat(), is_file() or
    resolve() of a file costs ~0.15 s, which made /zt/reports take 40-60 s for the
    project's ~60 root dashboards. os.scandir returns the file type, size and mtime
    with the listing on Windows; only links are resolved one by one.
    """
    listing: dict[str, tuple[Path, os.stat_result | None]] = {}
    with os.scandir(root) as entries:
        for entry in entries:
            path = root / entry.name
            try:
                if entry.is_symlink():
                    resolved = path.resolve()
                    if resolved.parent != root:
                        continue  # a link that leaves the root is not whitelisted
                    listing[entry.name] = (resolved, _regular_file_stat(resolved))
                elif entry.is_file():
                    listing[entry.name] = (path, entry.stat())
                else:
                    listing[entry.name] = (path, None)
            except OSError:
                continue
    return listing


def report_index(root: Path) -> dict[str, dict[str, Any]]:
    """Whitelist: root-level *.html plus HTML files the hub lists; id = relative path."""
    index: dict[str, dict[str, Any]] = {}
    listing = _root_listing(root)
    for name in sorted(listing):
        path, stat = listing[name]
        if Path(name).suffix.lower() in _HTML_SUFFIXES and stat is not None:
            index[name] = {"id": name, "path": name, "origin": "root", "_file": path, "_stat": stat}
    _, _, items = _hub_items(root)
    for item in items:
        raw = str(item.get("path"))
        if not raw.lower().endswith(_HTML_SUFFIXES):
            continue
        try:
            parts = _clean_parts(raw)
            rel = "/".join(parts)
            resolved = index[rel]["_file"] if rel in index else inside(root, raw)
        except ValueError:
            continue
        entry = index.get(rel) or {"id": rel, "path": rel, "origin": "hub", "_file": resolved}
        if entry["origin"] == "root":
            entry["origin"] = "hub+root"
        entry.update({"hub_id": item.get("id"), "title": item.get("title"),
                      "section": item.get("section"), "role": item.get("role"),
                      "state": item.get("state"), "hub_as_of": item.get("asOf"),
                      "note": item.get("note")})
        index[rel] = entry
    return index


@reader("project_reports")
def project_reports(*, root: Path | None = None, now: datetime | None = None,
                    warm: bool = True) -> dict[str, Any]:
    """PROJECT_HUB.json items plus root dashboards, with existence and digests.

    Answers from stat() and the report metadata cache: a report not read yet has
    ``sha256`` null and, when the hub gives no title, its file name as the title,
    until the background pass (``warm``) or the viewer has read it.
    """
    root = root or project_root()
    now = now or utc_now()
    hub_blob, hub_status, items = _hub_items(root)
    reports = []
    pending = 0
    for entry in report_index(root).values():
        path: Path = entry["_file"]
        stat = entry.get("_stat") or _regular_file_stat(path)
        exists = stat is not None
        info = {"bytes": None, "mtime_utc": None, "sha256": None}
        meta: dict[str, Any] = {}
        if stat is not None:
            meta = _REPORT_META.get(_report_key(path, stat), {})
            if not meta and stat.st_size <= MAX_READ_BYTES:
                pending += 1
            info = {"bytes": stat.st_size,
                    "mtime_utc": iso(datetime.fromtimestamp(stat.st_mtime, timezone.utc)),
                    "sha256": meta.get("sha256")}
        title = entry.get("title") or meta.get("title") or entry["path"]
        reports.append({
            "id": entry["id"], "title": title, "path": entry["path"], "origin": entry["origin"],
            "section": entry.get("section"), "role": entry.get("role"),
            "state": entry.get("state"), "hub_as_of": entry.get("hub_as_of"),
            "note": entry.get("note"), "exists": exists, "viewable": exists, **info})
    reports.sort(key=lambda r: (r["origin"] == "root", r["path"].lower()))
    listing = _root_listing(root) if items else {}
    other_items = [{"hub_id": i.get("id"), "title": i.get("title"), "path": i.get("path"),
                    "section": i.get("section"), "role": i.get("role"), "state": i.get("state"),
                    "exists": _exists(root, str(i.get("path")), listing)}
                   for i in items if not str(i.get("path")).lower().endswith(_HTML_SUFFIXES)]
    if pending and warm:
        _start_report_warmup(root)
    data = {
        "reports": reports,
        "counts": {"reports": len(reports), "viewable": sum(r["viewable"] for r in reports),
                   "missing": sum(not r["exists"] for r in reports),
                   "hub_items": len(items), "digests_pending": pending},
        "hub_status": hub_status or None,
        "other_hub_items": other_items,
        "viewer_policy": ("Only these ids are served, sandboxed, with a report CSP that blocks "
                          "network access; links to other files are not followed."),
    }
    if hub_blob is None:
        return envelope("project_reports", now=now, source_path=HUB_REL, sha256=None,
                        exists=False, as_of=None, as_of_basis=None, data=data,
                        notes=["PROJECT_HUB.json is missing; root dashboards are still listed."])
    as_of, precision = parse_instant((hub_status or {}).get("asOf"))
    basis = f"content:status.asOf ({precision})" if as_of else None
    if as_of is None:
        as_of, basis = _mtime_basis(hub_blob)
    return envelope("project_reports", now=now, source_path=HUB_REL, sha256=hub_blob.sha256,
                    exists=True, as_of=as_of, as_of_basis=basis, data=data)


def _exists(root: Path, rel: str, listing: dict[str, tuple[Path, os.stat_result | None]] | None = None) -> bool:
    try:
        parts = _clean_parts(rel)
        if listing is not None and len(parts) == 1 and parts[0] in listing:
            return True  # a root entry seen in the one directory listing
        return inside(root, rel).exists()
    except ValueError:
        return False


# Served copies get a small shim: the viewer frame is sandboxed without
# allow-same-origin, so storage access would throw inside dashboards that do
# not guard it, and links must be routed through the parent page (which mints
# a fresh single-use ticket) instead of navigating the frame unauthenticated.
REPORT_SHIM = b"""<script>/* ZT add-on: VT viewer shim (served copy only; the source file is unchanged) */
(function(){
  function mem(){var s={};return{getItem:function(k){k=String(k);return Object.prototype.hasOwnProperty.call(s,k)?s[k]:null},setItem:function(k,v){s[String(k)]=String(v)},removeItem:function(k){delete s[String(k)]},clear:function(){s={}},key:function(i){return Object.keys(s)[i]||null},get length(){return Object.keys(s).length}}}
  ["localStorage","sessionStorage"].forEach(function(n){try{window[n].getItem("zt")}catch(e){try{Object.defineProperty(window,n,{configurable:true,enumerable:true,value:mem()})}catch(_){}}});
  var PREFIX="/zt/reports/";
  document.addEventListener("click",function(ev){
    var a=ev.target&&ev.target.closest?ev.target.closest("a[href]"):null;if(!a)return;
    var raw=a.getAttribute("href")||"";if(!raw||raw.charAt(0)==="#")return;
    var u;try{u=new URL(a.href,document.baseURI)}catch(_){return}
    if(u.protocol!=="http:"&&u.protocol!=="https:")return;
    ev.preventDefault();
    if(u.host===location.host){
      if(u.pathname===location.pathname&&u.hash){location.hash=u.hash;return}
      var id=u.pathname.indexOf(PREFIX)===0?decodeURIComponent(u.pathname.slice(PREFIX.length)):"";
      parent.postMessage({type:"zt-report-link",id:id,href:raw},"*");
    }else{window.open(u.href,"_blank","noopener,noreferrer")}
  },true);
})();
</script>"""

REPORT_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
              "img-src data:; font-src data:; media-src data:; connect-src 'none'; "
              "frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; "
              "form-action 'none'; frame-ancestors 'self'; "
              "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox")
NOT_FOUND_CSP = ("default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'; "
                 "sandbox")
_HEAD_RE = re.compile(rb"<head(?:\s[^>]*)?>", re.IGNORECASE)
_HTML_RE = re.compile(rb"<html(?:\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(rb"<!doctype[^>]*>", re.IGNORECASE)


def inject_shim(document: bytes) -> bytes:
    for pattern in (_HEAD_RE, _HTML_RE, _DOCTYPE_RE):
        match = pattern.search(document, 0, 65536)
        if match:
            return document[:match.end()] + REPORT_SHIM + document[match.end():]
    return REPORT_SHIM + document


def report_headers(source_sha256: str, report_id: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": REPORT_CSP,
        "X-Frame-Options": "SAMEORIGIN",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "X-ZT-Report-Id": report_id.encode("ascii", "replace").decode("ascii"),
        "X-ZT-Source-SHA256": source_sha256,
        "X-ZT-PIT-Label": NON_PIT,
    }


def render_report(report_id: str, *, root: Path | None = None) -> tuple[bytes, dict[str, str]]:
    """Return (served bytes, headers) for a whitelisted report id."""
    root = root or project_root()
    entry = report_index(root).get(str(report_id))
    if entry is None:
        raise ReportNotFound(f"not a whitelisted report: {report_id!r}")
    path: Path = entry["_file"]
    stat = entry.get("_stat") or _regular_file_stat(path)
    if stat is None:
        raise ReportNotFound(f"report file is missing: {entry['path']}")
    if stat.st_size > MAX_READ_BYTES:
        raise ValueError(f"report is larger than {MAX_READ_BYTES} bytes")
    data = path.read_bytes()
    meta = _remember_report(_report_key(path, stat), data)
    return inject_shim(data), report_headers(meta["sha256"], entry["id"])


def not_found_headers() -> dict[str, str]:
    return {"Content-Security-Policy": NOT_FOUND_CSP, "X-Frame-Options": "SAMEORIGIN",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store"}


def not_found_page(message: str) -> bytes:
    body = html.escape(message)
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<title>ZT report not available</title><style>body{font:14px system-ui,sans-serif;"
            "margin:2rem;color:#555}</style></head><body><p>" + body +
            "</p><p>Only whitelisted project dashboards are served here.</p></body></html>"
            ).encode("utf-8")
