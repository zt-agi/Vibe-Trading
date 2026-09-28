"""Strategy parameter families and the hash-chained trial ledger (ZT add-on).

A reported Sharpe means nothing without the number of variants tried to find
it. When a run's ``validation`` block asks for any of ``dsr`` / ``pbo`` /
``cpcv`` / ``fdr`` (or ``"gate": true``), the runner calls
:func:`prepare_validation_variants` before the engine runs. It

1. expands the SignalEngine's ``PARAM_GRID`` class attribute (a dict of lists
   or a list of dicts; attribute names the engine reads in ``generate``) and
   adds the engine's own defaults, which are the *reported* variant;
2. evaluates every variant on the run's frozen data map with the engine's own
   alignment (``engines.base._align``: next-bar execution, ``sum|w| <= 1``,
   the run's optimizer and warm-up boundary) less a linear turnover cost;
3. writes ``artifacts/variant_returns.parquet``; and
4. appends one record per variant to the hash-chained trial ledger
   (``src.governance.ledger``) under the runtime root, so validation counts
   every trial ever ledgered on the same universe and a tampered ledger blocks
   validation.

Trial families. Records are grouped by ``question_key`` -- a hash of the
run's instrument universe only. Dates, interval, source and code changes are
all left out on purpose: trying another window, bar size or rewrite of the
same idea on the same universe is still a trial of that idea. A declared
``validation.family`` can only add trials (union), never remove them. The
ledger path is fixed by the runtime root, not by the run config.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

GATE_CHECKS = ("dsr", "pbo", "cpcv", "fdr")
TRIAL_SCHEMA = "vt-trial-ledger/v1"
TRIAL_RECORD_TYPE = "backtest_trial"
DEFAULT_COST_BPS = 5.0
MAX_VARIANTS = 1000
VARIANT_RETURNS_FILE = "variant_returns.parquet"
VARIANTS_CONFIG_KEY = "_validation_variants"


class TrialLedgerBlocked(RuntimeError):
    """The trial ledger cannot be trusted for this run (broken chain, missing trials)."""


def requested_gate_checks(v_cfg: Any) -> list[str]:
    """Which of dsr / pbo / cpcv / fdr a ``validation`` block asks for."""
    if not isinstance(v_cfg, Mapping):
        return []
    if v_cfg.get("gate") is True:
        return list(GATE_CHECKS)
    return [c for c in GATE_CHECKS if c in v_cfg and v_cfg.get(c) not in (None, False)]


def trial_ledger_path() -> Path:
    """The runtime's single trial ledger (E: on PC1 via VIBE_TRADING_HOME)."""
    from src.config.paths import get_runtime_root

    return get_runtime_root() / "governance" / "trial_ledger.jsonl"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def question_key(codes: Sequence[str]) -> str:
    """Trial-family key: the instrument universe, nothing else (see module docstring)."""
    universe = sorted({str(code).strip().upper() for code in codes if str(code).strip()})
    return _sha256_text(_canonical({"universe": universe}))


def returns_sha256(series: pd.Series) -> str:
    """Hash of a variant's return path: dates and float64 values, bit for bit."""
    index = pd.DatetimeIndex(series.index)
    if index.tz is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(index.as_unit("ns").asi8).tobytes())
    digest.update(np.ascontiguousarray(series.to_numpy(dtype=np.float64)).tobytes())
    return "sha256:" + digest.hexdigest()


def data_signature(data_map: Mapping[str, pd.DataFrame]) -> str:
    """Hash of the frozen bars the variants were evaluated on."""
    digest = hashlib.sha256()
    for code in sorted(data_map):
        frame = data_map[code]
        digest.update(code.encode("utf-8"))
        digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return "sha256:" + digest.hexdigest()


def expand_param_grid(grid: Any) -> list[dict[str, Any]]:
    """Expand ``PARAM_GRID`` into concrete parameter dicts.

    Args:
        grid: ``{"fast": [5, 10], "slow": [50, 100]}`` (cartesian product, keys
            sorted) or ``[{"fast": 5}, {"fast": 10}]`` (taken as listed).

    Returns:
        Parameter dicts; ``[]`` for ``None``.

    Raises:
        ValueError: Malformed grid, non-JSON values, or more than MAX_VARIANTS.
    """
    if grid is None:
        return []
    if isinstance(grid, Mapping):
        keys = sorted(grid)
        values = []
        for key in keys:
            options = grid[key]
            if not isinstance(options, (list, tuple)) or not options:
                raise ValueError(f"PARAM_GRID[{key!r}] must be a non-empty list")
            values.append(list(options))
        combos = [dict(zip(keys, combo)) for combo in itertools.product(*values)]
    elif isinstance(grid, (list, tuple)):
        if not all(isinstance(item, Mapping) for item in grid):
            raise ValueError("a list PARAM_GRID must hold parameter dicts")
        combos = [dict(item) for item in grid]
    else:
        raise ValueError("PARAM_GRID must be a dict of lists or a list of dicts")
    for combo in combos:
        _canonical(combo)
        json.dumps(combo)  # parameters must be plain JSON values
    if len(combos) > MAX_VARIANTS:
        raise ValueError(f"PARAM_GRID expands to {len(combos)} variants; the limit is {MAX_VARIANTS}")
    return combos


