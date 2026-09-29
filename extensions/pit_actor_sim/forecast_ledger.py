"""Forecast ledger v2 with the v2.1 chaining amendment, built on VT governance.

ZT add-on (VT-first integration, 2026-09-28). This module is the write, verify,
resolve and score path for ZT's forecast ledger contract
(``G:\\My Drive\\work\\_infra\\FORECAST_LEDGER.json``, schema
``zt-forecast-ledger/2``) plus the additive v2.1 amendment
(``_infra\\FORECAST_LEDGER_V2_1_AMENDMENT.json``). It adds nothing to VT's core:
hashing and chain verification are VT's own ``src.governance.ledger``
primitives, run manifests are ``src.governance.manifest``, and every score is
computed by ``src.quantlib.scoring``.

LAYOUT (per canonical project folder on G:)
-------------------------------------------
``<Project>/_ledger/runs/<run_id>.jsonl``
    One immutable shard per publication: the record of authority.
``<Project>/_ledger/index/``
    Disposable views rebuilt deterministically from the shards (byte-identical
    on every rebuild). Deleting the folder loses nothing.
``<Project>/_ledger/writer.lease``
    The project writer lease, held only while one shard is published.

A shard is assembled and verified on local scratch (E: at runtime), then
published once by exclusive create. An existing ``run_id`` or target path is a
hard failure, never a merge or overwrite. Amendments and resolutions are new
shards that point back at earlier rows by ``target_id``.

SHARD FORMAT (v2.1)
-------------------
JSON Lines, UTF-8, ``\\n`` line ends. Line 1 is the run manifest; lines 2..N are
rows. Every line carries VT's chain fields ``seq`` / ``prev_record_hash`` /
``record_hash`` (``compute_record_hash``: SHA-256 over the canonical JSON of
``{seq, prev_record_hash, payload}``), so ``src.governance.ledger.verify_chain``
verifies a shard unmodified. Line 1 carries the shard link: ``integrity`` =
``{hash_scheme, shard_seq, prev_shard_hash, shard_record_count}`` where
``prev_shard_hash`` is the terminal ``record_hash`` of the project's previous
shard. Lines are also byte-canonical (sorted keys, VT's serializer), so any
byte edit -- including whitespace or key order, which a content hash alone
would forgive -- fails verification.

ROW KINDS BUILT HERE
--------------------
``prediction``   binary marginals of an actor-simulation terminal distribution,
                 with the Monte Carlo Wilson interval, a predeclared baseline
                 (market-implied when the anchor estimand matches, else a base
                 rate) and ``anchor_status``.
``distribution`` quantile forecasts (e.g. q01..q99) at a horizon in trading
                 days, for mechanical baselines and market-implied references.
``indicator``    mechanical monitor rows for simulation leverage nodes that
                 have a canonical PIT series.
``resolution``   written by :func:`resolve_due` through a pluggable as-of
                 reader (default: the extension's own pitdb path), carrying the
                 resolved value and the scores, so every score reproduces from
                 the shards alone (:func:`rescore_from_shards`).

No probability is elicited here: prediction rows carry simulator output, and
distribution rows carry mechanical or market-implied quantiles.

CONTAMINATION (ZT add-on, 2026-09-29)
-------------------------------------
A shard manifest may carry ``contamination`` = ``{status, skill_eligible, ...}``
from a blind actor-simulation run (status CONTAMINATED, NOT_IDENTIFIED,
NOT_PROBED or NOT_BLIND). The key is optional; a manifest without it is read
as not contaminated, so older shards verify unchanged. ``record-mct`` takes it
from the run's run_manifest.json (the sibling of ``--result`` by default). A
CONTAMINATED run is resolved like any other but the scoreboard counts it under
``n_excluded_contaminated`` and never in a skill estimate.

CLI::

    python forecast_ledger.py verify        --project <dir> [--receipts <dir>]
    python forecast_ledger.py rebuild-index --project <dir>
    python forecast_ledger.py publish       --project <dir> --draft draft.json
    python forecast_ledger.py record-mct    --project <dir> --result pilot_result.json
                                            --spec spec.json [--evidence snapshot.json] [--dry-run]
    python forecast_ledger.py resolve-due   --project <dir> [--asof ISO] [--dry-run]
    python forecast_ledger.py rescore       --project <dir>
    python forecast_ledger.py export        --project <dir> --run-id ID --out file.json
    python forecast_ledger.py merge-heads   --project <dir>

Scratch defaults to ``$ZT_LEDGER_SCRATCH`` or ``$VIBE_TRADING_HOME/forecast_ledger``
and must be on E: on Windows; the project may not be on C:, D: or the system
drive.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import math
import ntpath
import numbers
import os
import re
import secrets
import socket
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence


def _ensure_vt_importable() -> None:
    """Prefer the VT checkout this extension ships in over any installed copy.

    The extension and VT's ``agent/`` tree are versioned together in the fork;
    an installed ``vibe-trading-ai`` pointing at another checkout may lack the
    add-on modules used here (``src.quantlib.scoring``). When ``src`` is
    already imported (running inside VT itself), that package is used as is.
    """
    if "src" in sys.modules:
        return
    agent_dir = Path(__file__).resolve().parents[2] / "agent"
    if (agent_dir / "src" / "governance" / "ledger.py").is_file():
        if str(agent_dir) in sys.path:
            sys.path.remove(str(agent_dir))
        sys.path.insert(0, str(agent_dir))


_ensure_vt_importable()

from src.governance.ledger import (  # noqa: E402
    GENESIS_PREV_HASH,
    build_export,
    compute_record_hash,
    export_chain_to_file,
    verify_chain,
    verify_export,
)
from src.governance.manifest import (  # noqa: E402
    SkillRecord,
    build_run_manifest,
    collect_key_package_versions,
)
from src.quantlib import scoring  # noqa: E402

__all__ = [
    "SCHEMA",
    "AMENDMENT",
    "LedgerError",
    "SchemaError",
    "LatePredictionError",
    "ShardCollisionError",
    "ShardInvalid",
    "ChainError",
    "ForkError",
    "LeaseHeldError",
    "Observation",
    "AsOfReader",
    "PitdbReader",
    "Shard",
    "ProjectLedger",
    "new_run_id",
    "load_project",
    "verify_shard_file",
    "verify_project",
    "publish_draft",
    "merge_heads",
    "rebuild_index",
    "resolve_due",
    "score_resolution",
    "rescore_from_shards",
    "export_shard",
    "mct_draft",
    "distribution_rows",
    "anchor_parity",
    "is_contaminated",
    "skill_version_entry",
    "pit_audit_receipt_entry",
    "main",
]

# ---------------------------------------------------------------------------
# Contract vocabulary
# ---------------------------------------------------------------------------

SCHEMA = "zt-forecast-ledger/2"
AMENDMENT = "zt-forecast-ledger/2.1"
#: v2 migration rule: later actions on V1 rows carry legacy_schema and target the v1 line.
LEGACY_V1_SCHEMA = "zt-forecast-ledger/1"
INDEX_SCHEMA = "zt-forecast-ledger/2.1-index"
#: Names the per-line hash: VT src.governance.ledger.compute_record_hash.
HASH_SCHEME = "vt-governance-ledger/sha256-canonical-json/v1"

LEDGER_DIRNAME = "_ledger"
RUNS_DIRNAME = "runs"
INDEX_DIRNAME = "index"
LEASE_NAME = "writer.lease"
RECEIPTS_DIRNAME = "receipts"

CHAIN_FIELDS = frozenset({"seq", "prev_record_hash", "record_hash"})

#: v2 modes plus two additive ones: ``baseline`` (mechanical or market-implied
#: reference forecasts) and ``merge`` (a row-less shard joining forked heads).
MODES = ("survey", "frontier", "scenario", "bayes", "full_mct", "resolution", "baseline", "merge")
PIT_EXEMPT_MODES = ("resolution", "merge")
STATUSES = ("VALID", "INVALID_PIT", "INVALID_SCHEMA", "INCOMPLETE", "RESOLVED")
ROW_KINDS = ("prediction", "probability_band", "indicator", "amend", "resolution", "distribution")
FORECAST_KINDS = ("prediction", "distribution", "indicator")
CLAIM_TYPES = ("binary", "categorical", "continuous", "indicator")
EMITTED_BY = (
    "attention-frontier", "scenario-basis", "montecarlo-outcome-forecasting",
    "bayes-causal-dag", "saturate", "human",
    # v2.1 additive emitters
    "mechanical-baseline", "market-implied", "forecast-resolver",
)
OPERATORS = (">", ">=", "<", "<=", "==", "in_set", "numeric_value")
COMPARISONS = (">", ">=", "<", "<=", "==")
MISSING_POLICIES = ("postpone_to_named_release", "resolve_missing", "void_with_reason")
SCORING_RULES = ("brier_binary", "brier_multiclass", "log_score", "crps", "interval_score",
                 "unscored_band", "unscored_indicator")
RULES_BY_CLAIM = {
    "binary": ("brier_binary", "log_score"),
    "categorical": ("brier_multiclass", "log_score"),
    "continuous": ("crps", "interval_score"),
    "indicator": ("unscored_indicator",),
}
ANCHOR_STATUSES = ("market-anchored", "base-rate-anchored", "elicited-only")
#: Blind-packet contamination status an actor-simulation run manifest carries
#: (ZT add-on 2026-09-29, extensions/pit_actor_sim/contamination.py). Optional
#: and additive: a manifest without it is treated as not contaminated.
CONTAMINATION_STATUSES = ("CONTAMINATED", "NOT_IDENTIFIED", "NOT_PROBED", "NOT_BLIND")
#: Schema of a blind packet rendering; the ledger records the unblinded packet.
BLIND_VIEW_SCHEMA = "vt.actor_packet.blind_view.v1"
PIT_CLASSES = ("TRUE_PIT", "OBSERVED_PIT", "RECONSTRUCTED_PIT", "NON_PIT")
MONITORABILITY = ("READY", "PARTIAL", "MANUAL", "BLOCKED")
OBSERVATION_RULES = ("exact_event_date", "last_on_or_before", "nth_after_origin")
RESOLUTION_STATUSES = ("RESOLVED", "MISSING", "VOID")
ANCHOR_ESTIMAND_KEYS = ("event", "horizon", "unit", "denominator", "run_asof_utc")
#: v2 hard rule incremental-skill-not-raw-hit-rate: display floor.
MIN_INDEPENDENT_EVENTS = 5
#: Central prediction intervals recorded as hit/miss diagnostics when the grid has both ends.
CENTRAL_INTERVALS = (0.5, 0.8, 0.9, 0.98)

_RUN_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,127}$")
_HASH_RE = re.compile(r"^sha256:(genesis|[0-9a-f]{64})$")
_REVISED_ON_RE = re.compile(r"^as_revised_on_(\d{4}-\d{2}-\d{2})$")
_DURATION_RE = re.compile(
    r"^P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LedgerError(RuntimeError):
    """Base class for ledger failures."""


class SchemaError(LedgerError, ValueError):
    """A manifest, row or draft violates the v2/v2.1 contract."""


class LatePredictionError(SchemaError):
    """A forecast would be written at or after its first resolvable instant."""


class ShardCollisionError(LedgerError):
    """A run_id or shard path already exists (a hard failure, never a merge)."""


class ChainError(LedgerError):
    """The project's shard chain does not verify."""


class ForkError(ChainError):
    """The project has more than one head; publish refuses until merged."""


class LeaseHeldError(LedgerError):
    """Another writer holds the project writer lease."""


class ShardInvalid(ChainError):
    """One shard failed verification."""

    def __init__(self, path: Path | str, reason: str, detail: str = "", line: int | None = None):
        where = f" line {line}" if line is not None else ""
        super().__init__(f"{path}{where}: {reason}: {detail}".rstrip(": "))
        self.path = str(path)
        self.reason = reason
        self.detail = detail
        self.line = line


# ---------------------------------------------------------------------------
# Time, ids, hashing
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def parse_utc(value: Any, field: str = "timestamp") -> datetime:
    """Parse an ISO-8601 instant that carries an explicit offset."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SchemaError(f"{field} is not ISO-8601: {value!r}") from exc
    else:
        raise SchemaError(f"{field} must be an ISO-8601 string, got {value!r}")
    if parsed.tzinfo is None:
        raise SchemaError(f"{field} must carry a UTC offset ('Z' or '+00:00'): {value!r}")
    return parsed.astimezone(timezone.utc)


def iso_utc(moment: datetime) -> str:
    """Render an instant as ISO-8601 UTC with a ``Z`` suffix."""
    if moment.tzinfo is None:
        raise SchemaError("naive datetimes are not accepted; attach timezone.utc")
    moment = moment.astimezone(timezone.utc)
    spec = "microseconds" if moment.microsecond else "seconds"
    return moment.isoformat(timespec=spec).replace("+00:00", "Z")


def _naive_utc(value: Any, field: str) -> datetime:
    """Parse a timestamp from the warehouse; naive values are UTC by contract."""
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SchemaError(f"{field} is not ISO-8601: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso_date(value: Any, where: str) -> date:
    """Parse ``YYYY-MM-DD`` or raise :class:`SchemaError`."""
    try:
        return date.fromisoformat(_nonempty_str(value, where))
    except ValueError as exc:
        raise SchemaError(f"{where} is not an ISO date: {value!r}") from exc


def parse_duration(text: Any, field: str = "grace_period") -> timedelta:
    """Parse the ISO-8601 duration subset ``PnW nD TnH nM nS``."""
    match = _DURATION_RE.match(str(text)) if isinstance(text, str) else None
    if not match or text == "P" or text.endswith("T"):
        raise SchemaError(f"{field} must be an ISO-8601 duration like 'P3D' or 'PT36H', got {text!r}")
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return timedelta(weeks=parts["w"], days=parts["d"], hours=parts["h"],
                     minutes=parts["m"], seconds=parts["s"])


def new_run_id(now: datetime | None = None) -> str:
    """Generate a ULID (48-bit millisecond time + 80 random bits, Crockford base32)."""
    moment = now or utc_now()
    value = (int(moment.timestamp() * 1000) << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def sha256_bytes(data: bytes) -> str:
    """``sha256:<hex>`` of raw bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """``sha256:<hex>`` of a file's bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def canonical_json(value: Any) -> str:
    """Deterministic compact JSON (sorted keys, ASCII-safe, finite numbers only)."""
    return json.dumps(_clean(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def sha256_json(value: Any) -> str:
    """``sha256:<hex>`` of :func:`canonical_json`."""
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _clean(value: Any) -> Any:
    """Return ``value`` as JSON-native data with every mapping's keys sorted.

    Refuses non-finite numbers and non-JSON types instead of stringifying them,
    so a hashed payload always round-trips to the same bytes.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        if not math.isfinite(number):
            raise SchemaError(f"non-finite number {value!r} cannot enter the ledger")
        return number
    if isinstance(value, Mapping):
        out = {}
        for key in sorted(value, key=str):
            if not isinstance(key, str):
                raise SchemaError(f"mapping keys must be strings, got {key!r}")
            out[key] = _clean(value[key])
        return out
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    raise SchemaError(f"unsupported value of type {type(value).__name__} in ledger payload")


