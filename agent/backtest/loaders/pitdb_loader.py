"""Point-in-time loader over the pitdb warehouse (ZT add-on).

``source="pitdb"`` serves end-of-day bars from ZT's bitemporal PIT warehouse
(``$INVESTMENT_AI_PROJECT_ROOT/implementation/pit_warehouse``) as a VT loader.
It is explicit-only (no fallback chain lists it, and an explicit request never
degrades to a network source) and it fails closed unless the run is bound to
a ``pit`` block::

    "pit": {
      "mode": "formation",                 # or "snapshot" (research only)
      "run_asof_utc": "2026-09-26T00:00:00Z",
      "availability_lag": "36h",           # pandas or ISO-8601 duration
      "claim": "research",                 # or "tradeable"
      "allow_reconstructed": false,
      "accepted_moves": []                 # optional, see below
    }

Reading rules
-------------
* Only the warehouse's sanctioned access layer is read: ``price_asof(kt)``
  (snapshot), ``price_formation(lag, kt := ...)`` (formation),
  ``corp_action_asof(kt)``, and the dimension tables used for identity and
  PIT class.  Every statement is parsed by DuckDB before it runs and refused
  if it names any other table or table function -- a check on what the query
  reads, not on how its text is spelled.
* Snapshot mode reads ``price_asof(run_asof)``: what was knowable at the run's
  as-of.  Backfilled bars keep their late values, so it is research-only.
* Formation mode reads, per bar, the latest revision with
  ``knowledge_time <= event_date + availability_lag`` (and never after
  ``run_asof``).  A window with no eligible bars raises, naming the backfill
  knowledge time, instead of silently returning a shorter series.
* Codes resolve to permanent ``sec_id`` s through the time-scoped ticker
  aliases valid inside the requested window; a ticker reused by two
  securities inside the window raises instead of splicing two companies.
* ``claim="tradeable"`` requires formation mode, refuses NON_PIT and missing
  PIT classes, allows RECONSTRUCTED_PIT only with ``allow_reconstructed``,
  and refuses a window whose start precedes formation-eligible history.
* Bars are checked with ``validate_ohlc(strategy="raise")`` and a one-day
  ``|log move| > ln 1.8`` raises unless a corporate action known at the as-of
  explains it, or the run lists it in ``accepted_moves`` as
  ``{"code", "date", "reason"}`` (recorded in provenance).
* The VT loader cache is never used: its key has no as-of.

Availability requires INVESTMENT_AI_PROJECT_ROOT, the E: index mirroring the
lake, and a PASS audit receipt under one hour old bound to the current lake
(formation mode also requires audit check A11), all through the extension's
``extensions/pit_actor_sim/pit_guard.py``.  Each served frame carries its PIT
provenance in ``frame.attrs["pit"]``; :meth:`PitdbLoader.run_provenance`
aggregates it for the run card's ``pit`` block.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence

import numpy as np
import pandas as pd

from backtest.asof_guard import US_EQUITY, encode_row_knowledge
from backtest.loaders.base import (
    NoAvailableSourceError,
    declared_currency_required,
    normalize_declared_quote_currency,
    validate_date_range,
    validate_ohlc,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "pitdb"
PIT_MODES = ("snapshot", "formation")
PIT_CLAIMS = ("research", "tradeable")
PIT_BLOCK_KEYS = frozenset(
    {"mode", "run_asof_utc", "availability_lag", "claim", "allow_reconstructed",
     "accepted_moves"}
)
#: One-day close-to-close log move beyond which a bar needs an explanation.
MAX_UNEXPLAINED_LOG_MOVE = math.log(1.8)
MAX_AVAILABILITY_LAG = pd.Timedelta(days=30)
FORMATION_AUDIT_CHECK = "A11"
#: Lake tables this loader reads (through macros); the index must mirror them.
LOADER_TABLES = ("dim_source", "dim_security", "dim_security_alias",
                 "fact_price_eod", "fact_corp_action")
REQUIRED_MACROS = {
    "snapshot": ("price_asof",),
    "formation": ("price_asof", "price_formation"),
}
SANCTIONED_TABLE_FUNCTIONS = frozenset({"price_asof", "price_formation", "corp_action_asof"})
SANCTIONED_BASE_TABLES = frozenset({"dim_security", "dim_security_alias", "dim_source"})
PRICE_BASIS = (
    "as captured: bars keep the vendor's split adjustment as of their "
    "knowledge_time; corporate actions are not applied by the loader"
)


class PitBindingError(ValueError):
    """The run is not bound to a valid ``pit`` block."""


class PitClaimError(ValueError):
    """The served data cannot support the run's declared claim."""


class PitDataError(ValueError):
    """Bars fail a PIT data check (identity, eligibility, OHLC, bad print)."""


class UnsanctionedQueryError(ValueError):
    """A statement reads something other than the sanctioned PIT access layer."""


class PitdbUnavailable(NoAvailableSourceError):
    """pitdb cannot serve now: project root, index or audit receipt is missing or stale."""


