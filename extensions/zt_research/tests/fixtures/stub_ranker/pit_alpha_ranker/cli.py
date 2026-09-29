"""Stub of ``python -m pit_alpha_ranker run --input --config --output``.

Writes signal_ranking.csv and run_manifest.json the way the real CLI does, with
deterministic placeholder statistics.  ZT_STUB_RANKER_FAIL=1 makes it fail.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--config")
    args = parser.parse_args()
    if os.environ.get("ZT_STUB_RANKER_FAIL") == "1":
        print("stub ranker: induced failure", file=sys.stderr)
        return 3
    frame = pd.read_csv(args.input)
    config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
    rows = []
    for (group, signal), part in frame.groupby(["ranking_group", "signal_id"], sort=True):
        rows.append({"ranking_group": group, "signal_id": signal,
                     "source_id": part["source_id"].iloc[0], "status": "RESEARCH_ONLY_PIT",
                     "pit_status": "PIT_RESEARCH_ONLY", "predictive_status": "REJECT_DEVELOPMENT",
                     "combined_loss_ratio": 1.0, "fdr_q_value": float("nan"),
                     "valid_rows": int(len(part))})
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    ranking = pd.DataFrame(rows)
    ranking["rank_within_group"] = ranking.groupby("ranking_group").cumcount() + 1
    ranking.to_csv(out / "signal_ranking.csv", index=False)
    (out / "run_manifest.json").write_text(json.dumps({
        "config": config, "input_validation": {"rows": int(len(frame))},
        "artifacts": {"ranking": {"path": "signal_ranking.csv"}}}), encoding="utf-8")
    print(json.dumps({"status": "PASS", "signals": len(rows)}))
    return 0
