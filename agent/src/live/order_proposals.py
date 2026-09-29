"""Order proposals: a human approves every order (ZT add-on).

With ``VIBE_ORDER_APPROVAL=required`` every order path in Vibe-Trading stops
at a *proposal* instead of reaching a broker:

* ``src.trading.service.place_order`` (agent ``trading_place_order``, paper
  profiles and the live direct-SDK gate) and ``_route_sdk_write`` (eToro
  position actions);
* ``src.live.order_guard.LiveOrderGuardTool`` (remote MCP live brokers, used
  by chat sessions and by the live runner's autonomous turns), after VT's
  own mandate / kill-switch / exposure checks passed;
* the runner's halt sweep, whose closing orders are held instead of sent;
* the ``zt-paper`` simulated broker, whose orders are proposals always,
  whatever the environment says.

A proposal is one JSON file under ``<VIBE_TRADING_HOME>/live/proposals/``:
the orders, the decision record (signals, current vs target weights, clamp
events, direction permissions, projected exposure), a validation report, the
exact call to replay, and ``content_hash`` over all of that. Lifecycle::

    PENDING -> APPROVED -> SUBMITTED -> FILLED | FAILED
    PENDING -> REJECTED | EXPIRED          (TTL: VIBE_ORDER_APPROVAL_TTL_MIN, 15)

Every transition is appended to a hash-chained ledger
(``src.governance.ledger``) at ``proposals/ledger.jsonl``. Approval is a
privileged surface action (the ``/zt/orders`` routes of
``extensions/zt_approvals``), never an agent tool: it re-checks expiry, the
content hash (against the file and the ledger's creation record), the
confirmation hash the human saw, the kill switch and mandate for live brokers,
and re-runs validation against a fresh book. The approved orders are then
submitted exactly once: a single-use claim file marks the proposal, and the
replay runs VT's own order path under a one-shot grant that lets exactly the
recorded call through the hook. Any change to an order means a new proposal.

Validation ports the ideas of ai-hedge-fund (virattt/ai-hedge-fund, MIT,
``portfolio/validation.py`` validate_targets and ``pipeline/run_cycle.py``
projection checks): evidence, weights and orders must agree; a long-only
source can reduce a long but never create a short; per-name, gross, net and
single-order caps; a price collar against the last completed close; every
order symbol needs a finite positive reference price; holdings outside the
proposal's account scope are never touched.
"""

from __future__ import annotations

import contextvars
import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import secrets
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from src.config.accessor import get_env_value
from src.live.paths import live_root

try:  # POSIX advisory lock.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows advisory byte-range lock.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SCHEMA = "vt-order-proposal/v1"
APPROVAL_ENV = "VIBE_ORDER_APPROVAL"
TTL_ENV = "VIBE_ORDER_APPROVAL_TTL_MIN"
DEFAULT_TTL_MINUTES = 15.0
MIN_TTL_MINUTES = 1.0
MAX_TTL_MINUTES = 24 * 60.0

#: The simulated broker (extensions/zt_approvals/zt_paper.py) and its account.
ZT_PAPER = "zt-paper"
ZT_PAPER_ACCOUNT = "ZT-PAPER"

#: Recorded approver for the single-user desktop / Web UI.
APPROVER = "local-user"

PENDING = "PENDING"
APPROVED = "APPROVED"
SUBMITTED = "SUBMITTED"
FILLED = "FILLED"
FAILED = "FAILED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
STATUSES = (PENDING, APPROVED, SUBMITTED, FILLED, FAILED, REJECTED, EXPIRED)
TERMINAL_STATUSES = frozenset({FILLED, FAILED, REJECTED, EXPIRED})

#: Fields covered by ``content_hash``. Lifecycle fields are not.
HASHED_FIELDS = (
    "schema", "id", "created_utc", "expires_utc", "ttl_minutes", "origin", "broker",
    "account_scope", "orders", "decision_record", "validation", "route",
)

_ID_RE = re.compile(r"^op_[0-9a-f]{32}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-_/:]{0,31}$")
_TOL = 1e-9
_OFF_VALUES = frozenset({"", "off", "0", "false", "no", "none", "disabled"})
_REQUIRED_VALUES = frozenset({"required", "on", "1", "true", "yes", "enabled"})

DEFAULT_POLICY: dict[str, Any] = {
    "max_position_pct": 0.25,
    "max_gross_exposure": 1.0,
    "max_net_exposure": 1.0,
    "max_order_notional_usd": 25_000.0,
    "price_collar_pct": 0.10,
    "short_capable_sources": [],
}
_POSITIVE_POLICY_KEYS = (
    "max_position_pct", "max_gross_exposure", "max_net_exposure",
    "max_order_notional_usd", "price_collar_pct",
)

_DIRECTIONS = {
    "long": "long", "bullish": "long", "buy": "long", "increase": "long",
    "short": "short", "bearish": "short", "sell": "short",
    "reduce": "reduce", "trim": "reduce", "decrease": "reduce",
    "flat": "flat", "close": "flat", "exit": "flat",
    "neutral": "neutral", "hold": "neutral",
}


class ProposalError(Exception):
    """A proposal operation was refused; carries an HTTP-style status and code."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "invalid",
                 proposal: dict | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.proposal = proposal

    def to_dict(self) -> dict[str, Any]:
        """Return the error as a JSON-serializable mapping."""
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.proposal is not None:
            payload["proposal"] = public_view(self.proposal)
        return payload


# ---------------------------------------------------------------------------
# Environment, clock, paths
# ---------------------------------------------------------------------------


def approval_mode() -> str:
    """Return ``"required"`` or ``"off"`` from ``VIBE_ORDER_APPROVAL``.

    An unrecognized non-empty value counts as ``required`` (fail closed).
    """
    raw = get_env_value(APPROVAL_ENV, "").strip().lower()
    if raw in _OFF_VALUES:
        return "off"
    if raw not in _REQUIRED_VALUES:
        logger.warning("%s=%r is not recognized; treating it as 'required'", APPROVAL_ENV, raw)
    return "required"


def approval_required() -> bool:
    """Whether every order must wait for a human approval."""
    return approval_mode() == "required"


def ttl_minutes() -> float:
    """Proposal lifetime in minutes (``VIBE_ORDER_APPROVAL_TTL_MIN``, default 15, 1..1440)."""
    raw = get_env_value(TTL_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TTL_MINUTES
    except ValueError:
        value = DEFAULT_TTL_MINUTES
    if not math.isfinite(value):
        value = DEFAULT_TTL_MINUTES
    return min(MAX_TTL_MINUTES, max(MIN_TTL_MINUTES, value))


def _now() -> datetime:
    """The current UTC time (patched in tests)."""
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(raw: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def proposals_dir() -> Path:
    """``<runtime root>/live/proposals``."""
    return live_root() / "proposals"


def ledger_path() -> Path:
    """Hash-chained transition ledger of every proposal."""
    return proposals_dir() / "ledger.jsonl"


def policy_path() -> Path:
    """Human-edited limits (``<runtime root>/live/order_approval_policy.json``)."""
    return live_root() / "order_approval_policy.json"


def valid_proposal_id(proposal_id: Any) -> bool:
    """Whether ``proposal_id`` has the ``op_<32 hex>`` form."""
    return isinstance(proposal_id, str) and bool(_ID_RE.fullmatch(proposal_id))


def proposal_path(proposal_id: str) -> Path:
    """The JSON file of one proposal."""
    if not valid_proposal_id(proposal_id):
        raise ProposalError("proposal id must look like op_<32 hex>", status_code=404, code="not_found")
    return proposals_dir() / f"{proposal_id}.json"


def new_proposal_id() -> str:
    """A fresh ``op_<32 hex>`` id."""
    return f"op_{secrets.token_hex(16)}"


# ---------------------------------------------------------------------------
# Canonical JSON, hashing, files, locks
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for hashing (sorted keys, no NaN)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(proposal: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` over the hashed fields of a proposal."""
    return _sha256(canonical_json({key: proposal.get(key) for key in HASHED_FIELDS}))


def hash_tail(value: str, length: int = 12) -> str:
    """The last ``length`` hex characters of a hash, as shown to the approver."""
    return str(value or "")[-length:]


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON through a same-directory temp file and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _try_lock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    raise OSError("no supported advisory lock backend")


def _unlock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def file_lock(path: Path, *, wait_seconds: float = 10.0) -> Iterator[None]:
    """Hold an exclusive cross-process advisory lock on ``path``.

    Raises:
        ProposalError: 409 ``busy`` when the lock stays held past ``wait_seconds``.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+b")
    deadline = time.monotonic() + max(0.0, wait_seconds)
    try:
        while True:
            try:
                _try_lock(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ProposalError("another operation on this record is in progress",
                                        status_code=409, code="busy") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


def _proposal_lock(proposal_id: str, *, wait_seconds: float = 10.0):
    return file_lock(proposals_dir() / ".locks" / f"{proposal_id}.lock", wait_seconds=wait_seconds)


def _finite(value: Any) -> float | None:
    """``value`` as a finite float, else ``None`` (bools are not numbers here)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 10) -> float | None:
    return None if value is None else round(float(value), digits)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _ledger_append(event: Mapping[str, Any]) -> dict[str, Any]:
    from src.governance.ledger import append_record

    payload = {"type": "order_proposal", "at_utc": _iso(_now()), **event}
    return append_record(ledger_path(), payload, fsync=True)


def ledger_events(proposal_id: str | None = None) -> list[dict[str, Any]]:
    """Ledger records, optionally only those of one proposal (oldest first)."""
    path = ledger_path()
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                break
            if proposal_id is None or record.get("proposal_id") == proposal_id:
                events.append(record)
    return events


def verify_ledger() -> dict[str, Any]:
    """Walk the ledger's hash chain (``src.governance.ledger.verify_chain``)."""
    from src.governance.ledger import verify_chain

    return verify_chain(ledger_path()).to_dict()


def _record_transition(proposal: dict, to: str, *, actor: str, reason: str,
                       detail: Mapping[str, Any] | None = None, event: str = "transition") -> dict:
    """Append a ledger record first, then reflect the transition on the proposal."""
    previous = proposal.get("status")
    record = _ledger_append({
        "event": event,
        "proposal_id": proposal["id"],
        "from": previous,
        "to": to,
        "actor": actor,
        "reason": reason,
        "content_hash": proposal.get("content_hash"),
        "detail": dict(detail or {}),
    })
    proposal["status"] = to
    proposal.setdefault("transitions", []).append({
        "from": previous, "to": to, "at_utc": record["at_utc"], "actor": actor,
        "reason": reason, "ledger_seq": record["seq"], "ledger_record_hash": record["record_hash"],
    })
    proposal["updated_utc"] = record["at_utc"]
    return record


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def load_policy() -> dict[str, Any]:
    """Validation limits: defaults, overlaid by the human-edited policy file.

    A policy file that cannot be read or holds an invalid value yields the
    defaults plus an ``error`` key, which fails validation (fail closed).
    """
    policy = {**DEFAULT_POLICY, "short_capable_sources": list(DEFAULT_POLICY["short_capable_sources"])}
    path = policy_path()
    policy["source"] = "defaults"
    if not path.exists():
        return policy
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("policy must be a JSON object")
        for key in _POSITIVE_POLICY_KEYS:
            if key in raw:
                value = _finite(raw[key])
                if value is None or value <= 0:
                    raise ValueError(f"{key} must be a finite positive number")
                policy[key] = value
        if "short_capable_sources" in raw:
            sources = raw["short_capable_sources"]
            if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
                raise ValueError("short_capable_sources must be a list of source names")
            policy["short_capable_sources"] = sorted({item.strip().lower() for item in sources if item.strip()})
        policy["source"] = str(path)
    except (OSError, ValueError) as exc:
        policy["error"] = f"order approval policy {path.name} is invalid: {exc}"
    return policy