# ---------------------------------------------------------------------------
# The run binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptedMove:
    """A large one-day move the run explicitly vouches for."""

    code: str
    date: dt.date
    reason: str


@dataclass(frozen=True)
class PitBinding:
    """A validated ``pit`` block plus the run facts the loader needs."""

    mode: str
    run_asof: pd.Timestamp  # tz-aware UTC
    availability_lag: pd.Timedelta
    availability_lag_raw: str
    claim: str
    allow_reconstructed: bool
    accepted_moves: tuple[AcceptedMove, ...] = ()
    benchmark: Optional[str] = None

    @property
    def run_asof_naive(self) -> dt.datetime:
        """The as-of as the UTC-naive TIMESTAMP pitdb stores knowledge_time in."""
        return self.run_asof.tz_convert("UTC").tz_localize(None).to_pydatetime()

    def accepted(self, code: str, date: dt.date) -> Optional[AcceptedMove]:
        return next(
            (m for m in self.accepted_moves if m.code == code and m.date == date), None
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "run_asof_utc": _iso_utc(self.run_asof),
            "availability_lag": self.availability_lag_raw,
            "availability_lag_seconds": self.availability_lag.total_seconds(),
            "claim": self.claim,
            "allow_reconstructed": self.allow_reconstructed,
            "accepted_moves": [
                {"code": m.code, "date": m.date.isoformat(), "reason": m.reason}
                for m in self.accepted_moves
            ],
        }


def parse_pit_block(config: Mapping[str, Any], *, now: pd.Timestamp | None = None) -> PitBinding:
    """Validate a run config's ``pit`` block.

    Args:
        config: Backtest config carrying ``pit`` (and ``end_date`` /
            ``benchmark`` when present).
        now: Clock override for tests.

    Returns:
        The binding.

    Raises:
        PitBindingError: The block is missing, has unknown keys, or a value
            is malformed.
        PitClaimError: The mode cannot support the claim.
    """
    block = config.get("pit")
    if not isinstance(block, Mapping):
        raise PitBindingError(
            "source='pitdb' serves only runs bound to a `pit` block "
            "{mode, run_asof_utc, availability_lag, claim, allow_reconstructed}"
        )
    unknown = sorted(set(block) - PIT_BLOCK_KEYS)
    if unknown:
        raise PitBindingError(f"unknown `pit` keys {unknown}; allowed: {sorted(PIT_BLOCK_KEYS)}")

    mode = block.get("mode")
    if mode not in PIT_MODES:
        raise PitBindingError(f"pit.mode must be one of {PIT_MODES}, got {mode!r}")
    claim = block.get("claim")
    if claim not in PIT_CLAIMS:
        raise PitBindingError(f"pit.claim must be one of {PIT_CLAIMS}, got {claim!r}")
    allow_reconstructed = block.get("allow_reconstructed", False)
    if not isinstance(allow_reconstructed, bool):
        raise PitBindingError("pit.allow_reconstructed must be true or false")

    raw_asof = block.get("run_asof_utc")
    if not isinstance(raw_asof, str) or not raw_asof.strip():
        raise PitBindingError("pit.run_asof_utc is required (ISO-8601 with an offset, e.g. ...Z)")
    try:
        run_asof = pd.Timestamp(raw_asof.strip())
    except (TypeError, ValueError) as exc:
        raise PitBindingError(f"pit.run_asof_utc is not a timestamp: {raw_asof!r}") from exc
    if run_asof.tzinfo is None:
        raise PitBindingError("pit.run_asof_utc needs an explicit UTC offset (e.g. 'Z')")
    run_asof = run_asof.tz_convert("UTC")
    clock = now if now is not None else pd.Timestamp.now(tz="UTC")
    if run_asof > clock:
        raise PitBindingError(f"pit.run_asof_utc {raw_asof} is in the future")

    raw_lag = block.get("availability_lag")
    if not isinstance(raw_lag, str) or not raw_lag.strip():
        raise PitBindingError(
            "pit.availability_lag is required as a duration string ('36h', '1 day', 'P1DT12H')"
        )
    try:
        lag = pd.Timedelta(raw_lag.strip())
    except (TypeError, ValueError) as exc:
        raise PitBindingError(f"pit.availability_lag is not a duration: {raw_lag!r}") from exc
    if pd.isna(lag) or lag < pd.Timedelta(0) or lag > MAX_AVAILABILITY_LAG:
        raise PitBindingError(f"pit.availability_lag must lie in [0, 30 days], got {raw_lag!r}")

    if mode == "snapshot" and claim == "tradeable":
        raise PitClaimError(
            "snapshot mode is research-only: price_asof keeps backfilled bars at "
            "their late values; use mode='formation' for a tradeable claim"
        )

    end_date = config.get("end_date")
    if end_date:
        try:
            end = pd.Timestamp(end_date).date()
        except (TypeError, ValueError) as exc:
            raise PitBindingError(f"invalid end_date {end_date!r}") from exc
        if end > run_asof.date():
            raise PitBindingError(
                f"end_date {end} is after pit.run_asof_utc {raw_asof}: bars after the "
                "as-of cannot be known"
            )

    moves = []
    for entry in block.get("accepted_moves") or []:
        if not isinstance(entry, Mapping) or not all(
            isinstance(entry.get(k), str) and entry.get(k).strip() for k in ("code", "date", "reason")
        ):
            raise PitBindingError(
                "each pit.accepted_moves entry needs non-empty 'code', 'date' and 'reason'"
            )
        try:
            day = pd.Timestamp(entry["date"]).date()
        except (TypeError, ValueError) as exc:
            raise PitBindingError(f"invalid accepted_moves date {entry['date']!r}") from exc
        moves.append(AcceptedMove(entry["code"].strip(), day, entry["reason"].strip()))

    benchmark = config.get("benchmark")
    benchmark = benchmark.strip() if isinstance(benchmark, str) and benchmark.strip() else None
    if benchmark == "auto":
        benchmark = None

    return PitBinding(
        mode=mode,
        run_asof=run_asof,
        availability_lag=lag,
        availability_lag_raw=raw_lag.strip(),
        claim=claim,
        allow_reconstructed=allow_reconstructed,
        accepted_moves=tuple(moves),
        benchmark=benchmark,
    )


