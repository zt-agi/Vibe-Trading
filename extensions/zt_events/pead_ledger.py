"""ZT add-on: forecast-ledger hook for PEAD -- mechanical, never an LLM.

For every fresh 8-K Item 2.02 event whose SUE is known and whose drift window
has not started, one ledger ``prediction`` row records

    P( CAR[+2, +20] > 0  |  SUE bucket )

as the posterior mean of a Beta-binomial hit rate: the bucket's historical
events (same window, same knowledge-time eligibility, CARs from
:mod:`src.quantlib.event_study` on prices known at the as-of) give ``k`` hits
in ``n``; with a Beta(1, 1) prior the probability is ``(k + 1) / (n + 2)``.
The baseline is the same rate over every bucket, so the ledger's skill score
measures what conditioning on SUE adds. Counts in, probability out: no model
elicits or edits it.

Eligible means: SUE status OK, bucket known, the as-of at or before the close
of day +1 (day 0 = first session whose close is after the 8-K acceptance), and
at least ``min_trials`` historical events in the bucket.

Rows follow ZT's forecast-ledger contract (``extensions/pit_actor_sim/
forecast_ledger.py``: ``zt-forecast-ledger/2`` + v2.1). The hook drafts; the
ledger validates and publishes::

    python pead_ledger.py stage   --asof 2026-09-29T12:40:00Z            # dry run: counts only
    python pead_ledger.py stage   --asof ... --write                     # E: staging draft
    python pead_ledger.py stage   --asof ... --publish --project <G:\\...> # + publish via the ledger
    python pead_ledger.py resolve --project <G:\\...> [--asof ...] [--dry-run]

``resolve`` resolves due PEAD rows only (``zt_events:car/...`` sources) with
:class:`CarReader`; every other row is left to the ledger's own resolver.
Staging lives under ``VIBE_TRADING_HOME/forecast_staging/pead`` (E: on
Windows) and is write-once; an event already staged is never staged again.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.dont_write_bytecode = True

import pandas as pd  # noqa: E402

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import core  # noqa: E402
from src.quantlib import event_study as es  # noqa: E402

METHOD_ID = "zt_events.pead_beta_binomial/v1"
RESOLVER = "zt-events-car/v1"
SOURCE_PREFIX = "zt_events:car/"
PRIOR = (1.0, 1.0)
MIN_TRIALS = 20
HORIZON = 20
WINDOW = core.DRIFT_WINDOW
STAGING_DIRNAME = "forecast_staging"
STAGING_SUBDIR = "pead"
INDEX_NAME = "staged_events.jsonl"


def ledger_module():
    """ZT's forecast ledger (``extensions/pit_actor_sim/forecast_ledger.py``), loaded by path."""
    cached = sys.modules.get("zt_forecast_ledger")
    if cached is not None:
        return cached
    path = core.VT_ROOT / "extensions" / "pit_actor_sim" / "forecast_ledger.py"
    spec = importlib.util.spec_from_file_location("zt_forecast_ledger", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zt_forecast_ledger"] = module
    spec.loader.exec_module(module)
    return module


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Hit rates and rows
# ---------------------------------------------------------------------------


POOLED = "POOLED"


def hit_table(study: Mapping[str, Any], *, prior: Sequence[float] = PRIOR, level: float = 0.90) -> pd.DataFrame:
    """Beta-binomial posterior per SUE bucket, plus ``POOLED`` over every bucket.

    ``POOLED`` counts the bucketed events only (same window, same SUE timing
    eligibility), so it is the unconditional rate the buckets refine; the
    study's ``ALL`` group also holds events whose SUE was not known in time.
    """
    report = study.get("_report")
    columns = ["window", "group", "threshold", "successes", "trials", "prior_alpha", "prior_beta",
               "posterior_alpha", "posterior_beta", "probability", "lower", "upper", "level"]
    if report is None:
        return pd.DataFrame(columns=columns)
    table = es.hit_rates(report, WINDOW, prior_alpha=prior[0], prior_beta=prior[1], level=level)
    buckets = table[table["group"].str.fullmatch(r"Q\d+")]
    pooled = es.beta_binomial_hit_rate(int(buckets["successes"].sum()), int(buckets["trials"].sum()),
                                       prior_alpha=prior[0], prior_beta=prior[1], level=level)
    row = {"window": es.window_label(WINDOW), "group": POOLED, "threshold": 0.0,
           **{k: getattr(pooled, k) for k in es.HitRate.__dataclass_fields__}}
    return pd.concat([table, pd.DataFrame([row])], ignore_index=True)[columns]


def _first_resolvable(day0: pd.Timestamp) -> tuple[str, str]:
    """Close of day +20 (UTC ISO) and day 0 (ISO date), from the NYSE session rules."""
    closes = core.calendar_closes(day0 - pd.Timedelta(days=3), day0 + pd.Timedelta(days=HORIZON * 2 + 20))
    start = int(closes.index.get_indexer([day0.normalize()])[0])
    if start < 0 or start + HORIZON >= len(closes):
        raise ValueError(f"day 0 {day0.date()} is not a session in the rule calendar")
    return core.iso(closes.iloc[start + HORIZON]), day0.date().isoformat()


def prediction_rows(candidates: Mapping[str, Any], table: pd.DataFrame, *, asof: datetime,
                    min_trials: int = MIN_TRIALS, edges: Sequence[float] = core.DEFAULT_SUE_EDGES,
                    table_sha256: str | None = None) -> tuple[list[dict], list[dict]]:
    """Ledger rows for the eligible fresh events, plus the reasons others were skipped."""
    by_group = {row["group"]: row for row in table.to_dict("records")}
    base = by_group.get(POOLED)
    rows, skipped = [], []
    events = list(candidates.get("candidates", [])) + list(candidates.get("watchlist", []))
    for event in events:
        why = None
        bucket = event.get("sue_bucket")
        if event.get("sue_status") != "OK" or not bucket:
            why = f"SUE not usable ({event.get('sue_status')})"
        elif not event.get("ledger_eligible"):
            why = "as-of is after the close of day +1: CAR[+2,+20] is no longer a forecast"
        elif bucket not in by_group or by_group[bucket]["trials"] < min_trials:
            n = by_group.get(bucket, {}).get("trials", 0)
            why = f"bucket {bucket} has {n} historical events (< {min_trials})"
        elif base is None or base["trials"] < min_trials:
            why = "no pooled base rate"
        if why:
            skipped.append({"ticker": event.get("ticker"), "accession": event.get("accession"), "reason": why})
            continue
        hit = by_group[bucket]
        day0 = pd.Timestamp(event["day0"])
        first, origin = _first_resolvable(day0)
        kts = [event["knowledge_time"], event["sue_knowledge_time"]]
        rows.append({
            "kind": "prediction",
            "emitted_by": "mechanical-baseline",
            "claim": (f"{event['ticker']} cumulative abnormal return over sessions +2..+20 after its "
                      f"Item 2.02 8-K ({event['accession']}, day 0 {origin}) is above 0 "
                      f"(market model on {core.DEFAULT_BENCHMARK}, estimation [-250,-11])."),
            "claim_type": "binary",
            "decision_link": {"playbook": "zt-pead-scan", "event": event["accession"],
                              "ticker": event["ticker"], "sue_bucket": bucket},
            "resolution_spec": {
                "resolver": RESOLVER,
                "source_or_series_id": f"{SOURCE_PREFIX}{event['ticker']}/{event['accession']}/{WINDOW[0]}-{WINDOW[1]}",
                "field": "car", "operator": ">", "threshold_or_categories": 0.0,
                "unit": "cumulative abnormal simple return", "timezone": "America/New_York",
                "first_resolvable_utc": first, "vintage_policy": "first_release",
                "missing_policy": "void_with_reason", "grace_period": "P10D",
                "observation_rule": "nth_after_origin", "origin_event_date": origin,
                "horizon_trading_days": HORIZON,
            },
            "forecast": {
                "probability": float(hit["probability"]),
                "probability_source": "beta_binomial_posterior_mean",
                "hit_counts": {"successes": int(hit["successes"]), "trials": int(hit["trials"])},
                "prior": list(PRIOR), "credible_interval": [float(hit["lower"]), float(hit["upper"])],
                "credible_level": float(hit["level"]),
                "anchor_status": "base-rate-anchored",
                "baseline_forecast": {"kind": "base_rate", "probability": float(base["probability"]),
                                      "hit_counts": {"successes": int(base["successes"]),
                                                     "trials": int(base["trials"])},
                                      "source": "all SUE buckets pooled: same window, sample and eligibility"},
            },
            "scoring": {"rule": "brier_binary"},
            "evidence_lineage": {
                "source_ids": ["sec_8k_earnings", "sec_xbrl_eps", "yahoo_eod"],
                "origin_graph": {"sec_8k_earnings": [f"sec:{event['accession']}"],
                                 "sec_xbrl_eps": [f"SEC:{event['ticker']}:EarningsPerShareDiluted@{event.get('sue_period_end')}"]},
                "evidence_record_ids": [f"sec:{event['accession']}",
                                        f"sue:{event['ticker']}@{event.get('sue_period_end')}"],
                "event_time": origin, "knowledge_time": [k for k in kts if k],
                "revision_seq": None, "pit_class": "OBSERVED_PIT",
                "artifact_sha256": table_sha256,
            },
            "attribution": {"forecast_method_id": f"{METHOD_ID}:window={es.window_label(WINDOW)}:"
                                                  f"prior={PRIOR[0]:g},{PRIOR[1]:g}:edges={list(edges)}",
                            "agent_model_id": None, "source_observation_reliability": None,
                            "transformation_ids": ["sue_seasonal_random_walk", "market_model_car",
                                                   "beta_binomial_posterior_mean"]},
            "monitor": None,
            "dependence_group": f"pead:{pd.Timestamp(origin).to_period('W')}",
        })
    return rows, skipped


def build_draft(con, provenance: Mapping[str, Any], *, asof: datetime, tickers: Sequence[str] | None = None,
                project_name: str = "Investment-AI-Drive-Research", pit_audit_receipt: Mapping | None = None,
                min_trials: int = MIN_TRIALS) -> dict:
    """A forecast-ledger draft ``{"run_manifest", "rows"}`` plus ``skipped`` and ``hit_table``."""
    started = time.monotonic()
    names = [t.upper() for t in tickers] if tickers else None
    study = core.history_study(con, provenance, asof=asof, tickers=names)
    candidates = core.pead_candidates(con, provenance, asof=asof, tickers=names, study=study)
    table = hit_table(study)
    table_records = core.records(table)
    table_sha = "sha256:" + hashlib.sha256(core.canonical(table_records).encode("utf-8")).hexdigest()
    rows, skipped = prediction_rows(candidates, table, asof=asof, min_trials=min_trials, table_sha256=table_sha)
    engine = {"engine": "zt_events.pead_ledger", "method": METHOD_ID, "window": es.window_label(WINDOW),
              "prior": list(PRIOR), "min_trials": min_trials, "sue_bucket_edges": list(core.DEFAULT_SUE_EDGES),
              "hit_table": table_records, "study_status": study.get("status"),
              "event_study_sha256": _sha256_file(Path(es.__file__)),
              "core_sha256": _sha256_file(Path(core.__file__)),
              "hook_sha256": _sha256_file(Path(__file__))}
    manifest = {"project": project_name, "decision_id": f"pead-scan:{core.iso(asof)}", "mode": "baseline",
                "run_asof_utc": core.iso(asof), "source_snapshot_id": table_sha,
                "pit_audit_receipt": dict(pit_audit_receipt) if pit_audit_receipt else None,
                "engine_stamp": engine, "attention_minutes": 0,
                "compute_seconds": round(time.monotonic() - started, 3), "estimated_cost_usd": 0,
                "lake_signature_sha256": provenance.get("lake_signature_sha256"),
                "extensions": {"authority": core.AUTHORITY, "skipped": skipped}}
    return {"run_manifest": manifest, "rows": rows, "skipped": skipped, "hit_table": table_records}


# ---------------------------------------------------------------------------
# Staging (E:) and publication (via the ledger)
# ---------------------------------------------------------------------------


def staging_root(home: Path | None = None) -> Path:
    return Path(home or core.runtime()) / STAGING_DIRNAME / STAGING_SUBDIR


def staged_accessions(root: Path) -> set[str]:
    index = root / INDEX_NAME
    if not index.is_file():
        return set()
    out = set()
    for line in index.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["accession"])
        except (ValueError, KeyError):
            continue
    return out


