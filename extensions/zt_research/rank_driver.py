"""Subprocess step of zt-research's rank_pit_signals: build the observation panel, cut it at asof.

Runs in its own interpreter (``python -B``) so the project's ranker package is
imported from the canonical folder without bytecode and never into the MCP
server process.  Reads only its inputs; writes only ``--out`` and ``--report``.

    python -B rank_driver.py --ranker-dir DIR --asof ISO --out OBS.csv --report CUT.json
        (--eod-csv EOD.csv --adapter NAME | --observations-csv OBS_IN.csv)

The cut keeps a row only when its available_time, decision_time and target_time
are all at or before asof: a signal the desk could have acted on, scored by an
outcome it could already have seen.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

import pandas as pd  # noqa: E402

TIME_COLUMNS = ("feature_time", "available_time", "decision_time", "target_time")
ALLOWED_ADAPTERS = ("eod_market_prices_to_signal_panel",)


def _iso(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).tz_convert("UTC").isoformat().replace("+00:00", "Z")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--ranker-dir", required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--eod-csv")
    source.add_argument("--observations-csv")
    parser.add_argument("--adapter", default=ALLOWED_ADAPTERS[0])
    args = parser.parse_args(argv)

    asof = pd.Timestamp(args.asof)
    if asof.tzinfo is None:
        parser.error("--asof needs a timezone offset")
    asof = asof.tz_convert("UTC")
    if args.eod_csv:
        if args.adapter not in ALLOWED_ADAPTERS:
            parser.error(f"--adapter must be one of {ALLOWED_ADAPTERS}")
        sys.path.insert(0, str(Path(args.ranker_dir).resolve()))
        from pit_alpha_ranker import adapters
        frame = getattr(adapters, args.adapter)(args.eod_csv)
    else:
        frame = pd.read_csv(args.observations_csv)
    missing = [c for c in TIME_COLUMNS if c not in frame.columns]
    if missing:
        raise SystemExit(f"observations lack time columns: {missing}")
    times = {c: pd.to_datetime(frame[c], utc=True, errors="coerce") for c in TIME_COLUMNS}
    parsed = pd.concat([t.notna() for t in times.values()], axis=1).all(axis=1)
    known = parsed & (times["available_time"] <= asof) & (times["decision_time"] <= asof) \
        & (times["target_time"] <= asof)
    cut = frame.loc[known]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    cut.to_csv(args.out, index=False)
    kept = {c: t[known] for c, t in times.items()}
    report = {
        "asof": _iso(asof),
        "rows_in": int(len(frame)),
        "rows_kept": int(len(cut)),
        "dropped_not_known_at_asof": int((parsed & ~known).sum()),
        "dropped_unparseable_time": int((~parsed).sum()),
        "signals": int(cut["signal_id"].nunique()) if "signal_id" in cut else 0,
        "entities": int(cut["entity_id"].nunique()) if "entity_id" in cut else 0,
        "ranking_groups": sorted(cut["ranking_group"].astype(str).unique().tolist())
        if "ranking_group" in cut else [],
        "pit_classes": sorted(cut["pit_class"].astype(str).unique().tolist()) if "pit_class" in cut else [],
        "decision_time_first": _iso(kept["decision_time"].min()) if len(cut) else None,
        "decision_time_last": _iso(kept["decision_time"].max()) if len(cut) else None,
        "target_time_last": _iso(kept["target_time"].max()) if len(cut) else None,
        "rule": "available_time <= asof and decision_time <= asof and target_time <= asof",
    }
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"rows_in": report["rows_in"], "rows_kept": report["rows_kept"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