def family_variants(engine_cls: type) -> tuple[list[dict[str, Any]], int]:
    """The variants to evaluate and the index of the engine's own (reported) one."""
    grid = expand_param_grid(getattr(engine_cls, "PARAM_GRID", None))
    names = sorted({name for combo in grid for name in combo})
    default = engine_cls()
    missing = [name for name in names if not hasattr(default, name)]
    if missing:
        raise ValueError(
            f"PARAM_GRID names {missing}, which are not attributes of SignalEngine; "
            "variants set attributes by name before generate()"
        )
    reported = {name: getattr(default, name) for name in names}
    variants = list(grid)
    if reported not in variants:
        variants.insert(0, reported)
    return variants, variants.index(reported)


def make_variant(engine_cls: type, params: Mapping[str, Any]) -> Any:
    """A SignalEngine instance with ``params`` set as attributes."""
    engine = engine_cls()
    for name, value in params.items():
        setattr(engine, name, value)
    return engine


def variant_return_series(
    engine: Any,
    data_map: Mapping[str, pd.DataFrame],
    config: Mapping[str, Any],
    *,
    cost_bps: float,
) -> pd.Series:
    """Net per-bar portfolio returns of one variant, as the engine would align it."""
    from backtest.engines.base import _align, _load_optimizer, evaluation_start_index

    signal_map = engine.generate(dict(data_map))
    if not isinstance(signal_map, dict):
        raise ValueError("SignalEngine.generate() must return a dict of Series")
    codes = sorted(c for c in signal_map if c in data_map and isinstance(signal_map[c], pd.Series))
    if not codes:
        raise ValueError("variant produced no signals for the run's instruments")
    dates, _close, _close_val, pos, ret = _align(
        dict(data_map), {c: signal_map[c] for c in codes}, codes,
        optimizer=_load_optimizer(dict(config)),
    )
    ret = ret.fillna(0.0)
    gross = (pos * ret).sum(axis=1)
    turnover = pos.diff().abs().sum(axis=1)
    turnover.iloc[0] = pos.iloc[0].abs().sum()
    net = gross - turnover * (cost_bps / 1e4)
    start = max(evaluation_start_index(dict(config), dates), 1)
    return net.iloc[start:].astype("float64")


