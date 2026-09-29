"""ZT add-on: knowledge-time earnings events, SUE and PEAD for Vibe-Trading.

The logic behind the read-only ``zt-events`` MCP server, the PEAD run builder
and the forecast-ledger hook. Everything is read from ZT's pitdb warehouse
through its sanctioned point-in-time layer only:

* ``event_asof(kt)`` -- 8-K Item 2.02 earnings releases and 10-Q/10-K filing
  instants (pitdb connector ``sec_8k_earnings``);
* ``obs_asof(kt)`` / ``obs_revisions_asof(kt)`` -- diluted EPS by duration
  (``sec_xbrl_eps``), turned into SUE by the project's own ``pitdb/sue.py``;
* ``price_asof(kt)`` -- closes (``yahoo_eod``), plus the dimension tables for
  identity and PIT class.

The statistics are VT's: :mod:`src.quantlib.event_study` aligns day 0 to the
first session whose close is after the knowledge time and carries the
clustering-robust tests. Nothing here writes, fetches, trades or states an
outcome probability; historical hit rates are reported as counts.

Store access: the disposable E: index (``PITDB_INDEX``) opened read-only after
``extensions/pit_actor_sim/pit_guard.py`` confirms it mirrors the G: lake for
every table read here. The event macros live in ``pitdb/events_schema.sql``
and are created as TEMP macros on that read-only connection.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True  # never drop __pycache__ into the canonical G: tree

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

_HERE = Path(__file__).resolve().parent
VT_ROOT = _HERE.parents[1]
_AGENT = VT_ROOT / "agent"
if str(_AGENT) not in sys.path:
    sys.path.insert(0, str(_AGENT))

from src.quantlib import event_study as es  # noqa: E402

EARNINGS_SOURCE = "sec_8k_earnings"
EPS_SOURCE = "sec_xbrl_eps"
READ_TABLES = ("dim_source", "dim_security", "dim_security_alias", "dim_series", "dim_entity",
               "fact_price_eod", "fact_observation", "fact_event")
DEFAULT_BENCHMARK = "SPY"
DEFAULT_WINDOWS: tuple[tuple[int, int], ...] = es.DEFAULT_CAR_WINDOWS
#: SUE bucket edges; buckets Q1 (most negative) .. Q5 (most positive). Fixed
#: edges are point-in-time by construction: no future SUE moves a boundary.
DEFAULT_SUE_EDGES: tuple[float, ...] = (-1.0, -0.25, 0.25, 1.0)
DEFAULT_HOLD_SESSIONS = 20
DEFAULT_SLOT_WEIGHT = 0.1
DRIFT_WINDOW = (2, 20)
#: Signal source named in proposal arguments and the tag that opens their rationale.
PROPOSAL_SOURCE = "zt-pead"
MAX_ROWS = 500
NEW_YORK = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
AUTHORITY = ("READ_ONLY_RESEARCH: point-in-time reads through pitdb's sanctioned macros; "
             "no order, no execution, no outcome probability")


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def asof_utc(raw: Any, *, now: datetime | None = None) -> datetime:
    """An explicit, past instant with an offset -> naive UTC (pitdb convention)."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("asof is required, e.g. 2026-09-29T12:00:00Z")
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("asof requires a timezone offset, e.g. 2026-09-29T12:00:00Z")
    value = value.astimezone(timezone.utc)
    if value > (now or datetime.now(timezone.utc)):
        raise ValueError("asof cannot be in the future")
    return value.replace(tzinfo=None)


def iso(value: Any) -> str | None:
    """Naive-UTC timestamp (or date) -> ISO text with ``Z`` (dates stay dates)."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    spec = "microseconds" if stamp.microsecond else "seconds"
    return stamp.to_pydatetime().isoformat(timespec=spec) + "Z"


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, (pd.Timestamp, datetime)):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, np.ndarray)):
        return [_json_value(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value if isinstance(value, str) else str(value)


def records(frame: pd.DataFrame | None, limit: int = MAX_ROWS) -> list[dict]:
    """JSON-safe rows (ISO timestamps, NaN -> null), capped at ``limit``."""
    if frame is None or frame.empty:
        return []
    return [{k: _json_value(v) for k, v in row.items()} for row in frame.head(limit).to_dict("records")]


# ---------------------------------------------------------------------------
# Project, guard and store
# ---------------------------------------------------------------------------


def load_pit_guard():
    """``extensions/pit_actor_sim/pit_guard.py``: one guard for every pitdb add-on."""
    cached = sys.modules.get("vt_pit_guard")
    if cached is not None:
        return cached
    path = VT_ROOT / "extensions" / "pit_actor_sim" / "pit_guard.py"
    spec = importlib.util.spec_from_file_location("vt_pit_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["vt_pit_guard"] = module
    return module


def project() -> Path:
    return load_pit_guard().validate_project_root(os.environ.get("INVESTMENT_AI_PROJECT_ROOT"))


def runtime() -> Path:
    return load_pit_guard().validate_runtime_root(os.environ.get("VIBE_TRADING_HOME"), project)


def pitdb():
    """The project's own ``pitdb.events`` and ``pitdb.sue`` modules."""
    load_pit_guard().pitdb_config(project())
    from pitdb import events, sue  # noqa: PLC0415 - resolved from the project at call time
    return events, sue


