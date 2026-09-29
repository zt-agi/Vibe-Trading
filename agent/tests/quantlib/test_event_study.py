"""Tests for src.quantlib.event_study (ZT add-on).

Day 0 comes from the knowledge time, so the alignment rule is tested at its
edges (pre-market, 15:59:59, 16:00:00, after close, weekends, holidays, early
closes, both DST regimes). The statistics are checked against independent
computations, and the clustering claim is checked the only way it can be: on
a null sample whose events cluster, where the naive tests reject far too often
and the robust ones do not.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from src.quantlib import event_study as es
from src.quantlib.eventstudy import event_study as vt_event_study
from src.quantlib.multipletesting import benjamini_hochberg

NY = "America/New_York"


def _ny(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=NY)


# --------------------------------------------------------------------------- calendar


def test_nyse_holidays_2024_match_the_published_calendar():
    expected = {date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
                date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
                date(2024, 11, 28), date(2024, 12, 25)}
    assert es.nyse_holidays(2024) == expected
    assert len(es.us_equity_sessions("2024-01-01", "2024-12-31")) == 252


def test_holiday_observance_rules():
    h2022 = es.nyse_holidays(2022)
    assert date(2022, 6, 20) in h2022          # Juneteenth on a Sunday -> Monday
    assert date(2022, 12, 26) in h2022         # Christmas on a Sunday -> Monday
    assert date(2021, 12, 31) not in es.nyse_holidays(2021)  # Saturday New Year not observed
    assert date(2021, 6, 18) not in es.nyse_holidays(2021)   # no Juneteenth before 2022
    assert date(2026, 7, 3) in es.nyse_holidays(2026)        # July 4 on a Saturday -> Friday
    assert date(2025, 1, 9) in es.nyse_holidays(2025)        # national day of mourning
    assert date(2012, 10, 29) in es.nyse_holidays(2012)      # Hurricane Sandy


def test_early_closes():
    assert es.nyse_early_closes(2024) == {date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24)}
    assert es.nyse_early_closes(2021) == {date(2021, 11, 26)}  # July 3 Sat, Dec 24 a holiday
    assert es.nyse_early_closes(2026) == {date(2026, 11, 27), date(2026, 12, 24)}


def test_session_close_times_follow_dst_and_early_closes():
    closes = es.session_close_times(["2024-03-08", "2024-03-11", "2024-11-29"])
    assert closes.loc["2024-03-08"] == pd.Timestamp("2024-03-08 21:00", tz="UTC")  # EST
    assert closes.loc["2024-03-11"] == pd.Timestamp("2024-03-11 20:00", tz="UTC")  # EDT
    assert closes.loc["2024-11-29"] == pd.Timestamp("2024-11-29 18:00", tz="UTC")  # 13:00 EST
    none = es.session_close_times(["2024-11-29"], early_closes=[])
    assert none.iloc[0] == pd.Timestamp("2024-11-29 21:00", tz="UTC")


# --------------------------------------------------------------------------- alignment


@pytest.fixture(scope="module")
def closes_2024():
    return es.session_close_times(es.us_equity_sessions("2024-06-01", "2024-12-31"))


def _day0(closes, *stamps):
    pos = es.first_session_after(list(stamps), closes)
    return [None if p < 0 else closes.index[p].date() for p in pos]


def test_day0_is_the_first_session_whose_close_is_after_the_knowledge_time(closes_2024):
    got = _day0(
        closes_2024,
        _ny("2024-08-28 07:30"),     # pre-market -> same session
        _ny("2024-08-28 15:59:59"),  # before the close -> same session
        _ny("2024-08-28 16:00:00"),  # at the close -> NOT in that close
        _ny("2024-08-28 16:21:16"),  # after close (NVDA's release) -> next session
        _ny("2024-08-30 17:00"),     # Friday after close -> Monday is Labor Day -> Tuesday
        _ny("2024-07-04 10:00"),     # holiday -> next session
        _ny("2024-11-29 13:30"),     # after an early close -> next session
        _ny("2024-11-29 12:59"),     # before the early close -> same session
    )
    assert got == [date(2024, 8, 28), date(2024, 8, 28), date(2024, 8, 29), date(2024, 8, 29),
                   date(2024, 9, 3), date(2024, 7, 5), date(2024, 12, 2), date(2024, 11, 29)]


def test_naive_knowledge_times_are_utc_and_missing_ones_are_minus_one(closes_2024):
    # 2024-08-28 20:21:16 UTC is 16:21 New York: after the close.
    pos = es.first_session_after([pd.Timestamp("2024-08-28 20:21:16"), None,
                                  "2024-08-28T19:59:59Z", "2031-01-01T00:00:00Z"], closes_2024)
    assert closes_2024.index[pos[0]].date() == date(2024, 8, 29)
    assert pos[1] == -1
    assert closes_2024.index[pos[2]].date() == date(2024, 8, 28)
    assert pos[3] == -1


def test_first_session_after_refuses_unsorted_closes(closes_2024):
    with pytest.raises(ValueError, match="strictly increasing"):
        es.first_session_after([_ny("2024-08-28 10:00")], closes_2024.iloc[::-1])


def test_window_labels():
    assert es.window_label((0, 1)) == "[0,+1]"
    assert es.window_label((2, 20)) == "[+2,+20]"
    assert es.window_label((-5, -1)) == "[-5,-1]"


# --------------------------------------------------------------------------- dedupe


def test_dedupe_keeps_the_earliest_event_per_firm_period():
    events = pd.DataFrame({
        "event_id": ["a", "b", "c", "d", "e", "f"],
        "firm": ["X", "X", "X", "Y", "Y", "Y"],
        "period": ["Q1", "Q1", "Q2", "Q1", "Q1", None],
        "knowledge_time": ["2024-05-01T21:00Z", "2024-04-20T12:00Z", "2024-08-01T21:00Z",
                           "2024-05-02T21:00Z", "2024-05-02T21:00Z", "2024-05-03T21:00Z"],
    })
    kept, dups = es.dedupe_events(events)
    assert sorted(kept["event_id"]) == ["b", "c", "d", "f"]  # b is earlier than a; d wins the tie by id
    assert dict(zip(dups["event_id"], dups["kept_event_id"])) == {"a": "b", "e": "d"}
    assert "__dedupe_order_utc__" not in kept.columns


# --------------------------------------------------------------------------- the study


def _market_panel(seed=0, start="2019-01-01", end="2024-12-31", firms=12, beta=1.2, vol=0.015):
    rng = np.random.default_rng(seed)
    sessions = es.us_equity_sessions(start, end)
    n = len(sessions)
    market = rng.normal(0.0003, 0.01, n)
    data = {"SPY": 100 * np.cumprod(1 + market)}
    for i in range(firms):
        r = 0.0001 + beta * market + rng.normal(0, vol, n)
        data[f"F{i}"] = 40 * np.cumprod(1 + r)
    return pd.DataFrame(data, index=sessions)


def _events(prices, positions, *, firms=None, when="after", period=True):
    closes = es.session_close_times(prices.index)
    rows = []
    firms = firms or [c for c in prices.columns if c != "SPY"]
    for f in firms:
        for k in positions:
            if when == "after":
                kt = closes.iloc[k] + pd.Timedelta(minutes=21)   # day 0 = k + 1
            else:
                kt = closes.iloc[k] - pd.Timedelta(hours=8)      # pre-market, day 0 = k
            rows.append({"event_id": f"{f}-{k}", "firm": f, "knowledge_time": kt,
                         **({"period": f"{f}-{k}"} if period else {})})
    return pd.DataFrame(rows)


def _inject(prices, firm, pos, returns):
    """Multiply the firm's closes from ``pos`` on so the returns at pos.. gain ``returns``."""
    out = prices.copy()
    col = out[firm].to_numpy().copy()
    for j, extra in enumerate(returns):
        p = pos + j
        ratio = (col[p] / col[p - 1]) + extra
        col[p:] *= ratio / (col[p] / col[p - 1])
    out[firm] = col
    return out