# ---------------------------------------------------------------------------
# Sanctioned reads
# ---------------------------------------------------------------------------

_ALIAS_SQL = """
SELECT DISTINCT a.sec_id, a.valid_from, a.valid_to, s.primary_ticker, s.currency
FROM dim_security_alias a JOIN dim_security s ON s.sec_id = a.sec_id
WHERE a.alias_type = 'ticker' AND upper(a.alias_value) = upper(?)
  AND (a.valid_from IS NULL OR a.valid_from <= ?::DATE)
  AND (a.valid_to IS NULL OR a.valid_to > ?::DATE)
ORDER BY a.sec_id, a.valid_from NULLS FIRST
"""

_BAR_COLUMNS = (
    "p.event_date, p.open, p.high, p.low, p.close, p.volume, p.currency, "
    "p.knowledge_time, p.revision_seq, p.source_id, ds.pit_class"
)
_BAR_FIELDS = ("event_date", "open", "high", "low", "close", "volume", "currency",
               "knowledge_time", "revision_seq", "source_id", "pit_class")

_SNAPSHOT_SQL = f"""
SELECT {_BAR_COLUMNS}
FROM price_asof(?::TIMESTAMP) p
LEFT JOIN dim_source ds ON ds.source_id = p.source_id
WHERE p.sec_id = ? AND p.event_date BETWEEN ?::DATE AND ?::DATE
ORDER BY p.event_date
"""

_FORMATION_SQL = f"""
SELECT {_BAR_COLUMNS}
FROM price_formation(to_seconds(?::DOUBLE), kt := ?::TIMESTAMP) p
LEFT JOIN dim_source ds ON ds.source_id = p.source_id
WHERE p.sec_id = ? AND p.event_date BETWEEN ?::DATE AND ?::DATE
ORDER BY p.event_date
"""

_CORP_ACTION_SQL = """
SELECT ex_date, action_type, ratio, amount, knowledge_time
FROM corp_action_asof(?::TIMESTAMP)
WHERE sec_id = ? AND ex_date BETWEEN ?::DATE AND ?::DATE
ORDER BY ex_date
"""

#: Catalog metadata only (no facts): which access-layer macros the index defines.
_MACRO_CATALOG_SQL = (
    "SELECT function_name, macro_definition FROM duckdb_functions() "
    "WHERE function_type = 'table_macro'"
)

_CHECKED_SQL: set[str] = set()


def referenced_relations(con: Any, sql: str) -> tuple[set[str], set[str]]:
    """Base tables and table functions a single SELECT reads, from DuckDB's parse tree.

    Args:
        con: A DuckDB connection (only its parser is used).
        sql: The statement.

    Returns:
        ``(base_tables, table_functions)``, lower-cased; CTE names are
        excluded and schema-qualified names keep their qualifier.

    Raises:
        UnsanctionedQueryError: The text is not exactly one SELECT statement.
    """
    raw = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    tree = json.loads(raw)
    if tree.get("error"):
        raise UnsanctionedQueryError(
            f"not a single SELECT statement: {tree.get('error_message', 'parse error')}"
        )
    statements = tree.get("statements") or []
    if len(statements) != 1:
        raise UnsanctionedQueryError(f"expected one statement, got {len(statements)}")
    tables: set[str] = set()
    functions: set[str] = set()
    ctes: set[str] = set()
    _walk_parse_tree(statements[0], tables, functions, ctes)
    return tables - ctes, functions