# ---------------------------------------------------------------------------
# Reference prices (last completed session close)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefPrice:
    """A reference close: the last bar of a completed session."""

    symbol: str
    price: float
    bar_date: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "price": self.price, "bar_date": self.bar_date, "source": self.source}


PriceLookup = Callable[[str], "RefPrice | None"]

_MARKET_TZ = {
    "us_equity": "America/New_York",
    "hk_equity": "Asia/Hong_Kong",
    "a_share": "Asia/Shanghai",
    "india_equity": "Asia/Kolkata",
    "crypto": "UTC",
    "forex": "UTC",
}


def completed_session_cutoff(market: str = "us_equity", now: datetime | None = None) -> date:
    """Latest bar date that counts as a completed session.

    Follows ai-hedge-fund's ``completed_through``: the market's current local
    date is excluded even after the close, because a vendor's same-day bar is
    provisional until it is finalized.
    """
    from zoneinfo import ZoneInfo

    moment = now or _now()
    local = moment.astimezone(ZoneInfo(_MARKET_TZ.get(market, "America/New_York")))
    return local.date() - timedelta(days=1)


def loader_code(symbol: str, market: str) -> str:
    """VT loader code for ``symbol`` (``AAPL`` -> ``AAPL.US`` for US equities)."""
    token = symbol.strip().upper()
    if market == "us_equity" and "." not in token:
        return f"{token}.US"
    return token


def vt_loader_close(symbol: str, *, market: str = "us_equity", now: datetime | None = None,
                    window_days: int = 12) -> RefPrice | None:
    """Last completed-session close from VT's own loader chain (fail closed: ``None``)."""
    try:
        import pandas as pd
        from backtest.loaders.base import NoAvailableSourceError
        from backtest.loaders.registry import resolve_loader
    except Exception as exc:  # noqa: BLE001
        logger.warning("VT price loaders unavailable: %s", exc)
        return None
    cutoff = completed_session_cutoff(market, now)
    start = cutoff - timedelta(days=window_days)
    code = loader_code(symbol, market)
    try:
        loader = resolve_loader(market)
        frames = loader.fetch([code], start.isoformat(), (cutoff + timedelta(days=1)).isoformat(), interval="1D")
    except NoAvailableSourceError as exc:
        logger.warning("no price source for %s: %s", symbol, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - loader/network failure fails closed
        logger.warning("price fetch failed for %s: %s", symbol, exc)
        return None
    frame = frames.get(code) if isinstance(frames, dict) else None
    if frame is None or getattr(frame, "empty", True) or "close" not in frame.columns:
        return None
    try:
        dates = pd.to_datetime(frame.index).date
    except Exception:  # noqa: BLE001
        return None
    best: tuple[date, float] | None = None
    for bar_date, close in zip(dates, frame["close"].tolist()):
        value = _finite(close)
        if value is None or value <= 0 or bar_date > cutoff:
            continue
        if best is None or bar_date >= best[0]:
            best = (bar_date, value)
    if best is None:
        return None
    return RefPrice(symbol=symbol.strip().upper(), price=best[1], bar_date=best[0].isoformat(),
                    source=f"vt:{getattr(loader, 'name', 'loader')}")


def _market_for(broker: Mapping[str, Any], symbol: str) -> str:
    """Loader market for a symbol traded through ``broker``."""
    if broker.get("key") == ZT_PAPER:
        return "us_equity"
    try:
        from src.live.enforcement import _ASSET_CLASS_MARKET, instrument_asset_class
        from src.trading.service import _order_classification

        instrument, asset_class = _order_classification(str(broker.get("connector") or ""), symbol)
        asset_class = asset_class or instrument_asset_class(instrument)
        return _ASSET_CLASS_MARKET.get(asset_class, "us_equity") if asset_class else "us_equity"
    except Exception:  # noqa: BLE001
        return "us_equity"


def default_price_lookup(broker: Mapping[str, Any]) -> PriceLookup:
    """The reference-price function for a broker (zt-paper uses its own source)."""
    if broker.get("key") == ZT_PAPER:
        return zt_paper_engine().reference_close
    return lambda symbol: vt_loader_close(symbol, market=_market_for(broker, symbol))


def cached_lookup(lookup: PriceLookup) -> PriceLookup:
    """Memoize a price lookup for one operation (one fetch per symbol)."""
    cache: dict[str, RefPrice | None] = {}

    def lookup_once(symbol: str) -> RefPrice | None:
        if symbol not in cache:
            cache[symbol] = lookup(symbol)
        return cache[symbol]

    return lookup_once


# ---------------------------------------------------------------------------
# Books (current positions and equity)
# ---------------------------------------------------------------------------


@dataclass
class Book:
    """An account's current positions (signed quantities), marks and equity."""

    account: str
    positions: dict[str, float]
    marks: dict[str, float]
    equity: float | None
    cash: float | None = None
    source: str = ""
    error: str | None = None
    equity_basis: str = "account"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account, "positions": dict(self.positions), "marks": dict(self.marks),
            "equity": self.equity, "cash": self.cash, "source": self.source, "error": self.error,
            "equity_basis": self.equity_basis,
        }


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("status") == "ok" and isinstance(payload.get("data"), (dict, list)):
        data = payload["data"]
        if isinstance(data, dict) and "positions" not in data and "account" not in data and "data" in data:
            return data["data"]
        return data
    return payload


def _signed_quantity(row: Mapping[str, Any]) -> float | None:
    quantity = None
    for key in ("quantity", "qty", "shares", "units", "position"):
        if key in row:
            quantity = _finite(row[key])
            break
    if quantity is None:
        return None
    side = str(row.get("side") or "").strip().lower()
    if side in {"short", "sell"} and quantity > 0:
        quantity = -quantity
    return quantity


def _row_price(row: Mapping[str, Any], quantity: float) -> float | None:
    for key in ("market_price", "price", "last_price", "mark_price", "current_price", "last"):
        value = _finite(row.get(key))
        if value is not None and value > 0:
            return value
    for key in ("market_value", "marketValue", "value_usd", "value"):
        value = _finite(row.get(key))
        if value is not None and quantity:
            price = abs(value / quantity)
            if price > 0:
                return price
    return None


def _equity_from_balance(balance: Any) -> float | None:
    candidates: list[Mapping[str, Any]] = []
    body = _unwrap(balance)
    if isinstance(body, dict):
        candidates.append(body)
        for key in ("account", "summary", "portfolio"):
            if isinstance(body.get(key), dict):
                candidates.append(body[key])
    for mapping in candidates:
        for key in ("equity", "portfolio_value", "net_liquidation", "total_equity", "account_value",
                    "net_asset_value", "total_assets", "total_value"):
            value = _finite(mapping.get(key))
            if value is not None and value > 0:
                return value
    return None


def book_from_payloads(positions: Any, balance: Any, *, account: str, source: str,
                       price_lookup: PriceLookup | None = None,
                       equity_fallback: float | None = None) -> Book:
    """Normalize a connector's positions / account payloads into a :class:`Book`."""
    from src.live.enforcement import coerce_position_rows

    rows = coerce_position_rows(_unwrap(positions))
    if rows is None:
        return Book(account=account, positions={}, marks={}, equity=None, source=source,
                    error="current positions could not be read")
    held: dict[str, float] = {}
    marks: dict[str, float] = {}
    for row in rows:
        symbol = str(row.get("symbol") or row.get("ticker") or row.get("code") or "").strip().upper()
        quantity = _signed_quantity(row)
        if not symbol or quantity is None:
            return Book(account=account, positions={}, marks={}, equity=None, source=source,
                        error="a position row has no symbol or quantity")
        if quantity == 0:
            continue
        held[symbol] = held.get(symbol, 0.0) + quantity
        price = _row_price(row, quantity)
        if price is None and price_lookup is not None:
            ref = price_lookup(symbol)
            price = ref.price if ref is not None else None
        if price is not None:
            marks[symbol] = price
    equity = _equity_from_balance(balance)
    basis = "account"
    if equity_fallback is not None:
        equity, basis = float(equity_fallback), "mandate.account_funding_usd"
    return Book(account=account, positions=held, marks=marks, equity=equity, source=source,
                equity_basis=basis,
                error=None if equity is not None else "account equity could not be read")


def _vt_profile_book(profile: Any, overrides: Mapping[str, Any], account: str,
                     price_lookup: PriceLookup, mandate: Any | None) -> Book:
    from src.trading import service

    options = {key: value for key, value in dict(overrides or {}).items() if key != "session_id"}
    try:
        positions = service.get_positions(profile.id, **options)
        balance = service.get_account(profile.id, **options)
    except Exception as exc:  # noqa: BLE001 - a failed read fails validation
        return Book(account=account, positions={}, marks={}, equity=None, source=f"vt:{profile.id}",
                    error=f"broker reads failed: {exc}")
    for payload in (positions, balance):
        if isinstance(payload, dict) and str(payload.get("status", "")).lower() == "error":
            return Book(account=account, positions={}, marks={}, equity=None, source=f"vt:{profile.id}",
                        error=f"broker read failed: {payload.get('error') or 'error'}")
    fallback = None
    if mandate is not None and profile.environment == "live":
        fallback = mandate.hard_caps.account_funding_usd
    return book_from_payloads(positions, balance, account=account, source=f"vt:{profile.id}",
                              price_lookup=price_lookup, equity_fallback=fallback)


# ---------------------------------------------------------------------------
# The zt-paper engine (extensions/zt_approvals/zt_paper.py)
# ---------------------------------------------------------------------------

_ENGINE_MODULE = "zt_approvals_zt_paper"


def repo_root() -> Path:
    """The fork checkout (``agent/src/live/order_proposals.py`` -> repo)."""
    return Path(__file__).resolve().parents[3]


def zt_paper_engine() -> Any:
    """Load the zt-paper engine from this checkout's extensions folder (cached)."""
    module = sys.modules.get(_ENGINE_MODULE)
    if module is not None:
        return module
    path = repo_root() / "extensions" / "zt_approvals" / "zt_paper.py"
    if not path.is_file():
        raise ProposalError(f"the zt-paper engine is missing ({path})", status_code=503, code="unavailable")
    spec = importlib.util.spec_from_file_location(_ENGINE_MODULE, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ENGINE_MODULE] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(_ENGINE_MODULE, None)
        raise
    return module


# ---------------------------------------------------------------------------
# Order and signal normalization
# ---------------------------------------------------------------------------