def test_injected_announcement_effect_is_found_and_day0_is_after_close():
    raw = _market_panel(seed=1)
    positions = [400, 700, 1000, 1300]
    events = _events(raw, positions, when="after")
    prices = raw
    for f in [c for c in raw.columns if c != "SPY"]:
        for k in positions:
            prices = _inject(prices, f, k + 1, [0.03, 0.01])       # day 0 and day +1
    rep = es.run_event_study(prices, events, benchmark="SPY", windows=[(0, 1), (2, 20)])
    base = es.run_event_study(raw, events, benchmark="SPY", windows=[(0, 1), (2, 20)])
    ann = rep.aggregates.query("window == '[0,+1]' and group == 'ALL'").iloc[0]
    ann0 = base.aggregates.query("window == '[0,+1]' and group == 'ALL'").iloc[0]
    assert ann["n"] == len(events)
    assert ann["mean_car"] - ann0["mean_car"] == pytest.approx(0.04, abs=0.002)
    assert ann["p_bmp_kp"] < 1e-6 and ann["p_t_clustered"] < 1e-3 and ann["p_grank"] < 1e-3
    # The injection touched day 0 and day +1 only, so every drift CAR is unchanged.
    drift = rep.events[rep.events.window == "[+2,+20]"].set_index("event_id")["car"]
    drift0 = base.events[base.events.window == "[+2,+20]"].set_index("event_id")["car"]
    assert np.allclose(drift.sort_index(), drift0.sort_index(), atol=1e-12)
    day0 = rep.events.query("window == '[0,+1]'").set_index("event_id")["day0"]
    for f in ("F0", "F5"):
        assert day0[f"{f}-400"] == prices.index[401]          # after close -> next session


