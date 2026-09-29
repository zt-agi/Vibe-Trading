"""ZT add-on: read-only Vibe-Trading connector over Fidelity's positions CSV export.

Installed as ``<VIBE_TRADING_HOME>/connectors/zt-fidelity/`` (``vibe-trading
connector install``), VT loads this file through
``src.trading.local_plugins.load_adapter`` and calls the three read operations
below with ``credentials={}`` (the manifest declares none) and
``config={"plugin_directory": ...}``.

What it reads: the newest ``Portfolio_Positions_<Mon-DD-YYYY>.csv`` under
``<broker_raw_dir>/<folder_glob>/`` (default ``*_refresh``, which covers both
``<date>_portfolio_refresh`` and ``<date>_refresh``), with the export's
``portfolio_export_context_<stamp>.json`` and ``csv_download_receipts.jsonl``
beside it.  Standard library only; nothing is written anywhere, no network.

Guarantees:
  * Account numbers never leave this module unmasked: every account is
    ``****`` + its last 4 characters, and any occurrence of a full number in a
    free-text field is replaced the same way.  Error messages never quote CSV
    content.
  * as-of = the export time: the context JSON, else the download receipt, else
    the CSV's own "Date downloaded" footer, else the file mtime; the payload
    names which one (``export.as_of_source``).
  * status is ``STALE`` when the export is older than ``stale_after_days``.  VT's
    portfolio then shows the source as failed with that reason instead of
    presenting old holdings as current; ``serve_stale: true`` in settings.json
    serves the data anyway, marked ``export.stale``.
  * Positions are merged per symbol across the export's accounts (one row per
    instrument, as VT's portfolio contract expects per source); the per-account
    split stays in ``accounts``.  ``market_price`` is per unit of ``quantity``
    (so an option's price is per contract and a bond's per $1 of face) and
    ``quantity * market_price`` equals Fidelity's current value.
  * Core cash (``SPAXX**``-style money-market and FCASH rows) and "Pending
    activity" are account cash, not positions.

Settings (all optional), ``settings.json`` beside this file:
  broker_raw_dir    default: $ZT_FIDELITY_BROKER_RAW, else
                    $INVESTMENT_AI_PROJECT_ROOT/broker/raw, else the canonical
                    G:\\My Drive\\work\\Investment-AI-Drive-Research\\broker\\raw
  folder_glob       default "*_refresh"
  file_glob         default "Portfolio_Positions_*.csv"
  stale_after_days  default 3
  serve_stale       default false
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_PROJECT_ROOT = r"G:\My Drive\work\Investment-AI-Drive-Research"
BROKER_RAW_ENV = "ZT_FIDELITY_BROKER_RAW"
PROJECT_ENV = "INVESTMENT_AI_PROJECT_ROOT"
SETTINGS_FILE = "settings.json"
DEFAULTS = {
    "folder_glob": "*_refresh",
    "file_glob": "Portfolio_Positions_*.csv",
    "stale_after_days": 3.0,
    "serve_stale": False,
}
CONTEXT_GLOB = "portfolio_export_context_*.json"
RECEIPTS_FILE = "csv_download_receipts.jsonl"
MAX_CSV_BYTES = 20 * 1024 * 1024
MAX_SIDECAR_BYTES = 5 * 1024 * 1024
FUTURE_TOLERANCE = timedelta(minutes=5)
CURRENCY = "USD"

REQUIRED_COLUMNS = ("account number", "account name", "symbol", "description",
                    "quantity", "last price", "current value")
OPTIONAL_COLUMNS = ("cost basis total", "average cost basis", "total gain/loss dollar", "type")
EXPORT_TIME_KEYS = ("exported_at", "exported_at_utc", "export_time", "export_completed_at",
                    "export_finished_at", "downloaded_at", "download_time", "download_completed_at",
                    "captured_at", "capture_time", "as_of", "asof", "generated_at",
                    "created_at", "timestamp")
RECEIPT_TIME_KEYS = ("downloaded_at", "download_time", "received_at", "verified_at",
                     "saved_at", "completed_at", "captured_at", "created_at", "timestamp", "ts")

_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,19}$")
_FILE_DATE_RE = re.compile(r"Portfolio_Positions_([A-Za-z]{3})-(\d{1,2})-(\d{4})", re.I)
_FOLDER_DATE_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")
_STAMP_RE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})[T_ -]?(\d{2})[-:]?(\d{2})[-:]?(\d{2})(Z)?", re.I)
_FOOTER_RE = re.compile(r"date downloaded\s+(.+)$", re.I)
_CUSIP_RE = re.compile(r"^\d{3}[0-9A-Z]{5}\d$")
_FUND_RE = re.compile(r"^[A-Z]{4}X$")
_OPTION_WORDS = re.compile(r"\b(CALL|PUT)\b", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


class ExportError(RuntimeError):
    """The export is missing or unreadable; the message quotes no CSV content."""


# ---------------------------------------------------------------------------
# Clock and time zone (US Eastern rules since 2007; no tz database needed)
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _eastern_offset_for_local(local: datetime) -> timedelta:
    """UTC offset of a naive America/New_York wall time."""
    start = datetime(local.year, 3, 8, 2)
    start += timedelta(days=(6 - start.weekday()) % 7)      # second Sunday of March, 02:00
    end = datetime(local.year, 11, 1, 2)
    end += timedelta(days=(6 - end.weekday()) % 7)          # first Sunday of November, 02:00
    return timedelta(hours=-4) if start <= local < end else timedelta(hours=-5)


def _from_eastern(local: datetime) -> datetime:
    return (local - _eastern_offset_for_local(local)).replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def parse_instant(raw: Any) -> datetime | None:
    """ISO-like instant -> aware UTC.  A value without an offset is New York
    wall time (Fidelity and the export tooling run on ET); a bare date is
    midnight ET; epoch seconds/milliseconds are accepted."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        seconds = raw / 1000.0 if raw > 1e11 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return _from_eastern(value)
    return value.astimezone(timezone.utc)