def _render_line(payload: Mapping[str, Any], seq: int, prev: str, record_hash: str) -> str:
    """One shard line exactly as VT's ``append_record`` would write it."""
    record = {**payload, "seq": seq, "prev_record_hash": prev, "record_hash": record_hash}
    return json.dumps(record, ensure_ascii=False, allow_nan=False)


def chain_payloads(payloads: Sequence[Mapping[str, Any]]) -> tuple[list[str], str]:
    """Hash-chain payloads from genesis; return the lines and the terminal hash."""
    lines: list[str] = []
    prev = GENESIS_PREV_HASH
    for seq, payload in enumerate(payloads, start=1):
        clean = _clean(payload)
        reserved = CHAIN_FIELDS & clean.keys()
        if reserved:
            raise SchemaError(f"payload sets reserved chain fields {sorted(reserved)}")
        record_hash = compute_record_hash(seq, prev, clean)
        lines.append(_render_line(clean, seq, prev, record_hash))
        prev = record_hash
    return lines, prev


# ---------------------------------------------------------------------------
# Storage rule (ZT 2026-09-28: E: runtime, G: canonical, nothing on C:/D:)
# ---------------------------------------------------------------------------


def _windows_drive(path: Any) -> str:
    drive = ntpath.splitdrive(str(path))[0].upper()
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if drive.startswith(prefix):
            drive = drive[len(prefix):]
    return drive


def drive_violation(path: Any, what: str, *, require: str | None = None,
                    os_name: str | None = None, system_drive: str | None = None) -> str | None:
    """Explain why ``path`` breaks the storage rule on Windows, else ``None``.

    Args:
        path: Candidate path.
        what: Human label for messages.
        require: A drive the path must be on (``"E:"`` for scratch).
        os_name: Override of ``os.name`` (tests).
        system_drive: Override of ``%SystemDrive%`` (tests).
    """
    if (os_name or os.name) != "nt":
        return None
    drive = _windows_drive(path)
    system = (system_drive or os.environ.get("SystemDrive") or "C:").upper()
    if drive in ("C:", "D:", system):
        return (f"{what} may not be on C:, D: or the system drive {system} "
                f"(ZT 2026-09-28: E: runtime, G: canonical); got {path}")
    if require and drive != require:
        return f"{what} must be on {require} (ZT 2026-09-28); got {path}"
    return None


def _guard(path: Path, what: str, require: str | None = None) -> Path:
    problem = drive_violation(path, what, require=require)
    if problem:
        raise LedgerError(problem)
    return path


def default_scratch_dir() -> Path:
    """Local scratch root: ``$ZT_LEDGER_SCRATCH`` or ``$VIBE_TRADING_HOME/forecast_ledger``."""
    raw = os.environ.get("ZT_LEDGER_SCRATCH", "").strip()
    if not raw:
        home = os.environ.get("VIBE_TRADING_HOME", "").strip()
        if not home:
            raise LedgerError("set ZT_LEDGER_SCRATCH or VIBE_TRADING_HOME (the E: runtime) for shard scratch")
        raw = str(Path(home) / "forecast_ledger")
    return Path(raw)


def _scratch_root(scratch_dir: Path | str | None, project_dir: Path) -> Path:
    root = Path(scratch_dir) if scratch_dir is not None else default_scratch_dir()
    root = _guard(root.resolve(), "Ledger scratch", require="E:")
    if root == project_dir or project_dir in root.parents:
        raise LedgerError("scratch may not live inside the shared project folder")
    return root


def _project_root(project_dir: Path | str) -> Path:
    root = Path(project_dir).resolve()
    _guard(root, "Ledger project folder")
    if not root.is_dir():
        raise LedgerError(f"project folder does not exist: {root}")
    return root


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise SchemaError(f"{where}: missing required field {key!r}")
    return mapping[key]


def _nonempty_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{where} must be a non-empty string")
    return value