def _walk_parse_tree(node: Any, tables: set[str], functions: set[str], ctes: set[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_parse_tree(item, tables, functions, ctes)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    if kind == "BASE_TABLE":
        name = str(node.get("table_name", "")).lower()
        qualifier = ".".join(
            str(node.get(key) or "").lower() for key in ("catalog_name", "schema_name")
            if node.get(key)
        )
        tables.add(f"{qualifier}.{name}" if qualifier and qualifier != "main" else name)
    elif kind == "TABLE_FUNCTION":
        function = node.get("function") or {}
        functions.add(str(function.get("function_name", "")).lower())
    cte_map = node.get("cte_map")
    if isinstance(cte_map, dict):
        for entry in cte_map.get("map") or []:
            if isinstance(entry, dict) and entry.get("key"):
                ctes.add(str(entry["key"]).lower())
    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_parse_tree(value, tables, functions, ctes)


def require_sanctioned(con: Any, sql: str) -> None:
    """Refuse a statement that reads anything but the sanctioned PIT access layer.

    Sanctioned: the ``price_asof`` / ``price_formation`` / ``corp_action_asof``
    macros and the dimension tables used for identity and PIT class. A direct
    fact-table read, a file scan (``read_parquet``, ``'x.parquet'``), another
    catalog, or anything that is not one SELECT is refused.
    """
    if sql in _CHECKED_SQL:
        return
    tables, functions = referenced_relations(con, sql)
    bad_tables = sorted(tables - SANCTIONED_BASE_TABLES)
    bad_functions = sorted(functions - SANCTIONED_TABLE_FUNCTIONS)
    if bad_tables or bad_functions:
        raise UnsanctionedQueryError(
            "pitdb loader reads only the sanctioned PIT access layer; refused "
            f"tables {bad_tables} and table functions {bad_functions}"
        )
    _CHECKED_SQL.add(sql)


def _read(con: Any, sql: str, params: Sequence[Any]) -> list[tuple]:
    require_sanctioned(con, sql)
    return con.execute(sql, list(params)).fetchall()


# ---------------------------------------------------------------------------
# Backends: where the read-only connection and the receipts come from
# ---------------------------------------------------------------------------


class PitdbBackend(Protocol):
    """Source of availability receipts and read-only connections."""

    def availability(self, required_checks: Sequence[str] = ()) -> dict[str, Any]:
        """Return warehouse provenance, or raise :class:`PitdbUnavailable`."""
        ...

    def connect(self) -> Any:
        """Return a read-only DuckDB connection; the caller closes it."""
        ...


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_pit_guard(path: Path | None = None):
    """Load ``extensions/pit_actor_sim/pit_guard.py`` by path (one guard for both add-ons)."""
    cached = sys.modules.get("vt_pit_guard")
    if cached is not None and path is None:
        return cached
    guard_path = path or _repo_root() / "extensions" / "pit_actor_sim" / "pit_guard.py"
    if not guard_path.is_file():
        raise PitdbUnavailable(f"pit_guard.py not found at {guard_path}")
    spec = importlib.util.spec_from_file_location("vt_pit_guard", guard_path)
    if spec is None or spec.loader is None:
        raise PitdbUnavailable(f"cannot load {guard_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if path is None:
        sys.modules["vt_pit_guard"] = module
    return module


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


class ProjectPitdbBackend:
    """Production backend: her pitdb package, the E: index, the extension's receipts."""

    def __init__(self, project_root: Path, runtime_root: Path, guard: Any) -> None:
        self.project_root = Path(project_root)
        self.runtime_root = Path(runtime_root)
        self.guard = guard

    @classmethod
    def from_environment(cls) -> "ProjectPitdbBackend":
        """Resolve roots through VT's config layer and validate them with pit_guard."""
        from src.config.accessor import get_env_value

        guard = load_pit_guard()
        try:
            project_root = guard.validate_project_root(
                get_env_value("INVESTMENT_AI_PROJECT_ROOT", "").strip() or None
            )
            runtime_root = guard.validate_runtime_root(
                get_env_value("VIBE_TRADING_HOME", "").strip() or None, lambda: project_root
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise PitdbUnavailable(str(exc)) from exc
        return cls(project_root, runtime_root, guard)

    def _config(self):
        C = self.guard.pitdb_config(self.project_root)
        own_root = getattr(C, "PROJECT_ROOT", None)
        if own_root is not None and Path(own_root).resolve() != self.project_root:
            raise PitdbUnavailable(
                f"pitdb resolves its project to {own_root} (INVESTMENT_PROJECT_ROOT) but "
                f"INVESTMENT_AI_PROJECT_ROOT is {self.project_root}; set both to one folder"
            )
        return C

    def availability(self, required_checks: Sequence[str] = ()) -> dict[str, Any]:
        try:
            C = self._config()
            index_receipt = self.guard.require_fresh_index(
                self.project_root, self.runtime_root, tables=LOADER_TABLES
            )
            signature = self.guard.lake_signature(self.project_root)
            audit_receipt = self.guard.require_fresh_audit(
                self.runtime_root,
                lambda: signature,
                required_checks=tuple(required_checks),
            )
        except PitdbUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - every guard failure means "unavailable"
            raise PitdbUnavailable(str(exc)) from exc
        package = sys.modules.get("pitdb")
        return {
            "backend": "project",
            "index_path": str(C.DB_PATH),
            "index_refreshed_at_utc": index_receipt.get("refreshed_at_utc"),
            "lake_root": signature.get("lake_root"),
            "lake_signature_sha256": self.guard.signature_digest(signature),
            "audit": {
                "audited_at_utc": audit_receipt.get("audited_at_utc"),
                "status": audit_receipt.get("status"),
                "checks_passed": list(audit_receipt.get("checks_passed") or []),
            },
            "schema_sha256": _sha256_file(C.SCHEMA_SQL),
            "pitdb_version": getattr(package, "__version__", None),
        }

    def connect(self) -> Any:
        import duckdb

        C = self._config()
        return duckdb.connect(str(C.DB_PATH), read_only=True)


def default_backend() -> PitdbBackend:
    """The backend a registry-constructed loader uses (overridable in tests)."""
    return ProjectPitdbBackend.from_environment()


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def pitdb_ticker(code: str) -> str:
    """VT code -> warehouse ticker alias: ``NVDA.US`` -> ``NVDA``; ``^GSPC`` unchanged."""
    code = str(code).strip()
    return code[:-3] if code.upper().endswith(".US") else code


def _iso_utc(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return None
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.isoformat().replace("+00:00", "Z")


class PitdbLoader:
    """Explicit-only, fail-closed point-in-time loader over pitdb (see module docstring).

    Not decorated with ``@register``: ``registry.ADDON_LOADERS`` registers it on
    the first explicit ``source="pitdb"`` request, so importing this module
    never changes the upstream loader registry.
    """

    name = SOURCE_NAME
    markets = {"us_equity", "index"}
    requires_auth = False
    volume_units = {"us_equity": "shares", "index": "shares"}

    def __init__(self, backend: PitdbBackend | None = None) -> None:
        self._backend = backend
        self._binding: Optional[PitBinding] = None
        self._provenance: Dict[str, dict[str, Any]] = {}
        self._warehouse: dict[str, Any] = {}
        self._warnings: List[str] = []
        self._side_frames: Dict[tuple[str, str, str], pd.DataFrame] = {}
        self._max_known: Dict[str, pd.Timestamp] = {}
        self._prefetching = False
        self._corp_actions_readable = False
        self.unavailable_reason: Optional[str] = None

    # -- wiring -------------------------------------------------------------

    def _get_backend(self) -> PitdbBackend:
        if self._backend is None:
            self._backend = default_backend()
        return self._backend

    def is_available(self) -> bool:
        """True when the project, the E: index and a fresh PASS audit receipt line up."""
        try:
            self._get_backend().availability(())
        except Exception as exc:  # noqa: BLE001 - any failure means unavailable
            self.unavailable_reason = str(exc)
            logger.warning("pitdb unavailable: %s", exc)
            return False
        self.unavailable_reason = None
        return True

    def bind_run_config(self, config: Mapping[str, Any]) -> PitBinding:
        """Bind this loader to a run's ``pit`` block; nothing is served unbound."""
        self._binding = parse_pit_block(config)
        self._provenance = {}
        self._max_known = {}
        self._warnings = []
        self._side_frames = {}
        return self._binding

    @property
    def binding(self) -> Optional[PitBinding]:
        return self._binding

    def engine_loader(self, data_map: Mapping[str, pd.DataFrame]) -> "PitdbRunLoader":
        """The loader the engine should hold for this run (serves the benchmark too)."""
        return PitdbRunLoader(self, data_map)

    # -- fetch --------------------------------------------------------------

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Serve daily bars for ``codes`` under the bound ``pit`` block.

        Returns:
            ``{code: DataFrame(open, high, low, close, volume)}`` on a
            UTC-naive ``trade_date`` index. A code with no bars at all is left
            out (the runner then raises, as for any no-fallback source).

        Raises:
            PitBindingError: The loader is not bound to a ``pit`` block.
            PitdbUnavailable: Receipts went stale since ``is_available``.
            PitDataError / PitClaimError: A PIT check failed.
        """
        binding = self._binding
        if binding is None:
            raise PitBindingError(
                "pitdb loader is unbound: the run must carry a `pit` block and the "
                "runner binds it before fetching (bind_run_config)"
            )
        if interval != "1D":
            raise ValueError(f"pitdb serves end-of-day bars only, got interval {interval!r}")
        validate_date_range(start_date, end_date)
        start = pd.Timestamp(start_date).date()
        end = pd.Timestamp(end_date).date()
        if end > binding.run_asof.date():
            raise PitBindingError(f"end_date {end} is after the run as-of {binding.run_asof}")

        result: Dict[str, pd.DataFrame] = {}
        wanted: List[str] = []
        for code in codes:
            cached = self._side_frames.get((code, str(start_date), str(end_date)))
            if cached is not None:
                result[code] = cached
            else:
                wanted.append(code)
        benchmark = binding.benchmark
        prefetch_benchmark = (
            benchmark is not None
            and not self._prefetching
            and benchmark not in codes
            and (benchmark, str(start_date), str(end_date)) not in self._side_frames
        )
        if not wanted and not prefetch_benchmark:
            return result

        required = (FORMATION_AUDIT_CHECK,) if binding.mode == "formation" else ()
        self._warehouse = self._get_backend().availability(required)
        con = self._get_backend().connect()
        try:
            self._check_macros(con, binding.mode)
            for code in wanted:
                frame = self._fetch_one(con, binding, code, start, end, role="strategy")
                if frame is not None:
                    result[code] = frame
            if prefetch_benchmark:
                frame = self._fetch_one(con, binding, benchmark, start, end, role="benchmark")
                if frame is None:
                    raise NoAvailableSourceError(
                        f"benchmark {benchmark} has no pitdb bars in [{start}, {end}]; "
                        "a pitdb run never fetches its benchmark from the network"
                    )
                self._side_frames[(benchmark, str(start_date), str(end_date))] = frame
        finally:
            con.close()
        return result

    def _check_macros(self, con: Any, mode: str) -> None:
        rows = con.execute(_MACRO_CATALOG_SQL).fetchall()
        defined = {str(name).lower(): str(body or "") for name, body in rows}
        missing = [m for m in REQUIRED_MACROS[mode] if m not in defined]
        if missing:
            raise PitdbUnavailable(
                f"the pitdb index lacks macro(s) {missing}; deploy the current "
                "schema.sql and rebuild the index (server.py --refresh-index)"
            )
        self._warehouse = dict(self._warehouse)
        self._warehouse["macros_sha256"] = {
            name: "sha256:" + hashlib.sha256(defined[name].encode("utf-8")).hexdigest()
            for name in sorted(SANCTIONED_TABLE_FUNCTIONS) if name in defined
        }
        self._corp_actions_readable = "corp_action_asof" in defined

    def _resolve(self, con: Any, code: str, start: dt.date, end: dt.date) -> Optional[tuple]:
        ticker = pitdb_ticker(code)
        rows = _read(con, _ALIAS_SQL, [ticker, end, start])
        sec_ids = sorted({row[0] for row in rows})
        if not sec_ids:
            return None
        if len(sec_ids) > 1:
            spans = "; ".join(
                f"sec_id {sec} valid {vf or '-inf'}..{vt or '+inf'}"
                for sec, vf, vt, *_ in rows
            )
            raise PitDataError(
                f"{code}: ticker {ticker} maps to {len(sec_ids)} securities inside "
                f"[{start}, {end}] ({spans}); the ticker was reused, so split the "
                "window at the reuse date instead of splicing two companies"
            )
        sec_id, valid_from, valid_to, primary, currency = rows[0]
        return sec_id, valid_from, valid_to, primary, currency, ticker

    def _fetch_one(
        self, con: Any, binding: PitBinding, code: str, start: dt.date, end: dt.date,
        *, role: str,
    ) -> Optional[pd.DataFrame]:
        identity = self._resolve(con, code, start, end)
        if identity is None:
            logger.warning("pitdb: no ticker alias for %s in [%s, %s]", code, start, end)
            return None
        sec_id, valid_from, valid_to, _primary, _currency, ticker = identity
        asof = binding.run_asof_naive
        snapshot = _read(con, _SNAPSHOT_SQL, [asof, sec_id, start, end])
        if binding.mode == "formation":
            rows = _read(
                con, _FORMATION_SQL,
                [binding.availability_lag.total_seconds(), asof, sec_id, start, end],
            )
        else:
            rows = snapshot
        if not snapshot:
            return None

        served_dates = {row[0] for row in rows}
        ineligible = [row for row in snapshot if row[0] not in served_dates]
        if not rows:
            kts = sorted(row[7] for row in ineligible)
            raise PitDataError(
                f"{code}: no eligible bars in [{start}, {end}] under availability_lag="
                f"{binding.availability_lag_raw} (formation mode): {len(ineligible)} "
                f"bar(s) exist but were first known at {_iso_utc(kts[0])}.."
                f"{_iso_utc(kts[-1])} (backfill kt {kts[0].date()})"
            )
        first_served = min(served_dates)
        leading = [row for row in ineligible if row[0] < first_served]
        if binding.claim == "tradeable" and leading:
            raise PitClaimError(
                f"{code}: tradeable claim but the window starts {start} while the first "
                f"formation-eligible bar is {first_served}; {len(leading)} earlier bar(s) "
                f"were only known later (first known {_iso_utc(min(r[7] for r in leading))}). "
                "Move start_date to the first eligible bar"
            )

        frame = pd.DataFrame(rows, columns=list(_BAR_FIELDS))
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame["event_date"]), name="trade_date")
        frame = frame.sort_index()

        pit_classes = sorted({
            "MISSING" if c is None or (isinstance(c, float) and math.isnan(c)) else str(c)
            for c in frame["pit_class"]
        })
        self._check_claim(code, binding, pit_classes)
        # Bars whose value was revised after the formation window closed: the
        # replay uses the earlier print; a snapshot would have used the later.
        latest = {row[0]: (row[7], row[8]) for row in snapshot}
        revised_later = sum(1 for row in rows if latest.get(row[0]) != (row[7], row[8]))

        prices = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
        null_bars = prices.isna().any(axis=1)
        # ZT add-on: a revision captured before its NYSE session closed (including
        # early closes) is an unfinished intraday bar, not a finished one known
        # early; it is treated as unknown, like a bar outside the lag.
        closes = US_EQUITY.close_utc(pd.to_datetime(frame["event_date"])).asi8
        captured = pd.DatetimeIndex(pd.to_datetime(frame["knowledge_time"])).tz_localize("UTC").asi8
        unfinished = pd.Series(captured < closes, index=frame.index)
        dropped = null_bars | unfinished
        bars = prices[~dropped].astype("float64")
        volume = pd.to_numeric(frame.loc[~dropped, "volume"], errors="coerce")
        bars["volume"] = volume.fillna(0.0).astype("float64")
        if bars.empty:
            raise PitDataError(
                f"{code}: every served bar has a missing OHLC value or was captured "
                "before its session closed"
            )
        try:
            bars = validate_ohlc(bars, strategy="raise")
        except ValueError as exc:
            raise PitDataError(f"{code}: {exc}") from exc

        moves = self._check_moves(con, binding, code, sec_id, bars)

        kept = frame.loc[~dropped]
        currencies = sorted({str(c) for c in kept["currency"].dropna()})
        if len(currencies) > 1:
            raise PitDataError(f"{code}: bars quoted in several currencies {currencies}")
        currency = currencies[0] if currencies else None
        if declared_currency_required(code):
            bars = normalize_declared_quote_currency(bars, code, currency)
        elif currency:
            bars.attrs["quote_currency"] = currency

        known = pd.to_datetime(kept["knowledge_time"])
        formed_by = pd.to_datetime(kept["event_date"]) + binding.availability_lag
        backfill_bars = int((known > formed_by).sum())
        record = {
            "role": role,
            "ticker": ticker,
            "sec_id": int(sec_id),
            "alias_valid_from": valid_from.isoformat() if valid_from else None,
            "alias_valid_to": valid_to.isoformat() if valid_to else None,
            "bars": int(len(bars)),
            "first_bar": bars.index[0].date().isoformat(),
            "last_bar": bars.index[-1].date().isoformat(),
            "pit_classes": pit_classes,
            "max_knowledge_time_utc": _iso_utc(known.max()),
            "backfill_bars": backfill_bars,
            "backfill_share": round(backfill_bars / max(len(kept), 1), 6),
            "revised_bars": int((pd.to_numeric(kept["revision_seq"]) > 0).sum()),
            "revised_after_formation": revised_later,
            "dropped_null_bars": int(null_bars.sum()),
            "unfinished_bars": int((unfinished & ~null_bars).sum()),
            "ineligible_bars": len(ineligible),
            "ineligible_leading_bars": len(leading),
            "quote_currency": currency,
            **moves,
        }
        if binding.mode == "snapshot" and backfill_bars:
            self._warnings.append(
                f"{code}: {backfill_bars}/{len(kept)} bars were first known after "
                f"event_date + {binding.availability_lag_raw} (snapshot mode, research only)"
            )
        if ineligible:
            self._warnings.append(
                f"{code}: {len(ineligible)} bar(s) in the window were not knowable within "
                f"{binding.availability_lag_raw} and are treated as unknown"
            )
        if record["unfinished_bars"]:
            self._warnings.append(
                f"{code}: {record['unfinished_bars']} bar(s) were captured before their "
                "session closed and are treated as unknown"
            )
        bars.attrs["pit"] = {
            "source": SOURCE_NAME,
            "mode": binding.mode,
            "run_asof_utc": _iso_utc(binding.run_asof),
            "availability_lag": binding.availability_lag_raw,
            "claim": binding.claim,
            **record,
            # ZT add-on: each bar's knowledge time, for backtest.asof_guard
            # (packed bytes; not part of the run card).
            **encode_row_knowledge(kept["event_date"], kept["knowledge_time"]),
        }
        self._provenance[code] = record
        self._max_known[code] = known.max()
        return bars

    def _check_claim(self, code: str, binding: PitBinding, pit_classes: Sequence[str]) -> None:
        if binding.claim != "tradeable":
            return
        refused = [c for c in pit_classes if c in ("NON_PIT", "MISSING")]
        if refused:
            raise PitClaimError(
                f"{code}: tradeable claim refuses PIT classes {refused} "
                "(NON_PIT has no vintage trail; MISSING is an unregistered source)"
            )
        if "RECONSTRUCTED_PIT" in pit_classes and not binding.allow_reconstructed:
            raise PitClaimError(
                f"{code}: RECONSTRUCTED_PIT bars need pit.allow_reconstructed=true "
                "for a tradeable claim"
            )
        unknown = [c for c in pit_classes if c not in ("TRUE_PIT", "OBSERVED_PIT", "RECONSTRUCTED_PIT")]
        if unknown:
            raise PitClaimError(f"{code}: tradeable claim refuses unknown PIT classes {unknown}")

    def _check_moves(
        self, con: Any, binding: PitBinding, code: str, sec_id: int, bars: pd.DataFrame,
    ) -> dict[str, Any]:
        close = bars["close"]
        log_move = np.log(close).diff()
        flagged = log_move[log_move.abs() > MAX_UNEXPLAINED_LOG_MOVE]
        explained: list[dict[str, Any]] = []
        accepted: list[dict[str, Any]] = []
        if flagged.empty:
            return {"explained_moves": explained, "accepted_moves": accepted}
        actions: list[tuple] = []
        if getattr(self, "_corp_actions_readable", False):
            actions = _read(
                con, _CORP_ACTION_SQL,
                [binding.run_asof_naive, sec_id, bars.index[0].date(), bars.index[-1].date()],
            )
        dates = list(bars.index)
        for when, move in flagged.items():
            prev = dates[dates.index(when) - 1].date()
            day = when.date()
            entry = {"date": day.isoformat(), "from": prev.isoformat(),
                     "move_pct": round(float(math.expm1(move)) * 100, 2)}
            hits = [a for a in actions if prev < a[0] <= day]
            if hits:
                entry["corp_actions"] = [
                    {"ex_date": a[0].isoformat(), "type": a[1], "ratio": a[2], "amount": a[3]}
                    for a in hits
                ]
                if binding.claim == "tradeable":
                    raise PitClaimError(
                        f"{code}: {entry['move_pct']}% on {day} crosses an unadjusted "
                        f"corporate action ({hits[0][1]} ex {hits[0][0]}); the loader does "
                        "not adjust prices, so a tradeable claim cannot span it"
                    )
                self._warnings.append(
                    f"{code}: {entry['move_pct']}% on {day} is an unadjusted "
                    f"{hits[0][1]}; returns across it are not economic"
                )
                explained.append(entry)
                continue
            vouched = binding.accepted(code, day)
            if vouched is not None:
                entry["reason"] = vouched.reason
                accepted.append(entry)
                continue
            raise PitDataError(
                f"{code}: unexplained one-day move of {entry['move_pct']}% on {day} "
                f"(from {prev}; |log move| {abs(float(move)):.3f} > ln 1.8) with no "
                "corporate action known at the as-of. Fix the print, record the "
                "corporate action, or list it in pit.accepted_moves with a reason"
            )
        return {"explained_moves": explained, "accepted_moves": accepted}

    # -- provenance ---------------------------------------------------------

    def run_provenance(self) -> Optional[dict[str, Any]]:
        """Everything the run card's ``pit`` block records about this run."""
        binding = self._binding
        if binding is None:
            return None
        symbols = dict(self._provenance)
        bars = sum(rec["bars"] for rec in symbols.values())
        backfill = sum(rec["backfill_bars"] for rec in symbols.values())
        known = [stamp for stamp in self._max_known.values() if not pd.isna(stamp)]
        classes = sorted({c for rec in symbols.values() for c in rec["pit_classes"]})
        return {
            "source": SOURCE_NAME,
            **binding.to_record(),
            "benchmark": binding.benchmark,
            "pit_classes": classes,
            "max_knowledge_time_utc": _iso_utc(max(known)) if known else None,
            "bars": bars,
            "backfill_share": round(backfill / bars, 6) if bars else 0.0,
            "price_basis": PRICE_BASIS,
            "loader_cache": "never used (its key has no as-of)",
            "warehouse": dict(self._warehouse),
            "symbols": symbols,
            "warnings": list(dict.fromkeys(self._warnings)),
        }


DataLoader = PitdbLoader


class PitdbRunLoader:
    """The engine's loader for a pitdb run.

    Serves the run's own fetched snapshot, and sends anything else (the
    configured benchmark) back through the bound loader, so the benchmark is
    read under the same ``pit`` block and never from the network.
    """

    name = SOURCE_NAME

    def __init__(self, bound: PitdbLoader, data_map: Mapping[str, pd.DataFrame]) -> None:
        self._bound = bound
        self._data = dict(data_map)

    def fetch(self, codes, start_date, end_date, fields=None, interval="1D", **_kwargs):
        """Return the fetched frames, reading any other code through the bound loader."""
        served = {code: self._data[code] for code in codes if code in self._data}
        rest = [code for code in codes if code not in served]
        if rest:
            served.update(self._bound.fetch(rest, start_date, end_date, interval=interval))
        return served


__all__ = [
    "AcceptedMove",
    "DataLoader",
    "PitBinding",
    "PitBindingError",
    "PitClaimError",
    "PitDataError",
    "PitdbLoader",
    "PitdbRunLoader",
    "PitdbUnavailable",
    "ProjectPitdbBackend",
    "UnsanctionedQueryError",
    "default_backend",
    "load_pit_guard",
    "parse_pit_block",
    "pitdb_ticker",
    "referenced_relations",
    "require_sanctioned",
]