def parse_footer_time(text: str) -> datetime | None:
    """Fidelity's "Date downloaded Sep-28-2026 10:15 p.m ET" footer -> UTC."""
    match = _FOOTER_RE.search(text.strip().strip('"'))
    if not match:
        return None
    value = match.group(1).strip().strip('"').rstrip(".")
    value = re.sub(r"\s*\b(ET|EST|EDT)\b\.?$", "", value, flags=re.I).strip()
    value = re.sub(r"\b([ap])\.?\s?m\.?", lambda m: m.group(1).upper() + "M", value, flags=re.I)
    for fmt in ("%b-%d-%Y %I:%M %p", "%m/%d/%Y %I:%M %p", "%b-%d-%Y %I:%M:%S %p",
                "%m/%d/%Y %I:%M:%S %p", "%b-%d-%Y %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return _from_eastern(datetime.strptime(value, fmt))
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    plugin_dir = Path(str((config or {}).get("plugin_directory") or Path(__file__).resolve().parent))
    settings: dict[str, Any] = dict(DEFAULTS)
    path = plugin_dir / SETTINGS_FILE
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ExportError(f"{SETTINGS_FILE} is not valid JSON ({type(exc).__name__})") from exc
        if not isinstance(loaded, dict):
            raise ExportError(f"{SETTINGS_FILE} must hold a JSON object")
        settings.update({k: v for k, v in loaded.items() if v is not None})
    broker_raw = (os.environ.get(BROKER_RAW_ENV, "").strip()
                  or str(settings.get("broker_raw_dir") or "").strip())
    if not broker_raw:
        project = os.environ.get(PROJECT_ENV, "").strip() or DEFAULT_PROJECT_ROOT
        broker_raw = str(Path(project) / "broker" / "raw")
    settings["broker_raw_dir"] = broker_raw
    try:
        settings["stale_after_days"] = float(settings["stale_after_days"])
    except (TypeError, ValueError) as exc:
        raise ExportError("stale_after_days must be a number") from exc
    if settings["stale_after_days"] <= 0:
        raise ExportError("stale_after_days must be positive")
    settings["serve_stale"] = settings["serve_stale"] is True
    return settings


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------

def parse_number(raw: Any) -> Decimal | None:
    """ "$1,234.56", "-$1.00", "+$2.50", "($3.00)", "1.2%", "--", "n/a" -> Decimal | None."""
    text = str(raw if raw is not None else "").strip()
    if text in ("", "--", "-", "n/a", "N/A", "NA", "None"):
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip()
    text = text.replace("$", "").replace(",", "").replace("%", "").replace(" ", "")
    if text.startswith("+"):
        text = text[1:]
    elif text.startswith("-"):
        negative, text = not negative, text[1:]
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return -value if negative else value


def _float(value: Decimal | None, places: str = "0.000001") -> float | None:
    return None if value is None else float(value.quantize(Decimal(places)))


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

def mask_account(account: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "", str(account or ""))
    return "****" + clean[-4:] if clean else "****"


def _scrub(value: Any, secrets: Iterable[str]) -> Any:
    """Replace every full account number in a (nested) payload with its mask."""
    secrets = sorted({s for s in secrets if s and len(s) > 4}, key=len, reverse=True)
    if not secrets:
        return value

    def walk(obj: Any) -> Any:
        if isinstance(obj, str):
            for secret in secrets:
                if secret in obj:
                    obj = obj.replace(secret, mask_account(secret))
            return obj
        if isinstance(obj, dict):
            return {walk(k): walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        return obj
    return walk(value)


# ---------------------------------------------------------------------------
# Locating the newest export
# ---------------------------------------------------------------------------

def _file_date(path: Path) -> date | None:
    match = _FILE_DATE_RE.search(path.name)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(2)))
            except ValueError:
                pass
    match = _FOLDER_DATE_RE.match(path.parent.name)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    return None