def normalize_symbol(symbol: Any, broker: Mapping[str, Any]) -> str:
    token = str(symbol or "").strip().upper()
    if broker.get("key") == ZT_PAPER:
        return zt_paper_engine().normalize_symbol(token)
    if not _SYMBOL_RE.fullmatch(token):
        raise ValueError(f"invalid symbol {symbol!r}")
    return token


def normalize_orders(orders: Sequence[Mapping[str, Any]], broker: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate and normalize explicit orders; raises ``ValueError`` on bad input."""
    if not isinstance(orders, (list, tuple)) or not orders:
        raise ValueError("orders must be a non-empty list")
    if len(orders) > 50:
        raise ValueError("at most 50 orders per proposal")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(orders):
        if not isinstance(raw, Mapping):
            raise ValueError(f"order {index} must be an object")
        symbol = normalize_symbol(raw.get("symbol"), broker)
        if symbol in seen:
            raise ValueError(f"order {index}: {symbol} appears twice; use one order per symbol")
        seen.add(symbol)
        side = str(raw.get("side") or "").strip().lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"order {index}: side must be 'buy' or 'sell'")
        raw_qty = raw.get("qty") if raw.get("qty") is not None else raw.get("quantity")
        raw_notional = raw.get("notional")
        qty = _finite(raw_qty)
        notional = _finite(raw_notional)
        if (raw_qty is not None and qty is None) or (raw_notional is not None and notional is None):
            raise ValueError(f"order {index}: qty and notional must be finite numbers")
        # As in VT's order tool, a zero size counts as "not given".
        qty = None if qty == 0 else qty
        notional = None if notional == 0 else notional
        if (qty is None) == (notional is None):
            raise ValueError(f"order {index}: give exactly one of qty or notional")
        if qty is not None and qty < 0:
            raise ValueError(f"order {index}: qty must be positive (side gives the direction)")
        if notional is not None and notional < 0:
            raise ValueError(f"order {index}: notional must be positive")
        order_type = str(raw.get("order_type") or "market").strip().lower()
        if order_type not in ("market", "limit"):
            raise ValueError(f"order {index}: order_type must be 'market' or 'limit'")
        limit_price = _finite(raw.get("limit_price"))
        if order_type == "limit" and (limit_price is None or limit_price <= 0):
            raise ValueError(f"order {index}: a limit order needs a finite positive limit_price")
        if order_type == "market":
            limit_price = None
        tif = str(raw.get("tif", raw.get("time_in_force")) or "day").strip().lower()
        if tif not in ("day", "gtc"):
            raise ValueError(f"order {index}: tif must be 'day' or 'gtc'")
        result.append({"symbol": symbol, "side": side, "qty": _round(qty, 8), "notional": _round(notional, 6),
                       "order_type": order_type, "limit_price": limit_price, "tif": tif})
    return result


def normalize_targets(targets: Mapping[str, Any], broker: Mapping[str, Any]) -> dict[str, float]:
    """Validate target weights (fractions of account equity)."""
    if not isinstance(targets, Mapping) or not targets:
        raise ValueError("targets must be a non-empty {symbol: weight} object")
    if len(targets) > 50:
        raise ValueError("at most 50 targets per proposal")
    result: dict[str, float] = {}
    for raw_symbol, raw_weight in targets.items():
        symbol = normalize_symbol(raw_symbol, broker)
        weight = _finite(raw_weight)
        if weight is None or abs(weight) > 10:
            raise ValueError(f"target for {symbol} must be a finite weight (fraction of equity)")
        result[symbol] = weight
    return result


def normalize_signals(signals: Sequence[Mapping[str, Any]] | None, policy: Mapping[str, Any],
                      broker: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize signals; the long/short permission comes from the policy, never the caller."""
    short_capable = {str(item).lower() for item in policy.get("short_capable_sources", [])}
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(signals or []):
        if not isinstance(raw, Mapping):
            raise ValueError(f"signal {index} must be an object")
        source = str(raw.get("source") or "").strip()
        if not source or len(source) > 80:
            raise ValueError(f"signal {index}: source is required (at most 80 characters)")
        direction = _DIRECTIONS.get(str(raw.get("direction") or "").strip().lower())
        if direction is None:
            raise ValueError(f"signal {index}: direction must be long, short, reduce, flat or neutral")
        symbol = normalize_symbol(raw.get("symbol"), broker) if raw.get("symbol") else None
        evidence = [str(item).strip()[:200] for item in (raw.get("evidence_ids") or []) if str(item).strip()]
        result.append({
            "source": source,
            "symbol": symbol,
            "direction": direction,
            "rationale": str(raw.get("rationale") or "").strip()[:2000],
            "evidence_ids": evidence[:50],
            "approach": "long_short" if source.lower() in short_capable else "long_only",
        })
    return result


# ---------------------------------------------------------------------------
# Portfolio math (ported from ai-hedge-fund risk/limits.py and pipeline/execution.py)
# ---------------------------------------------------------------------------


def apply_limits(weights: Mapping[str, float], max_position_pct: float,
                 max_gross_exposure: float) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Clamp per-name weights, then scale the book to the gross cap; record every clamp."""
    clamped: dict[str, float] = {}
    events: list[dict[str, Any]] = []
    for symbol in sorted(weights):
        weight = float(weights[symbol])
        if abs(weight) > max_position_pct + _TOL:
            new_weight = max_position_pct if weight > 0 else -max_position_pct
            events.append({"limit": "max_position_pct", "symbol": symbol, "before": weight, "after": new_weight})
            clamped[symbol] = new_weight
        else:
            clamped[symbol] = weight
    gross = sum(abs(value) for value in clamped.values())
    if gross > max_gross_exposure + _TOL:
        scale = max_gross_exposure / gross
        clamped = {symbol: value * scale for symbol, value in clamped.items()}
        events.append({"limit": "max_gross_exposure", "symbol": None, "before": gross, "after": max_gross_exposure})
    return clamped, events


def build_orders(targets: Mapping[str, float], positions: Mapping[str, float], marks: Mapping[str, float],
                 equity: float, scope: Sequence[str]) -> list[dict[str, Any]]:
    """Diff target weights against the book, within ``scope`` only.

    Held names outside the scope are never touched (unlike ai-hedge-fund's
    whole-book diff). Target shares floor toward zero; sells come first.
    """
    sells: list[dict[str, Any]] = []
    buys: list[dict[str, Any]] = []
    for symbol in sorted(set(scope)):
        price = marks.get(symbol)
        if price is None or price <= 0:
            continue
        target_shares = math.trunc(float(targets.get(symbol, 0.0)) * equity / price)
        current = positions.get(symbol, 0.0)
        delta = target_shares - current
        if abs(delta) < 1e-9:
            continue
        order = {"symbol": symbol, "side": "buy" if delta > 0 else "sell", "qty": _round(abs(delta), 8),
                 "notional": None, "order_type": "market", "limit_price": None, "tif": "day"}
        (buys if delta > 0 else sells).append(order)
    return sells + buys


def _order_qty(order: Mapping[str, Any], price: float | None) -> float | None:
    qty = _finite(order.get("qty"))
    if qty is not None:
        return qty
    notional = _finite(order.get("notional"))
    if notional is not None and price:
        return notional / price
    return None


def _exposure(positions: Mapping[str, float], marks: Mapping[str, float], equity: float | None) -> dict[str, Any]:
    values = {symbol: qty * marks[symbol] for symbol, qty in positions.items() if symbol in marks}
    long_value = sum(value for value in values.values() if value > 0)
    short_value = -sum(value for value in values.values() if value < 0)
    result: dict[str, Any] = {"long_usd": _round(long_value, 6), "short_usd": _round(short_value, 6),
                              "gross_usd": _round(long_value + short_value, 6),
                              "net_usd": _round(long_value - short_value, 6)}
    if equity and equity > 0:
        weights = {symbol: value / equity for symbol, value in values.items()}
        top = max(weights.items(), key=lambda item: abs(item[1]), default=(None, 0.0))
        result.update({
            "gross": _round(sum(abs(w) for w in weights.values())),
            "net": _round(sum(weights.values())),
            "max_name": _round(abs(top[1])), "max_name_symbol": top[0],
            "leverage": _round((long_value + short_value) / equity),
        })
    unpriced = sorted(symbol for symbol in positions if symbol not in marks)
    if unpriced:
        result["unpriced"] = unpriced
    return result


def _weights(positions: Mapping[str, float], marks: Mapping[str, float], equity: float | None) -> dict[str, float]:
    if not equity or equity <= 0:
        return {}
    return {symbol: _round(qty * marks[symbol] / equity) for symbol, qty in sorted(positions.items())
            if symbol in marks}


# ---------------------------------------------------------------------------
# Decision record + validation
# ---------------------------------------------------------------------------


def _check(name: str, ok: bool, detail: str, *, blocking: bool = True) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail, "blocking": blocking}


def _describe_ref(ref: Mapping[str, Any]) -> str:
    return f"{ref['symbol']}={ref['price']:g}@{ref['bar_date']} ({ref['source']})"


def evaluate(*, broker: Mapping[str, Any], account_scope: Mapping[str, Any], orders: list[dict[str, Any]],
             targets: Mapping[str, float] | None, signals: list[dict[str, Any]], book: Book,
             prices: Mapping[str, RefPrice | None], policy: Mapping[str, Any],
             live: Mapping[str, Any] | None = None, clamp_events: list[dict[str, Any]] | None = None,
             requested_targets: Mapping[str, float] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the decision record and the validation report for a set of orders."""
    marks = dict(book.marks)
    refs: dict[str, dict[str, Any]] = {}
    for symbol, ref in prices.items():
        if ref is not None and _finite(ref.price) and ref.price > 0:
            refs[symbol] = ref.to_dict()
            marks[symbol] = ref.price
    equity = book.equity
    current = {symbol: qty for symbol, qty in book.positions.items() if abs(qty) > 1e-12}
    projected = dict(current)
    order_rows: list[dict[str, Any]] = []
    for order in orders:
        ref = refs.get(order["symbol"])
        price = ref["price"] if ref else None
        qty = _order_qty(order, price)
        row = dict(order)
        row["reference_price"] = price
        row["reference_date"] = ref["bar_date"] if ref else None
        row["reference_source"] = ref["source"] if ref else None
        if qty is not None:
            signed = qty if order["side"] == "buy" else -qty
            projected[order["symbol"]] = projected.get(order["symbol"], 0.0) + signed
            fill_basis = price
            if order["side"] == "buy" and order.get("limit_price") and price:
                fill_basis = max(price, order["limit_price"])
            row["est_qty"] = _round(qty, 8)
            row["est_notional_usd"] = _round(qty * fill_basis, 6) if fill_basis else None
        order_rows.append(row)
    projected = {symbol: qty for symbol, qty in projected.items() if abs(qty) > 1e-12}
    checks: list[dict[str, Any]] = []
    scope_symbols = list(account_scope.get("symbols") or [])

    # -- policy and schema
    if policy.get("error"):
        checks.append(_check("policy", False, str(policy["error"])))
    checks.append(_check("orders_present", bool(orders),
                         f"{len(orders)} order(s)" if orders else "no order to submit (targets already met or unpriced)"))

    # -- reference prices: every order symbol needs a finite positive completed close
    missing = sorted(order["symbol"] for order in orders if order["symbol"] not in refs)
    checks.append(_check(
        "reference_prices", not missing,
        "; ".join(_describe_ref(refs[o["symbol"]]) for o in orders if o["symbol"] in refs) if not missing
        else "no finite positive completed-session close for " + ", ".join(missing)))

    # -- price collar for limit orders
    collar = float(policy.get("price_collar_pct", DEFAULT_POLICY["price_collar_pct"]))
    outside = []
    for order in orders:
        ref = refs.get(order["symbol"])
        if order["order_type"] == "limit" and ref:
            drift = order["limit_price"] / ref["price"] - 1.0
            if abs(drift) > collar + _TOL:
                outside.append(f"{order['symbol']} limit {order['limit_price']:g} is {drift:+.1%} from close {ref['price']:g}")
    checks.append(_check("price_collar", not outside,
                         "; ".join(outside) if outside else
                         f"limit prices within ±{collar:.0%} of the reference close; market orders use the reference close"))

    # -- account scope
    account = str(account_scope.get("account") or "")
    out_of_scope = sorted({o["symbol"] for o in orders if o["symbol"] not in scope_symbols}
                          | {s for s in (targets or {}) if s not in scope_symbols})
    scope_ok = not out_of_scope and account == book.account
    untouched = sorted(symbol for symbol in current if symbol not in scope_symbols)
    detail = (f"account {account}; scope {', '.join(scope_symbols) or '(none)'}"
              + (f"; held outside scope, never touched: {', '.join(untouched)}" if untouched else ""))
    if out_of_scope:
        detail = "outside the proposal's account scope: " + ", ".join(out_of_scope)
    elif account != book.account:
        detail = f"account scope {account!r} does not match the book's account {book.account!r}"
    checks.append(_check("account_scope", scope_ok, detail))

    # -- book
    unpriced_held = sorted(symbol for symbol in current if symbol not in marks)
    book_ok = book.error is None and equity is not None and equity > 0 and not unpriced_held
    if book_ok:
        book_detail = f"equity {equity:,.2f} ({book.equity_basis}); {len(current)} position(s); source {book.source}"
    elif book.error:
        book_detail = book.error
    elif unpriced_held:
        book_detail = "holdings without a price: " + ", ".join(unpriced_held)
    else:
        book_detail = "account equity unavailable"
    checks.append(_check("current_book", book_ok, book_detail))

    # -- orders match targets
    final_targets = dict(targets or {})
    if targets is not None:
        if book_ok and not missing:
            expected = build_orders(final_targets, current, marks, float(equity), scope_symbols)
            key = lambda o: (o["symbol"], o["side"], round(float(o["qty"] or 0), 6))  # noqa: E731
            same = sorted(map(key, expected)) == sorted(map(key, orders))
            checks.append(_check("orders_match_targets", same,
                                 "orders are exactly the diff between the clamped targets and the current book"
                                 if same else "orders differ from the diff between the clamped targets and the current book"))
        else:
            checks.append(_check("orders_match_targets", False, "cannot recompute orders without a priced book"))
    else:
        checks.append(_check("orders_match_targets", True, "explicit orders (no target weights); projection shown"))

    # -- evidence
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        by_symbol.setdefault(signal.get("symbol") or "*", []).append(signal)
    unsupported: list[str] = []
    contradicted: list[str] = []
    for order in orders:
        symbol = order["symbol"]
        found = by_symbol.get(symbol, []) + by_symbol.get("*", [])
        cited = [s for s in found if s["evidence_ids"]]
        if not cited:
            unsupported.append(symbol)
        if not found:
            continue
        held = current.get(symbol, 0.0)
        directions = {s["direction"] for s in found}
        if order["side"] == "buy":
            ok = "long" in directions or (held < 0 and directions & {"reduce", "flat"})
        else:
            trim = (targets is not None and "long" in directions and held > 0
                    and 0 <= final_targets.get(symbol, 0.0) and projected.get(symbol, 0.0) >= -1e-9)
            ok = bool(directions & {"short", "reduce", "flat"}) or trim
        if not ok:
            contradicted.append(f"{order['side']} {symbol} vs {', '.join(sorted(directions))}")
    checks.append(_check("evidence_cited", not unsupported,
                         "every order has a signal citing evidence" if not unsupported
                         else "no signal citing evidence for " + ", ".join(unsupported), blocking=False))
    checks.append(_check("evidence_agrees", not contradicted,
                         "order directions agree with their signals" if not contradicted
                         else "orders contradict their signals: " + "; ".join(contradicted)))

    # -- direction permission: long-only sources may reduce longs, never create shorts
    permissions: list[dict[str, Any]] = []
    for symbol in sorted({o["symbol"] for o in orders} | {s for s, w in final_targets.items() if w < 0}):
        before = current.get(symbol, 0.0)
        after = projected.get(symbol, 0.0)
        target = final_targets.get(symbol)
        creates_short = (after < -1e-9 and after < before - 1e-9) or (target is not None and target < -_TOL)
        found = by_symbol.get(symbol, []) + by_symbol.get("*", [])
        short_sources = sorted({s["source"] for s in found if s["direction"] == "short" and s["approach"] == "long_short"})
        long_only_bears = sorted({s["source"] for s in found if s["direction"] == "short" and s["approach"] == "long_only"})
        allowed = not creates_short or bool(short_sources)
        permissions.append({"symbol": symbol, "current_qty": _round(before, 8), "projected_qty": _round(after, 8),
                            "creates_or_increases_short": creates_short, "short_capable_sources": short_sources,
                            "long_only_bearish_sources": long_only_bears, "status": "PASS" if allowed else "FAIL"})
    denied = [p for p in permissions if p["status"] == "FAIL"]
    checks.append(_check(
        "direction_permission", not denied,
        "no order creates or increases a short without short-capable evidence" if not denied else
        "long-only sources cannot create shorts (they may only reduce longs): "
        + ", ".join(f"{p['symbol']} -> {p['projected_qty']:g}" for p in denied)))

    # -- exposure caps (a proposal may always move a breach toward compliance)
    current_w = _weights(current, marks, equity)
    projected_w = _weights(projected, marks, equity)
    max_pos = float(policy.get("max_position_pct", DEFAULT_POLICY["max_position_pct"]))
    over = [f"{s} {projected_w.get(s, 0.0):+.2%}" for s in sorted({o['symbol'] for o in orders})
            if abs(projected_w.get(s, 0.0)) > max_pos + _TOL and abs(projected_w.get(s, 0.0)) > abs(current_w.get(s, 0.0)) + _TOL]
    checks.append(_check("per_name_cap", book_ok and not over,
                         (f"projected |weight| <= {max_pos:.0%} per name" if not over else "above per-name cap: " + ", ".join(over))
                         if book_ok else "no priced book"))
    now_x = _exposure(current, marks, equity)
    new_x = _exposure(projected, marks, equity)
    gross_cap = float(policy.get("max_gross_exposure", DEFAULT_POLICY["max_gross_exposure"]))
    net_cap = float(policy.get("max_net_exposure", DEFAULT_POLICY["max_net_exposure"]))
    if book_ok and "gross" in new_x:
        gross_ok = new_x["gross"] <= gross_cap + _TOL or new_x["gross"] <= now_x.get("gross", 0.0) + _TOL
        net_ok = abs(new_x["net"]) <= net_cap + _TOL or abs(new_x["net"]) <= abs(now_x.get("net", 0.0)) + _TOL
        checks.append(_check("gross_exposure", gross_ok, f"projected gross {new_x['gross']:.2%} (cap {gross_cap:.0%}, now {now_x.get('gross', 0.0):.2%})"))
        checks.append(_check("net_exposure", net_ok, f"projected net {new_x['net']:+.2%} (cap ±{net_cap:.0%}, now {now_x.get('net', 0.0):+.2%})"))
    else:
        checks.append(_check("gross_exposure", False, "no priced book"))
        checks.append(_check("net_exposure", False, "no priced book"))

    # -- single-order notional (risk-increasing orders)
    cap = float(policy.get("max_order_notional_usd", DEFAULT_POLICY["max_order_notional_usd"]))
    if live and live.get("mandate_max_order_notional_usd") is not None:
        cap = min(cap, float(live["mandate_max_order_notional_usd"]))
    big = []
    for row in order_rows:
        symbol = row["symbol"]
        increases = abs(projected.get(symbol, 0.0)) > abs(current.get(symbol, 0.0)) + 1e-9
        notional = row.get("est_notional_usd")
        if increases and (notional is None or notional > cap + 1e-6):
            big.append(f"{symbol} {notional if notional is not None else 'unpriced'}")
    checks.append(_check("order_notional", not big,
                         f"risk-increasing orders <= ${cap:,.0f} each" if not big else f"above ${cap:,.0f}: " + ", ".join(map(str, big))))

    # -- live brokers: mandate and kill switch
    if live is not None:
        checks.append(_check("mandate", bool(live.get("mandate_ok")), str(live.get("mandate_detail") or "")))
        checks.append(_check("halt", not live.get("halted"), "live trading halted (HALT)" if live.get("halted") else "kill switch clear"))
    else:
        checks.append(_check("halt", True, "not applicable: simulated or paper broker (HALT gates live brokers)"))

    blocking_ok = all(c["status"] == "PASS" for c in checks if c["blocking"])
    validation = {"ok": blocking_ok, "checked_utc": _iso(_now()), "checks": checks}
    decision = {
        "signals": signals,
        "equity": _round(equity, 6) if equity is not None else None,
        "equity_basis": book.equity_basis,
        "cash": _round(book.cash, 6) if book.cash is not None else None,
        "book_source": book.source,
        "current_positions": {s: _round(q, 8) for s, q in sorted(current.items())},
        "projected_positions": {s: _round(q, 8) for s, q in sorted(projected.items())},
        "current_weights": current_w,
        "requested_targets": dict(requested_targets) if requested_targets is not None else None,
        "target_weights": {s: _round(w) for s, w in sorted(final_targets.items())} if targets is not None else None,
        "clamp_events": list(clamp_events or []),
        "direction_permissions": permissions,
        "projected_weights": projected_w,
        "exposure_before": now_x,
        "projected_exposure": new_x,
        "reference_prices": refs,
        "limits": {k: policy.get(k) for k in (*_POSITIVE_POLICY_KEYS, "short_capable_sources")},
        "orders_detail": order_rows,
    }
    return decision, validation


# ---------------------------------------------------------------------------
# Broker descriptors
# ---------------------------------------------------------------------------


def _is_zt_paper_profile(profile: Any) -> bool:
    return str(getattr(profile, "connector", "") or "").lower() == ZT_PAPER or str(getattr(profile, "id", "")).lower() == ZT_PAPER


def zt_paper_broker() -> dict[str, Any]:
    """Broker descriptor of the simulated account."""
    return {"key": ZT_PAPER, "profile_id": ZT_PAPER, "connector": ZT_PAPER, "environment": "simulated",
            "transport": "zt_paper", "live": False, "label": "SIMULATED"}


def profile_broker(profile: Any) -> dict[str, Any]:
    """Broker descriptor of a VT trading profile."""
    if _is_zt_paper_profile(profile):
        return zt_paper_broker()
    return {"key": profile.connector, "profile_id": profile.id, "connector": profile.connector,
            "environment": profile.environment, "transport": profile.transport,
            "live": profile.environment == "live", "label": profile.label}


def resolve_broker(broker: str | None, profile: Any = None) -> tuple[dict[str, Any], Any | None]:
    """``(descriptor, TradingProfile | None)`` for a broker/profile name or a resolved profile."""
    if profile is None:
        token = str(broker or ZT_PAPER).strip().lower()
        if token == ZT_PAPER:
            return zt_paper_broker(), None
        from src.trading import service

        profile = service.profile_by_id(token)
    if _is_zt_paper_profile(profile):
        return zt_paper_broker(), profile
    if profile.transport != "broker_sdk" or profile.readonly:
        raise ValueError(f"profile {profile.id!r} cannot place orders")
    return profile_broker(profile), profile


def _live_context(broker_key: str) -> dict[str, Any]:
    from src.live.halt import halt_flag_set
    from src.live.mandate.model import MANDATE_SCHEMA_VERSION
    from src.live.mandate.store import load_mandate

    mandate = load_mandate(broker_key)
    ok, detail, cap = False, "no valid mandate on file", None
    if mandate is not None and mandate.schema_version == MANDATE_SCHEMA_VERSION:
        expires = _parse_iso(mandate.consent.expires_at)
        if expires is None or _now() >= expires:
            detail = "mandate expired; re-authorize"
        else:
            ok, detail = True, f"mandate valid until {mandate.consent.expires_at}"
            cap = mandate.hard_caps.max_order_notional_usd
    return {"mandate_ok": ok, "mandate_detail": detail, "halted": halt_flag_set(broker_key),
            "mandate_max_order_notional_usd": cap, "mandate": mandate}


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_proposal(*, broker: str | None = ZT_PAPER, orders: Sequence[Mapping[str, Any]] | None = None,
                    targets: Mapping[str, Any] | None = None, rationale: str = "",
                    evidence_ids: Sequence[str] | None = None,
                    signals: Sequence[Mapping[str, Any]] | None = None,
                    scope_symbols: Sequence[str] | None = None,
                    origin: Mapping[str, Any] | None = None, route: Mapping[str, Any] | None = None,
                    book: Book | None = None, price_lookup: PriceLookup | None = None,
                    overrides: Mapping[str, Any] | None = None, account: str | None = None,
                    profile: Any = None) -> dict[str, Any]:
    """Record a PENDING proposal (never submits anything).

    Exactly one of ``orders`` or ``targets`` is required. ``targets`` are
    fractions of account equity; they are clamped to the policy caps (clamp
    events are recorded) and diffed against the current book within the
    account scope. Raises ``ValueError`` on malformed input.
    """
    if (orders is None) == (targets is None):
        raise ValueError("give exactly one of orders or targets")
    policy = load_policy()
    descriptor, profile = resolve_broker(broker, profile)
    is_paper = descriptor["key"] == ZT_PAPER
    lookup = cached_lookup(price_lookup or default_price_lookup(descriptor))
    live = _live_context(descriptor["key"]) if descriptor.get("live") else None
    mandate = live.pop("mandate", None) if live else None

    requested_targets = normalize_targets(targets, descriptor) if targets is not None else None
    explicit = normalize_orders(orders, descriptor) if orders is not None else None
    symbols = sorted(set(requested_targets or {}) | {o["symbol"] for o in explicit or []})
    scope = sorted({normalize_symbol(s, descriptor) for s in (scope_symbols or [])} | set(symbols))
    if is_paper:
        account_ref = ZT_PAPER_ACCOUNT
    else:
        account_ref = str(account or (mandate.consent.account_ref if mandate is not None else "") or "default")
    if book is None:
        if is_paper:
            book = zt_paper_engine().book(price_lookup=lookup)
        else:
            book = _vt_profile_book(profile, overrides or {}, account_ref, lookup, mandate)
    prices: dict[str, RefPrice | None] = {}
    for symbol in sorted(set(scope) | set(book.positions)):
        prices[symbol] = lookup(symbol) if symbol in scope or symbol not in book.marks else None
    clamp_events: list[dict[str, Any]] = []
    final_targets: dict[str, float] | None = None
    if requested_targets is not None:
        final_targets, clamp_events = apply_limits(requested_targets, float(policy["max_position_pct"]),
                                                   float(policy["max_gross_exposure"]))
        marks = {**book.marks, **{s: r.price for s, r in prices.items() if r is not None}}
        if book.equity and book.equity > 0 and book.error is None:
            explicit = build_orders(final_targets, book.positions, marks, float(book.equity), scope)
        else:
            explicit = []
    assert explicit is not None
    origin_map = dict(origin or {"kind": "api"})
    base_signals = normalize_signals(signals, policy, descriptor)
    if not base_signals:
        source = str(origin_map.get("source") or "agent")
        base_signals = normalize_signals([
            {"source": source, "symbol": o["symbol"],
             "direction": "long" if o["side"] == "buy" else (
                 "reduce" if book.positions.get(o["symbol"], 0.0) > 0 else "short"),
             "rationale": rationale, "evidence_ids": list(evidence_ids or [])}
            for o in explicit], policy, descriptor)
    decision, validation = evaluate(
        broker=descriptor, account_scope={"account": account_ref, "symbols": scope}, orders=explicit,
        targets=final_targets, signals=base_signals, book=book, prices=prices, policy=policy, live=live,
        clamp_events=clamp_events, requested_targets=requested_targets)
    decision["rationale"] = str(rationale or "").strip()[:4000]
    decision["evidence_ids"] = [str(e).strip()[:200] for e in (evidence_ids or []) if str(e).strip()][:100]
    created = _now()
    minutes = ttl_minutes()
    proposal: dict[str, Any] = {
        "schema": SCHEMA,
        "id": new_proposal_id(),
        "created_utc": _iso(created),
        "expires_utc": _iso(created + timedelta(minutes=minutes)),
        "ttl_minutes": minutes,
        "origin": origin_map,
        "broker": descriptor,
        "account_scope": {"account": account_ref, "symbols": scope},
        "orders": explicit,
        "decision_record": decision,
        "validation": validation,
        "route": dict(route or ({"kind": "zt_paper", "account": ZT_PAPER_ACCOUNT} if is_paper else
                                {"kind": "vt_profile", "profile_id": descriptor["profile_id"],
                                 "overrides": _clean_overrides(overrides), "session_id": ""})),
    }
    json.loads(canonical_json({k: proposal[k] for k in HASHED_FIELDS}))  # refuses NaN/unserializable
    proposal["content_hash"] = content_hash(proposal)
    proposal["status"] = PENDING
    proposal["transitions"] = []
    proposal["approval"] = None
    proposal["submission"] = None
    with _proposal_lock(proposal["id"]):
        _record_transition(proposal, PENDING, actor=str(origin_map.get("actor") or "agent"),
                           reason="proposed", event="created",
                           detail={"broker": descriptor["profile_id"], "orders": len(explicit),
                                   "validation_ok": validation["ok"]})
        write_json_atomic(proposal_path(proposal["id"]), proposal)
    return proposal


def _clean_overrides(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(k): v for k, v in dict(overrides or {}).items() if k != "session_id" and v is not None}


# ---------------------------------------------------------------------------
# Reading, listing, expiry
# ---------------------------------------------------------------------------


def load_proposal(proposal_id: str) -> dict[str, Any]:
    """Read one proposal file (404 when absent)."""
    path = proposal_path(proposal_id)
    if not path.is_file():
        raise ProposalError(f"no proposal {proposal_id}", status_code=404, code="not_found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProposalError(f"proposal {proposal_id} is unreadable: {exc}", status_code=409,
                            code="unreadable") from exc
    if not isinstance(payload, dict) or payload.get("id") != proposal_id:
        raise ProposalError(f"proposal {proposal_id} is malformed", status_code=409, code="unreadable")
    return payload


def _is_expired(proposal: Mapping[str, Any], now: datetime | None = None) -> bool:
    expires = _parse_iso(proposal.get("expires_utc"))
    return expires is None or (now or _now()) >= expires


def expire_if_due(proposal_id: str) -> dict[str, Any]:
    """Mark a PENDING proposal EXPIRED once its TTL elapsed; return the current record."""
    proposal = load_proposal(proposal_id)
    if proposal.get("status") != PENDING or not _is_expired(proposal):
        return proposal
    try:
        with _proposal_lock(proposal_id, wait_seconds=0.0):
            proposal = load_proposal(proposal_id)
            if proposal.get("status") == PENDING and _is_expired(proposal):
                _record_transition(proposal, EXPIRED, actor="system", reason="approval window elapsed")
                write_json_atomic(proposal_path(proposal_id), proposal)
    except ProposalError as exc:
        if exc.code != "busy":
            raise
    return proposal


def list_proposals(*, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first proposals (lazily expiring PENDING ones past their TTL)."""
    folder = proposals_dir()
    if not folder.is_dir():
        return []
    wanted = str(status or "").strip().upper() or None
    rows: list[dict[str, Any]] = []
    for path in folder.glob("op_*.json"):
        proposal_id = path.stem
        if not valid_proposal_id(proposal_id):
            continue
        try:
            proposal = expire_if_due(proposal_id)
        except ProposalError:
            continue
        if wanted and proposal.get("status") != wanted:
            continue
        rows.append(proposal)
    rows.sort(key=lambda row: str(row.get("created_utc") or ""), reverse=True)
    return rows[: max(1, min(int(limit or 50), 500))]


def integrity(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the stored hash with the recomputed one and the ledger's creation record."""
    recomputed = content_hash(proposal)
    stored = proposal.get("content_hash")
    created = next((e for e in ledger_events(str(proposal.get("id"))) if e.get("event") == "created"), None)
    ledger_hash = created.get("content_hash") if created else None
    ok = recomputed == stored == ledger_hash
    reason = None
    if recomputed != stored:
        reason = "the proposal's content no longer matches its content_hash"
    elif ledger_hash is None:
        reason = "the ledger has no creation record for this proposal"
    elif ledger_hash != stored:
        reason = "the proposal's content_hash differs from the ledger's creation record"
    return {"ok": ok, "stored": stored, "recomputed": recomputed, "ledger": ledger_hash, "reason": reason}


def public_view(proposal: Mapping[str, Any], *, full_hash: bool = True) -> dict[str, Any]:
    """A proposal for the API/UI: the replay route reduced to its kind and target."""
    view = {key: value for key, value in proposal.items() if key != "route"}
    route = proposal.get("route") or {}
    view["route"] = {"kind": route.get("kind"),
                     "target": route.get("profile_id") or route.get("remote_name") or route.get("function")
                     or route.get("account") or route.get("broker")}
    view["content_hash_tail"] = hash_tail(str(proposal.get("content_hash") or ""))
    if not full_hash:
        view.pop("content_hash", None)
    expires = _parse_iso(proposal.get("expires_utc"))
    view["expires_in_seconds"] = (int((expires - _now()).total_seconds()) if expires else None)
    # Approval always re-validates against a fresh book, so a proposal whose
    # creation-time checks failed (e.g. HALT was set) may still be tried.
    view["approvable"] = bool(proposal.get("status") == PENDING and view["expires_in_seconds"] is not None
                              and view["expires_in_seconds"] > 0)
    return view


def summary(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """A compact list row."""
    view = public_view(proposal, full_hash=False)
    validation = proposal.get("validation") or {}
    failed = [c["name"] for c in validation.get("checks", []) if c.get("status") == "FAIL" and c.get("blocking")]
    advisory = [c["name"] for c in validation.get("checks", []) if c.get("status") == "FAIL" and not c.get("blocking")]
    return {
        "id": proposal.get("id"), "status": proposal.get("status"), "created_utc": proposal.get("created_utc"),
        "expires_utc": proposal.get("expires_utc"), "expires_in_seconds": view["expires_in_seconds"],
        "broker": (proposal.get("broker") or {}).get("profile_id"),
        "environment": (proposal.get("broker") or {}).get("environment"),
        "account": (proposal.get("account_scope") or {}).get("account"),
        "orders": [{k: o.get(k) for k in ("symbol", "side", "qty", "notional", "order_type", "limit_price", "tif")}
                   for o in proposal.get("orders") or []],
        "validation_ok": bool(validation.get("ok")), "failed_checks": failed, "advisory_checks": advisory,
        "origin": (proposal.get("origin") or {}).get("kind"), "approvable": view["approvable"],
        "content_hash_tail": view["content_hash_tail"],
        "rationale": ((proposal.get("decision_record") or {}).get("rationale") or "")[:300],
    }


def hold_envelope(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """What an order path returns instead of a broker response."""
    row = summary(proposal)
    return {
        "status": "pending_approval",
        "proposal_id": proposal["id"],
        "proposal_status": proposal["status"],
        "expires_utc": proposal["expires_utc"],
        "approval_url": f"/zt/approvals?proposal={proposal['id']}",
        "content_hash_tail": row["content_hash_tail"],
        "validation_ok": row["validation_ok"],
        "failed_checks": row["failed_checks"],
        "orders": row["orders"],
        "message": ("Held for human approval: nothing was sent to the broker. The user approves or "
                    "rejects this proposal in Vibe-Trading (/zt/approvals) before it expires. Do not "
                    "resubmit; any change needs a new proposal."),
    }


# ---------------------------------------------------------------------------
# Reject / approve
# ---------------------------------------------------------------------------


def reject_proposal(proposal_id: str, *, reason: str, actor: str = APPROVER,
                    principal: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """PENDING -> REJECTED with the human's reason."""
    text = str(reason or "").strip()
    if not text:
        raise ProposalError("a rejection reason is required", status_code=422, code="reason_required")
    with _proposal_lock(proposal_id):
        proposal = load_proposal(proposal_id)
        if proposal.get("status") == PENDING and _is_expired(proposal):
            _record_transition(proposal, EXPIRED, actor="system", reason="approval window elapsed")
            write_json_atomic(proposal_path(proposal_id), proposal)
            raise ProposalError("the proposal expired", status_code=410, code="expired", proposal=proposal)
        if proposal.get("status") != PENDING:
            raise ProposalError(f"the proposal is {proposal.get('status')}, not PENDING", status_code=409,
                                code="not_pending", proposal=proposal)
        _record_transition(proposal, REJECTED, actor=actor, reason=text[:500],
                           detail={"principal": dict(principal or {})})
        write_json_atomic(proposal_path(proposal_id), proposal)
        return proposal


def _claim_path(proposal_id: str) -> Path:
    return proposals_dir() / ".claims" / f"{proposal_id}.approve"


def _claim(proposal_id: str) -> None:
    """Create the single-use approval claim; a second claim is refused forever."""
    path = _claim_path(proposal_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ProposalError("this proposal was already approved once", status_code=409,
                            code="already_approved") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_iso(_now()) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _normalized_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text.startswith("sha256:") else f"sha256:{text}"


def revalidate(proposal: Mapping[str, Any], *, price_lookup: PriceLookup | None = None) -> dict[str, Any]:
    """Re-run validation for the stored orders against a fresh book and prices."""
    descriptor = dict(proposal["broker"])
    policy = load_policy()
    lookup = cached_lookup(price_lookup or default_price_lookup(descriptor))
    live = _live_context(descriptor["key"]) if descriptor.get("live") else None
    mandate = live.pop("mandate", None) if live else None
    scope = list((proposal.get("account_scope") or {}).get("symbols") or [])
    account = str((proposal.get("account_scope") or {}).get("account") or "")
    route = proposal.get("route") or {}
    if descriptor["key"] == ZT_PAPER:
        book = zt_paper_engine().book(price_lookup=lookup)
    elif route.get("kind") == "vt_profile":
        from src.trading import service

        book = _vt_profile_book(service.profile_by_id(descriptor["profile_id"]), route.get("overrides") or {},
                                account, lookup, mandate)
    else:
        book = None
    decision = proposal.get("decision_record") or {}
    orders = list(proposal.get("orders") or [])
    if book is None:
        # Remote-MCP and sweep routes: VT's own gate re-reads the broker at submission.
        book = Book(account=account, positions=dict(decision.get("current_positions") or {}),
                    marks={s: r["price"] for s, r in (decision.get("reference_prices") or {}).items()},
                    equity=decision.get("equity"), source="proposal snapshot (VT gate re-reads at submission)",
                    equity_basis=str(decision.get("equity_basis") or "account"))
    prices = {symbol: lookup(symbol) for symbol in sorted(set(scope) | {o["symbol"] for o in orders})}
    targets = decision.get("target_weights")
    signals = list(decision.get("signals") or [])
    _, validation = evaluate(broker=descriptor, account_scope=proposal.get("account_scope") or {},
                             orders=orders, targets=targets, signals=signals, book=book, prices=prices,
                             policy=policy, live=live, clamp_events=decision.get("clamp_events"),
                             requested_targets=decision.get("requested_targets"))
    return validation


def approve_proposal(proposal_id: str, *, confirm_hash: str, actor: str = APPROVER,
                     principal: Mapping[str, Any] | None = None,
                     price_lookup: PriceLookup | None = None) -> dict[str, Any]:
    """Approve and submit a proposal's orders exactly once.

    Raises:
        ProposalError: 404 unknown; 409 not pending / already approved /
            tampered / hash mismatch / busy; 410 expired; 422 validation fails
            now; 423 live trading halted.
    """
    with _proposal_lock(proposal_id):
        proposal = load_proposal(proposal_id)
        if proposal.get("status") == PENDING and _is_expired(proposal):
            _record_transition(proposal, EXPIRED, actor="system", reason="approval window elapsed")
            write_json_atomic(proposal_path(proposal_id), proposal)
            raise ProposalError("the proposal expired before approval", status_code=410, code="expired",
                                proposal=proposal)
        if proposal.get("status") == EXPIRED:
            raise ProposalError("the proposal expired", status_code=410, code="expired", proposal=proposal)
        if proposal.get("status") != PENDING or _claim_path(proposal_id).exists():
            raise ProposalError(f"the proposal is {proposal.get('status')}; it can be approved only once",
                                status_code=409, code="not_pending", proposal=proposal)
        check = integrity(proposal)
        if not check["ok"]:
            _ledger_append({"event": "tamper_detected", "proposal_id": proposal_id, "from": proposal.get("status"),
                            "to": proposal.get("status"), "actor": "system", "reason": check["reason"],
                            "content_hash": check["recomputed"], "detail": check})
            raise ProposalError(f"refused: {check['reason']}", status_code=409, code="tampered")
        if _normalized_hash(confirm_hash) != proposal["content_hash"]:
            raise ProposalError("confirm_hash does not match this proposal's content hash", status_code=409,
                                code="confirm_hash_mismatch")
        descriptor = proposal.get("broker") or {}
        if descriptor.get("live"):
            from src.live.halt import halt_flag_set

            if halt_flag_set(str(descriptor.get("key") or "")):
                _ledger_append({"event": "approval_refused", "proposal_id": proposal_id, "from": PENDING,
                                "to": PENDING, "actor": actor, "reason": "live trading halted",
                                "content_hash": proposal["content_hash"], "detail": {}})
                raise ProposalError("live trading is halted (HALT); clear it before approving live orders",
                                    status_code=423, code="halted")
        validation = revalidate(proposal, price_lookup=price_lookup)
        if not validation["ok"]:
            failed = [c["name"] for c in validation["checks"] if c["status"] == "FAIL" and c["blocking"]]
            _ledger_append({"event": "approval_refused", "proposal_id": proposal_id, "from": PENDING,
                            "to": PENDING, "actor": actor, "reason": "validation failed at approval",
                            "content_hash": proposal["content_hash"], "detail": {"failed": failed}})
            raise ProposalError("validation fails now: " + ", ".join(failed), status_code=422,
                                code="validation_failed", proposal={**proposal, "revalidation": validation})
        chain = verify_ledger()
        if not chain.get("ok"):
            raise ProposalError("the proposal ledger's hash chain is broken; approvals are refused",
                                status_code=409, code="ledger_broken")
        _claim(proposal_id)
        proposal["approval"] = {"approver": actor, "approved_utc": _iso(_now()),
                                "confirm_hash": proposal["content_hash"], "principal": dict(principal or {}),
                                "revalidation": validation}
        _record_transition(proposal, APPROVED, actor=actor, reason="approved by the human operator",
                           detail={"principal": dict(principal or {}), "revalidation_ok": True})
        write_json_atomic(proposal_path(proposal_id), proposal)
        results = _submit(proposal)
        final = _final_status(results)
        proposal["submission"] = {"submitted_utc": _iso(_now()), "results": results}
        _record_transition(proposal, SUBMITTED, actor=actor, reason="approved orders submitted once",
                           detail={"orders": len(results)})
        if final != SUBMITTED:
            _record_transition(proposal, final, actor="system",
                               reason="all orders filled" if final == FILLED else "an order failed",
                               detail={"statuses": [r.get("status") for r in results]})
        write_json_atomic(proposal_path(proposal_id), proposal)
        return proposal


def _final_status(results: list[dict[str, Any]]) -> str:
    statuses = [r.get("status") for r in results]
    if not statuses or any(status in ("failed", "not_submitted") for status in statuses):
        return FAILED
    if all(status == "filled" for status in statuses):
        return FILLED
    return SUBMITTED


# ---------------------------------------------------------------------------
# Replay: one-shot grants and executors
# ---------------------------------------------------------------------------


@dataclass
class _Grant:
    proposal_id: str
    fingerprint: str
    used: bool = field(default=False)


_GRANT: contextvars.ContextVar[_Grant | None] = contextvars.ContextVar("vt_order_approval_grant", default=None)


def fingerprint(kind: str, payload: Mapping[str, Any]) -> str:
    """Stable identity of one order call (numbers normalized to floats)."""
    def norm(value: Any) -> Any:
        if isinstance(value, bool) or value is None or isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            number = float(value)
            return number if math.isfinite(number) else str(value)
        if isinstance(value, Mapping):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        return str(value)

    return _sha256(canonical_json({"kind": kind, "call": norm(dict(payload))}))


@contextmanager
def _granted(proposal_id: str, call_print: str) -> Iterator[_Grant]:
    grant = _Grant(proposal_id, call_print)
    token = _GRANT.set(grant)
    try:
        yield grant
    finally:
        _GRANT.reset(token)


def consume_grant(call_print: str) -> str | None:
    """Let exactly one matching call through; returns the proposal id when granted."""
    grant = _GRANT.get()
    if grant is None or grant.used or grant.fingerprint != call_print:
        return None
    grant.used = True
    return grant.proposal_id


def _order_result(order: Mapping[str, Any], status: str, response: Any = None, error: str | None = None) -> dict[str, Any]:
    return {"symbol": order.get("symbol"), "side": order.get("side"), "qty": order.get("qty"),
            "notional": order.get("notional"), "status": status, "error": error,
            "response": _jsonable(response)}


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str, allow_nan=False))
    except (TypeError, ValueError):
        return {"repr": repr(value)[:500]}


def _broker_status(response: Any) -> tuple[str, str | None]:
    if not isinstance(response, dict):
        return "failed", "non-dict broker result"
    status = str(response.get("status") or "").lower()
    if status in ("error", "blocked", "pending_approval") or response.get("ok") is False:
        return "failed", str(response.get("error") or response.get("reason") or status or "broker error")
    data = response.get("data")
    if isinstance(data, dict) and (str(data.get("status") or "").lower() == "error" or data.get("ok") is False):
        return "failed", str(data.get("error") or data.get("message") or "broker rejected the order")
    order_status = str(response.get("order_status") or "").lower()
    if order_status in ("filled", "simulated_fill"):
        return "filled", None
    return "submitted", None


def _submit(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    route = proposal.get("route") or {}
    executor = _EXECUTORS.get(str(route.get("kind")))
    orders = list(proposal.get("orders") or [])
    if executor is None:
        return [_order_result(o, "not_submitted", error=f"no executor for route {route.get('kind')!r}") for o in orders]
    try:
        return executor(proposal)
    except Exception as exc:  # noqa: BLE001 - recorded, never retried
        logger.exception("order proposal %s submission raised", proposal.get("id"))
        return [_order_result(o, "failed", error=str(exc)) for o in orders]


def _execute_zt_paper(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    engine = zt_paper_engine()
    try:
        fills = engine.fill_orders(proposal["orders"], proposal_id=proposal["id"])
    except engine.PaperRejected as exc:
        return [_order_result(o, "failed", error=str(exc)) for o in proposal["orders"]]
    return [{**_order_result(order, "filled", response=fill), "fill_price": fill.get("price"),
             "fill_qty": fill.get("qty")} for order, fill in zip(proposal["orders"], fills)]


def place_order_call(profile_id: str, order: Mapping[str, Any], *, session_id: str,
                     overrides: Mapping[str, Any]) -> dict[str, Any]:
    """The exact ``service.place_order`` keyword call for one order."""
    return {"symbol": order["symbol"], "profile_id": profile_id, "side": order["side"],
            "quantity": order.get("qty"), "notional": order.get("notional"),
            "order_type": order.get("order_type") or "market", "limit_price": order.get("limit_price"),
            "time_in_force": order.get("tif") or "day", "session_id": session_id or "",
            "overrides": dict(overrides or {})}


def _execute_vt_profile(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    from src.trading import service

    route = proposal["route"]
    results: list[dict[str, Any]] = []
    stop = None
    for order in proposal["orders"]:
        if stop is not None:
            results.append(_order_result(order, "not_submitted", error=f"not sent after an earlier failure: {stop}"))
            continue
        call = place_order_call(route["profile_id"], order, session_id=route.get("session_id") or "",
                                overrides=route.get("overrides") or {})
        with _granted(proposal["id"], fingerprint("place_order", call)):
            response = service.place_order(
                call["symbol"], call["profile_id"], side=call["side"], quantity=call["quantity"],
                notional=call["notional"], order_type=call["order_type"], limit_price=call["limit_price"],
                time_in_force=call["time_in_force"], session_id=call["session_id"], **call["overrides"])
        status, error = _broker_status(response)
        results.append(_order_result(order, status, response, error))
        if status == "failed":
            stop = error
    return results


def mcp_adapter(server_name: str) -> Any:
    """An ``MCPServerAdapter`` for a configured MCP server (patched in tests)."""
    from src.config.loader import load_agent_config
    from src.tools.mcp import MCPServerAdapter

    servers = load_agent_config().mcp_servers or {}
    if server_name not in servers:
        raise RuntimeError(f"MCP server {server_name!r} is not configured")
    return MCPServerAdapter(server_name, servers[server_name])


def live_broker_adapter(broker: str) -> Any:
    """The MCP adapter of a live broker, found the way the live runner finds it."""
    from src.config.loader import load_agent_config

    servers = load_agent_config().mcp_servers or {}
    if broker in servers:
        return mcp_adapter(broker)
    try:
        from src.config.schema import is_live_broker_entry
    except Exception:  # noqa: BLE001
        is_live_broker_entry = None  # type: ignore[assignment]
    for name, config in servers.items():
        if is_live_broker_entry is not None and broker == "robinhood" and is_live_broker_entry(name, config):
            return mcp_adapter(name)
    raise RuntimeError(f"no MCP server configured for live broker {broker!r}")


def _execute_mcp_guard(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    from src.live.order_guard import LiveOrderGuardTool
    from src.tools.mcp import MCPRemoteToolSpec

    route = proposal["route"]
    spec = MCPRemoteToolSpec(server_name=route["server_name"], remote_name=route["remote_name"],
                             local_name=route.get("local_name") or route["remote_name"], description="",
                             parameters=route.get("parameters") or {"type": "object", "properties": {}})
    guard = LiveOrderGuardTool(mcp_adapter(route["server_name"]), spec, broker=route["broker"],
                               session_id=route.get("session_id") or "")
    call = {"broker": route["broker"], "remote_name": route["remote_name"], "arguments": route["arguments"]}
    with _granted(proposal["id"], fingerprint("mcp_guard", call)):
        output = guard.execute(**route["arguments"])
    try:
        response = json.loads(output)
    except (TypeError, ValueError):
        response = {"status": "error", "error": "unparseable broker result", "raw": str(output)[:500]}
    status, error = _broker_status(response)
    order = (proposal.get("orders") or [{}])[0]
    return [_order_result(order, status, response, error)]


def _execute_mcp_flatten(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    from src.live.runtime.flatten import _audit
    from src.trading.service import runner_arguments, runner_tool_name

    route = proposal["route"]
    broker = route["broker"]
    context = _live_context(broker)
    if not context["mandate_ok"]:
        return [_order_result(o, "not_submitted", error=context["mandate_detail"]) for o in proposal["orders"]]
    mandate = context["mandate"]
    tool = runner_tool_name(broker, "submit_order") or "place_order"
    adapter = live_broker_adapter(broker)
    results: list[dict[str, Any]] = []
    for order, request in zip(proposal["orders"], route["requests"]):
        bound = {**request, **runner_arguments(broker, "orders", mandate.consent.account_ref if mandate else "")}
        try:
            response = adapter.call_tool(tool, bound)
        except Exception as exc:  # noqa: BLE001 - recorded, never retried
            response = {"status": "error", "error": str(exc)}
        status, error = _broker_status(response)
        _audit(broker, tool, f"approved flatten {order['symbol']}: {order['side']} {order['qty']} @ market",
               request, response if isinstance(response, dict) else None,
               "accepted" if status != "failed" else "error", error)
        results.append(_order_result(order, status, response, error))
    return results


#: Service functions a ``vt_service`` route may replay, with how their audit
#: request maps back to keyword arguments.
_SERVICE_REPLAY = {
    "close_position": ("close_position", ("position_id", "instrument_id", "units_to_close", "request_id")),
    "cancel_close_order": ("cancel_close_order", ("order_id", "request_id")),
    "edit_position_stops": ("edit_position_stops", ("position_id", "stop_loss", "take_profit", "trailing_stop_loss",
                                                    "clear_stop_loss", "clear_take_profit", "request_id")),
    "copy_start_or_adjust": ("etoro_copy_start", ("parent_cid", "amount", "reference_id", "request_id")),
    "copy_close": ("etoro_copy_close", ("mirror_id", "unregister_type", "request_id")),
}


def _execute_vt_service(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    from src.trading import service

    route = proposal["route"]
    function_name, fields = _SERVICE_REPLAY[route["remote_tool"]]
    request = dict(route["audit_request"])
    kwargs = {name: request.get(name) for name in fields}
    overrides = dict(route.get("overrides") or {})
    session_id = str(overrides.pop("session_id", "") or "")
    with _granted(proposal["id"], fingerprint("sdk_write", _sdk_write_call(route["profile_id"], route["remote_tool"],
                                                                           request, route.get("overrides") or {}))):
        response = getattr(service, function_name)(profile_id=route["profile_id"], session_id=session_id,
                                                   **kwargs, **overrides)
    status, error = _broker_status(response)
    return [_order_result((proposal.get("orders") or [{}])[0], status, response, error)]


_EXECUTORS: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = {
    "zt_paper": _execute_zt_paper,
    "vt_profile": _execute_vt_profile,
    "mcp_guard": _execute_mcp_guard,
    "mcp_flatten": _execute_mcp_flatten,
    "vt_service": _execute_vt_service,
}


# ---------------------------------------------------------------------------
# Hooks called from VT's order paths
# ---------------------------------------------------------------------------


def _held_error(exc: Exception) -> dict[str, Any]:
    return {"status": "error", "error": ("order approval is required, but the order proposal could not be "
                                         f"recorded ({exc}); nothing was sent to the broker")}


def intercept_place_order(profile: Any, *, symbol: str, side: str, quantity: float | None,
                          notional: float | None, order_type: str, limit_price: float | None,
                          time_in_force: str, session_id: str, overrides: Mapping[str, Any]) -> dict[str, Any] | None:
    """Hook at the top of ``service.place_order`` (after the profile resolved).

    Returns ``None`` to let VT place the order (approval off, or this exact
    call was approved), else the hold envelope (or a fail-closed error).
    """
    order = {"symbol": str(symbol or "").strip().upper(), "side": str(side or "").strip().lower(),
             "qty": quantity, "notional": notional, "order_type": str(order_type or "market").strip().lower(),
             "limit_price": limit_price, "tif": str(time_in_force or "day").strip().lower()}
    paper = _is_zt_paper_profile(profile)
    if not paper:
        call = place_order_call(profile.id, order, session_id=session_id, overrides=_clean_overrides(overrides))
        if consume_grant(fingerprint("place_order", call)):
            return None
        if not approval_required() or profile.transport != "broker_sdk" or profile.readonly:
            return None
    try:
        proposal = create_proposal(
            broker=ZT_PAPER if paper else profile.id, profile=None if paper else profile, orders=[order],
            origin={"kind": "vt_order_tool", "tool": "trading_place_order", "actor": "agent",
                    "session_id": session_id or ""},
            route=None if paper else {"kind": "vt_profile", "profile_id": profile.id,
                                      "overrides": _clean_overrides(overrides), "session_id": session_id or ""},
            overrides=overrides, account=str((overrides or {}).get("account") or "") or None)
    except ValueError as exc:
        return {"status": "error", "error": f"order refused before approval: {exc}"}
    except Exception as exc:  # noqa: BLE001 - never fall through to the broker
        logger.exception("order proposal could not be recorded")
        return _held_error(exc)
    return hold_envelope(proposal)


def _sdk_write_call(profile_id: str, remote_tool: str, audit_request: Mapping[str, Any],
                    overrides: Mapping[str, Any]) -> dict[str, Any]:
    return {"profile_id": profile_id, "remote_tool": remote_tool, "audit_request": dict(audit_request or {}),
            "overrides": dict(overrides or {})}


def intercept_sdk_write(profile: Any, *, remote_tool: str, audit_request: Mapping[str, Any],
                        overrides: Mapping[str, Any]) -> dict[str, Any] | None:
    """Hook in ``service._route_sdk_write`` (eToro position actions)."""
    if consume_grant(fingerprint("sdk_write", _sdk_write_call(profile.id, remote_tool, audit_request, overrides))):
        return None
    if not approval_required() or remote_tool not in _SERVICE_REPLAY:
        return None
    request = dict(audit_request or {})
    subject = next((str(request[k]) for k in ("position_id", "parent_cid", "mirror_id", "order_id") if request.get(k)), "?")
    try:
        descriptor = profile_broker(profile)
        live = _live_context(descriptor["key"]) if descriptor.get("live") else None
        if live:
            live.pop("mandate", None)
        symbol = f"ETORO-{remote_tool.upper().replace('_', '-')}-{subject}"[:32]
        qty = _finite(request.get("units_to_close")) or _finite(request.get("amount"))
        order = {"symbol": re.sub(r"[^A-Z0-9.\-_/:]", "-", symbol.upper()),
                 "side": "buy" if remote_tool == "copy_start_or_adjust" and (qty or 0) > 0 else "sell",
                 "qty": abs(qty) if qty else None, "notional": None, "order_type": "market",
                 "limit_price": None, "tif": "day", "action": remote_tool, "action_request": _jsonable(request)}
        book = Book(account="default", positions={}, marks={}, equity=None, source=f"vt:{profile.id}",
                    error="position-level eToro actions carry no symbol to value")
        decision, validation = evaluate(broker=descriptor, account_scope={"account": "default", "symbols": [order["symbol"]]},
                                        orders=[order], targets=None, signals=[], book=book,
                                        prices={order["symbol"]: None}, policy=load_policy(), live=live)
        proposal = _persist_raw(descriptor=descriptor, orders=[order], decision=decision, validation=validation,
                                origin={"kind": "vt_sdk_write", "tool": remote_tool, "actor": "agent"},
                                route={"kind": "vt_service", "profile_id": profile.id, "remote_tool": remote_tool,
                                       "audit_request": _jsonable(request), "overrides": _jsonable(dict(overrides or {}))},
                                account_scope={"account": "default", "symbols": [order["symbol"]]})
    except Exception as exc:  # noqa: BLE001
        logger.exception("order proposal could not be recorded")
        return _held_error(exc)
    return hold_envelope(proposal)


def _persist_raw(*, descriptor: Mapping[str, Any], orders: list[dict[str, Any]], decision: dict[str, Any],
                 validation: dict[str, Any], origin: Mapping[str, Any], route: Mapping[str, Any],
                 account_scope: Mapping[str, Any]) -> dict[str, Any]:
    created = _now()
    minutes = ttl_minutes()
    proposal: dict[str, Any] = {
        "schema": SCHEMA, "id": new_proposal_id(), "created_utc": _iso(created),
        "expires_utc": _iso(created + timedelta(minutes=minutes)), "ttl_minutes": minutes,
        "origin": dict(origin), "broker": dict(descriptor), "account_scope": dict(account_scope),
        "orders": orders, "decision_record": decision, "validation": validation, "route": dict(route),
    }
    proposal["content_hash"] = content_hash(proposal)
    proposal.update({"status": PENDING, "transitions": [], "approval": None, "submission": None})
    with _proposal_lock(proposal["id"]):
        _record_transition(proposal, PENDING, actor=str(origin.get("actor") or "agent"), reason="proposed",
                           event="created", detail={"broker": descriptor.get("profile_id"), "orders": len(orders),
                                                    "validation_ok": validation["ok"]})
        write_json_atomic(proposal_path(proposal["id"]), proposal)
    return proposal


def hold_guard_order(guard: Any, *, kwargs: Mapping[str, Any], intent: Any, mandate: Any,
                     positions: Any, balance: Any) -> str | None:
    """Hook in ``LiveOrderGuardTool._allow``: VT's gate passed; hold instead of forwarding."""
    call = {"broker": guard.broker, "remote_name": guard.remote_name, "arguments": dict(kwargs)}
    if consume_grant(fingerprint("mcp_guard", call)):
        return None
    if not approval_required():
        return None
    try:
        broker = guard.broker
        descriptor = {"key": broker, "profile_id": f"{broker}-live-mcp", "connector": broker,
                      "environment": "live", "transport": "remote_mcp", "live": True, "label": broker}
        order = {"symbol": intent.symbol, "side": intent.side,
                 "qty": _round(intent.quantity, 8) if intent.quantity is not None else None,
                 "notional": None if intent.quantity is not None else _round(intent.notional_usd, 6),
                 "order_type": "limit" if intent.limit_price else "market",
                 "limit_price": intent.limit_price,
                 "tif": str(kwargs.get("time_in_force") or "day").strip().lower() or "day"}
        orders = normalize_orders([order], descriptor)
        account = mandate.consent.account_ref or "default"
        lookup = default_price_lookup(descriptor)
        book = book_from_payloads(positions, balance, account=account, source=f"mcp:{broker}",
                                  price_lookup=lookup, equity_fallback=mandate.hard_caps.account_funding_usd)
        policy = load_policy()
        live = _live_context(broker)
        live.pop("mandate", None)
        scope = [orders[0]["symbol"]]
        prices = {orders[0]["symbol"]: lookup(orders[0]["symbol"])}
        signals = normalize_signals([{"source": "agent", "symbol": orders[0]["symbol"],
                                      "direction": "long" if orders[0]["side"] == "buy" else "reduce",
                                      "rationale": "order requested through the broker's MCP order tool"}],
                                    policy, descriptor)
        decision, validation = evaluate(broker=descriptor, account_scope={"account": account, "symbols": scope},
                                        orders=orders, targets=None, signals=signals, book=book, prices=prices,
                                        policy=policy, live=live)
        proposal = _persist_raw(descriptor=descriptor, orders=orders, decision=decision, validation=validation,
                                origin={"kind": "mcp_order_tool", "tool": guard.remote_name, "actor": "agent",
                                        "session_id": guard.session_id},
                                route={"kind": "mcp_guard", "broker": broker, "server_name": guard._spec.server_name,
                                       "remote_name": guard.remote_name, "local_name": guard.name,
                                       "parameters": _jsonable(guard._spec.parameters),
                                       "arguments": _jsonable(dict(kwargs)), "session_id": guard.session_id},
                                account_scope={"account": account, "symbols": scope})
    except Exception as exc:  # noqa: BLE001 - never fall through to the broker
        logger.exception("order proposal could not be recorded")
        return json.dumps(_held_error(exc), ensure_ascii=False)
    envelope = hold_envelope(proposal)
    try:
        guard._audit(kind="order_rejected", outcome="blocked", mandate=mandate, intent=intent,
                     broker_request=None, broker_response=None,
                     gate_decision={"allowed": False, "decision": "held_for_approval",
                                    "proposal_id": proposal["id"]},
                     error=f"held for human approval (proposal {proposal['id']})")
    except Exception:  # noqa: BLE001 - auditing never blocks
        pass
    return json.dumps(envelope, ensure_ascii=False)


def hold_flatten(broker: str, positions: Sequence[Mapping[str, Any]]) -> str:
    """Hold a halt sweep's closing orders as one proposal; returns the skip reason."""
    requests: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        qty = _finite(position.get("qty")) or 0.0
        symbol = str(position.get("symbol") or "").strip().upper()
        if not qty or not symbol:
            continue
        side = "sell" if qty > 0 else "buy"
        requests.append({"action": "close", "symbol": symbol, "side": side, "qty": abs(qty), "type": "market"})
        orders.append({"symbol": symbol, "side": side, "qty": abs(qty), "notional": None, "order_type": "market",
                       "limit_price": None, "tif": "day"})
    if not orders:
        return "order approval required: no position to close"
    descriptor = {"key": broker, "profile_id": f"{broker}-live-mcp", "connector": broker, "environment": "live",
                  "transport": "remote_mcp", "live": True, "label": broker}
    policy = load_policy()
    live = _live_context(broker)
    mandate = live.pop("mandate", None)
    account = (mandate.consent.account_ref if mandate is not None else "") or "default"
    lookup = default_price_lookup(descriptor)
    held = {o["symbol"]: (o["qty"] if o["side"] == "sell" else -o["qty"]) for o in orders}
    prices = {o["symbol"]: lookup(o["symbol"]) for o in orders}
    book = Book(account=account, positions=held, marks={}, equity=(mandate.hard_caps.account_funding_usd
                                                                   if mandate is not None else None),
                source=f"sweep:{broker}", equity_basis="mandate.account_funding_usd",
                error=None if mandate is not None else "no valid mandate on file")
    signals = normalize_signals([{"source": "halt-sweep", "direction": "flat",
                                  "rationale": "kill switch tripped; the mandate permits flattening"}], policy, descriptor)
    decision, validation = evaluate(broker=descriptor, account_scope={"account": account, "symbols": sorted(held)},
                                    orders=orders, targets=None, signals=signals, book=book, prices=prices,
                                    policy=policy, live=live)
    proposal = _persist_raw(descriptor=descriptor, orders=orders, decision=decision, validation=validation,
                            origin={"kind": "halt_sweep", "tool": "flatten_and_cancel", "actor": "runner"},
                            route={"kind": "mcp_flatten", "broker": broker, "requests": requests},
                            account_scope={"account": account, "symbols": sorted(held)})
    return (f"order approval required: {len(orders)} closing order(s) held as proposal {proposal['id']} "
            "(approve in /zt/approvals once HALT is cleared)")


def annotate_order_tool(tool: Any) -> Any:
    """Tell the model that a live order tool returns a proposal (approval mode only)."""
    if approval_required():
        note = (" [Order approval is required: this call records a PENDING order proposal that the user "
                "approves or rejects in Vibe-Trading; it does not place the order. Do not retry.]")
        if note not in str(getattr(tool, "description", "")):
            tool.description = f"{getattr(tool, 'description', '')}{note}"
    return tool