def test_statistics_match_independent_computations():
    prices = _market_panel(seed=2)
    events = _events(prices, [350, 650, 950, 1250], when="pre")
    rep = es.run_event_study(prices, events, benchmark="SPY", windows=[(0, 1)],
                             estimation_window=(-130, -11), exclusion_window=None,
                             min_estimation_days=120)
    row = rep.aggregates.iloc[0]
    cars = rep.events["car"].to_numpy()
    t_ref = stats.ttest_1samp(cars, 0.0)
    assert row["t"] == pytest.approx(t_ref.statistic)
    assert row["p_t"] == pytest.approx(t_ref.pvalue)

    # VT's own event study anchors on the same rows when the event is known before
    # the close, uses rows [-130, -11] for estimation_window=120 with gap 10, and
    # shares the CAR standard error, so CARs, Patell and BMP must coincide.
    returns = prices.pct_change(fill_method=None)
    pairs = [(e.firm, prices.index[int(e.event_id.split("-")[1])]) for e in events.itertuples()]
    ref = vt_event_study(returns.drop(columns="SPY"), returns["SPY"], pairs, event_window=(0, 1),
                         estimation_window=120, estimation_gap=10)
    ref_cars = np.array([o.car for o in ref.events])
    assert np.allclose(np.sort(ref_cars), np.sort(cars), atol=1e-12)
    assert row["patell_z"] == pytest.approx(ref.patell_z, rel=1e-9)
    assert row["bmp_t"] == pytest.approx(ref.bmp_z, rel=1e-9)


def test_other_event_windows_are_cut_out_of_the_estimation_sample():
    prices = _market_panel(seed=3, firms=1)
    events = _events(prices, [900, 1000], firms=["F0"], when="pre")
    shocked = _inject(prices, "F0", 900, [0.25, -0.20])      # a violent prior event
    with_cut = es.run_event_study(shocked, events, benchmark="SPY", windows=[(0, 1)],
                                  exclusion_window=(-1, 1))
    without = es.run_event_study(shocked, events, benchmark="SPY", windows=[(0, 1)],
                                 exclusion_window=None)
    a = with_cut.events.set_index("event_id").loc["F0-1000"]
    b = without.events.set_index("event_id").loc["F0-1000"]
    assert a["estimation_days"] == b["estimation_days"] - 3
    assert a["residual_std"] < 0.8 * b["residual_std"]