def _number(value: Any, where: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(float(value)):
        raise SchemaError(f"{where} must be a finite number")
    if minimum is not None and float(value) < minimum:
        raise SchemaError(f"{where} must be >= {minimum}")
    return float(value)


def _probability(value: Any, where: str) -> float:
    number = _number(value, where)
    if not 0.0 <= number <= 1.0:
        raise SchemaError(f"{where} must lie in [0, 1]")
    return number


def _timezone_ok(name: str) -> None:
    if name == "UTC":
        return
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 - any failure means unusable
        raise SchemaError(f"timezone {name!r} is not an IANA zone name") from exc


def _bins(spec: Any, where: str) -> dict[str, tuple[float | None, float | None]]:
    """Parse ``{"categories": {name: [lo, hi]}}`` (lo inclusive, hi exclusive, null open)."""
    if not isinstance(spec, Mapping) or not isinstance(spec.get("categories"), Mapping):
        raise SchemaError(f"{where} must be {{'categories': {{name: [lo, hi]}}}}")
    out: dict[str, tuple[float | None, float | None]] = {}
    for name, bounds in spec["categories"].items():
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise SchemaError(f"{where}: bin {name!r} must be [lo, hi]")
        lo, hi = (None if b is None else _number(b, f"{where}.{name}") for b in bounds)
        if lo is not None and hi is not None and not lo < hi:
            raise SchemaError(f"{where}: bin {name!r} needs lo < hi")
        out[str(name)] = (lo, hi)
    if not out:
        raise SchemaError(f"{where}: no categories")
    return out


def _exhaustive_bins(bins: Mapping[str, tuple[float | None, float | None]], where: str) -> None:
    ordered = sorted(bins.items(), key=lambda item: -math.inf if item[1][0] is None else item[1][0])
    if ordered[0][1][0] is not None or ordered[-1][1][1] is not None:
        raise SchemaError(f"{where}: categories must cover the whole line (open lowest and highest bins)")
    for (name, (_, hi)), (nxt, (lo, _)) in zip(ordered, ordered[1:]):
        if hi is None or lo is None or hi != lo:
            raise SchemaError(f"{where}: bins {name!r} and {nxt!r} leave a gap or overlap")


def _in_bin(value: float, bounds: tuple[float | None, float | None]) -> bool:
    lo, hi = bounds
    return (lo is None or value >= lo) and (hi is None or value < hi)


def validate_resolution_spec(spec: Any, claim_type: str, where: str = "resolution_spec") -> dict:
    """Check a resolution spec is complete and mechanical (v2 mechanical-resolution-contract)."""
    if not isinstance(spec, Mapping):
        raise SchemaError(f"{where} must be an object")
    for key in ("resolver", "source_or_series_id", "field", "unit", "timezone"):
        _nonempty_str(_require(spec, key, where), f"{where}.{key}")
    operator = _require(spec, "operator", where)
    if operator not in OPERATORS:
        raise SchemaError(f"{where}.operator must be one of {OPERATORS}")
    target = _require(spec, "threshold_or_categories", where)
    _timezone_ok(spec["timezone"])
    first = parse_utc(_require(spec, "first_resolvable_utc", where), f"{where}.first_resolvable_utc")
    vintage = _nonempty_str(_require(spec, "vintage_policy", where), f"{where}.vintage_policy")
    revised = _REVISED_ON_RE.match(vintage)
    if vintage not in ("first_release", "final_release", "specified_snapshot") and not revised:
        raise SchemaError(f"{where}.vintage_policy must be first_release, final_release, "
                          "specified_snapshot or as_revised_on_<YYYY-MM-DD>")
    if revised:
        _iso_date(revised.group(1), f"{where}.vintage_policy")
    if vintage == "specified_snapshot":
        parse_utc(_require(spec, "snapshot_asof_utc", where), f"{where}.snapshot_asof_utc")
    if _require(spec, "missing_policy", where) not in MISSING_POLICIES:
        raise SchemaError(f"{where}.missing_policy must be one of {MISSING_POLICIES}")
    parse_duration(_require(spec, "grace_period", where), f"{where}.grace_period")

    rule = spec.get("observation_rule", "exact_event_date")
    if rule not in OBSERVATION_RULES:
        raise SchemaError(f"{where}.observation_rule must be one of {OBSERVATION_RULES}")
    if rule in ("exact_event_date", "last_on_or_before"):
        event = _iso_date(_require(spec, "event_date", where), f"{where}.event_date")
        if first.date() < event:
            raise SchemaError(f"{where}: first_resolvable_utc precedes event_date {event}")
    else:
        origin = _iso_date(_require(spec, "origin_event_date", where), f"{where}.origin_event_date")
        horizon = _require(spec, "horizon_trading_days", where)
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
            raise SchemaError(f"{where}.horizon_trading_days must be a positive integer")
        if first.date() <= origin:
            raise SchemaError(f"{where}: first_resolvable_utc must follow origin_event_date")

    if claim_type == "binary":
        if operator in COMPARISONS:
            _number(target, f"{where}.threshold_or_categories")
        elif operator == "in_set":
            if isinstance(target, list):
                if not target or not all(isinstance(t, str) for t in target):
                    raise SchemaError(f"{where}: in_set list must hold category strings")
            else:
                _bins(target, f"{where}.threshold_or_categories")
        else:
            raise SchemaError(f"{where}: a binary claim cannot use operator {operator!r}")
    elif claim_type == "categorical":
        if operator != "in_set":
            raise SchemaError(f"{where}: a categorical claim resolves with operator 'in_set'")
        if isinstance(target, list):
            if len(target) < 2 or not all(isinstance(t, str) for t in target):
                raise SchemaError(f"{where}: categorical in_set needs at least two category strings")
        else:
            _exhaustive_bins(_bins(target, f"{where}.threshold_or_categories"), where)
    elif claim_type == "continuous":
        if operator != "numeric_value":
            raise SchemaError(f"{where}: a continuous claim resolves with operator 'numeric_value'")
    elif claim_type == "indicator":
        if operator not in COMPARISONS:
            raise SchemaError(f"{where}: an indicator trigger uses one of {COMPARISONS}")
        _number(target, f"{where}.threshold_or_categories")
    return dict(spec)


def _quantile_grid(dist: Any, where: str) -> tuple[list[float], list[float]]:
    if not isinstance(dist, Mapping) or dist.get("type") != "quantiles":
        raise SchemaError(f"{where} must be {{'type': 'quantiles', 'levels': [...], 'values': [...]}}")
    levels = [_number(v, f"{where}.levels") for v in _require(dist, "levels", where)]
    values = [_number(v, f"{where}.values") for v in _require(dist, "values", where)]
    if len(levels) < 2 or len(levels) != len(values):
        raise SchemaError(f"{where}: needs at least two levels and one value per level")
    if any(not 0.0 < t < 1.0 for t in levels) or any(b <= a for a, b in zip(levels, levels[1:])):
        raise SchemaError(f"{where}: levels must be strictly increasing inside (0, 1)")
    if any(b < a for a, b in zip(values, values[1:])):
        raise SchemaError(f"{where}: quantile values must be non-decreasing")
    return levels, values


def _categorical_distribution(dist: Any, where: str) -> dict[str, float]:
    if not isinstance(dist, Mapping) or len(dist) < 2:
        raise SchemaError(f"{where} must map at least two categories to probabilities")
    probs = {str(k): _probability(v, f"{where}.{k}") for k, v in dist.items()}
    total = math.fsum(probs.values())
    if abs(total - 1.0) > scoring.SUM_TO_ONE_TOLERANCE:
        raise SchemaError(f"{where} sums to {total!r}; it must be exhaustive and sum to 1")
    return probs


def _category_names(spec: Mapping[str, Any]) -> list[str]:
    target = spec["threshold_or_categories"]
    if isinstance(target, list):
        return [str(t) for t in target]
    return sorted(_bins(target, "threshold_or_categories"))


def _knowledge_times(lineage: Any) -> list[str]:
    if not isinstance(lineage, Mapping):
        return []
    raw = lineage.get("knowledge_time")
    if raw is None:
        return []
    return [raw] if isinstance(raw, str) else [str(item) for item in raw]


def pit_violations(manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Evidence knowable only after the cutoff, or a missing/failed PIT audit."""
    cutoff = parse_utc(manifest["run_asof_utc"], "run_asof_utc")
    found: list[dict] = []
    for row in rows:
        for stamp in _knowledge_times(row.get("evidence_lineage")):
            if _naive_utc(stamp, "knowledge_time") > cutoff:
                found.append({"row_id": row.get("id"), "knowledge_time": stamp,
                              "reason": "evidence knowledge_time after run_asof_utc"})
    if manifest.get("mode") not in PIT_EXEMPT_MODES:
        receipt = manifest.get("pit_audit_receipt")
        if not isinstance(receipt, Mapping) or receipt.get("status") != "PASS":
            found.append({"row_id": None, "reason": "no passing pit_audit_receipt for an investment run"})
    return found


def validate_forecast(row: Mapping[str, Any], where: str) -> None:
    kind, claim_type = row["kind"], row["claim_type"]
    forecast = _require(row, "forecast", where)
    if not isinstance(forecast, Mapping):
        raise SchemaError(f"{where}.forecast must be an object")
    if kind == "indicator":
        return
    anchor = forecast.get("anchor_status")
    if anchor not in ANCHOR_STATUSES:
        raise SchemaError(f"{where}.forecast.anchor_status must be one of {ANCHOR_STATUSES}")
    spec = row["resolution_spec"]
    baseline = forecast.get("baseline_forecast")
    if claim_type == "binary":
        _probability(_require(forecast, "probability", f"{where}.forecast"), f"{where}.forecast.probability")
        if isinstance(baseline, Mapping) and "probability" in baseline:
            _probability(baseline["probability"], f"{where}.forecast.baseline_forecast.probability")
    elif claim_type == "categorical":
        dist = _categorical_distribution(_require(forecast, "categorical_distribution", where),
                                         f"{where}.forecast.categorical_distribution")
        if sorted(dist) != sorted(_category_names(spec)):
            raise SchemaError(f"{where}: forecast categories must equal the resolution categories")
        if isinstance(baseline, Mapping) and "categorical_distribution" in baseline:
            base = _categorical_distribution(baseline["categorical_distribution"], f"{where}.baseline")
            if sorted(base) != sorted(dist):
                raise SchemaError(f"{where}: baseline categories differ from the forecast's")
    elif claim_type == "continuous":
        levels, _ = _quantile_grid(_require(forecast, "numeric_distribution", where),
                                   f"{where}.forecast.numeric_distribution")
        if isinstance(baseline, Mapping) and "quantiles" in baseline:
            _quantile_grid(baseline["quantiles"], f"{where}.forecast.baseline_forecast.quantiles")
        if row["scoring"]["rule"] == "interval_score":
            alpha = _number(_require(row["scoring"], "interval_alpha", where), f"{where}.scoring.interval_alpha")
            for level in (alpha / 2.0, 1.0 - alpha / 2.0):
                if not any(math.isclose(level, t, abs_tol=1e-12) for t in levels):
                    raise SchemaError(f"{where}: interval_score needs level {level} on the grid")


def validate_row(row: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    """Check one row against v2 (+ v2.1) and its run manifest."""
    where = f"row {row.get('id', '?')}"
    for key in ("record_type", "schema", "schema_amendment", "id", "run_id", "kind", "target_id",
                "emitted_by", "emitted_utc", "claim", "claim_type", "decision_link", "resolution_spec",
                "forecast", "scoring", "evidence_lineage", "attribution", "monitor", "dependence_group"):
        _require(row, key, where)
    if row["record_type"] != "row" or row["schema"] != SCHEMA or row["schema_amendment"] != AMENDMENT:
        raise SchemaError(f"{where}: wrong record_type/schema/schema_amendment")
    if row["run_id"] != manifest["run_id"]:
        raise SchemaError(f"{where}: run_id differs from the shard's run manifest")
    kind = row["kind"]
    if kind not in ROW_KINDS:
        raise SchemaError(f"{where}.kind must be one of {ROW_KINDS}")
    if row["emitted_by"] not in EMITTED_BY:
        raise SchemaError(f"{where}.emitted_by must be one of {EMITTED_BY}")
    emitted = parse_utc(row["emitted_utc"], f"{where}.emitted_utc")
    published = parse_utc(manifest["emitted_utc"], "manifest.emitted_utc")
    cutoff = parse_utc(manifest["run_asof_utc"], "manifest.run_asof_utc")
    if emitted > published or emitted < cutoff:
        raise SchemaError(f"{where}: emitted_utc must lie between run_asof_utc and publication")
    _nonempty_str(row["claim"], f"{where}.claim")
    claim_type = row["claim_type"]
    if claim_type not in CLAIM_TYPES:
        raise SchemaError(f"{where}.claim_type must be one of {CLAIM_TYPES}")
    if not isinstance(row["attribution"], Mapping) or "forecast_method_id" not in row["attribution"]:
        raise SchemaError(f"{where}.attribution needs forecast_method_id")
    scoring_block = row["scoring"]
    if not isinstance(scoring_block, Mapping) or scoring_block.get("rule") not in SCORING_RULES:
        raise SchemaError(f"{where}.scoring.rule must be one of {SCORING_RULES}")

    if kind in ("amend", "resolution"):
        _nonempty_str(row["target_id"], f"{where}.target_id")
    if kind == "resolution":
        if not _HASH_RE.match(str(row.get("target_record_hash", ""))):
            raise SchemaError(f"{where}.target_record_hash must be sha256:<hex>")
        resolution = _require(row, "resolution", where)
        if not isinstance(resolution, Mapping) or resolution.get("status") not in RESOLUTION_STATUSES:
            raise SchemaError(f"{where}.resolution.status must be one of {RESOLUTION_STATUSES}")
        for key in ("resolved_value", "raw_score", "baseline_score", "skill_score", "date_scored_utc"):
            _require(scoring_block, key, f"{where}.scoring")
        return
    if kind not in FORECAST_KINDS:
        return  # probability_band / amend rows are accepted for v2 compatibility, not built here.

    if row["decision_link"] in (None, "", {}):
        raise SchemaError(f"{where}.decision_link is required for a forecast row")
    if row["target_id"] is not None:
        raise SchemaError(f"{where}: a {kind} row does not target another row")
    expected = {"prediction": ("binary", "categorical", "continuous"), "distribution": ("continuous",),
                "indicator": ("indicator",)}[kind]
    if claim_type not in expected:
        raise SchemaError(f"{where}: a {kind} row must have claim_type in {expected}")
    if scoring_block["rule"] not in RULES_BY_CLAIM[claim_type]:
        raise SchemaError(f"{where}: rule {scoring_block['rule']!r} is not valid for a {claim_type} claim "
                          "(v2 proper-score-matches-claim-type)")
    spec = validate_resolution_spec(row["resolution_spec"], claim_type, f"{where}.resolution_spec")
    first = parse_utc(spec["first_resolvable_utc"], "first_resolvable_utc")
    latest_write = max(emitted, published, cutoff)
    if latest_write >= first:
        raise LatePredictionError(
            f"{where}: written at {iso_utc(latest_write)}, at or after its first resolvable instant "
            f"{spec['first_resolvable_utc']}; a forecast cannot follow its own resolution date")
    if kind == "distribution" and spec.get("observation_rule") == "nth_after_origin":
        dist = row["forecast"].get("numeric_distribution") or {}
        if dist.get("horizon_trading_days") != spec["horizon_trading_days"]:
            raise SchemaError(f"{where}: numeric_distribution.horizon_trading_days must match the spec")
    if kind in ("prediction", "distribution"):
        _nonempty_str(row["dependence_group"], f"{where}.dependence_group")
    if kind == "indicator":
        monitor = row["monitor"]
        if not isinstance(monitor, Mapping) or monitor.get("monitorability") not in MONITORABILITY:
            raise SchemaError(f"{where}.monitor.monitorability must be one of {MONITORABILITY}")
        if monitor["monitorability"] == "READY":
            for key in ("series_or_query_id", "cadence", "owner"):
                _nonempty_str(monitor.get(key), f"{where}.monitor.{key}")
            if monitor.get("trigger") in (None, "", {}, []):
                raise SchemaError(f"{where}.monitor.trigger is required for a READY indicator")
        else:
            _nonempty_str(monitor.get("unlocking_action"), f"{where}.monitor.unlocking_action")
        if monitor["monitorability"] == "MANUAL":
            _nonempty_str(monitor.get("owner"), f"{where}.monitor.owner")
    validate_forecast(row, where)


def is_contaminated(manifest: Mapping[str, Any]) -> bool:
    """Whether a run manifest records a CONTAMINATED blind run (absent means no)."""
    block = manifest.get("contamination")
    return isinstance(block, Mapping) and block.get("status") == "CONTAMINATED"


def _validate_contamination(block: Any, where: str) -> None:
    if block is None:
        return
    if not isinstance(block, Mapping) or block.get("status") not in CONTAMINATION_STATUSES:
        raise SchemaError(f"{where}.contamination.status must be one of {CONTAMINATION_STATUSES}")
    if block.get("skill_eligible") is not (block["status"] != "CONTAMINATED"):
        raise SchemaError(f"{where}.contamination.skill_eligible must be false exactly when the run is "
                          "CONTAMINATED")


def validate_manifest(manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    """Check a run manifest (line 1) against v2 (+ v2.1) and its rows."""
    where = "run_manifest"
    for key in ("record_type", "schema", "schema_amendment", "run_id", "project", "decision_id", "mode",
                "run_asof_utc", "emitted_utc", "source_lattice_id", "source_snapshot_id",
                "pit_audit_receipt", "skill_versions", "engine_stamp", "action_set_hash",
                "attention_minutes", "compute_seconds", "estimated_cost_usd", "status",
                "pit_violations", "integrity"):
        _require(manifest, key, where)
    if (manifest["record_type"] != "run_manifest" or manifest["schema"] != SCHEMA
            or manifest["schema_amendment"] != AMENDMENT):
        raise SchemaError(f"{where}: wrong record_type/schema/schema_amendment")
    if not _RUN_ID_RE.match(str(manifest["run_id"])):
        raise SchemaError(f"{where}.run_id {manifest['run_id']!r} is not a safe collision-resistant id")
    _nonempty_str(manifest["project"], f"{where}.project")
    _nonempty_str(manifest["decision_id"], f"{where}.decision_id")
    if manifest["mode"] not in MODES:
        raise SchemaError(f"{where}.mode must be one of {MODES}")
    if manifest["status"] not in STATUSES:
        raise SchemaError(f"{where}.status must be one of {STATUSES}")
    asof = parse_utc(manifest["run_asof_utc"], f"{where}.run_asof_utc")
    emitted = parse_utc(manifest["emitted_utc"], f"{where}.emitted_utc")
    if asof > emitted:
        raise SchemaError(f"{where}: run_asof_utc is after emitted_utc")
    for key in ("attention_minutes", "compute_seconds", "estimated_cost_usd"):
        _number(manifest[key], f"{where}.{key}", minimum=0.0)
    receipt = manifest["pit_audit_receipt"]
    if receipt is not None and not (isinstance(receipt, Mapping) and _HASH_RE.match(str(receipt.get("sha256", "")))
                                    and receipt.get("path")):
        raise SchemaError(f"{where}.pit_audit_receipt must be null or {{path, sha256, status}}")
    for entry in manifest["skill_versions"]:
        if not isinstance(entry, Mapping) or not entry.get("path") or not _HASH_RE.match(str(entry.get("sha256", ""))):
            raise SchemaError(f"{where}.skill_versions entries need path and sha256")
    integrity = manifest["integrity"]
    if not isinstance(integrity, Mapping) or integrity.get("hash_scheme") != HASH_SCHEME:
        raise SchemaError(f"{where}.integrity.hash_scheme must be {HASH_SCHEME!r}")
    seq = integrity.get("shard_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise SchemaError(f"{where}.integrity.shard_seq must be a positive integer")
    if not _HASH_RE.match(str(integrity.get("prev_shard_hash", ""))):
        raise SchemaError(f"{where}.integrity.prev_shard_hash must be sha256:<hex> or sha256:genesis")
    parents = integrity.get("merge_parent_hashes")
    if parents is not None:
        if (manifest["mode"] != "merge" or not isinstance(parents, list) or len(parents) < 2
                or any(not _HASH_RE.match(str(p)) for p in parents) or parents != sorted(set(parents))
                or integrity["prev_shard_hash"] not in parents):
            raise SchemaError(f"{where}.integrity.merge_parent_hashes must be a sorted list of >= 2 "
                              "distinct hashes including prev_shard_hash, on a merge shard")
    elif manifest["mode"] == "merge":
        raise SchemaError(f"{where}: a merge shard needs integrity.merge_parent_hashes")
    if manifest["mode"] == "merge" and rows:
        raise SchemaError(f"{where}: a merge shard carries no rows")
    if integrity.get("shard_record_count") != 1 + len(rows):
        raise SchemaError(f"{where}.integrity.shard_record_count must equal 1 + number of rows")
    recomputed = pit_violations(manifest, rows)
    if recomputed and manifest["status"] != "INVALID_PIT":
        raise SchemaError(f"{where}: PIT violations present but status is {manifest['status']!r}")
    if _clean(manifest["pit_violations"]) != _clean(recomputed):
        raise SchemaError(f"{where}.pit_violations does not match the recomputed violations")
    _validate_contamination(manifest.get("contamination"), where)


# ---------------------------------------------------------------------------
# Shards and the project chain
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Shard:
    """One verified shard."""

    path: Path
    run_id: str
    shard_seq: int
    prev_shard_hash: str
    merge_parent_hashes: tuple[str, ...]
    terminal_hash: str
    file_sha256: str
    manifest: dict
    rows: tuple[dict, ...]
    row_hashes: tuple[str, ...]

    @property
    def parents(self) -> tuple[str, ...]:
        """Terminal hashes this shard links back to."""
        return self.merge_parent_hashes or (self.prev_shard_hash,)


def verify_shard_file(path: Path | str) -> Shard:
    """Verify one shard: VT chain, byte-canonical lines, structure and schema.

    Raises:
        ShardInvalid: On the first problem found.
    """
    path = Path(path)
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShardInvalid(path, "encoding", str(exc)) from exc
    try:
        chain = verify_chain(path)
    except Exception as exc:  # noqa: BLE001 - e.g. a line that is JSON but not an object
        raise ShardInvalid(path, "chain_unreadable", f"{type(exc).__name__}: {exc}") from exc
    if not chain.ok:
        brk = chain.first_break
        raise ShardInvalid(path, "chain_break", f"{brk.reason}: {brk.detail}", line=brk.index + 1)
    if not text.endswith("\n"):
        raise ShardInvalid(path, "unterminated_final_line")
    if "\r" in text:
        raise ShardInvalid(path, "carriage_return")
    raw_lines = text[:-1].split("\n")
    if chain.record_count != len(raw_lines) or any(not line for line in raw_lines):
        raise ShardInvalid(path, "blank_or_unchained_line")
    payloads: list[dict] = []
    hashes: list[str] = []
    for number, line in enumerate(raw_lines, start=1):
        record = json.loads(line)
        payload = {k: v for k, v in record.items() if k not in CHAIN_FIELDS}
        try:
            expected = _render_line(_clean(payload), record["seq"], record["prev_record_hash"],
                                    record["record_hash"])
        except SchemaError as exc:
            raise ShardInvalid(path, "non_canonical_value", str(exc), line=number) from exc
        if expected != line:
            raise ShardInvalid(path, "non_canonical_line", "bytes differ from the canonical serialization",
                               line=number)
        payloads.append(payload)
        hashes.append(record["record_hash"])
    manifest, rows = payloads[0], payloads[1:]
    if manifest.get("record_type") != "run_manifest":
        raise ShardInvalid(path, "first_line_not_run_manifest", line=1)
    try:
        run_id = manifest["run_id"]
        for index, row in enumerate(rows, start=1):
            if row.get("record_type") != "row" or row.get("id") != f"{run_id}-{index}":
                raise SchemaError(f"row {index} must be record_type 'row' with id {run_id}-{index}")
        validate_manifest(manifest, rows)
        for row in rows:
            validate_row(row, manifest)
    except SchemaError as exc:
        raise ShardInvalid(path, "schema", str(exc)) from exc
    if path.stem != run_id:
        raise ShardInvalid(path, "filename_run_id_mismatch", f"manifest run_id is {run_id}")
    integrity = manifest["integrity"]
    return Shard(
        path=path,
        run_id=run_id,
        shard_seq=integrity["shard_seq"],
        prev_shard_hash=integrity["prev_shard_hash"],
        merge_parent_hashes=tuple(integrity.get("merge_parent_hashes") or ()),
        terminal_hash=hashes[-1],
        file_sha256=sha256_bytes(data),
        manifest=manifest,
        rows=tuple(rows),
        row_hashes=tuple(hashes[1:]),
    )


@dataclasses.dataclass
class ProjectLedger:
    """All verified shards of one project, in deterministic topological order."""

    project_dir: Path
    shards: list[Shard]
    heads: list[Shard]

    @property
    def fork(self) -> bool:
        """``True`` when more than one shard has no successor."""
        return len(self.heads) > 1

    def rows(self) -> Iterator[tuple[Shard, dict, str]]:
        """Yield ``(shard, row, record_hash)`` in ledger order."""
        for shard in self.shards:
            for row, record_hash in zip(shard.rows, shard.row_hashes):
                yield shard, row, record_hash


def _runs_dir(project_dir: Path) -> Path:
    return project_dir / LEDGER_DIRNAME / RUNS_DIRNAME


def load_project(project_dir: Path | str) -> ProjectLedger:
    """Verify every shard and the links between them (fails closed).

    Raises:
        ShardInvalid: A shard does not verify.
        ChainError: Links are broken (a missing shard, a second genesis, a bad
            ``shard_seq`` or a duplicated run_id).
    """
    project_dir = Path(project_dir)
    runs = _runs_dir(project_dir)
    shards = [verify_shard_file(p) for p in sorted(runs.glob("*.jsonl"))] if runs.is_dir() else []
    by_hash: dict[str, Shard] = {}
    for shard in shards:
        if shard.terminal_hash in by_hash:
            raise ChainError(f"two shards share terminal hash {shard.terminal_hash}")
        by_hash[shard.terminal_hash] = shard
    run_ids = [s.run_id for s in shards]
    if len(run_ids) != len(set(run_ids)):
        raise ChainError("duplicated run_id across shards")
    genesis = [s for s in shards if s.parents == (GENESIS_PREV_HASH,)]
    if shards and len(genesis) != 1:
        raise ChainError(f"expected exactly one genesis shard, found {len(genesis)}")
    for shard in shards:
        if shard.parents == (GENESIS_PREV_HASH,):
            if shard.shard_seq != 1:
                raise ChainError(f"{shard.path.name}: genesis shard must have shard_seq 1")
            continue
        missing = [p for p in shard.parents if p not in by_hash]
        if missing:
            raise ChainError(f"{shard.path.name}: links to missing shard(s) {missing} (deleted or not synced)")
        expected = 1 + max(by_hash[p].shard_seq for p in shard.parents)
        if shard.shard_seq != expected:
            raise ChainError(f"{shard.path.name}: shard_seq {shard.shard_seq}, expected {expected}")
    referenced = {p for s in shards for p in s.parents}
    ordered = sorted(shards, key=lambda s: (s.shard_seq, s.terminal_hash))
    heads = [s for s in ordered if s.terminal_hash not in referenced]
    return ProjectLedger(project_dir=project_dir, shards=ordered, heads=heads)


def verify_project(project_dir: Path | str, receipts_dir: Path | str | None = None) -> dict:
    """Verify a project's ledger; optionally cross-check publication receipts.

    Receipts (VT export envelopes written at publication on E:) are what
    detect a deleted *last* shard, which the in-project chain alone cannot.
    """
    ledger = load_project(project_dir)
    report = {
        "ok": True,
        "shards": len(ledger.shards),
        "rows": sum(len(s.rows) for s in ledger.shards),
        "heads": [s.terminal_hash for s in ledger.heads],
        "fork": ledger.fork,
        "receipts_checked": 0,
        "problems": [],
    }
    if receipts_dir is not None:
        by_run = {s.run_id: s for s in ledger.shards}
        for receipt in sorted(Path(receipts_dir).glob("*.export.json")):
            report["receipts_checked"] += 1
            export = json.loads(receipt.read_text(encoding="utf-8"))
            result = verify_export(export)
            records = export.get("records") or []
            run_id = records[0].get("run_id") if records else None
            shard = by_run.get(run_id)
            if not result.ok:
                report["problems"].append({"receipt": receipt.name, "problem": "receipt does not verify"})
            elif shard is None:
                report["problems"].append({"receipt": receipt.name, "problem": f"shard {run_id} missing from project"})
            elif records[-1].get("record_hash") != shard.terminal_hash:
                report["problems"].append({"receipt": receipt.name, "problem": "shard differs from its receipt"})
    report["ok"] = not report["problems"]
    return report


# ---------------------------------------------------------------------------
# Writer lease and exclusive publication
# ---------------------------------------------------------------------------


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _exclusive_write(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively and write ``data`` durably (FileExistsError if present)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        try:
            os.fsync(fd)
        except OSError:
            pass  # e.g. a streamed Drive volume; the read-back check still guards the bytes.
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _read_lease(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def writer_lease(project_dir: Path, owner: str, *, ttl_seconds: int = 900,
                 now: Callable[[], datetime] = utc_now, break_stale: bool = False) -> Iterator[str]:
    """Hold the project writer lease for the duration of the block.

    The lease is an exclusively created file. A stale lease (past its expiry)
    is broken only when ``break_stale`` is set, so a crashed writer is taken
    over by an explicit operator decision rather than silently.
    """
    ledger_dir = project_dir / LEDGER_DIRNAME
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / LEASE_NAME
    token = secrets.token_hex(16)
    acquired = now()
    body = {"owner": owner, "host": socket.gethostname(), "pid": os.getpid(), "token": token,
            "acquired_utc": iso_utc(acquired), "expires_utc": iso_utc(acquired + timedelta(seconds=ttl_seconds))}
    data = json.dumps(body, sort_keys=True).encode("utf-8")
    try:
        _exclusive_write(path, data)
    except FileExistsError:
        held = _read_lease(path)
        expired = held is None or parse_utc(held.get("expires_utc", "1970-01-01T00:00:00Z")) < now()
        if not (expired and break_stale):
            state = "expired" if expired else "active"
            raise LeaseHeldError(
                f"{state} writer lease held by {held and held.get('owner')}@{held and held.get('host')} "
                f"until {held and held.get('expires_utc')}; retry later"
                + (" or rerun with --break-stale-lease" if expired else "")) from None
        stale = path.with_name(f"{LEASE_NAME}.stale-{token}")
        os.replace(path, stale)
        _exclusive_write(path, data)
        with contextlib.suppress(OSError):
            stale.unlink()
    try:
        yield token
    finally:
        current = _read_lease(path)
        if current and current.get("token") == token:
            with contextlib.suppress(OSError):
                path.unlink()


# ---------------------------------------------------------------------------
# Drafts -> shards
# ---------------------------------------------------------------------------

_MANIFEST_DEFAULTS: dict[str, Any] = {
    "source_lattice_id": None,
    "source_snapshot_id": None,
    "pit_audit_receipt": None,
    "skill_versions": [],
    "preset_versions": [],
    "engine_stamp": {},
    "action_set_hash": None,
    "status": "VALID",
    "lake_signature_sha256": None,
    "ontology_version": None,
    "graph_asof_utc": None,
    "model_config": None,
    "vt_run_manifest": None,
    "extensions": {},
}

_ROW_DEFAULTS: dict[str, Any] = {
    "target_id": None,
    "decision_link": None,
    "resolution_spec": None,
    "forecast": None,
    "scoring": None,
    "evidence_lineage": None,
    "attribution": None,
    "monitor": None,
    "dependence_group": None,
}


def _prepare(draft_manifest: Mapping[str, Any], draft_rows: Sequence[Mapping[str, Any]], *,
             run_id: str, published: datetime, integrity: Mapping[str, Any]) -> tuple[dict, list[dict]]:
    """Fill contract fields, stamp times and ids, and compute the PIT status."""
    manifest = {**_MANIFEST_DEFAULTS, **dict(draft_manifest)}
    for reserved in ("record_type", "schema", "schema_amendment", "emitted_utc", "integrity", "pit_violations"):
        if reserved in draft_manifest:
            raise SchemaError(f"draft run_manifest may not set {reserved!r}; the ledger stamps it")
    manifest.update(record_type="run_manifest", schema=SCHEMA, schema_amendment=AMENDMENT,
                    run_id=run_id, emitted_utc=iso_utc(published), integrity=dict(integrity))
    for key in ("project", "decision_id", "mode", "run_asof_utc", "attention_minutes",
                "compute_seconds", "estimated_cost_usd"):
        _require(manifest, key, "draft run_manifest")
    rows = []
    for index, draft in enumerate(draft_rows, start=1):
        for reserved in ("record_type", "schema", "schema_amendment", "id", "run_id"):
            if reserved in draft:
                raise SchemaError(f"draft row {index} may not set {reserved!r}; the ledger stamps it")
        row = {**_ROW_DEFAULTS, **dict(draft)}
        row.update(record_type="row", schema=SCHEMA, schema_amendment=AMENDMENT,
                   id=f"{run_id}-{index}", run_id=run_id)
        row.setdefault("emitted_utc", iso_utc(published))
        rows.append(_clean(row))
    manifest = _clean(manifest)
    violations = pit_violations(manifest, rows)
    manifest["pit_violations"] = violations
    if violations:
        manifest["status"] = "INVALID_PIT"
    manifest = _clean(manifest)
    manifest["integrity"]["shard_record_count"] = 1 + len(rows)
    validate_manifest(manifest, rows)
    for row in rows:
        validate_row(row, manifest)
    return manifest, rows


def _single_head(ledger: ProjectLedger) -> tuple[int, str]:
    if ledger.fork:
        raise ForkError(f"project ledger has {len(ledger.heads)} heads "
                        f"({[h.run_id for h in ledger.heads]}); run merge-heads before publishing")
    if not ledger.heads:
        return 0, GENESIS_PREV_HASH
    return ledger.heads[0].shard_seq, ledger.heads[0].terminal_hash


def _publish(project_dir: Path, draft_manifest: Mapping[str, Any], draft_rows: Sequence[Mapping[str, Any]], *,
             scratch_dir: Path | str | None, owner: str, now: Callable[[], datetime],
             break_stale_lease: bool, merge: bool, index: bool) -> dict:
    project_dir = _project_root(project_dir)
    scratch = _scratch_root(scratch_dir, project_dir)
    run_id = str(draft_manifest.get("run_id") or new_run_id(now()))
    if not _RUN_ID_RE.match(run_id):
        raise SchemaError(f"run_id {run_id!r} is not a safe collision-resistant id")
    runs = _runs_dir(project_dir)
    final = runs / f"{run_id}.jsonl"
    if final.exists():
        raise ShardCollisionError(f"run_id {run_id} already published at {final}")
    manifest_draft = {k: v for k, v in draft_manifest.items() if k != "run_id"}
    with writer_lease(project_dir, owner, now=now, break_stale=break_stale_lease):
        ledger = load_project(project_dir)
        if any(s.run_id == run_id for s in ledger.shards):
            raise ShardCollisionError(f"run_id {run_id} already present in the project chain")
        if merge:
            if not ledger.fork:
                raise LedgerError("nothing to merge: the project has a single head")
            parents = sorted(h.terminal_hash for h in ledger.heads)
            first = min(ledger.heads, key=lambda h: (h.shard_seq, h.terminal_hash))
            integrity = {"hash_scheme": HASH_SCHEME, "shard_seq": 1 + max(h.shard_seq for h in ledger.heads),
                         "prev_shard_hash": first.terminal_hash, "merge_parent_hashes": parents}
        else:
            head_seq, head_hash = _single_head(ledger)
            integrity = {"hash_scheme": HASH_SCHEME, "shard_seq": head_seq + 1, "prev_shard_hash": head_hash}
        published = now()
        manifest, rows = _prepare(manifest_draft, draft_rows, run_id=run_id, published=published,
                                  integrity=integrity)
        lines, terminal = chain_payloads([manifest, *rows])
        data = ("\n".join(lines) + "\n").encode("utf-8")

        work = scratch / project_dir.name
        work.mkdir(parents=True, exist_ok=True)
        staged = work / f"{run_id}.jsonl"
        try:
            _exclusive_write(staged, data)
        except FileExistsError as exc:
            raise ShardCollisionError(f"scratch shard {staged} already exists") from exc
        verified = verify_shard_file(staged)
        if verified.terminal_hash != terminal:
            raise ChainError("scratch shard terminal hash differs from the built chain")
        export = build_export(staged)
        export_check = verify_export(export)
        if not export_check.ok:
            raise ChainError(f"VT export of the scratch shard does not verify: {export_check.to_dict()}")
        receipts = scratch / RECEIPTS_DIRNAME / project_dir.name
        receipts.mkdir(parents=True, exist_ok=True)
        receipt_path = receipts / f"{run_id}.export.json"
        _exclusive_write(receipt_path, json.dumps(export, sort_keys=True, ensure_ascii=False, indent=2)
                         .encode("utf-8"))

        runs.mkdir(parents=True, exist_ok=True)
        try:
            _exclusive_write(final, data)
        except FileExistsError as exc:
            raise ShardCollisionError(f"shard path {final} appeared during publication") from exc
        if sha256_file(final) != sha256_bytes(data):
            raise ChainError(f"published bytes at {final} differ from the verified scratch shard")
        index_receipt = None
        if index:
            # The shard is published and authoritative; the index is a disposable
            # view, so a failed rebuild is reported, not raised as a failed publish.
            try:
                index_receipt = rebuild_index(project_dir)
            except OSError as exc:
                index_receipt = {"error": f"{type(exc).__name__}: {exc}",
                                 "action": "run forecast_ledger.py rebuild-index"}
    return {
        "run_id": run_id,
        "path": str(final),
        "file_sha256": sha256_bytes(data),
        "terminal_hash": terminal,
        "shard_seq": integrity["shard_seq"],
        "prev_shard_hash": integrity["prev_shard_hash"],
        "records": len(lines),
        "status": manifest["status"],
        "pit_violations": manifest["pit_violations"],
        "scratch_path": str(staged),
        "export_receipt": str(receipt_path),
        "index": index_receipt,
    }


def publish_draft(project_dir: Path | str, draft: Mapping[str, Any], *, scratch_dir: Path | str | None = None,
                  owner: str = "forecast_ledger", now: Callable[[], datetime] = utc_now,
                  break_stale_lease: bool = False, index: bool = True) -> dict:
    """Validate a draft ``{"run_manifest": {...}, "rows": [...]}`` and publish it once.

    The ledger stamps schema fields, ids, ``emitted_utc`` (the real publication
    clock), the shard link and the PIT status. Returns a publication receipt.
    """
    if not isinstance(draft, Mapping) or not isinstance(draft.get("run_manifest"), Mapping):
        raise SchemaError("draft must be {'run_manifest': {...}, 'rows': [...]}")
    rows = draft.get("rows") or []
    if draft["run_manifest"].get("mode") == "merge":
        raise SchemaError("use merge_heads for merge shards")
    return _publish(Path(project_dir), draft["run_manifest"], rows, scratch_dir=scratch_dir, owner=owner,
                    now=now, break_stale_lease=break_stale_lease, merge=False, index=index)


def merge_heads(project_dir: Path | str, *, scratch_dir: Path | str | None = None,
                owner: str = "forecast_ledger", now: Callable[[], datetime] = utc_now,
                break_stale_lease: bool = False) -> dict:
    """Publish a row-less merge shard whose parents are every current head."""
    moment = now()
    manifest = {"project": Path(project_dir).resolve().name, "decision_id": "ledger-maintenance:merge-heads",
                "mode": "merge", "run_asof_utc": iso_utc(moment), "attention_minutes": 0,
                "compute_seconds": 0, "estimated_cost_usd": 0,
                "engine_stamp": {"engine": "forecast_ledger.merge_heads"}}
    return _publish(Path(project_dir), manifest, [], scratch_dir=scratch_dir, owner=owner, now=now,
                    break_stale_lease=break_stale_lease, merge=True, index=True)


# ---------------------------------------------------------------------------
# Deterministic index
# ---------------------------------------------------------------------------

INDEX_FILES = ("shards.jsonl", "rows.jsonl", "open.jsonl", "monitor_queue.jsonl", "resolutions.jsonl",
               "scoreboard.json", "INDEX_MANIFEST.json")


def _jsonl(items: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(_clean(item), sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False) + "\n" for item in items).encode("utf-8")


def _json(item: Mapping[str, Any]) -> bytes:
    return (json.dumps(_clean(item), sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n").encode("utf-8")


def _resolution_map(ledger: ProjectLedger) -> tuple[dict[str, dict], list[str]]:
    """First resolution per target (ledger order) and ids of duplicate resolutions."""
    first: dict[str, dict] = {}
    duplicates: list[str] = []
    for _, row, _ in ledger.rows():
        if row["kind"] == "resolution":
            if row["target_id"] in first:
                duplicates.append(row["id"])
            else:
                first[row["target_id"]] = row
    return first, duplicates


def _scoreboard(ledger: ProjectLedger, resolutions: Mapping[str, dict]) -> dict:
    """Aggregate stored scores per method; only VALID, uncontaminated runs count toward skill."""
    targets = {row["id"]: (row, shard.manifest["status"], is_contaminated(shard.manifest))
               for shard, row, _ in ledger.rows()}
    groups: dict[tuple, dict] = {}
    for target_id, res in resolutions.items():
        if target_id not in targets:
            continue
        target, run_status, contaminated = targets[target_id]
        key = (target["emitted_by"], str(target["attribution"].get("forecast_method_id")),
               target["claim_type"], target["scoring"]["rule"])
        group = groups.setdefault(key, {"raw": [], "base_pairs": [], "deps": set(), "missing": 0,
                                        "void": 0, "unscored": 0, "excluded": 0, "contaminated": 0})
        status = res["resolution"]["status"]
        if run_status not in ("VALID", "RESOLVED"):
            # An INVALID_PIT (or incomplete) run produces no conclusion, so its
            # forecasts never enter a skill estimate; they stay visible here.
            group["excluded"] += 1
            continue
        if contaminated:
            # A blind run whose packet was recognised (contamination probe or a
            # fork naming the sealed identity) is recorded, never scored as skill.
            group["contaminated"] += 1
            continue
        if status == "MISSING":
            group["missing"] += 1
            continue
        if status == "VOID":
            group["void"] += 1
            continue
        raw = res["scoring"]["raw_score"]
        if raw is None:
            group["unscored"] += 1
            continue
        group["raw"].append(float(raw))
        group["deps"].add(target["dependence_group"] or target["id"])
        if res["scoring"]["baseline_score"] is not None:
            group["base_pairs"].append((float(raw), float(res["scoring"]["baseline_score"])))
    out = []
    for key in sorted(groups):
        group = groups[key]
        n = len(group["raw"])
        pairs = group["base_pairs"]
        mean_base = math.fsum(b for _, b in pairs) / len(pairs) if pairs else None
        mean_raw_paired = math.fsum(r for r, _ in pairs) / len(pairs) if pairs else None
        skill = None
        if mean_base not in (None, 0.0):
            skill = 1.0 - mean_raw_paired / mean_base
        independent = len(group["deps"])
        out.append({
            "emitted_by": key[0], "forecast_method_id": key[1], "claim_type": key[2], "rule": key[3],
            "n_scored": n, "n_independent": independent, "n_missing": group["missing"], "n_void": group["void"],
            "n_resolved_unscored": group["unscored"], "n_excluded_run_status": group["excluded"],
            "n_excluded_contaminated": group["contaminated"],
            "mean_raw_score": math.fsum(group["raw"]) / n if n else None,
            "n_with_baseline": len(pairs), "mean_baseline_score": mean_base,
            "mean_raw_score_on_baseline_events": mean_raw_paired, "aggregate_skill": skill,
            "display": "SCORED" if independent >= MIN_INDEPENDENT_EVENTS else "UNSCORED_LT5_INDEPENDENT",
        })
    return {"schema": INDEX_SCHEMA, "min_independent_events": MIN_INDEPENDENT_EVENTS, "groups": out}


def build_index(ledger: ProjectLedger) -> dict[str, bytes]:
    """Render every index file from verified shards alone (no clock, no environment)."""
    resolutions, duplicates = _resolution_map(ledger)
    shard_lines, row_lines, open_lines, monitor_lines, resolution_lines = [], [], [], [], []
    for shard in ledger.shards:
        m = shard.manifest
        shard_lines.append({
            "run_id": shard.run_id, "file": f"{RUNS_DIRNAME}/{shard.path.name}", "file_sha256": shard.file_sha256,
            "terminal_hash": shard.terminal_hash, "shard_seq": shard.shard_seq, "parents": list(shard.parents),
            "mode": m["mode"], "status": m["status"], "project": m["project"], "decision_id": m["decision_id"],
            "contamination": (m.get("contamination") or {}).get("status"),
            "run_asof_utc": m["run_asof_utc"], "emitted_utc": m["emitted_utc"], "rows": len(shard.rows),
        })
    for shard, row, record_hash in ledger.rows():
        spec = row.get("resolution_spec") or {}
        state = "RESOLUTION"
        if row["kind"] in FORECAST_KINDS:
            res = resolutions.get(row["id"])
            state = "OPEN" if res is None else res["resolution"]["status"]
        row_lines.append({
            "id": row["id"], "run_id": row["run_id"], "kind": row["kind"], "claim_type": row["claim_type"],
            "emitted_by": row["emitted_by"], "target_id": row["target_id"], "record_hash": record_hash,
            "first_resolvable_utc": spec.get("first_resolvable_utc"), "state": state,
            "run_status": shard.manifest["status"], "run_contaminated": is_contaminated(shard.manifest),
        })
        if row["kind"] in FORECAST_KINDS and state == "OPEN":
            open_lines.append({"id": row["id"], "kind": row["kind"], "first_resolvable_utc": spec["first_resolvable_utc"],
                               "source_or_series_id": spec["source_or_series_id"], "run_status": shard.manifest["status"]})
            monitor = row.get("monitor") or {}
            if row["kind"] == "indicator" and (monitor.get("monitorability") == "READY"
                                               or (monitor.get("monitorability") == "MANUAL" and monitor.get("owner"))):
                monitor_lines.append({"id": row["id"], "monitorability": monitor["monitorability"],
                                      "series_or_query_id": monitor.get("series_or_query_id"),
                                      "trigger": monitor.get("trigger"), "cadence": monitor.get("cadence"),
                                      "owner": monitor.get("owner")})
        if row["kind"] == "resolution":
            sc = row["scoring"]
            resolution_lines.append({
                "id": row["id"], "target_id": row["target_id"], "status": row["resolution"]["status"],
                "rule": sc["rule"], "outcome": sc.get("outcome"), "resolved_value": sc["resolved_value"],
                "raw_score": sc["raw_score"], "baseline_score": sc["baseline_score"],
                "skill_score": sc["skill_score"], "dependence_group": row["dependence_group"],
                "authoritative": row["id"] not in duplicates,
            })
    open_lines.sort(key=lambda item: (item["first_resolvable_utc"], item["id"]))
    files = {
        "shards.jsonl": _jsonl(shard_lines),
        "rows.jsonl": _jsonl(row_lines),
        "open.jsonl": _jsonl(open_lines),
        "monitor_queue.jsonl": _jsonl(monitor_lines),
        "resolutions.jsonl": _jsonl(resolution_lines),
        "scoreboard.json": _json(_scoreboard(ledger, resolutions)),
    }
    files["INDEX_MANIFEST.json"] = _json({
        "schema": INDEX_SCHEMA,
        "ledger_schema": [SCHEMA, AMENDMENT],
        "shard_count": len(ledger.shards),
        "row_count": len(row_lines),
        "heads": [h.terminal_hash for h in ledger.heads],
        "fork": ledger.fork,
        "duplicate_resolutions": duplicates,
        "files": {name: sha256_bytes(data) for name, data in sorted(files.items())},
    })
    return files


def rebuild_index(project_dir: Path | str) -> dict:
    """Regenerate ``_ledger/index/`` from the shards (byte-identical on rebuild)."""
    project_dir = Path(project_dir)
    ledger = load_project(project_dir)
    files = build_index(ledger)
    target = project_dir / LEDGER_DIRNAME / INDEX_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    for stale in target.glob(".*.tmp"):  # left by an interrupted rebuild
        with contextlib.suppress(OSError):
            stale.unlink()
    for name, data in files.items():
        path = target / name
        if path.exists() and path.read_bytes() == data:
            continue
        temporary = target / f".{name}.{secrets.token_hex(6)}.tmp"
        temporary.write_bytes(data)
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:  # e.g. Drive for desktop holding the old file while uploading
                if attempt == 3:
                    raise
                time.sleep(0.5 * (attempt + 1))
    return {"index_dir": str(target), "files": {name: sha256_bytes(data) for name, data in files.items()},
            "heads": [h.terminal_hash for h in ledger.heads], "fork": ledger.fork}


# ---------------------------------------------------------------------------
# Scoring a resolution (pure: shards alone)
# ---------------------------------------------------------------------------


def _compare(value: float, operator: str, threshold: float) -> bool:
    return {">": value > threshold, ">=": value >= threshold, "<": value < threshold,
            "<=": value <= threshold, "==": value == threshold}[operator]


def outcome_of(target: Mapping[str, Any], resolved_value: Any) -> Any:
    """Map a resolved value to the claim's outcome under its resolution spec."""
    spec = target["resolution_spec"]
    operator, rule_target, claim_type = spec["operator"], spec["threshold_or_categories"], target["claim_type"]
    if claim_type == "continuous":
        return _number(resolved_value, "resolved_value")
    if operator in COMPARISONS:
        return int(_compare(_number(resolved_value, "resolved_value"), operator, float(rule_target)))
    if isinstance(rule_target, list):
        label = str(resolved_value)
        if claim_type == "categorical":
            if label not in rule_target:
                raise LedgerError(f"resolved category {label!r} is outside the exhaustive set {rule_target}")
            return label
        return int(label in rule_target)
    bins = _bins(rule_target, "threshold_or_categories")
    value = _number(resolved_value, "resolved_value")
    hits = [name for name, bounds in bins.items() if _in_bin(value, bounds)]
    if claim_type == "categorical":
        if len(hits) != 1:
            raise LedgerError(f"value {value} falls in {len(hits)} categories; bins are not a partition")
        return hits[0]
    return int(bool(hits))


def _skill(raw: float | None, base: float | None) -> tuple[float | None, str | None]:
    if raw is None or base is None:
        return None, "no predeclared baseline"
    if base == 0.0:
        return None, "baseline scored perfectly; skill undefined"
    return scoring.skill_score(raw, base), None


def _grid_diagnostics(levels: list[float], values: list[float], y: float) -> dict:
    diagnostics: dict[str, Any] = {
        "mean_pinball": scoring.mean_pinball_loss(levels, values, y),
        "pit": scoring.pit_values(levels, values, y),
        "central_interval_hit": {},
        "tail_exceedance": {},
    }
    for nominal in CENTRAL_INTERVALS:
        lower = [i for i, t in enumerate(levels) if math.isclose(t, (1.0 - nominal) / 2.0, abs_tol=1e-12)]
        upper = [i for i, t in enumerate(levels) if math.isclose(t, (1.0 + nominal) / 2.0, abs_tol=1e-12)]
        if lower and upper:
            diagnostics["central_interval_hit"][f"{nominal:.2f}"] = bool(values[lower[0]] <= y <= values[upper[0]])
    for level in (0.01, 0.05, 0.95, 0.99):
        matches = [i for i, t in enumerate(levels) if math.isclose(t, level, abs_tol=1e-12)]
        if matches:
            q = values[matches[0]]
            diagnostics["tail_exceedance"][f"{level:.2f}"] = bool(y < q if level < 0.5 else y > q)
    return diagnostics


def score_resolution(target: Mapping[str, Any], resolved_value: Any) -> dict:
    """Outcome and scores of one forecast row given its resolved value (pure)."""
    claim_type = target["claim_type"]
    rule = target["scoring"]["rule"]
    forecast = target["forecast"]
    baseline = forecast.get("baseline_forecast") if isinstance(forecast, Mapping) else None
    outcome = outcome_of(target, resolved_value)
    raw = base = None
    diagnostics: dict[str, Any] = {}
    if claim_type == "binary":
        p = float(forecast["probability"])
        diagnostics = {"brier": scoring.brier_score([p], [outcome]), "log_loss": scoring.log_loss([p], [outcome])}
        raw = diagnostics["brier"] if rule == "brier_binary" else diagnostics["log_loss"]
        if isinstance(baseline, Mapping) and baseline.get("probability") is not None:
            pb = float(baseline["probability"])
            diagnostics["baseline_brier"] = scoring.brier_score([pb], [outcome])
            diagnostics["baseline_log_loss"] = scoring.log_loss([pb], [outcome])
            base = diagnostics["baseline_brier"] if rule == "brier_binary" else diagnostics["baseline_log_loss"]
    elif claim_type == "categorical":
        dist = forecast["categorical_distribution"]
        diagnostics = {"brier": scoring.brier_score_multiclass([dist], [outcome]),
                       "log_loss": scoring.log_loss_multiclass([dist], [outcome])}
        raw = diagnostics["brier"] if rule == "brier_multiclass" else diagnostics["log_loss"]
        if isinstance(baseline, Mapping) and baseline.get("categorical_distribution"):
            bd = baseline["categorical_distribution"]
            diagnostics["baseline_brier"] = scoring.brier_score_multiclass([bd], [outcome])
            diagnostics["baseline_log_loss"] = scoring.log_loss_multiclass([bd], [outcome])
            base = diagnostics["baseline_brier"] if rule == "brier_multiclass" else diagnostics["baseline_log_loss"]
    elif claim_type == "continuous":
        grid = forecast["numeric_distribution"]
        levels, values = [float(t) for t in grid["levels"]], [float(v) for v in grid["values"]]
        y = float(outcome)
        diagnostics = _grid_diagnostics(levels, values, y)
        diagnostics["crps"] = scoring.crps_from_quantiles(levels, values, y)

        def interval(lv: list[float], vs: list[float]) -> float:
            alpha = float(target["scoring"]["interval_alpha"])
            lo = vs[[i for i, t in enumerate(lv) if math.isclose(t, alpha / 2, abs_tol=1e-12)][0]]
            hi = vs[[i for i, t in enumerate(lv) if math.isclose(t, 1 - alpha / 2, abs_tol=1e-12)][0]]
            return scoring.interval_score([lo], [hi], [y], alpha)

        raw = diagnostics["crps"] if rule == "crps" else interval(levels, values)
        if isinstance(baseline, Mapping) and isinstance(baseline.get("quantiles"), Mapping):
            bl = [float(t) for t in baseline["quantiles"]["levels"]]
            bv = [float(v) for v in baseline["quantiles"]["values"]]
            diagnostics["baseline_crps"] = scoring.crps_from_quantiles(bl, bv, y)
            base = diagnostics["baseline_crps"] if rule == "crps" else interval(bl, bv)
    elif claim_type == "indicator":
        diagnostics = {"trigger_fired": bool(outcome)}
    skill, note = _skill(raw, base)
    return {"rule": rule, "resolved_value": resolved_value, "outcome": outcome, "raw_score": raw,
            "baseline_score": base, "skill_score": skill, "skill_note": note, "diagnostics": diagnostics}


# ---------------------------------------------------------------------------
# As-of readers and the resolver
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Observation:
    """One warehouse value as it was known at a cutoff."""

    event_date: date
    value: Any
    knowledge_time: datetime
    revision_seq: int
    source_id: str | None
    pit_class: str | None
    record: dict


class AsOfReader(Protocol):
    """Anything that returns observations exactly as knowable at a cutoff."""

    name: str

    def observations(self, source_or_series_id: str, field: str, start: date, end: date,
                     asof_utc: datetime) -> list[Observation]:
        """Latest revision per event date with ``knowledge_time <= asof_utc``."""


class UnsupportedSource(LedgerError):
    """The reader cannot resolve this source id mechanically."""


def _tool_fn(tool: Any) -> Callable[..., Any]:
    """Underlying function of a FastMCP tool (FastMCP 2 wraps it, 3 does not)."""
    return getattr(tool, "fn", tool)


class PitdbReader:
    """Default reader: the extension's own pitdb path (``price_asof`` / ``obs_asof``).

    Source ids: ``pitdb:obs/<series_id>`` (fields ``value_num`` / ``value_str``)
    and ``pitdb:price/<sec_id>`` (fields ``open``/``high``/``low``/``close``/``volume``).
    Queries run through ``server.pit_series_history`` / ``server.pit_price_history``,
    i.e. the approved-SQL worker with its freshness and E: guards.
    """

    name = "pitdb-asof/v1"

    def observations(self, source_or_series_id: str, field: str, start: date, end: date,
                     asof_utc: datetime) -> list[Observation]:
        import server  # noqa: PLC0415 - the extension module, imported only when resolving

        asof = iso_utc(asof_utc)
        if source_or_series_id.startswith("pitdb:obs/"):
            payload = _tool_fn(server.pit_series_history)(
                source_or_series_id.split("/", 1)[1], asof, start.isoformat(), end.isoformat(), 1000)
            date_key = "event_time"
        elif source_or_series_id.startswith("pitdb:price/"):
            payload = _tool_fn(server.pit_price_history)(
                int(source_or_series_id.split("/", 1)[1]), asof, start.isoformat(), end.isoformat(), 1000)
            date_key = "event_date"
        else:
            raise UnsupportedSource(f"{source_or_series_id!r} is not a pitdb:obs/ or pitdb:price/ id")
        out = []
        for row in payload["rows"]:
            if field not in row:
                raise UnsupportedSource(f"field {field!r} not returned for {source_or_series_id}")
            out.append(Observation(
                event_date=date.fromisoformat(str(row[date_key])[:10]),
                value=row[field],
                knowledge_time=_naive_utc(row["knowledge_time"], "knowledge_time"),
                revision_seq=int(row.get("revision_seq") or 0),
                source_id=row.get("source_id"),
                pit_class=row.get("pit_class"),
                record=_clean(row),
            ))
        return out


@dataclasses.dataclass
class ResolutionOutcome:
    """What the resolver decided for one due forecast row."""

    target_id: str
    status: str  # RESOLVED | MISSING | VOID | PENDING | POSTPONED | MANUAL
    reason: str | None
    row: dict | None
    observation: Observation | None = None


def _vintage_cutoff(spec: Mapping[str, Any], asof: datetime) -> tuple[str, datetime | None]:
    """``("ok", cutoff)``, ``("wait", None)`` or ``("manual", None)``."""
    vintage = spec["vintage_policy"]
    if vintage == "first_release":
        return "ok", asof
    if vintage == "final_release":
        return "manual", None
    if vintage == "specified_snapshot":
        cutoff = parse_utc(spec["snapshot_asof_utc"], "snapshot_asof_utc")
        return ("ok", cutoff) if cutoff <= asof else ("wait", None)
    revised = _REVISED_ON_RE.match(vintage)
    cutoff = datetime.combine(date.fromisoformat(revised.group(1)), datetime.max.time(), tzinfo=timezone.utc)
    return ("ok", cutoff) if cutoff <= asof else ("wait", None)


def _select(observations: list[Observation], spec: Mapping[str, Any]) -> Observation | None:
    rule = spec.get("observation_rule", "exact_event_date")
    # Intraday series share an event date; the full event stamp breaks the tie.
    ordered = sorted(observations, key=lambda o: (o.event_date, str(o.record.get("event_time") or "")))
    if rule == "exact_event_date":
        target = date.fromisoformat(spec["event_date"])
        matches = [o for o in ordered if o.event_date == target]
        return matches[-1] if matches else None
    if rule == "last_on_or_before":
        target = date.fromisoformat(spec["event_date"])
        matches = [o for o in ordered if o.event_date <= target]
        return matches[-1] if matches else None
    origin = date.fromisoformat(spec["origin_event_date"])
    after = [o for o in ordered if o.event_date > origin]
    horizon = int(spec["horizon_trading_days"])
    return after[horizon - 1] if len(after) >= horizon else None


def _window(spec: Mapping[str, Any]) -> tuple[date, date]:
    rule = spec.get("observation_rule", "exact_event_date")
    if rule == "exact_event_date":
        day = date.fromisoformat(spec["event_date"])
        return day, day
    if rule == "last_on_or_before":
        day = date.fromisoformat(spec["event_date"])
        return day - timedelta(days=int(spec.get("lookback_days", 10))), day
    origin = date.fromisoformat(spec["origin_event_date"])
    horizon = int(spec["horizon_trading_days"])
    return origin + timedelta(days=1), origin + timedelta(days=2 * horizon + 14)


def _lineage_for(obs: Observation, source_id: str) -> dict:
    return {
        "source_ids": [obs.source_id] if obs.source_id else [],
        "origin_graph": {str(obs.source_id): [source_id]},
        "evidence_record_ids": [f"{source_id}@{obs.event_date.isoformat()}#rev{obs.revision_seq}"],
        "event_time": obs.event_date.isoformat(),
        "knowledge_time": iso_utc(obs.knowledge_time),
        "revision_seq": obs.revision_seq,
        "pit_class": obs.pit_class if obs.pit_class in PIT_CLASSES else "NON_PIT",
        "artifact_sha256": sha256_json(obs.record),
    }


def _resolution_row(target: Mapping[str, Any], target_hash: str, *, status: str, reason: str | None,
                    obs: Observation | None, cutoff: datetime | None, reader_name: str, scored: dict | None,
                    now: datetime) -> dict:
    spec = target["resolution_spec"]
    sc = {"rule": target["scoring"]["rule"], "resolved_value": None, "outcome": None, "raw_score": None,
          "baseline_score": None, "skill_score": None, "skill_note": reason, "diagnostics": {},
          "date_scored_utc": iso_utc(now), "scoring_impl": "src.quantlib.scoring"}
    if scored is not None:
        sc.update(scored)
    return {
        "kind": "resolution",
        "target_id": target["id"],
        "target_record_hash": target_hash,
        "emitted_by": "forecast-resolver",
        "emitted_utc": iso_utc(now),
        "claim": f"Resolution of {target['id']}: {target['claim']}",
        "claim_type": target["claim_type"],
        "decision_link": target["decision_link"],
        "resolution_spec": None,
        "forecast": None,
        "resolution": {"status": status, "reason": reason, "reader": reader_name,
                       "read_asof_utc": iso_utc(cutoff) if cutoff else None,
                       "vintage_policy": spec["vintage_policy"],
                       "observation": obs.record if obs else None},
        "scoring": sc,
        "evidence_lineage": _lineage_for(obs, spec["source_or_series_id"]) if obs else None,
        "attribution": {"forecast_method_id": "forecast_ledger.resolve_due", "agent_model_id": None,
                        "source_observation_reliability": None, "transformation_ids": ["outcome_of", "score_resolution"]},
        "monitor": None,
        "dependence_group": target.get("dependence_group"),
    }


def _missing(target, target_hash, spec, reason, cutoff, reader_name, now) -> ResolutionOutcome:
    policy = spec["missing_policy"]
    if policy == "postpone_to_named_release":
        return ResolutionOutcome(target["id"], "POSTPONED", reason, None)
    status = "MISSING" if policy == "resolve_missing" else "VOID"
    row = _resolution_row(target, target_hash, status=status, reason=reason, obs=None, cutoff=cutoff,
                          reader_name=reader_name, scored=None, now=now)
    return ResolutionOutcome(target["id"], status, reason, row)


def resolve_row(target: Mapping[str, Any], target_hash: str, run_asof: datetime, asof: datetime,
                reader: AsOfReader, now: datetime) -> ResolutionOutcome:
    """Decide one due forecast row (no publication)."""
    spec = target["resolution_spec"]
    first = parse_utc(spec["first_resolvable_utc"])
    grace = parse_duration(spec["grace_period"])
    state, cutoff = _vintage_cutoff(spec, asof)
    if state == "manual":
        return ResolutionOutcome(target["id"], "MANUAL", "final_release is not mechanically resolvable", None)
    if state == "wait":
        return ResolutionOutcome(target["id"], "PENDING", "vintage cutoff not reached", None)
    start, end = _window(spec)
    try:
        observations = reader.observations(spec["source_or_series_id"], spec["field"], start, end, cutoff)
    except UnsupportedSource as exc:
        return ResolutionOutcome(target["id"], "MANUAL", str(exc), None)
    for obs in observations:
        if obs.knowledge_time > cutoff:
            raise LedgerError(f"reader {reader.name} returned a value known after the cutoff: {obs}")
    obs = _select(observations, spec)
    past_grace = asof >= first + grace
    if obs is None:
        if not past_grace:
            return ResolutionOutcome(target["id"], "PENDING", "resolving observation not yet available", None)
        return _missing(target, target_hash, spec, "no resolving observation by the end of the grace period",
                        cutoff, reader.name, now)
    if spec["vintage_policy"] == "first_release" and obs.revision_seq != 0:
        return _missing(target, target_hash, spec, f"first release superseded (revision {obs.revision_seq} "
                        "is the earliest the as-of macro exposes)", cutoff, reader.name, now)
    if obs.knowledge_time <= run_asof:
        row = _resolution_row(target, target_hash, status="VOID",
                              reason="resolving value was knowable at the forecast's run_asof_utc", obs=obs,
                              cutoff=cutoff, reader_name=reader.name, scored=None, now=now)
        return ResolutionOutcome(target["id"], "VOID", row["resolution"]["reason"], row, obs)
    if obs.value is None:
        return _missing(target, target_hash, spec, "resolving observation has no value", cutoff, reader.name, now)
    scored = score_resolution(target, obs.value)
    row = _resolution_row(target, target_hash, status="RESOLVED", reason=None, obs=obs, cutoff=cutoff,
                          reader_name=reader.name, scored=scored, now=now)
    return ResolutionOutcome(target["id"], "RESOLVED", None, row, obs)


def _impl_hashes() -> dict:
    here = Path(__file__).resolve()
    return {"forecast_ledger_sha256": sha256_file(here),
            "scoring_sha256": sha256_file(Path(scoring.__file__).resolve())}


def resolve_due(project_dir: Path | str, asof: datetime | None = None, *, reader: AsOfReader | None = None,
                scratch_dir: Path | str | None = None, owner: str = "forecast_ledger.resolve_due",
                now: Callable[[], datetime] = utc_now, publish: bool = True,
                break_stale_lease: bool = False) -> dict:
    """Resolve every due forecast row and publish one resolution shard.

    A row is due when ``asof >= first_resolvable_utc`` and no resolution row
    targets it yet. Rows whose value has not arrived stay PENDING until the
    grace period ends; then their missing_policy applies.
    """
    started = time.monotonic()
    project_dir = _project_root(project_dir)
    moment = now()
    asof = moment if asof is None else parse_utc(asof, "asof")
    if asof > moment:
        raise LedgerError("resolution asof cannot be in the future")
    reader = reader or PitdbReader()
    ledger = load_project(project_dir)
    resolved, _ = _resolution_map(ledger)
    outcomes: list[ResolutionOutcome] = []
    for shard, row, record_hash in ledger.rows():
        if row["kind"] not in FORECAST_KINDS or row["id"] in resolved:
            continue
        if parse_utc(row["resolution_spec"]["first_resolvable_utc"]) > asof:
            continue
        run_asof = parse_utc(shard.manifest["run_asof_utc"])
        outcomes.append(resolve_row(row, record_hash, run_asof, asof, reader, moment))
    rows = [o.row for o in outcomes if o.row is not None]
    report = {"asof_utc": iso_utc(asof), "due": len(outcomes),
              "outcomes": [{"target_id": o.target_id, "status": o.status, "reason": o.reason} for o in outcomes],
              "published": None}
    if rows and publish:
        evidence = [o.observation.record for o in outcomes if o.row is not None and o.observation is not None]
        manifest = {
            "project": project_dir.name, "decision_id": "forecast-resolution", "mode": "resolution",
            "run_asof_utc": iso_utc(asof), "source_snapshot_id": sha256_json(evidence),
            "engine_stamp": {"engine": "forecast_ledger.resolve_due", "reader": reader.name, **_impl_hashes()},
            "attention_minutes": 0, "compute_seconds": round(time.monotonic() - started, 3),
            "estimated_cost_usd": 0,
        }
        report["published"] = _publish(project_dir, manifest, rows, scratch_dir=scratch_dir, owner=owner,
                                       now=now, break_stale_lease=break_stale_lease, merge=False, index=True)
    return report


def rescore_from_shards(project_dir: Path | str, *, rel_tol: float = 1e-12) -> dict:
    """Recompute every stored outcome and score from the shards alone."""
    ledger = load_project(project_dir)
    by_id = {row["id"]: (row, record_hash) for _, row, record_hash in ledger.rows()}
    checked, legacy, mismatches = 0, 0, []
    for _, row, _ in ledger.rows():
        if row["kind"] != "resolution":
            continue
        if row.get("legacy_schema") == LEGACY_V1_SCHEMA:
            legacy += 1  # target lives in the v1 file; its hash binds the v1 line, not a shard row
            continue
        target, target_hash = by_id.get(row["target_id"], (None, None))
        if target is None or target_hash != row["target_record_hash"]:
            mismatches.append({"id": row["id"], "problem": "target missing or target_record_hash differs"})
            continue
        if row["resolution"]["status"] != "RESOLVED":
            continue
        checked += 1
        again = score_resolution(target, row["scoring"]["resolved_value"])
        for key in ("outcome", "raw_score", "baseline_score", "skill_score"):
            stored, fresh = row["scoring"].get(key), again[key]
            same = stored == fresh or (isinstance(stored, float) and isinstance(fresh, float)
                                       and math.isclose(stored, fresh, rel_tol=rel_tol, abs_tol=1e-15))
            if not same:
                mismatches.append({"id": row["id"], "field": key, "stored": stored, "recomputed": fresh})
    return {"checked": checked, "legacy_v1_skipped": legacy, "mismatches": mismatches, "ok": not mismatches}


def export_shard(project_dir: Path | str, run_id: str, dest: Path | str) -> dict:
    """Write VT's offline-verifiable export envelope for one shard."""
    project_dir = Path(project_dir)
    if not _RUN_ID_RE.match(run_id):
        raise SchemaError(f"invalid run_id {run_id!r}")
    source = _runs_dir(project_dir) / f"{run_id}.jsonl"
    verify_shard_file(source)
    dest = _guard(Path(dest).resolve(), "Export destination")
    export_chain_to_file(source, dest)
    result = verify_export(dest)
    return {"export": str(dest), "ok": result.ok, "records": result.record_count}


# ---------------------------------------------------------------------------
# Builders: manifests, MCT predictions, distributions
# ---------------------------------------------------------------------------


def pit_audit_receipt_entry(path: Path | str) -> dict:
    """``{path, sha256, status, audited_at_utc}`` for a pit_audit_receipt.json."""
    path = Path(path)
    receipt = json.loads(path.read_text(encoding="utf-8"))
    return {"path": str(path), "sha256": sha256_file(path), "status": receipt.get("status"),
            "audited_at_utc": receipt.get("audited_at_utc")}


def skill_version_entry(path: Path | str) -> dict:
    """``{name, path, sha256}`` for a skill; prefers the packaged sidecar's canonical source."""
    path = Path(path)
    skill_md = path / "SKILL.md" if path.is_dir() else path
    sidecar = skill_md.parent / "skill_contract.json"
    entry = {"name": skill_md.parent.name, "path": str(skill_md), "sha256": sha256_file(skill_md)}
    if sidecar.is_file():
        contract = json.loads(sidecar.read_text(encoding="utf-8"))
        provenance = contract.get("provenance", {})
        entry.update(name=contract.get("name", entry["name"]),
                     path=provenance.get("canonical_path") or entry["path"],
                     sha256=provenance.get("source_sha256") or entry["sha256"],
                     packaged_path=str(skill_md), packaged_sha256=entry["sha256"],
                     vt_content_hash=provenance.get("vt_skill_record", {}).get("content_hash"))
    return entry


def anchor_parity(claim_estimand: Mapping[str, Any] | None, anchor_estimand: Mapping[str, Any] | None) -> str:
    """``PARITY_OK`` only when event, horizon, unit, denominator and cutoff all match."""
    if not isinstance(claim_estimand, Mapping) or not isinstance(anchor_estimand, Mapping):
        return "ANCHOR_MISMATCH"
    for key in ANCHOR_ESTIMAND_KEYS:
        if key not in claim_estimand or key not in anchor_estimand:
            return "ANCHOR_MISMATCH"
        a, b = claim_estimand[key], anchor_estimand[key]
        if key == "run_asof_utc":
            try:
                a, b = parse_utc(a), parse_utc(b)
            except SchemaError:
                return "ANCHOR_MISMATCH"
        if a != b:
            return "ANCHOR_MISMATCH"
    return "PARITY_OK"


def _bin_text(bounds: tuple[float | None, float | None], unit: str) -> str:
    lo, hi = bounds
    if lo is None:
        return f"below {hi:g} {unit}"
    if hi is None:
        return f"at or above {lo:g} {unit}"
    return f"in [{lo:g}, {hi:g}) {unit}"


def _evidence_lineage(evidence: Mapping[str, Any] | None, evidence_sha: str | None) -> dict:
    if not evidence:
        return {"source_ids": [], "origin_graph": {}, "evidence_record_ids": [], "event_time": None,
                "knowledge_time": [], "revision_seq": None, "pit_class": "NON_PIT", "artifact_sha256": evidence_sha}
    items = list(evidence.get("admitted_observations") or []) + list(evidence.get("admitted_prices") or [])
    graph: dict[str, list[str]] = {}
    ids, stamps, classes = [], [], []
    for item in items:
        ticker = item.get("ticker") or item.get("primary_ticker")
        record_id = str(item.get("series_id") or f"price:{ticker}@{item.get('event_date')}")
        source = str(item.get("source_id"))
        graph.setdefault(source, []).append(record_id)
        ids.append(record_id)
        if item.get("knowledge_time"):
            stamps.append(iso_utc(_naive_utc(item["knowledge_time"], "knowledge_time")))
        classes.append(item.get("pit_class") or "NON_PIT")
    order = {name: i for i, name in enumerate(PIT_CLASSES)}
    worst = max(classes, key=lambda c: order.get(c, len(order))) if classes else "NON_PIT"
    return {"source_ids": sorted(graph), "origin_graph": {k: sorted(v) for k, v in sorted(graph.items())},
            "evidence_record_ids": sorted(ids), "event_time": None, "knowledge_time": sorted(set(stamps)),
            "revision_seq": None, "pit_class": worst if worst in PIT_CLASSES else "NON_PIT",
            "artifact_sha256": evidence_sha, "snapshot_id": evidence.get("snapshot_id")}


def mct_draft(result: Mapping[str, Any], spec: Mapping[str, Any], *,
              evidence: Mapping[str, Any] | None = None, evidence_sha256: str | None = None,
              run_manifest: Mapping[str, Any] | None = None) -> dict:
    """Draft shard for one actor-simulation result (``pilot_result.json``).

    One binary prediction row per terminal outcome (the simulator's
    ``ensemble_mc`` frequency, its Wilson interval, the predeclared baseline and
    ``anchor_status``), plus one indicator row per leverage node that ``spec``
    maps to a canonical PIT series. Unmapped leverage nodes stay visible in the
    manifest's ``extensions``. The spec supplies what the simulator cannot:
    the resolving series and bins, the decision link and any market anchor.

    ``run_manifest`` is the extension's run_manifest.json for the same run
    (ZT add-on): its blind-packet contamination status is carried into the
    shard manifest (``spec["contamination"]`` overrides it), so a CONTAMINATED
    run is recorded but never counted toward skill. For a blind run, pass the
    unblinded packet.json as ``evidence``, not the blind rendering.
    """
    if evidence is not None and evidence.get("schema") == BLIND_VIEW_SCHEMA:
        raise SchemaError("evidence is a blind rendering without knowledge times; record the run with "
                          "the unblinded packet.json named in run_manifest.json blind.unblinded_packet_path")
    if run_manifest is not None and run_manifest.get("run_id") not in (None, result.get("run_id")):
        raise SchemaError("run_manifest belongs to another simulator run (run_id differs)")
    probabilities = {str(k): float(v) for k, v in result["ensemble_mc"].items()}
    if abs(math.fsum(probabilities.values()) - 1.0) > scoring.SUM_TO_ONE_TOLERANCE:
        raise SchemaError("ensemble_mc does not sum to 1; terminal outcomes are not exhaustive")
    base_spec = dict(spec["resolution_spec"])
    bins = _bins(base_spec["threshold_or_categories"], "resolution_spec.threshold_or_categories")
    _exhaustive_bins(bins, "resolution_spec.threshold_or_categories")
    if sorted(bins) != sorted(probabilities):
        raise SchemaError(f"spec bins {sorted(bins)} differ from simulator outcomes {sorted(probabilities)}")
    stamp = result.get("run_stamp", {})
    n = int(stamp["n_rollouts"])
    z = float(spec.get("wilson_z", 1.96))
    reported = result.get("mc_wilson_95") or {}
    run_asof = spec.get("run_asof_utc") or (evidence and evidence.get("asof_utc_naive") and
                                            iso_utc(_naive_utc(evidence["asof_utc_naive"], "asof_utc_naive")))
    if not run_asof:
        raise SchemaError("run_asof_utc must come from the spec or the evidence snapshot")
    claim_estimand = dict(spec.get("claim_estimand") or {})
    anchor = spec.get("market_anchor") or None
    parity = anchor_parity(claim_estimand, anchor.get("estimand")) if anchor else None
    base_rates = spec.get("base_rates") or {}
    lineage = _evidence_lineage(evidence, evidence_sha256 or stamp.get("evidence_sha256") and
                                f"sha256:{stamp['evidence_sha256']}")
    scenario_sha = stamp.get("scenario_sha256", "")
    method = spec.get("forecast_method_id") or f"market_actor_sim/run_governed_pilot@scenario:{scenario_sha[:12]}"
    dependence = spec.get("dependence_group") or f"mct:{result.get('run_id')}"
    unit = base_spec["unit"]
    rows = []
    for outcome in sorted(bins, key=lambda k: -math.inf if bins[k][0] is None else bins[k][0]):
        p = probabilities[outcome]
        wilson = scoring.wilson_interval(p, n, z=z)
        if outcome in reported and "wilson_halfwidth_95" in reported[outcome]:
            if abs(float(reported[outcome]["wilson_halfwidth_95"]) - wilson.halfwidth) > 1e-9:
                raise SchemaError(f"reported Wilson half-width for {outcome} does not reproduce with z={z}")
        forecast: dict[str, Any] = {
            "probability": p,
            "probability_source": "ensemble_mc",
            "mc_rollouts": n,
            "mc_wilson_95": [wilson.lower, wilson.upper],
            "mc_wilson_z": z,
            "exact_probability": (result.get("ensemble_exact") or {}).get(outcome),
            "model_form_range": (result.get("model_form_range") or {}).get(outcome),
            "anchor_estimand": anchor.get("estimand") if anchor else None,
            "anchor_parity": parity,
            "edge_vs_anchor": None,
        }
        market_p = (anchor or {}).get("probabilities", {}).get(outcome) if parity == "PARITY_OK" else None
        if market_p is not None:
            forecast.update(anchor_status="market-anchored", edge_vs_anchor=p - float(market_p),
                            baseline_forecast={"kind": "market_implied", "probability": float(market_p),
                                               "instrument": anchor.get("instrument"),
                                               "observed_utc": anchor.get("observed_utc")})
        elif outcome in base_rates:
            forecast.update(anchor_status="base-rate-anchored",
                            baseline_forecast={"kind": "base_rate", "probability": float(base_rates[outcome]),
                                               "source": spec.get("base_rate_source")})
        else:
            forecast.update(anchor_status="elicited-only",
                            baseline_forecast={"kind": "uniform_prior", "probability": 1.0 / len(bins)})
        row_spec = dict(base_spec)
        row_spec["threshold_or_categories"] = {"categories": {outcome: list(bins[outcome])}}
        rows.append({
            "kind": "prediction",
            "emitted_by": "montecarlo-outcome-forecasting",
            "claim": f"{spec['claim_subject']} resolves {_bin_text(bins[outcome], unit)} (terminal outcome '{outcome}').",
            "claim_type": "binary",
            "decision_link": spec["decision_link"],
            "resolution_spec": row_spec,
            "forecast": forecast,
            "scoring": {"rule": spec.get("rule", "brier_binary")},
            "evidence_lineage": lineage,
            "attribution": {"forecast_method_id": method, "agent_model_id": spec.get("agent_model_id"),
                            "source_observation_reliability": None,
                            "transformation_ids": ["temperament_ensemble", "ensemble_mc", "wilson_interval"]},
            "monitor": None,
            "dependence_group": dependence,
        })
    series_map = spec.get("leverage_series_map") or {}
    unmapped = []
    for node in result.get("leverage_plus_0_05") or []:
        key = f"{node['state_key']}|{node['actor']}|{node['action_plus_0_05']}"
        mapping = series_map.get(key)
        if not mapping:
            unmapped.append({"node": key, "delta_expected_reward": node.get("delta_expected_reward"),
                             "unlocking_action": "map this leverage node to a canonical PIT series "
                                                 "(actor_series_registry) in the record-mct spec"})
            continue
        rows.append({
            "kind": "indicator",
            "emitted_by": "montecarlo-outcome-forecasting",
            "claim": mapping.get("claim") or (f"Leverage node {key} (delta expected reward "
                                              f"{node.get('delta_expected_reward'):+.4f}) is revealed when "
                                              f"{mapping['resolution_spec']['source_or_series_id']} "
                                              f"{mapping['resolution_spec']['operator']} "
                                              f"{mapping['resolution_spec']['threshold_or_categories']}."),
            "claim_type": "indicator",
            "decision_link": spec["decision_link"],
            "resolution_spec": dict(mapping["resolution_spec"]),
            "forecast": {"leverage_node": key, "delta_expected_reward": node.get("delta_expected_reward")},
            "scoring": {"rule": "unscored_indicator"},
            "evidence_lineage": lineage,
            "attribution": {"forecast_method_id": method, "agent_model_id": None,
                            "source_observation_reliability": None, "transformation_ids": ["leverage_plus_0_05"]},
            "monitor": dict(mapping["monitor"]),
            "dependence_group": dependence,
        })
    manifest = {
        "project": spec["project"],
        "decision_id": spec["decision_id"],
        "mode": spec.get("mode", "full_mct"),
        "run_asof_utc": run_asof,
        "source_lattice_id": spec.get("source_lattice_id"),
        "source_snapshot_id": lineage.get("artifact_sha256"),
        "pit_audit_receipt": spec.get("pit_audit_receipt"),
        "skill_versions": [skill_version_entry(p) if isinstance(p, str) else p for p in spec.get("skill_versions", [])],
        "preset_versions": spec.get("preset_versions", []),
        "engine_stamp": {"engine": "market_actor_sim/run_governed_pilot.py", "simulator_run_id": result.get("run_id"),
                         "run_stamp": stamp, "validation": result.get("validation"),
                         "authority": result.get("authority")},
        "action_set_hash": spec.get("action_set_hash"),
        "attention_minutes": spec.get("attention_minutes", 0),
        "compute_seconds": spec.get("compute_seconds", 0),
        "estimated_cost_usd": spec.get("estimated_cost_usd", 0),
        "lake_signature_sha256": spec.get("lake_signature_sha256"),
        "ontology_version": spec.get("ontology_version"),
        "graph_asof_utc": spec.get("graph_asof_utc"),
        "model_config": spec.get("model_config"),
        "extensions": {"authority": result.get("authority"), "limitations": result.get("limitations"),
                       "leverage_nodes_unmapped": unmapped},
    }
    contamination = spec.get("contamination") or (run_manifest or {}).get("contamination")
    if contamination is not None:
        if not isinstance(contamination, Mapping):
            raise SchemaError("contamination must be an object with status and skill_eligible")
        manifest["contamination"] = {
            "status": contamination.get("status"),
            "skill_eligible": contamination.get("skill_eligible"),
            "source": "spec" if spec.get("contamination") else "run_manifest",
            "probes": [{k: p.get(k) for k in ("probe_label", "verdict", "hits")}
                       for p in contamination.get("probes") or []],
            "forks_with_identity_mentions": sorted(contamination.get("fork_identity_mentions") or {}),
        }
        _validate_contamination(manifest["contamination"], "draft run_manifest")
    blind = (run_manifest or {}).get("blind")
    if isinstance(blind, Mapping):
        manifest["extensions"]["blind"] = {key: blind.get(key) for key in (
            "blind", "mode", "max_age_days", "blind_view_sha256", "sealed_mapping_sha256")}
    if spec.get("vt_manifest", True):
        manifest["vt_run_manifest"] = build_run_manifest(
            run_id=str(result.get("run_id")), timestamp=run_asof, system_prompt=spec.get("system_prompt", ""),
            skills=[SkillRecord(name=s["name"], content_hash=s.get("vt_content_hash") or s["sha256"])
                    for s in manifest["skill_versions"]],
            tool_names=spec.get("tool_names", []), package_versions=collect_key_package_versions(),
            extra={"system_prompt_recorded": bool(spec.get("system_prompt")), "run_asof_utc": run_asof,
                   "scenario_sha256": scenario_sha, "forks_sha256": stamp.get("forks_sha256"),
                   "evidence_sha256": stamp.get("evidence_sha256"), "seed": stamp.get("seed"),
                   "rollouts": n, "lake_signature_sha256": spec.get("lake_signature_sha256"),
                   "audit_sha256": (spec.get("pit_audit_receipt") or {}).get("sha256")},
        ).to_dict()
    return {"run_manifest": manifest, "rows": rows}


def _add_weekdays(day: date, count: int) -> date:
    while count > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count -= 1
    return day


def distribution_rows(*, claim_subject: str, source_or_series_id: str, field: str, unit: str,
                      origin_event_date: str, levels: Sequence[float], quantiles_by_horizon: Mapping[int, Sequence[float]],
                      emitted_by: str, forecast_method_id: str, decision_link: Any, dependence_group: str,
                      anchor_status: str = "base-rate-anchored", timezone_name: str = "America/New_York",
                      resolver: str = PitdbReader.name, vintage_policy: str = "first_release",
                      missing_policy: str = "void_with_reason", grace_period: str = "P7D",
                      baseline_by_horizon: Mapping[int, Mapping[str, Sequence[float]]] | None = None,
                      evidence_lineage: Mapping[str, Any] | None = None) -> list[dict]:
    """One ``distribution`` row per horizon (trading days counted on the series itself).

    ``first_resolvable_utc`` is the horizon's weekday count from the origin: a
    lower bound (holidays only delay the value), which keeps the late-write
    rule conservative; the grace period absorbs the delay.
    """
    if emitted_by not in ("mechanical-baseline", "market-implied"):
        raise SchemaError("distribution rows are emitted by 'mechanical-baseline' or 'market-implied'")
    origin = date.fromisoformat(origin_event_date)
    rows = []
    for horizon in sorted(quantiles_by_horizon):
        first = datetime.combine(_add_weekdays(origin, int(horizon)), datetime.min.time(), tzinfo=timezone.utc)
        grid = {"type": "quantiles", "levels": [float(t) for t in levels],
                "values": [float(v) for v in quantiles_by_horizon[horizon]], "horizon_trading_days": int(horizon),
                "origin_event_date": origin_event_date}
        forecast: dict[str, Any] = {"numeric_distribution": grid, "anchor_status": anchor_status,
                                    "baseline_forecast": None}
        if baseline_by_horizon and horizon in baseline_by_horizon:
            base = baseline_by_horizon[horizon]
            forecast["baseline_forecast"] = {"kind": base.get("kind", "reference_distribution"),
                                             "quantiles": {"type": "quantiles", "levels": list(base["levels"]),
                                                           "values": list(base["values"])}}
        rows.append({
            "kind": "distribution",
            "emitted_by": emitted_by,
            "claim": f"{claim_subject} {int(horizon)} trading days after {origin_event_date} "
                     f"({field}, {unit}) follows the recorded quantile grid.",
            "claim_type": "continuous",
            "decision_link": decision_link,
            "resolution_spec": {
                "resolver": resolver, "source_or_series_id": source_or_series_id, "field": field,
                "operator": "numeric_value", "threshold_or_categories": None, "unit": unit,
                "timezone": timezone_name, "first_resolvable_utc": iso_utc(first),
                "vintage_policy": vintage_policy, "missing_policy": missing_policy, "grace_period": grace_period,
                "observation_rule": "nth_after_origin", "origin_event_date": origin_event_date,
                "horizon_trading_days": int(horizon),
            },
            "forecast": forecast,
            "scoring": {"rule": "crps"},
            "evidence_lineage": dict(evidence_lineage or {}),
            "attribution": {"forecast_method_id": forecast_method_id, "agent_model_id": None,
                            "source_observation_reliability": None, "transformation_ids": []},
            "monitor": None,
            "dependence_group": dependence_group,
        })
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_manifest_for(result_path: str, explicit: str | None, result: Mapping[str, Any]) -> dict | None:
    """The run's run_manifest.json: explicit, else the sibling of the result when its run_id matches.

    Reading the sibling by default keeps a CONTAMINATED run from being
    recorded as skill-eligible because a flag was forgotten.
    """
    if explicit:
        return _load_json(explicit)
    sibling = Path(result_path).with_name("run_manifest.json")
    if sibling.is_file():
        manifest = _load_json(str(sibling))
        if manifest.get("run_id") == result.get("run_id"):
            return manifest
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; prints one JSON document."""
    parser = argparse.ArgumentParser(prog="forecast_ledger", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--project", required=True, help="canonical project folder (G: at runtime)")
        return cmd

    for name, text in (("publish", "publish a draft shard"), ("record-mct", "record an actor-simulation result"),
                       ("resolve-due", "resolve due forecasts and publish a resolution shard"),
                       ("merge-heads", "publish a merge shard joining forked heads")):
        cmd = add(name, text)
        cmd.add_argument("--scratch", help="local scratch root (E: at runtime)")
        cmd.add_argument("--owner", default=f"forecast_ledger:{name}")
        cmd.add_argument("--break-stale-lease", action="store_true")
        if name == "publish":
            cmd.add_argument("--draft", required=True)
        if name == "record-mct":
            cmd.add_argument("--result", required=True, help="pilot_result.json")
            cmd.add_argument("--spec", required=True, help="record-mct spec JSON")
            cmd.add_argument("--evidence", help="evidence_snapshot.json, or a packet run's unblinded packet.json")
            cmd.add_argument("--run-manifest", help="the run's run_manifest.json (default: the one beside "
                                                    "--result when its run_id matches)")
            cmd.add_argument("--dry-run", action="store_true")
        if name == "resolve-due":
            cmd.add_argument("--asof", help="ISO-8601 cutoff (default: now)")
            cmd.add_argument("--dry-run", action="store_true")
    add("verify", "verify every shard and link").add_argument("--receipts", help="E: export receipts folder")
    add("rebuild-index", "regenerate _ledger/index from the shards")
    add("rescore", "recompute every score from the shards alone")
    export = add("export", "write a VT export envelope for one shard")
    export.add_argument("--run-id", required=True)
    export.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "verify":
            out = verify_project(args.project, args.receipts)
        elif args.command == "rebuild-index":
            out = rebuild_index(args.project)
        elif args.command == "rescore":
            out = rescore_from_shards(args.project)
        elif args.command == "export":
            out = export_shard(args.project, args.run_id, args.out)
        elif args.command == "publish":
            out = publish_draft(args.project, _load_json(args.draft), scratch_dir=args.scratch, owner=args.owner,
                                break_stale_lease=args.break_stale_lease)
        elif args.command == "record-mct":
            evidence = _load_json(args.evidence) if args.evidence else None
            result = _load_json(args.result)
            draft = mct_draft(result, _load_json(args.spec), evidence=evidence,
                              evidence_sha256=sha256_file(Path(args.evidence)) if args.evidence else None,
                              run_manifest=_run_manifest_for(args.result, args.run_manifest, result))
            if args.dry_run:
                out = {"dry_run": True, "draft": draft}
            else:
                out = publish_draft(args.project, draft, scratch_dir=args.scratch, owner=args.owner,
                                    break_stale_lease=args.break_stale_lease)
        elif args.command == "resolve-due":
            out = resolve_due(args.project, parse_utc(args.asof, "--asof") if args.asof else None,
                              scratch_dir=args.scratch, owner=args.owner, publish=not args.dry_run,
                              break_stale_lease=args.break_stale_lease)
        else:
            out = merge_heads(args.project, scratch_dir=args.scratch, owner=args.owner,
                              break_stale_lease=args.break_stale_lease)
    except LedgerError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    ok = out.get("ok", True) if isinstance(out, dict) else True
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