def stage(draft: Mapping[str, Any], *, root: Path | None = None) -> dict:
    """Write the draft once under the staging root; events already staged are dropped."""
    root = Path(root or staging_root())
    seen = staged_accessions(root)
    rows = [r for r in draft["rows"] if r["decision_link"]["event"] not in seen]
    dropped = len(draft["rows"]) - len(rows)
    if not rows:
        return {"status": "NOTHING_STAGED", "rows": 0, "already_staged": dropped, "path": None}
    manifest = dict(draft["run_manifest"])
    run_id = manifest.get("run_id") or ledger_module().new_run_id()
    manifest["run_id"] = run_id
    day = str(manifest["run_asof_utc"])[:10]
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run_id}.draft.json"
    body = json.dumps({"run_manifest": manifest, "rows": rows}, sort_keys=True, indent=2).encode("utf-8")
    with open(path, "xb") as handle:          # write-once: an existing draft is never replaced
        handle.write(body)
    with open(root / INDEX_NAME, "a", encoding="utf-8") as index:
        for row in rows:
            index.write(json.dumps({"accession": row["decision_link"]["event"], "run_id": run_id,
                                    "run_asof_utc": manifest["run_asof_utc"]}) + "\n")
    return {"status": "STAGED", "rows": len(rows), "already_staged": dropped, "path": str(path),
            "run_id": run_id, "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}


def publish(project_dir: Path | str, draft_path: Path | str, *, scratch_dir: Path | str | None = None,
            now=None) -> dict:
    """Publish a staged draft through the ledger's own validator and writer.

    Publish promptly: the ledger refuses a forecast published at or after its
    first resolvable instant (the close of session +20). ``now`` overrides the
    publication clock (tests).
    """
    ledger = ledger_module()
    draft = json.loads(Path(draft_path).read_text(encoding="utf-8"))
    kwargs = {"now": now} if now is not None else {}
    return ledger.publish_draft(project_dir, draft, scratch_dir=scratch_dir, owner="zt_events.pead_ledger",
                                **kwargs)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class CarReader:
    """Resolves ``zt_events:car/<TICKER>/<ACCESSION>/<s>-<e>`` from pitdb as of a cutoff.

    Returns one observation per session after day 0 whose value is the
    cumulative abnormal return from session ``s`` through that session (None
    before ``s``), stamped with the latest knowledge time of the bars used, so
    the ledger's ``nth_after_origin`` rule picks CAR[s, e] at session ``e``.
    Anything else is not this reader's to resolve.
    """

    name = RESOLVER

    def observations(self, source_or_series_id: str, field: str, start: date, end: date,
                     asof_utc: datetime) -> list:
        ledger = ledger_module()
        if not str(source_or_series_id).startswith(SOURCE_PREFIX) or field != "car":
            raise ledger.UnsupportedSource(f"{source_or_series_id!r} is not a {SOURCE_PREFIX} CAR")
        try:
            ticker, accession, span = str(source_or_series_id)[len(SOURCE_PREFIX):].split("/")
            first, last = (int(x) for x in span.split("-"))
        except ValueError as exc:
            raise ledger.UnsupportedSource(f"malformed CAR id {source_or_series_id!r}") from exc
        at = pd.Timestamp(asof_utc)
        at = (at.tz_convert("UTC").tz_localize(None) if at.tzinfo else at).to_pydatetime()
        with core.store() as (con, _):
            events = core.earnings_events(con, at, tickers=[ticker])
            event = events[events["accession"] == accession]
            if event.empty:
                return []
            kt = event["knowledge_time"].iloc[0]
            known: dict = {}
            closes, _info = core.price_panel(con, at, [ticker, core.DEFAULT_BENCHMARK],
                                             (kt - pd.Timedelta(days=420)).date(),
                                             min(pd.Timestamp(end), pd.Timestamp(at)).date(),
                                             knowledge=known)
        bench = core.DEFAULT_BENCHMARK
        if ticker not in closes or bench not in closes:
            return []
        frame = pd.DataFrame({"event_id": [accession], "firm": [ticker], "knowledge_time": [kt]})
        report = es.run_event_study(closes, frame, benchmark=bench, windows=[(first, last)],
                                    period_col=None, n_boot=100)
        rows = report.events
        if rows.empty:
            return []
        day0 = pd.Timestamp(rows["day0"].iloc[0])
        alpha, beta = float(rows["alpha"].iloc[0]), float(rows["beta"].iloc[0])
        calendar = closes[bench].dropna().index
        pos = int(calendar.get_loc(day0))
        stock, market = closes[ticker].reindex(calendar), closes[bench].reindex(calendar)
        out, cumulative = [], 0.0
        for k in range(1, last + 1):
            if pos + k >= len(calendar):
                break
            day = calendar[pos + k]
            r = stock.iloc[pos + k] / stock.iloc[pos + k - 1] - 1.0
            m = market.iloc[pos + k] / market.iloc[pos + k - 1] - 1.0
            if pd.isna(r) or pd.isna(m):
                break
            if k >= first:
                cumulative += float(r - (alpha + beta * m))
            stamp = pd.Series([known[ticker].get(day), known[bench].get(day)], dtype="datetime64[ns]").max()
            if pd.isna(stamp):
                break
            value = cumulative if k >= first else None
            record = {"ticker": ticker, "accession": accession, "rel_day": k, "date": day.date().isoformat(),
                      "car": value, "alpha": alpha, "beta": beta}
            out.append(ledger.Observation(event_date=day.date(), value=value,
                                          knowledge_time=stamp.tz_localize("UTC").to_pydatetime(),
                                          revision_seq=0, source_id="zt_events", pit_class="OBSERVED_PIT",
                                          record=record))
        return [o for o in out if start <= o.event_date <= end]


def resolve(project_dir: Path | str, *, asof: datetime | None = None, publish_shard: bool = True,
            scratch_dir: Path | str | None = None) -> dict:
    """Resolve due PEAD rows with :class:`CarReader` (other rows stay for the ledger)."""
    ledger = ledger_module()
    return ledger.resolve_due(project_dir, asof, reader=CarReader(), scratch_dir=scratch_dir,
                              owner="zt_events.pead_ledger.resolve", publish=publish_shard)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pead_ledger", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("stage", help="draft PEAD prediction rows (dry run unless --write)")
    st.add_argument("--asof", required=True)
    st.add_argument("--tickers", nargs="*")
    st.add_argument("--write", action="store_true", help="write the draft to the E: staging folder")
    st.add_argument("--publish", action="store_true", help="also publish it through the ledger")
    st.add_argument("--project", help="canonical project folder (G:) for --publish")
    st.add_argument("--scratch", help="ledger scratch root (E:)")
    rs = sub.add_parser("resolve", help="resolve due PEAD rows and publish one resolution shard")
    rs.add_argument("--project", required=True)
    rs.add_argument("--asof")
    rs.add_argument("--scratch")
    rs.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    ledger = ledger_module()
    if args.command == "resolve":
        out = resolve(args.project, asof=ledger.parse_utc(args.asof) if args.asof else None,
                      publish_shard=not args.dry_run, scratch_dir=args.scratch)
        print(json.dumps(out, indent=2, default=str))
        return 0
    at = core.asof_utc(args.asof)
    receipt_path = core.runtime() / "pit_audit_receipt.json"
    receipt = ledger.pit_audit_receipt_entry(receipt_path) if receipt_path.is_file() else None
    with core.store() as (con, provenance):
        draft = build_draft(con, provenance, asof=at, tickers=args.tickers, pit_audit_receipt=receipt,
                            project_name=(Path(args.project).name if args.project else
                                          "Investment-AI-Drive-Research"))
    out = {"asof": core.iso(at), "rows": len(draft["rows"]), "skipped": draft["skipped"],
           "hit_counts": [{k: r[k] for k in ("group", "successes", "trials")} for r in draft["hit_table"]],
           "note": "probabilities are recorded in the ledger, not printed"}
    if args.write or args.publish:
        out["staged"] = stage(draft)
        if args.publish:
            if not args.project:
                parser.error("--publish needs --project")
            if out["staged"]["path"]:
                out["published"] = publish(args.project, out["staged"]["path"], scratch_dir=args.scratch)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