def test_skips_are_reported_per_event_and_per_window():
    prices = _market_panel(seed=4, firms=3)
    n = len(prices)
    prices.iloc[805, prices.columns.get_loc("F1")] = np.nan          # inside F1's drift window
    prices.iloc[300:520, prices.columns.get_loc("F2")] = np.nan      # F2's estimation window
    events = pd.DataFrame([
        {"event_id": "ok", "firm": "F0", "knowledge_time": _ny("2022-03-01 07:00")},
        {"event_id": "no_kt", "firm": "F0", "knowledge_time": None},
        {"event_id": "no_px", "firm": "ZZZ", "knowledge_time": _ny("2022-03-01 07:00")},
        {"event_id": "late", "firm": "F0", "knowledge_time": _ny("2030-01-01 07:00")},
        {"event_id": "early", "firm": "F0", "knowledge_time": _ny("2019-02-01 07:00")},
        {"event_id": "end", "firm": "F0", "knowledge_time": prices.index[n - 5].tz_localize(NY)},
        {"event_id": "hole", "firm": "F1",
         "knowledge_time": prices.index[800].tz_localize(NY) + pd.Timedelta(hours=9)},
        {"event_id": "thin", "firm": "F2",
         "knowledge_time": prices.index[560].tz_localize(NY) + pd.Timedelta(hours=9)},
    ])
    rep = es.run_event_study(prices, events, benchmark="SPY", windows=[(0, 1), (2, 20)])
    reasons = {(r.event_id, r.window): r.reason for r in rep.skips.itertuples()}
    assert reasons[("no_kt", "*")] == "missing knowledge time"
    assert reasons[("no_px", "*")] == "firm not in the price panel"
    assert "no session closes after" in reasons[("late", "*")]
    assert "insufficient" in reasons[("early", "*")]
    assert "runs past the last session" in reasons[("end", "[+2,+20]")]
    assert reasons[("hole", "[+2,+20]")] == "missing price in the window"
    assert "insufficient estimation data" in reasons[("thin", "*")]
    measured = set(zip(rep.events["event_id"], rep.events["window"]))
    assert {("ok", "[0,+1]"), ("ok", "[+2,+20]"), ("end", "[0,+1]"), ("hole", "[0,+1]")} <= measured
    assert ("hole", "[+2,+20]") not in measured


def test_duplicate_firm_quarter_disclosures_count_once():
    prices = _market_panel(seed=5, firms=2)
    closes = es.session_close_times(prices.index)
    events = pd.DataFrame([
        {"event_id": "pre", "firm": "F0", "period": "2022Q2", "knowledge_time": closes.iloc[900] - pd.Timedelta(hours=9)},
        {"event_id": "release", "firm": "F0", "period": "2022Q2", "knowledge_time": closes.iloc[910]},
        {"event_id": "amend", "firm": "F0", "period": "2022Q2", "knowledge_time": closes.iloc[911]},
        {"event_id": "other", "firm": "F1", "period": "2022Q2", "knowledge_time": closes.iloc[910]},
    ])
    rep = es.run_event_study(prices, events, benchmark="SPY", windows=[(0, 1)])
    assert sorted(rep.events["event_id"]) == ["other", "pre"]
    assert set(rep.duplicates["event_id"]) == {"release", "amend"}
    assert set(rep.duplicates["kept_event_id"]) == {"pre"}
    assert rep.params["duplicates_dropped"] == 2


def test_groups_signed_cars_and_fdr():
    prices = _market_panel(seed=6, firms=10)
    events = _events(prices, [400, 600, 800, 1000, 1200, 1400], when="after")
    rng = np.random.default_rng(0)
    events["sue"] = rng.normal(size=len(events))
    events["bucket"] = np.where(events["sue"] > 0, "up", "down")
    for e in events.itertuples():
        k = int(e.event_id.split("-")[1]) + 1
        prices = _inject(prices, e.firm, k, [0.02 * np.sign(e.sue)])
    rep = es.run_event_study(prices, events, benchmark="SPY", windows=[(0, 1), (2, 20)],
                             group_col="bucket", signed_col="sue", fdr=0.10, fdr_test="bmp_kp")
    agg = rep.aggregates.set_index(["window", "group"])
    assert set(agg.index.get_level_values("group")) == {"ALL", "up", "down", "SIGNED"}
    ann = rep.events[rep.events.window == "[0,+1]"]
    signed_mean = float((np.sign(ann["surprise_sign"]) * ann["car"]).mean())
    assert agg.loc[("[0,+1]", "SIGNED"), "mean_car"] == pytest.approx(signed_mean)
    assert agg.loc[("[0,+1]", "up"), "mean_car"] > 0.01 > -0.01 > agg.loc[("[0,+1]", "down"), "mean_car"]
    assert agg.loc[("[0,+1]", "SIGNED"), "fdr_reject"]
    bh = benjamini_hochberg(rep.aggregates["p_bmp_kp"].to_numpy(), fdr=0.10)
    assert np.allclose(rep.aggregates["p_fdr"].to_numpy(), bh.adjusted_p_values)
    assert list(rep.aggregates["fdr_reject"]) == list(bh.rejected)