def candidate_exports(settings: Mapping[str, Any]) -> list[Path]:
    root = Path(settings["broker_raw_dir"])
    if not root.is_dir():
        raise ExportError(f"broker raw folder not found: {root}")
    found = [p for p in root.glob(f"{settings['folder_glob']}/{settings['file_glob']}") if p.is_file()]
    if not found:
        raise ExportError(f"no {settings['file_glob']} under {root / settings['folder_glob']}")
    return found


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _strings(obj: Any, depth: int = 0) -> Iterable[str]:
    if depth > 6:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _strings(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _strings(value, depth + 1)


def _time_by_keys(obj: Any, keys: tuple[str, ...], depth: int = 0) -> tuple[datetime | None, str | None]:
    """First parseable timestamp found under the preferred keys (breadth first)."""
    if not isinstance(obj, dict) or depth > 4:
        return None, None
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        if key in lowered:
            value = parse_instant(lowered[key])
            if value is not None:
                return value, key
    for value in obj.values():
        if isinstance(value, dict):
            found, key = _time_by_keys(value, keys, depth + 1)
            if found is not None:
                return found, key
    return None, None


def _context_time(csv_path: Path) -> tuple[datetime | None, str | None]:
    contexts = sorted(csv_path.parent.glob(CONTEXT_GLOB), key=lambda p: p.name, reverse=True)
    if not contexts:
        return None, None
    docs = [(p, _read_json(p)) for p in contexts]
    naming = [(p, d) for p, d in docs if any(csv_path.name in s for s in _strings(d))]
    chosen = naming or docs[:1]
    for path, doc in chosen:
        value, key = _time_by_keys(doc, EXPORT_TIME_KEYS)
        if value is not None:
            return value, f"context:{path.name}:{key}"
        match = _STAMP_RE.search(path.stem.replace("portfolio_export_context_", ""))
        if match:
            y, mo, d, h, mi, s, zulu = match.groups()
            local = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
            value = local.replace(tzinfo=timezone.utc) if zulu else _from_eastern(local)
            return value, f"context:{path.name}:filename_stamp"
    return None, None


def _receipt_time(csv_path: Path) -> tuple[datetime | None, str | None]:
    path = csv_path.parent / RECEIPTS_FILE
    try:
        if not path.is_file() or path.stat().st_size > MAX_SIDECAR_BYTES:
            return None, None
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None, None
    best: tuple[datetime | None, str | None] = (None, None)
    for line in lines:
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not any(csv_path.name in s for s in _strings(doc)):
            continue
        value, key = _time_by_keys(doc, RECEIPT_TIME_KEYS)
        if value is not None and (best[0] is None or value > best[0]):
            best = (value, f"receipt:{RECEIPTS_FILE}:{key}")
    return best


def export_as_of(csv_path: Path, footer_time: datetime | None, now: datetime
                 ) -> tuple[datetime, str, list[str]]:
    """The export instant and where it came from; later sources only fill gaps."""
    warnings: list[str] = []
    for label, finder in (("context", lambda: _context_time(csv_path)),
                          ("receipt", lambda: _receipt_time(csv_path)),
                          ("footer", lambda: (footer_time, "csv_footer:Date downloaded"))):
        value, source = finder()
        if value is None:
            continue
        if value > now + FUTURE_TOLERANCE:
            warnings.append(f"{label} time is in the future and was ignored")
            continue
        return value, source, warnings
    mtime = datetime.fromtimestamp(csv_path.stat().st_mtime, timezone.utc)
    warnings.append("no export time in the context JSON, receipts or CSV footer; using the file mtime")
    return mtime, "file_mtime", warnings


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", " ", str(cell or "").replace("\ufeff", "").replace("\u2019", "'")).strip().lower()


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8-sig", errors="replace")


def _asset_type(symbol: str, description: str) -> str | None:
    if symbol.startswith("-") or (_OPTION_WORDS.search(description) and re.search(r"\d", symbol)):
        return "option"
    if _CUSIP_RE.match(symbol):
        return "bond"
    if _FUND_RE.match(symbol):
        return "mutual_fund"
    return None


def parse_positions_csv(raw: bytes) -> dict[str, Any]:
    """Parse one export.  Returns accounts, holdings, cash, counts and the footer time.

    Raw account numbers appear only in the returned ``_secrets`` set, which the
    callers use to scrub the payload and never return.
    """
    rows = list(csv.reader(io.StringIO(_decode(raw))))
    header_at = next((i for i, row in enumerate(rows)
                      if row and _norm_header(row[0]) == "account number"), None)
    if header_at is None:
        raise ExportError("no 'Account number' header row found; not a Fidelity positions export")
    header = [_norm_header(c) for c in rows[header_at]]
    index = {name: i for i, name in enumerate(header) if name}
    missing = [c for c in REQUIRED_COLUMNS if c not in index]
    if missing:
        raise ExportError("positions export is missing columns: " + ", ".join(missing))

    def cell(row: list[str], name: str) -> str:
        i = index.get(name)
        return row[i].strip() if i is not None and i < len(row) and row[i] is not None else ""

    counts = {"positions": 0, "cash": 0, "pending": 0, "skipped_no_quantity": 0,
              "skipped_no_symbol": 0, "non_data_lines": 0, "repeated_header": 0}
    accounts: dict[str, dict[str, Any]] = {}
    lines: list[dict[str, Any]] = []
    secrets: set[str] = set()
    footer_time: datetime | None = None
    value_column = index["current value"]
    for row in rows[header_at + 1:]:
        cells = [str(c).strip() for c in row]
        if not any(cells):
            continue
        first = cells[0]
        if _norm_header(first) == "account number":
            counts["repeated_header"] += 1
            continue
        # A data row starts with an account id and reaches the value column; the
        # disclaimer paragraphs and the "Date downloaded" line are single cells.
        if (not _ACCOUNT_RE.match(first) or sum(1 for c in cells if c) < 3
                or len(cells) <= value_column):
            counts["non_data_lines"] += 1
            footer_time = footer_time or parse_footer_time(" ".join(cells))
            continue
        secrets.add(first)
        masked = mask_account(first)
        account = accounts.setdefault(masked, {
            "account": masked, "name": cell(row, "account name"), "currency": CURRENCY,
            "value": Decimal("0"), "core_cash": Decimal("0"), "pending_activity": Decimal("0"),
            "positions": 0, "value_complete": True})
        symbol = cell(row, "symbol").replace(" ", "").upper()
        description = cell(row, "description")
        value = parse_number(cell(row, "current value"))
        if value is None:
            account["value_complete"] = False
        else:
            account["value"] += value
        if symbol.startswith("PENDINGACTIVITY") or (not symbol and "pending activity" in description.lower()):
            counts["pending"] += 1
            account["pending_activity"] += value or Decimal("0")
            continue
        if (symbol.endswith("**") or symbol in ("FCASH", "CORE")
                or re.search(r"HELD IN (MONEY MARKET|FCASH)", description, re.I)):
            counts["cash"] += 1
            if value is None:
                qty = parse_number(cell(row, "quantity"))
                value = qty if qty is not None else Decimal("0")
                account["value"] += value
            account["core_cash"] += value
            continue
        if not symbol:
            counts["skipped_no_symbol"] += 1
            continue
        quantity = parse_number(cell(row, "quantity"))
        if quantity is None or quantity == 0:
            counts["skipped_no_quantity"] += 1
            continue
        counts["positions"] += 1
        account["positions"] += 1
        lines.append({
            "account": masked, "account_name": account["name"], "symbol": symbol,
            "description": description, "quantity": quantity,
            "last_price": parse_number(cell(row, "last price")), "value": value,
            "cost_basis": parse_number(cell(row, "cost basis total")),
            "average_cost": parse_number(cell(row, "average cost basis")),
            "gain": parse_number(cell(row, "total gain/loss dollar")),
            "type": cell(row, "type"),
        })
    if not accounts:
        raise ExportError("the positions export holds no account rows")
    return {"accounts": accounts, "lines": lines, "counts": counts,
            "footer_time": footer_time, "_secrets": secrets}


def _unit_price(quantity: Decimal, value: Decimal | None, last: Decimal | None
                ) -> tuple[Decimal | None, Decimal]:
    """(price per unit of quantity, multiplier vs the quoted last price)."""
    if value is None or quantity == 0:
        return last, Decimal("1")
    implied = value / quantity
    if last is not None and last != 0:
        tolerance = max(Decimal("0.01"), abs(last) * Decimal("0.005"))
        if abs(implied - last) <= tolerance:
            return last, Decimal("1")
        return implied, implied / last
    return implied, Decimal("1")


def merge_holdings(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One holding per symbol across accounts; values summed, prices per unit."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        groups.setdefault(line["symbol"], []).append(line)
    holdings = []
    for symbol, parts in groups.items():
        quantity = sum((p["quantity"] for p in parts), Decimal("0"))
        values = [p["value"] for p in parts]
        value = sum(values, Decimal("0")) if all(v is not None for v in values) else None
        lasts = {p["last_price"] for p in parts if p["last_price"] is not None}
        last = lasts.pop() if len(lasts) == 1 else None
        if quantity != 0 and value is not None:
            price, multiplier = _unit_price(quantity, value, last)
        else:
            price, multiplier = last, Decimal("1")
        bases = [p["cost_basis"] for p in parts]
        cost_basis = sum(bases, Decimal("0")) if all(b is not None for b in bases) else None
        gains = [p["gain"] for p in parts]
        gain = sum(gains, Decimal("0")) if all(g is not None for g in gains) else None
        if cost_basis is not None and quantity != 0:
            cost_price = cost_basis / quantity
        elif len(parts) == 1 and parts[0]["average_cost"] is not None:
            cost_price = parts[0]["average_cost"] * multiplier
        else:
            cost_price = None
        description = parts[0]["description"] or symbol
        holding = {
            "symbol": symbol,
            "name": description,
            "quantity": _float(quantity),
            "market_price": _float(price) if price is not None and price > 0 else None,
            "cost_price": _float(cost_price) if cost_price is not None and cost_price > 0 else None,
            "market_value": _float(value, "0.01"),
            "unrealized_pnl": _float(gain, "0.01"),
            "currency": CURRENCY,
            "market": "US",
            "last_price": _float(last),
            "price_multiplier": _float(multiplier, "0.0001"),
            "account": ", ".join(sorted({p["account"] for p in parts})),
            "accounts": sorted({p["account"] for p in parts}),
            "held_in": [f"{p['account_name']} {p['account']}".strip() for p in parts],
            "position_type": "|".join(sorted({p["type"] for p in parts if p["type"]})),
            "source": "fidelity_positions_export",
        }
        asset_type = _asset_type(symbol, description)
        if asset_type:
            holding["asset_type"] = asset_type
        holdings.append(holding)
    return sorted(holdings, key=lambda h: -(h["market_value"] or 0.0))


# ---------------------------------------------------------------------------
# Export snapshot (selection + parse + staleness)
# ---------------------------------------------------------------------------

def load_export(config: Mapping[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    """Pick the newest readable export and describe it.

    Exports are grouped by the date in their file name (else their folder's
    date, else their mtime).  Within the newest date the latest export time
    wins; an unreadable file there is reported and the next one is used, and
    only when a whole date is unreadable does an older date get a turn.
    """
    now = now or _utcnow()
    settings = load_settings(config)
    candidates = candidate_exports(settings)
    by_day: dict[date, list[Path]] = {}
    for path in candidates:
        day = _file_date(path) or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()
        by_day.setdefault(day, []).append(path)
    skipped: list[str] = []
    best = None
    for day in sorted(by_day, reverse=True):
        choices = []
        for path in sorted(by_day[day]):
            try:
                if path.stat().st_size > MAX_CSV_BYTES:
                    raise ExportError(f"larger than {MAX_CSV_BYTES} bytes")
                raw = path.read_bytes()
                parsed = parse_positions_csv(raw)
            except (OSError, ExportError) as exc:
                reason = str(exc) if isinstance(exc, ExportError) else type(exc).__name__
                skipped.append(f"{path.parent.name}/{path.name} unreadable: {reason}")
                continue
            as_of, source, warnings = export_as_of(path, parsed["footer_time"], now)
            key = (as_of, path.stat().st_mtime_ns, path.name)
            choices.append((key, path, day, parsed, as_of, source, warnings, raw))
        if choices:
            best = max(choices, key=lambda choice: choice[0])
            break
    if best is None:
        raise ExportError("no readable positions export: " + "; ".join(skipped[:3]))
    _, path, day, parsed, as_of, source, warnings, raw = best
    warnings = skipped + warnings
    age_days = max(0.0, (now - as_of).total_seconds() / 86400.0)
    stale = age_days > settings["stale_after_days"]
    root = Path(settings["broker_raw_dir"])
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    if day and (as_of.date() - day).days not in (-1, 0, 1):
        warnings.append("the export time and the date in the file name differ by more than a day")
    dropped = parsed["counts"]["skipped_no_quantity"] + parsed["counts"]["skipped_no_symbol"]
    if dropped:
        warnings.append(f"{dropped} row(s) without a symbol or quantity were left out of the holdings")
    export = {
        "file": relative,
        "file_date": day.isoformat() if day else None,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "as_of": _iso(as_of),
        "as_of_source": source,
        "age_days": round(age_days, 3),
        "stale_after_days": settings["stale_after_days"],
        "stale": stale,
        "serve_stale": settings["serve_stale"],
        "candidates_considered": len(candidates),
        "rows": parsed["counts"],
        "warnings": warnings,
    }
    return {"export": export, "parsed": parsed, "settings": settings}


def _stale_message(export: Mapping[str, Any]) -> str:
    return (f"Fidelity export is STALE: newest positions file {export['file']} is as of "
            f"{export['as_of']} ({export['age_days']:.1f} days old; limit "
            f"{export['stale_after_days']:g}). Download a fresh Portfolio_Positions CSV, or set "
            f"serve_stale in the connector's settings.json to view it anyway.")


def _envelope(export: Mapping[str, Any]) -> dict[str, Any]:
    stale = bool(export["stale"])
    if stale and not export["serve_stale"]:
        return {"status": "STALE", "error": _stale_message(export)}
    return {"status": "ok"}


def _failure(exc: Exception) -> dict[str, Any]:
    message = str(exc) if isinstance(exc, ExportError) else f"{type(exc).__name__} while reading the export"
    return {"status": "error", "error": message, "readonly": True}


# ---------------------------------------------------------------------------
# VT read operations
# ---------------------------------------------------------------------------

def check_status(*, credentials: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    try:
        snapshot = load_export(config)
    except Exception as exc:  # noqa: BLE001 - reported, never raised into VT
        return {**_failure(exc), "export_found": False}
    export = snapshot["export"]
    payload: dict[str, Any] = {"readonly": True, "export_found": True, "export": export,
                               "capabilities": ["account.read", "positions.read"],
                               "accounts": sorted(snapshot["parsed"]["accounts"])}
    if export["stale"]:
        # 'configured' is omitted on purpose: the Web UI's connection test treats
        # configured=true as success, and a stale export must read as a failure.
        payload.update(status="STALE", error=_stale_message(export))
    else:
        payload.update(status="ok", configured=True)
    return _scrub(payload, snapshot["parsed"]["_secrets"])


def get_account_snapshot(*, credentials: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    try:
        snapshot = load_export(config)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    export, parsed = snapshot["export"], snapshot["parsed"]
    envelope = _envelope(export)
    total = sum((a["value"] for a in parsed["accounts"].values()), Decimal("0"))
    core = sum((a["core_cash"] for a in parsed["accounts"].values()), Decimal("0"))
    pending = sum((a["pending_activity"] for a in parsed["accounts"].values()), Decimal("0"))
    breakdown = [{
        "account": a["account"], "name": a["name"], "currency": a["currency"],
        "total_value": _float(a["value"], "0.01"), "core_cash": _float(a["core_cash"], "0.01"),
        "pending_activity": _float(a["pending_activity"], "0.01"),
        "positions": a["positions"], "total_complete": a["value_complete"],
    } for a in sorted(parsed["accounts"].values(), key=lambda a: a["account"])]
    payload = {
        **envelope,
        "readonly": True,
        "account": {
            "currency": CURRENCY,
            "portfolio_value": _float(total, "0.01"),
            "cash": _float(core + pending, "0.01"),
            "core_cash": _float(core, "0.01"),
            "pending_activity": _float(pending, "0.01"),
            "account_count": len(breakdown),
            "as_of": export["as_of"],
            "as_of_source": export["as_of_source"],
            "export_file": export["file"],
            "stale": export["stale"],
        },
        # VT's CLI joins ``accounts`` as account ids (IBKR convention): masked ids only
        "accounts": [row["account"] for row in breakdown],
        "account_breakdown": breakdown,
        "export": export,
    }
    return _scrub(payload, parsed["_secrets"])


def get_positions(*, credentials: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    try:
        snapshot = load_export(config)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    export, parsed = snapshot["export"], snapshot["parsed"]
    payload = {**_envelope(export), "readonly": True,
               "positions": merge_holdings(parsed["lines"]), "export": export}
    return _scrub(payload, parsed["_secrets"])