#: Tests (and the run builder) may install a factory returning ``(con, provenance)``.
STORE_FACTORY: Callable[[], tuple[Any, dict]] | None = None


@contextlib.contextmanager
def store() -> Iterator[tuple[Any, dict]]:
    """A read-only warehouse connection plus its provenance.

    Production: the E: index, refused unless its receipt matches the lake for
    every table this module reads; the event macros are created as TEMP macros.
    """
    if STORE_FACTORY is not None:
        con, provenance = STORE_FACTORY()
        yield con, provenance
        return
    import duckdb  # noqa: PLC0415

    guard = load_pit_guard()
    root, home = project(), runtime()
    config = guard.pitdb_config(root)
    receipt = guard.require_fresh_index(root, home, tables=READ_TABLES)
    events, _ = pitdb()
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    try:
        events.ensure_event_schema(con, temporary=True)
        signature = guard.lake_signature(root, READ_TABLES)
        yield con, {"store": "pitdb E: index (read-only)", "index_path": str(config.DB_PATH),
                    "index_refreshed_at_utc": receipt.get("refreshed_at_utc"),
                    "lake_signature_sha256": guard.signature_digest(signature)}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Readers (sanctioned macros and dimension tables only)
# ---------------------------------------------------------------------------

_ALIAS_SQL = """
SELECT DISTINCT a.sec_id
FROM dim_security_alias a
WHERE a.alias_type = 'ticker' AND upper(a.alias_value) = upper(?)
  AND (a.valid_from IS NULL OR a.valid_from <= ?::DATE)
  AND (a.valid_to IS NULL OR a.valid_to > ?::DATE)
"""


def resolve_sec_id(con, ticker: str, day: date) -> int | None:
    """The one security a ticker named on ``day`` (None when zero or several)."""
    rows = con.execute(_ALIAS_SQL, [ticker, day, day]).fetchall()
    return int(rows[0][0]) if len(rows) == 1 else None