def test_caar_path_averages_only_events_observed_on_every_session():
    prices = _market_panel(seed=7, firms=4)
    n = len(prices)
    events = _events(prices, [500, n - 30], when="pre")      # the late ones lack a full [+2,+60]
    rep = es.run_event_study(prices, events, benchmark="SPY", windows=[(0, 1), (2, 60)])
    path = rep.caar_path[rep.caar_path.group == "ALL"]
    assert set(path["n"]) == {4}                             # only the fully observed events
    assert path["rel_day"].min() == 0 and path["rel_day"].max() == 60
    full = rep.events[(rep.events.window == "[+2,+60]")]
    ann = rep.events[(rep.events.window == "[0,+1]") & rep.events.event_id.isin(full.event_id)]
    expected = ann["car"].mean() + full["car"].mean()
    assert path.iloc[-1]["caar"] == pytest.approx(expected)


def test_clustered_null_naive_tests_overreject_robust_tests_hold():
    sessions = es.us_equity_sessions("2019-01-01", "2021-12-31")
    closes = es.session_close_times(sessions)
    n = len(sessions)
    firms = [f"F{i}" for i in range(20)]
    naive = robust = kp = grank = 0
    sims = 40
    for sim in range(sims):
        rng = np.random.default_rng(500 + sim)
        market = rng.normal(0.0004, 0.01, n)
        sector = rng.normal(0, 0.012, n)                     # common shock the market misses
        data = {"SPY": 100 * np.cumprod(1 + market)}
        for f in firms:
            data[f] = 50 * np.cumprod(1 + market + sector + rng.normal(0, 0.012, n))
        prices = pd.DataFrame(data, index=sessions)
        days = rng.choice(np.arange(300, n - 30), size=6, replace=False)
        events = pd.DataFrame([{"event_id": f"{f}-{k}", "firm": f,
                                "knowledge_time": closes.iloc[k] - pd.Timedelta(hours=3)}
                               for f in firms for k in days])
        row = es.run_event_study(prices, events, benchmark="SPY", windows=[(0, 1)],
                                 cluster_freq="D", n_boot=200, period_col=None).aggregates.iloc[0]
        naive += row["p_t"] < 0.05
        robust += row["p_t_clustered"] < 0.05
        kp += row["p_bmp_kp"] < 0.05
        grank += row["p_grank"] < 0.05
    assert naive / sims > 0.35
    assert robust / sims <= 0.15 and kp / sims <= 0.15 and grank / sims <= 0.2


def test_input_validation():
    prices = _market_panel(seed=8, firms=1)
    events = _events(prices, [500], firms=["F0"])
    with pytest.raises(ValueError, match="overlaps the estimation window"):
        es.run_event_study(prices, events, benchmark="SPY", windows=[(-15, 1)])
    with pytest.raises(ValueError, match="start > end"):
        es.run_event_study(prices, events, benchmark="SPY", windows=[(3, 1)])
    with pytest.raises(ValueError, match="benchmark"):
        es.run_event_study(prices, events, benchmark="QQQ")
    with pytest.raises(ValueError, match="cluster_freq"):
        es.run_event_study(prices, events, benchmark="SPY", cluster_freq="Y")
    with pytest.raises(ValueError, match="fdr_test"):
        es.run_event_study(prices, events, benchmark="SPY", fdr_test="z")
    with pytest.raises(ValueError, match="min_estimation_days"):
        es.run_event_study(prices, events, benchmark="SPY", min_estimation_days=10)
    with pytest.raises(ValueError, match="unique"):
        es.run_event_study(prices, pd.concat([events, events]), benchmark="SPY")
    dup = pd.concat([prices, prices.iloc[[10]]])
    with pytest.raises(ValueError, match="duplicate session"):
        es.run_event_study(dup, events, benchmark="SPY")


# --------------------------------------------------------------------------- building blocks


def test_clustered_t_matches_the_cr1_formula():
    x = np.array([1.0, 2.0, 3.0, 4.0, 10.0, 12.0])
    g = ["a", "a", "b", "b", "c", "c"]
    t_value, p, groups = es.clustered_t_test(x, g)
    resid = x - x.mean()
    sums = np.array([resid[:2].sum(), resid[2:4].sum(), resid[4:].sum()])
    variance = 3 / 2 * np.sum(sums**2) / 36
    assert groups == 3
    assert t_value == pytest.approx(x.mean() / math.sqrt(variance))
    assert p == pytest.approx(2 * stats.t.sf(abs(t_value), 2))
    assert math.isnan(es.clustered_t_test([1.0, 2.0], ["a", "a"])[0])


