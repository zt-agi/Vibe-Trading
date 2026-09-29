"""Stub of the ranker's EOD adapter with the real one's timing convention.

feature at the day-t close (16:00 New York), available at the next session's
00:00, decided at 09:25, target = next session's close/open log return.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def eod_market_prices_to_signal_panel(path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    raw["event_date"] = pd.to_datetime(raw["event_date"])
    pieces = []
    for _ticker, g in raw.sort_values(["ticker", "event_date"]).groupby("ticker"):
        g = g.copy()
        nxt = g["event_date"].shift(-1)
        g["signal_value"] = np.log(g["Close"] / g["Close"].shift(1))
        g["feature_time"] = g["event_date"].dt.tz_localize("America/New_York") + pd.Timedelta(hours=16)
        g["available_time"] = nxt.dt.tz_localize("America/New_York")
        g["decision_time"] = nxt.dt.tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=25)
        g["target_time"] = nxt.dt.tz_localize("America/New_York") + pd.Timedelta(hours=16)
        g["target_value"] = np.log(g["Close"].shift(-1) / g["Open"].shift(-1))
        part = g[["ticker", "feature_time", "available_time", "decision_time", "target_time",
                  "signal_value", "target_value"]].dropna().rename(columns={"ticker": "entity_id"})
        part["signal_id"] = "stub_momentum_1"
        part["source_id"] = "prices_eod"
        part["pit_class"] = "REVISED_VALUE"
        part["ranking_group"] = "stub_next_session_return"
        pieces.append(part)
    return pd.concat(pieces, ignore_index=True)