def price_panel(con, asof: datetime, tickers: Sequence[str], start: date,
                end: date, *, knowledge: dict | None = None) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Closes as known at ``asof`` (``price_asof``), one column per ticker.

    Args:
        knowledge: When a dict is passed, it receives ``{ticker: Series of
            each bar's knowledge time}``.

    Returns:
        ``(closes, provenance)``; provenance per ticker holds its ``sec_id``,
        bar count, the latest knowledge time and the PIT classes served, or a
        ``problem`` when the ticker has no single security or no bars.
    """
    columns, info = {}, {}
    for ticker in dict.fromkeys(t.upper() for t in tickers):
        sec_id = resolve_sec_id(con, ticker, end)
        if sec_id is None:
            info[ticker] = {"problem": "no single security for this ticker at the window end"}
            continue
        rows = con.execute(
            "SELECT p.event_date, p.close, p.knowledge_time, ds.pit_class "
            "FROM price_asof(?::TIMESTAMP) p LEFT JOIN dim_source ds ON ds.source_id = p.source_id "
            "WHERE p.sec_id = ? AND p.event_date BETWEEN ?::DATE AND ?::DATE ORDER BY p.event_date",
            [asof, sec_id, start, end]).fetchall()
        if not rows:
            info[ticker] = {"sec_id": sec_id, "problem": "no bars knowable at asof in the window"}
            continue
        frame = pd.DataFrame(rows, columns=["event_date", "close", "knowledge_time", "pit_class"])
        index = pd.DatetimeIndex(pd.to_datetime(frame["event_date"]))
        columns[ticker] = pd.Series(frame["close"].to_numpy(dtype=float), index=index)
        if knowledge is not None:
            knowledge[ticker] = pd.Series(pd.to_datetime(frame["knowledge_time"]).to_numpy(), index=index)
        info[ticker] = {"sec_id": sec_id, "bars": int(len(frame)),
                        "first_bar": iso(frame["event_date"].iloc[0]),
                        "last_bar": iso(frame["event_date"].iloc[-1]),
                        "max_knowledge_time": iso(frame["knowledge_time"].max()),
                        "pit_classes": sorted({str(c) if c else "MISSING" for c in frame["pit_class"]})}
    closes = pd.DataFrame(columns).sort_index() if columns else pd.DataFrame()
    return closes, info


def earnings_events(con, asof: datetime, *, tickers: Iterable[str] | None = None,
                    start: Any = None, end: Any = None) -> pd.DataFrame:
    """Item 2.02 8-K events knowable at ``asof``, with firm-quarter duplicates marked.

    The earliest event per ticker and ``fiscal_period_key`` is the event; the
    rest carry ``duplicate_of`` (the kept accession).
    """
    events, _ = pitdb()
    rows = events.events_asof(con, asof, event_type="earnings_8k", source_id=EARNINGS_SOURCE,
                              tickers=tickers)
    columns = ["accession", "ticker", "knowledge_time", "event_time", "revision_seq", "form",
               "items", "fiscal_period_key", "fiscal_period_end_est", "fiscal_period_basis",
               "filing_date", "url", "pit_class", "duplicate_of"]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    payload = rows["payload"]
    frame = pd.DataFrame({
        "accession": rows["accession"], "ticker": rows["ticker"],
        "knowledge_time": pd.to_datetime(rows["knowledge_time"]),
        "event_time": pd.to_datetime(rows["event_time"]), "revision_seq": rows["revision_seq"],
        "form": payload.map(lambda p: p.get("form")), "items": payload.map(lambda p: p.get("items")),
        "fiscal_period_key": payload.map(lambda p: p.get("fiscal_period_key")),
        "fiscal_period_end_est": payload.map(lambda p: p.get("fiscal_period_end_est")),
        "fiscal_period_basis": payload.map(lambda p: p.get("fiscal_period_basis")),
        "filing_date": payload.map(lambda p: p.get("filing_date")),
        "url": payload.map(lambda p: p.get("url")),
        "pit_class": rows["pit_class"].fillna("MISSING")})
    kept, duplicates = es.dedupe_events(frame, firm_col="ticker", period_col="fiscal_period_key",
                                        time_col="knowledge_time", id_col="accession")
    frame["duplicate_of"] = frame["accession"].map(
        dict(zip(duplicates["accession"], duplicates["kept_event_id"])))
    if start is not None:
        frame = frame[frame["knowledge_time"] >= pd.Timestamp(start)]
    if end is not None:
        frame = frame[frame["knowledge_time"] <= pd.Timestamp(end)]
    return frame[columns].sort_values(["knowledge_time", "accession"]).reset_index(drop=True)


def sue_histories(con, tickers: Iterable[str], asof: datetime) -> dict[str, pd.DataFrame]:
    """Every quarter's SUE as computable at its own knowledge time (pitdb/sue.py)."""
    _, sue = pitdb()
    return {t: sue.sue_history(con, t, asof) for t in dict.fromkeys(x.upper() for x in tickers)}


def bucket_of(value: Any, edges: Sequence[float] = DEFAULT_SUE_EDGES) -> str | None:
    """SUE bucket ``Q1`` (lowest) .. ``Q{len(edges)+1}``; None for a missing SUE."""
    if value is None or not isinstance(value, (int, float, np.floating)) or not math.isfinite(float(value)):
        return None
    if list(edges) != sorted(edges) or len(set(edges)) != len(edges):
        raise ValueError("bucket edges must be strictly increasing")
    return f"Q{int(np.searchsorted(np.asarray(edges, dtype=float), float(value), side='right')) + 1}"


def attach_sue(events: pd.DataFrame, histories: Mapping[str, pd.DataFrame], *,
               edges: Sequence[float] = DEFAULT_SUE_EDGES, tolerance_days: int = 20) -> pd.DataFrame:
    """Each event's quarter SUE (matched on the estimated period end) and its bucket.

    Adds ``sue``, ``sue_status``, ``sue_period_end``, ``sue_knowledge_time``,
    ``sue_bucket`` and ``decision_time`` = the later of the 8-K and the SUE
    knowledge time (the first instant a SUE-conditioned decision is possible).
    """
    _, sue = pitdb()
    out = events.copy()
    found = []
    for row in out.itertuples(index=False):
        match = sue.match_period(histories.get(row.ticker), row.fiscal_period_end_est,
                                 tolerance_days=tolerance_days)
        found.append(match or {})
    out["sue"] = [m.get("sue") if m.get("status") == "OK" else None for m in found]
    out["sue_status"] = [m.get("status") or "PENDING" for m in found]
    out["sue_period_end"] = [m.get("period_end") for m in found]
    out["sue_knowledge_time"] = pd.to_datetime([m.get("knowledge_time") for m in found], utc=True)
    out["sue_knowledge_time"] = out["sue_knowledge_time"].dt.tz_localize(None)
    out["sue_pit_class"] = [m.get("pit_class") for m in found]
    out["sue_bucket"] = [bucket_of(v, edges) for v in out["sue"]]
    decision = out[["knowledge_time", "sue_knowledge_time"]].max(axis=1)
    out["decision_time"] = decision.where(out["sue_knowledge_time"].notna())
    return out


def calendar_closes(start: Any, end: Any) -> pd.Series:
    """NYSE session closes (UTC) from the holiday rules, for sessions not yet traded."""
    return es.session_close_times(es.us_equity_sessions(start, end))


def session_open_utc(day: Any) -> pd.Timestamp:
    local = pd.Timestamp(datetime.combine(pd.Timestamp(day).date(), SESSION_OPEN), tz=NEW_YORK)
    return local.tz_convert("UTC")


# ---------------------------------------------------------------------------
# The study
# ---------------------------------------------------------------------------


def _windows(raw: Any) -> list[tuple[int, int]]:
    if raw in (None, [], ()):
        return list(DEFAULT_WINDOWS)
    out = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("windows must be [[start, end], ...] in relative sessions")
        out.append((int(item[0]), int(item[1])))
    if len(out) > 8:
        raise ValueError("at most 8 windows")
    return out


def universe(con, asof: datetime) -> list[str]:
    """Tickers with at least one Item 2.02 event knowable at ``asof``."""
    frame = earnings_events(con, asof)
    return sorted(set(frame["ticker"].dropna()))


def study_frame(con, asof: datetime, tickers: Sequence[str] | None, start: Any, end: Any, *,
                edges: Sequence[float] = DEFAULT_SUE_EDGES,
                sue_known_by_day: int | None = None) -> tuple[pd.DataFrame, dict]:
    """Events with their SUE and bucket, ready for :func:`es.run_event_study`."""
    names = [t.upper() for t in tickers] if tickers else universe(con, asof)
    events = earnings_events(con, asof, tickers=names, start=start, end=end)
    histories = sue_histories(con, sorted(set(events["ticker"])), asof)
    events = attach_sue(events, histories, edges=edges)
    notes = {"sue_after_day_limit": 0}
    if sue_known_by_day is not None and not events.empty:
        closes = calendar_closes(events["knowledge_time"].min() - pd.Timedelta(days=7),
                                 max(events["knowledge_time"].max(), pd.Timestamp(asof)) + pd.Timedelta(days=40))
        day0 = es.first_session_after(events["knowledge_time"], closes)
        limit = [closes.iloc[p + sue_known_by_day] if p >= 0 and p + sue_known_by_day < len(closes) else None
                 for p in day0]
        late = [lim is None or pd.isna(kt) or pd.Timestamp(kt).tz_localize("UTC") > lim
                for kt, lim in zip(events["sue_knowledge_time"], limit)]
        notes["sue_after_day_limit"] = int(sum(bool(x) and b is not None
                                               for x, b in zip(late, events["sue_bucket"])))
        events.loc[late, "sue_bucket"] = None
    return events, notes


def run_study(con, provenance: Mapping[str, Any], *, tickers: Sequence[str] | None, windows: Any,
              start: Any, end: Any, asof: datetime, benchmark: str = DEFAULT_BENCHMARK,
              edges: Sequence[float] = DEFAULT_SUE_EDGES, cluster_freq: str = "W",
              sue_known_by_day: int | None = None, n_boot: int = 1000, fdr: float = 0.10,
              fdr_test: str = "bmp_kp", seed: int = 20260929) -> dict:
    """8-K earnings event study, grouped by SUE bucket, as knowable at ``asof``."""
    wins = _windows(windows)
    begin = pd.Timestamp(start) if start else None
    finish = min(pd.Timestamp(end), pd.Timestamp(asof)) if end else pd.Timestamp(asof)
    events, notes = study_frame(con, asof, tickers, begin, finish, edges=edges,
                                sue_known_by_day=sue_known_by_day)
    base = {"asof": iso(asof), "authority": AUTHORITY, "provenance": dict(provenance),
            "params": {"windows": [es.window_label(w) for w in wins], "benchmark": benchmark,
                       "sue_bucket_edges": list(edges), "cluster_freq": cluster_freq,
                       "sue_known_by_day": sue_known_by_day, "fdr": fdr, "fdr_test": fdr_test,
                       "start": iso(begin), "end": iso(finish), "day0_rule": "first session whose "
                       "close is strictly after the 8-K acceptance"}}
    if events.empty:
        return {**base, "status": "NO_EVENTS", "aggregates": [], "hit_counts": [], "skips": [],
                "duplicates": [], "events": 0}
    first = events["knowledge_time"].min() - pd.Timedelta(days=420)
    last = min(pd.Timestamp(asof), events["knowledge_time"].max() + pd.Timedelta(days=130))
    closes, price_info = price_panel(con, asof, sorted(set(events["ticker"])) + [benchmark.upper()],
                                     first.date(), last.date())
    if benchmark.upper() not in closes.columns:
        return {**base, "status": "NO_BENCHMARK", "price_provenance": price_info,
                "aggregates": [], "hit_counts": [], "skips": [], "duplicates": [], "events": len(events)}
    report = es.run_event_study(
        closes, events.rename(columns={"accession": "event_id"}), benchmark=benchmark.upper(),
        windows=wins, firm_col="ticker", time_col="knowledge_time", id_col="event_id",
        period_col="fiscal_period_key", group_col="sue_bucket", signed_col="sue",
        cluster_freq=cluster_freq, n_boot=n_boot, fdr=fdr, fdr_test=fdr_test, seed=seed)
    counts = pd.concat([es.hit_rates(report, w) for w in wins], ignore_index=True)
    counts = counts[["window", "group", "successes", "trials"]].assign(
        hit_fraction=lambda d: d["successes"] / d["trials"].where(d["trials"] > 0))
    kts = events["knowledge_time"]
    return {**base, "status": "OK", "events": int(len(events)),
            "events_measured": report.params["events_measured"],
            "duplicates_dropped": report.params["duplicates_dropped"], "notes": notes,
            "knowledge_times": {"first_event": iso(kts.min()), "last_event": iso(kts.max()),
                                "last_sue": iso(events["sue_knowledge_time"].max()),
                                "last_price": max((v.get("max_knowledge_time") or "" for v in price_info.values()),
                                                  default=None) or None},
            "pit_classes": {"events": sorted(set(events["pit_class"])),
                            "sue": sorted({c for c in events["sue_pit_class"].dropna()}),
                            "prices": sorted({c for v in price_info.values() for c in v.get("pit_classes", [])})},
            "aggregates": records(report.aggregates), "hit_counts": records(counts),
            "caar_path": records(report.caar_path[report.caar_path["group"] == "ALL"]),
            "skips": records(report.skips), "duplicates": records(report.duplicates),
            "price_provenance": price_info, "_report": report, "_events": events}


# ---------------------------------------------------------------------------
# PEAD candidates
# ---------------------------------------------------------------------------


def history_study(con, provenance: Mapping[str, Any], *, asof: datetime,
                  tickers: Sequence[str] | None = None, edges: Sequence[float] = DEFAULT_SUE_EDGES,
                  history_years: int = 8, benchmark: str = DEFAULT_BENCHMARK,
                  sue_known_by_day: int = 1) -> dict:
    """The CAR[+2,+20] study behind bucket hit counts, with the eligibility of a live row.

    Only events whose SUE was known by the close of day ``sue_known_by_day``
    (default +1) keep their bucket: the history a forecast or a candidate is
    conditioned on has the same information timing as the decision itself.
    """
    return run_study(con, provenance, tickers=tickers, windows=[DRIFT_WINDOW],
                     start=pd.Timestamp(asof) - pd.Timedelta(days=365 * history_years),
                     end=pd.Timestamp(asof), asof=asof, benchmark=benchmark, edges=edges,
                     sue_known_by_day=sue_known_by_day, n_boot=200)


def entry_pit_reason(row: Mapping[str, Any]) -> str | None:
    """Fail closed for entry claims; research rows and risk-reducing exits remain visible."""
    allowed = {"TRUE_PIT", "OBSERVED_PIT"}
    for field in ("pit_class", "sue_pit_class"):
        value = row.get(field)
        if not isinstance(value, str) or value not in allowed:
            return f"{field}={value!r} is ineligible for a tradeable entry"
    return None


def pead_candidates(con, provenance: Mapping[str, Any], *, asof: datetime,
                    tickers: Sequence[str] | None = None, lookback_days: int = 10,
                    edges: Sequence[float] = DEFAULT_SUE_EDGES, long_bucket: str | None = None,
                    short_bucket: str | None = None, hold_sessions: int = DEFAULT_HOLD_SESSIONS,
                    max_entry_lag_sessions: int = 1, max_sue_lag_sessions: int = 1,
                    slot_weight: float = DEFAULT_SLOT_WEIGHT, history_years: int = 8,
                    benchmark: str = DEFAULT_BENCHMARK, study: Mapping[str, Any] | None = None) -> dict:
    """Fresh Item 2.02 events with their SUE, entry timing and bucket history.

    Timing mirrors the ``pead_8k`` backtest: the decision session is the first
    session whose close is after both the 8-K acceptance and the SUE's
    knowledge time; the backtest fills at the next session's open and holds
    ``hold_sessions`` sessions. Status per event: ``SUE_PENDING`` (no 10-Q/10-K
    EPS yet), ``SUE_<status>`` (SUE not computable), ``SUE_LATE`` (the SUE
    became known after the close of day ``max_sue_lag_sessions``, the cut-off
    the bucket history uses too), ``AWAITING_DECISION_CLOSE``, ``ENTRY_DUE``
    (before the next open), ``LATE`` (within ``max_entry_lag_sessions``) or
    ``STALE``. Only ``ENTRY_DUE`` and ``LATE`` events in the long (or, when
    enabled, short) bucket are candidates; the rest are listed with their
    status. ``exits`` lists traded-bucket events whose holding window ends at
    the next open (``EXIT_DUE``) or ended at most ``max_entry_lag_sessions``
    sessions ago (``EXIT_LATE``).

    Candidates and exits carry ``proposal``: the arguments of an order
    proposal (rationale, evidence ids, a target weight of +/-``slot_weight``
    or 0 for an exit, the signal and the symbol scope), built mechanically so
    the agent forwards them unchanged to the approvals tool, where a human
    approves or rejects them. Nothing is proposed or placed here.
    """
    top = f"Q{len(edges) + 1}"
    long_bucket = long_bucket or top
    if hold_sessions < 1 or max_entry_lag_sessions < 0 or lookback_days < 1 or max_sue_lag_sessions < 0:
        raise ValueError("hold_sessions >= 1, max_entry_lag_sessions >= 0, max_sue_lag_sessions >= 0, "
                         "lookback_days >= 1")
    if not 0.0 < slot_weight <= 1.0:
        raise ValueError("slot_weight must lie in (0, 1]")
    names = [t.upper() for t in tickers] if tickers else universe(con, asof)
    fresh_from = pd.Timestamp(asof) - pd.Timedelta(days=lookback_days)
    # Exits fall hold_sessions + 1 sessions after the decision session: look back far enough.
    exit_from = pd.Timestamp(asof) - pd.Timedelta(
        days=(hold_sessions + 2 + max_entry_lag_sessions + max_sue_lag_sessions) * 7 // 5 + 12)
    if study is None:
        study = history_study(con, provenance, asof=asof, tickers=names, edges=edges,
                              history_years=history_years, benchmark=benchmark,
                              sue_known_by_day=max_sue_lag_sessions)
    events = study.get("_events")
    if events is None or events.empty:
        events, _ = study_frame(con, asof, names, min(fresh_from, exit_from), pd.Timestamp(asof),
                                edges=edges)
    fresh = events[(events["knowledge_time"] > fresh_from) & events["duplicate_of"].isna()]
    closes = calendar_closes(min(fresh_from, exit_from) - pd.Timedelta(days=7),
                             pd.Timestamp(asof) + pd.Timedelta(days=60 + 2 * hold_sessions))
    now = pd.Timestamp(asof).tz_localize("UTC")
    next_open = next(i for i in range(len(closes)) if session_open_utc(closes.index[i]) > now)
    counts = {r["group"]: r for r in study.get("hit_counts", [])}
    rows = []
    for event in fresh.itertuples(index=False):
        day0 = int(es.first_session_after([event.knowledge_time], closes)[0])
        row = {"ticker": event.ticker, "accession": event.accession, "form": event.form,
               "knowledge_time": iso(event.knowledge_time), "pit_class": event.pit_class,
               "url": event.url, "fiscal_period_end_est": event.fiscal_period_end_est,
               "day0": iso(closes.index[day0].date()) if day0 >= 0 else None,
               "sue": event.sue, "sue_status": event.sue_status,
               "sue_period_end": iso(event.sue_period_end) if event.sue_period_end is not None else None,
               "sue_knowledge_time": iso(event.sue_knowledge_time), "sue_pit_class": event.sue_pit_class,
               "sue_bucket": bucket_of(event.sue, edges), "direction": "NONE",
               "decision_session": None, "entry_open_utc": None, "exit_open_utc": None,
               "ledger_eligible": False}
        if day0 >= 0 and day0 + 1 < len(closes):
            row["ledger_eligible"] = bool(row["sue_bucket"]) and now <= closes.iloc[day0 + 1]
        if pd.isna(event.decision_time):
            row["status"] = "SUE_PENDING" if event.sue_status == "PENDING" else f"SUE_{event.sue_status}"
            rows.append(row)
            continue
        cutoff = day0 + max_sue_lag_sessions
        if day0 >= 0 and cutoff < len(closes) and \
                pd.Timestamp(event.sue_knowledge_time).tz_localize("UTC") > closes.iloc[cutoff]:
            row["status"] = "SUE_LATE"
            rows.append(row)
            continue
        decision = int(es.first_session_after([event.decision_time], closes)[0])
        if decision < 0 or decision + 1 + hold_sessions >= len(closes):
            row["status"] = "CALENDAR_OUT_OF_RANGE"
            rows.append(row)
            continue
        entry_day = closes.index[decision + 1]
        row.update(decision_session=iso(closes.index[decision].date()),
                   entry_open_utc=iso(session_open_utc(entry_day)),
                   exit_open_utc=iso(session_open_utc(closes.index[decision + 1 + hold_sessions])))
        if now < closes.iloc[decision]:
            row["status"] = "AWAITING_DECISION_CLOSE"
        elif now <= session_open_utc(entry_day):
            row["status"] = "ENTRY_DUE"
        else:
            # Sessions between the backtest's entry open and the next open still ahead.
            lateness = next_open - (decision + 1)
            row["entry_lag_sessions"] = lateness
            row["status"] = "LATE" if lateness <= max_entry_lag_sessions else "STALE"
        if row["sue_bucket"] == long_bucket:
            row["direction"] = "LONG"
        elif short_bucket and row["sue_bucket"] == short_bucket:
            row["direction"] = "SHORT"
        hist = counts.get(row["sue_bucket"] or "")
        row["bucket_history"] = ({"window": hist["window"], "successes": hist["successes"],
                                  "trials": hist["trials"], "hit_fraction": hist["hit_fraction"]}
                                 if hist else None)
        rows.append(row)
    for row in rows:
        reason = entry_pit_reason(row)
        if reason:
            row.update(pit_ineligible_reason=reason, ledger_eligible=False)
            if row["status"] in ("ENTRY_DUE", "LATE"):
                row["status"] = "PIT_INELIGIBLE"
        if row["direction"] != "NONE" and row["status"] in ("ENTRY_DUE", "LATE"):
            weight = slot_weight if row["direction"] == "LONG" else -slot_weight
            row["proposal_tag"] = proposal_tag("entry", row["accession"])
            row["proposal"] = proposal_arguments(row, "entry", weight, hold_sessions)
    exits = _exits_due(events, closes, exit_from=exit_from, next_open=next_open, edges=edges,
                       long_bucket=long_bucket, short_bucket=short_bucket, hold_sessions=hold_sessions,
                       max_lag=max_entry_lag_sessions, max_sue_lag=max_sue_lag_sessions)
    frame = pd.DataFrame(rows)
    actionable = (frame["direction"] != "NONE") & frame["status"].isin(["ENTRY_DUE", "LATE"]) \
        if not frame.empty else pd.Series(dtype=bool)
    return {
        "asof": iso(asof), "authority": AUTHORITY, "provenance": dict(provenance),
        "params": {"lookback_days": lookback_days, "sue_bucket_edges": list(edges),
                   "long_bucket": long_bucket, "short_bucket": short_bucket,
                   "hold_sessions": hold_sessions, "max_entry_lag_sessions": max_entry_lag_sessions,
                   "max_sue_lag_sessions": max_sue_lag_sessions, "slot_weight": slot_weight,
                   "entry_rule": "decision at the first session close after both the 8-K acceptance "
                                 "and the SUE knowledge time; fill at the next session open",
                   "history_window": es.window_label(DRIFT_WINDOW), "history_years": history_years},
        "candidates": records(frame[actionable]) if not frame.empty else [],
        "watchlist": records(frame[~actionable]) if not frame.empty else [],
        "exits": records(pd.DataFrame(exits)) if exits else [],
        "bucket_history": study.get("hit_counts", []),
        "history_status": study.get("status"),
        "note": ("Candidates and exits are research inputs for order PROPOSALS; a human approves "
                 "every order. Hit counts are historical frequencies of CAR[+2,+20] > 0, not "
                 "probabilities."),
    }


def _exits_due(events: pd.DataFrame, closes: pd.Series, *, exit_from: pd.Timestamp, next_open: int,
               edges: Sequence[float], long_bucket: str, short_bucket: str | None, hold_sessions: int,
               max_lag: int, max_sue_lag: int) -> list[dict]:
    """Traded-bucket events whose holding window ends at the next open (or just did)."""
    now_rows = []
    recent = events[(events["knowledge_time"] > exit_from) & events["duplicate_of"].isna()
                    & events["decision_time"].notna()]
    for event in recent.itertuples(index=False):
        bucket = bucket_of(event.sue, edges)
        direction = ("LONG" if bucket == long_bucket else
                     "SHORT" if short_bucket and bucket == short_bucket else None)
        day0 = int(es.first_session_after([event.knowledge_time], closes)[0])
        if direction is None or day0 < 0 or day0 + max_sue_lag >= len(closes):
            continue
        if pd.Timestamp(event.sue_knowledge_time).tz_localize("UTC") > closes.iloc[day0 + max_sue_lag]:
            continue                                   # SUE_LATE: never a candidate, so never held
        decision = int(es.first_session_after([event.decision_time], closes)[0])
        exit_at = decision + 1 + hold_sessions
        missed = next_open - exit_at                   # 0: the exit is the next open
        if decision < 0 or exit_at >= len(closes) or not 0 <= missed <= max_lag:
            continue
        row = {"ticker": event.ticker, "accession": event.accession, "form": event.form,
               "knowledge_time": iso(event.knowledge_time), "pit_class": event.pit_class,
               "sue": event.sue, "sue_bucket": bucket, "sue_period_end": iso(event.sue_period_end),
               "sue_knowledge_time": iso(event.sue_knowledge_time), "sue_pit_class": event.sue_pit_class,
               "direction": direction, "decision_session": iso(closes.index[decision].date()),
               "entry_open_utc": iso(session_open_utc(closes.index[decision + 1])),
               "exit_open_utc": iso(session_open_utc(closes.index[exit_at])),
               "status": "EXIT_DUE" if missed == 0 else "EXIT_LATE", "exit_lag_sessions": missed,
               "entry_tag": proposal_tag("entry", event.accession),
               "proposal_tag": proposal_tag("exit", event.accession)}
        row["proposal"] = proposal_arguments(row, "exit", 0.0, hold_sessions)
        now_rows.append(row)
    return now_rows


def proposal_tag(kind: str, accession: str) -> str:
    """The text that opens a proposal's rationale: ``zt-pead entry sec:<accession>``."""
    return f"{PROPOSAL_SOURCE} {kind} sec:{accession}"


def proposal_arguments(row: Mapping[str, Any], kind: str, weight: float, hold_sessions: int) -> dict:
    """Order-proposal arguments for one entry or exit, forwarded unchanged by the agent.

    Mechanical text only: the release, the SUE and its bucket, the timing. No
    outcome probability, hit rate or price target is written here.
    """
    if kind == "entry":
        reason = entry_pit_reason(row)
        if reason:
            raise ValueError(reason)
    symbol = row["ticker"]
    evidence = [f"sec:{row['accession']}"]
    if row.get("sue_knowledge_time"):
        evidence.append(f"sue:{symbol}:{row.get('sue_period_end')}@{row['sue_knowledge_time']}")
    if kind == "entry":
        text = (f"{proposal_tag('entry', row['accession'])}: {row['direction']} {symbol} after the 8-K "
                f"Item 2.02 accepted {row['knowledge_time']}; SUE {float(row['sue']):.2f} "
                f"({row['sue_bucket']}) first known {row['sue_knowledge_time']}; decision session "
                f"{row['decision_session']}; the pead_8k rule enters at the {row['entry_open_utc']} open "
                f"and exits at the {row['exit_open_utc']} open ({hold_sessions} sessions).")
        if row.get("entry_lag_sessions"):
            text += f" This entry is {row['entry_lag_sessions']} session(s) late."
        direction = row["direction"].lower()
    else:
        text = (f"{proposal_tag('exit', row['accession'])}: close the {row['direction']} PEAD position in "
                f"{symbol} opened for that release ({row['entry_tag']}); its {hold_sessions}-session "
                f"hold ends at the {row['exit_open_utc']} open.")
        direction = "flat"
    return {"rationale": text, "evidence_ids": evidence, "targets": {symbol: float(weight)},
            "scope_symbols": [symbol],
            "signals": [{"source": PROPOSAL_SOURCE, "symbol": symbol, "direction": direction,
                         "rationale": text, "evidence_ids": evidence}]}


def drop_private(payload: dict) -> dict:
    """Remove in-process objects (``_report``, ``_events``) before returning JSON."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