def test_kolari_pynnonen_factor():
    assert es.kolari_pynnonen_factor(10, 0.0) == pytest.approx(1.0)
    assert es.kolari_pynnonen_factor(10, 0.1) == pytest.approx(math.sqrt(0.9 / 1.9))
    assert es.kolari_pynnonen_factor(10, 0.1, statistic="patell") == pytest.approx(math.sqrt(1 / 1.9))
    assert math.isnan(es.kolari_pynnonen_factor(10, -0.5))
    with pytest.raises(ValueError):
        es.kolari_pynnonen_factor(10, 0.1, statistic="corrado")


def test_generalized_sign_test():
    z, p = es.generalized_sign_test([0.1, 0.2, -0.1, 0.3, 0.05, 0.02, -0.2, 0.4], 0.5)
    assert z == pytest.approx((6 - 4) / math.sqrt(8 * 0.25))
    assert p == pytest.approx(2 * stats.norm.sf(abs(z)))
    assert math.isnan(es.generalized_sign_test([0.1, 0.2], 0.0)[0])


def test_grank_accepts_both_input_shapes_and_detects_an_effect():
    rng = np.random.default_rng(3)
    maps, pairs, scars = [], [], []
    for _ in range(40):
        keys = np.arange(-250, -10)
        values = rng.normal(size=keys.size)
        maps.append(dict(zip(keys.tolist(), values.tolist())))
        pairs.append((keys, values))
        scars.append(rng.normal(loc=1.5))
    a = es.grank_test(maps, scars)
    b = es.grank_test(pairs, scars)
    assert a == pytest.approx(b)
    assert a[2] == 241 and a[1] < 1e-4
    null = es.grank_test(pairs, rng.normal(size=40))
    assert null[1] > 0.001


def test_cluster_bootstrap_is_reproducible_and_needs_two_clusters():
    x = np.random.default_rng(1).normal(0.01, 0.05, 200)
    clusters = np.repeat(np.arange(20), 10)
    first = es.cluster_bootstrap_ci(x, clusters, n_boot=500, seed=7)
    assert first == es.cluster_bootstrap_ci(x, clusters, n_boot=500, seed=7)
    assert first[0] < x.mean() < first[1] and first[2] == 20
    assert math.isnan(es.cluster_bootstrap_ci(x, np.zeros(200), n_boot=500)[0])
    with pytest.raises(ValueError):
        es.cluster_bootstrap_ci(x, clusters, n_boot=10)


def test_beta_binomial_hit_rate():
    rate = es.beta_binomial_hit_rate(7, 10)
    assert rate.probability == pytest.approx(8 / 12)
    lo, hi = stats.beta.ppf([0.05, 0.95], 8, 4)
    assert (rate.lower, rate.upper) == (pytest.approx(lo), pytest.approx(hi))
    jeffreys = es.beta_binomial_hit_rate(0, 0, prior_alpha=0.5, prior_beta=0.5)
    assert jeffreys.probability == pytest.approx(0.5)
    with pytest.raises(ValueError):
        es.beta_binomial_hit_rate(11, 10)
    with pytest.raises(ValueError):
        es.beta_binomial_hit_rate(1, 10, prior_alpha=0)


def test_hit_rates_table_counts_cars_above_the_threshold():
    prices = _market_panel(seed=9, firms=6)
    events = _events(prices, [400, 700, 1000, 1300], when="pre")
    events["bucket"] = np.where(events.index % 2 == 0, "top", "bottom")
    rep = es.run_event_study(prices, events, benchmark="SPY", windows=[(2, 20)], group_col="bucket")
    table = es.hit_rates(rep, (2, 20)).set_index("group")
    drift = rep.events[rep.events.window == "[+2,+20]"]
    assert table.loc["ALL", "trials"] == len(drift)
    assert table.loc["ALL", "successes"] == int((drift["car"] > 0).sum())
    top = drift[drift["group"] == "top"]
    assert table.loc["top", "probability"] == pytest.approx(((top["car"] > 0).sum() + 1) / (len(top) + 2))