def trial_statistics(series: pd.Series) -> dict[str, Any]:
    """Per-observation Sharpe, moments and the one-sided p-value (H0: Sharpe <= 0)."""
    from src.quantlib.multipletesting import probabilistic_sharpe_ratio, sharpe_ratio

    values = pd.Series(series, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    n = int(values.size)
    stats: dict[str, Any] = {"n_obs": n, "sharpe_per_obs": None, "skew": None,
                             "kurtosis": None, "p_value": None}
    if n < 3:
        return stats
    sharpe = sharpe_ratio(values)
    skew = float(values.skew())
    kurt = float(values.kurt()) + 3.0  # pandas reports excess kurtosis
    stats.update(
        sharpe_per_obs=None if not math.isfinite(sharpe) else float(sharpe),
        skew=skew if math.isfinite(skew) else None,
        kurtosis=kurt if math.isfinite(kurt) else None,
    )
    if stats["sharpe_per_obs"] is not None and stats["skew"] is not None \
            and stats["kurtosis"] is not None:
        try:
            psr = probabilistic_sharpe_ratio(sharpe, n, 0.0, skew, kurt)
            stats["p_value"] = float(1.0 - psr)
        except ValueError:
            stats["p_value"] = None
    return stats


@dataclass
class LedgeredFamily:
    """What :func:`ledger_trials` wrote for one run."""

    run_id: str
    question_key: str
    family: Optional[str]
    reported_variant: str
    ledger_path: Path
    variants: list[dict[str, Any]] = field(default_factory=list)

    def to_config(self, returns_path: Path, **extra: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "run_id": self.run_id,
            "question_key": self.question_key,
            "family": self.family,
            "reported_variant": self.reported_variant,
            "ledger_path": str(self.ledger_path),
            "variant_returns_path": str(returns_path),
            "variants": self.variants,
            **extra,
        }


def ledger_trials(
    returns: pd.DataFrame,
    params: Sequence[Mapping[str, Any]],
    reported: int,
    *,
    ledger_path: Path,
    codes: Sequence[str],
    family: Optional[str] = None,
    run_id: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> LedgeredFamily:
    """Append one hash-chained ledger record per variant column of ``returns``.

    Raises:
        LedgerCorruptionError: The existing chain is already broken (the
            ledger refuses to extend it).
    """
    from src.governance.ledger import append_record

    if len(params) != returns.shape[1]:
        raise ValueError("one parameter dict per variant column is required")
    run_id = run_id or f"run-{uuid.uuid4().hex}"
    qkey = question_key(codes)
    columns = list(returns.columns)
    written = LedgeredFamily(run_id=run_id, question_key=qkey, family=family,
                             reported_variant=str(columns[reported]),
                             ledger_path=Path(ledger_path))
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for position, column in enumerate(columns):
        series = returns[column]
        payload = {
            "record_type": TRIAL_RECORD_TYPE,
            "schema": TRIAL_SCHEMA,
            "question_key": qkey,
            "family": family,
            "run_id": run_id,
            "variant": str(column),
            "variant_count": len(columns),
            "reported": position == reported,
            "params": dict(params[position]),
            "params_sha256": _sha256_text(_canonical(dict(params[position]))),
            "window": [str(series.index[0].date()), str(series.index[-1].date())]
            if len(series) else None,
            "returns_sha256": returns_sha256(series),
            **trial_statistics(series),
            "recorded_at_utc": now,
            **dict(extra or {}),
        }
        record = append_record(Path(ledger_path), payload)
        written.variants.append({
            "variant": str(column),
            "params": dict(params[position]),
            "record_hash": record["record_hash"],
            "returns_sha256": payload["returns_sha256"],
        })
    return written


def run_trial_family(
    config: Mapping[str, Any],
    engine_cls: type,
    data_map: Mapping[str, pd.DataFrame],
    run_dir: Path,
    *,
    ledger_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Evaluate, persist and ledger the run's variant family (see module docstring)."""
    from backtest.engines.base import _maybe_enrich_events, _maybe_enrich_fundamentals

    v_cfg = config.get("validation") if isinstance(config.get("validation"), Mapping) else {}
    variants_cfg = v_cfg.get("variants") if isinstance(v_cfg.get("variants"), Mapping) else {}
    cost_bps = float(variants_cfg.get("cost_bps", DEFAULT_COST_BPS))
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError(f"validation.variants.cost_bps must be >= 0, got {cost_bps}")
    family = v_cfg.get("family")
    family = str(family).strip() if isinstance(family, str) and family.strip() else None

    frozen = _maybe_enrich_events(_maybe_enrich_fundamentals(dict(data_map), dict(config)),
                                  dict(config))
    params, reported = family_variants(engine_cls)
    columns = {}
    for position, combo in enumerate(params):
        series = variant_return_series(make_variant(engine_cls, combo), frozen, config,
                                       cost_bps=cost_bps)
        columns[f"v{position:03d}"] = series
    returns = pd.DataFrame(columns).fillna(0.0).astype("float64")
    returns.index.name = "trade_date"

    artifacts = Path(run_dir) / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    returns_path = artifacts / VARIANT_RETURNS_FILE
    returns.to_parquet(returns_path)

    strategy_file = Path(run_dir) / "code" / "signal_engine.py"
    extra = {
        "strategy_sha256": "sha256:" + hashlib.sha256(strategy_file.read_bytes()).hexdigest()
        if strategy_file.is_file() else None,
        "data_signature": data_signature(frozen),
        "cost_bps": cost_bps,
        "source": config.get("source"),
        "interval": config.get("interval", "1D"),
        "pit_mode": (config.get("pit") or {}).get("mode") if isinstance(config.get("pit"), Mapping) else None,
    }
    written = ledger_trials(
        returns, params, reported,
        ledger_path=ledger_path or trial_ledger_path(),
        codes=list(config.get("codes") or []),
        family=family,
        run_id=f"{Path(run_dir).name}-{uuid.uuid4().hex[:12]}",
        extra=extra,
    )
    return written.to_config(returns_path, cost_bps=cost_bps,
                             data_signature=extra["data_signature"])


def prepare_validation_variants(
    config: dict,
    engine_cls: type,
    data_map: Mapping[str, pd.DataFrame],
    run_dir: Path,
) -> Optional[dict[str, Any]]:
    """Runner hook: ledger the family when the run asks for gate checks, else no-op.

    Always overwrites ``config["_validation_variants"]`` when gate checks are
    requested, so a value smuggled in through config.json is never trusted.
    Failures are recorded (validation then reports INCONCLUSIVE), not raised:
    the backtest itself still runs.
    """
    if not requested_gate_checks(config.get("validation")):
        config.pop(VARIANTS_CONFIG_KEY, None)
        return None
    try:
        info = run_trial_family(config, engine_cls, data_map, run_dir)
    except Exception as exc:  # noqa: BLE001 - recorded, reported as INCONCLUSIVE/BLOCKED
        logger.warning("validation variants failed: %s", exc)
        info = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    config[VARIANTS_CONFIG_KEY] = info
    return info


# ---------------------------------------------------------------------------
# Reading a family back (used by backtest.validation)
# ---------------------------------------------------------------------------


@dataclass
class TrialFamily:
    """A verified family: every ledgered trial of the question plus this run's matrix."""

    records: list[dict[str, Any]]
    returns: pd.DataFrame
    reported_variant: str
    run_id: str
    ledger_head: Optional[str]
    ledger_records: int
    question_key: str
    family: Optional[str]


def _ledger_records(path: Path) -> tuple[list[dict[str, Any]], Optional[str]]:
    from src.governance.ledger import archive_segments, verify_chain_with_archives

    verification = verify_chain_with_archives(path)
    if not verification.ok:
        brk = verification.first_break
        raise TrialLedgerBlocked(
            f"trial ledger chain broken at record {brk.index} (seq {brk.seq}): {brk.reason}"
        )
    records: list[dict[str, Any]] = []
    for segment in [*archive_segments(path), path]:
        if segment.exists():
            for line in segment.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
    return records, (records[-1]["record_hash"] if records else None)


def load_trial_family(info: Mapping[str, Any]) -> TrialFamily:
    """Verify the ledger and this run's trials; return the family.

    Raises:
        TrialLedgerBlocked: No family was prepared, the chain is broken, this
            run's trials are missing from it, or the returns on disk differ
            from the ledgered hashes.
    """
    if not isinstance(info, Mapping) or info.get("status") != "ok":
        reason = info.get("error") if isinstance(info, Mapping) else None
        raise TrialLedgerBlocked(f"no ledgered variant family for this run ({reason or 'not prepared'})")
    path = Path(str(info["ledger_path"]))
    records, head = _ledger_records(path)
    trials = [r for r in records if r.get("record_type") == TRIAL_RECORD_TYPE]
    own = {r.get("record_hash"): r for r in trials if r.get("run_id") == info["run_id"]}
    for variant in info.get("variants") or []:
        record = own.get(variant.get("record_hash"))
        if record is None:
            raise TrialLedgerBlocked(
                f"variant {variant.get('variant')} of run {info['run_id']} is not in the trial ledger"
            )
    returns = pd.read_parquet(Path(str(info["variant_returns_path"])))
    for variant in info.get("variants") or []:
        column = variant["variant"]
        if column not in returns.columns or returns_sha256(returns[column]) != variant["returns_sha256"]:
            raise TrialLedgerBlocked(
                f"variant_returns.parquet column {column} differs from the ledgered trial"
            )
    qkey = info["question_key"]
    family = info.get("family")
    members = [r for r in trials
               if r.get("question_key") == qkey or (family and r.get("family") == family)]
    return TrialFamily(
        records=members,
        returns=returns,
        reported_variant=str(info["reported_variant"]),
        run_id=str(info["run_id"]),
        ledger_head=head,
        ledger_records=len(records),
        question_key=qkey,
        family=family,
    )


__all__ = [
    "GATE_CHECKS",
    "LedgeredFamily",
    "TrialFamily",
    "TrialLedgerBlocked",
    "data_signature",
    "expand_param_grid",
    "family_variants",
    "ledger_trials",
    "load_trial_family",
    "make_variant",
    "prepare_validation_variants",
    "question_key",
    "requested_gate_checks",
    "returns_sha256",
    "run_trial_family",
    "trial_ledger_path",
    "trial_statistics",
    "variant_return_series",
]
