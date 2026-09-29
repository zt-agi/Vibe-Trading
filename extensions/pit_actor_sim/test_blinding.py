"""ZT add-on tests: blind packets, the sealed mapping and the contamination probe.

Run from this folder with VIBE_TRADING_HOME set (E: on Windows):

    python -m unittest test_blinding

The leak checks here do not reuse blinding.py's patterns: each concept
(ticker, company name, year and date, sector and industry, absolute amounts
such as the market cap) is searched in several written forms by this file's
own scanner, over everything a role fork or a probe can read.
"""
from __future__ import annotations

import copy
import json
import math
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import blinding
import contamination
import fork_rules
import server
from test_packets import (PILOT_ASOF, PILOT_SERIES, PILOT_TEMPERAMENTS, SCENARIO_YAML, USO_ROW, PacketHarness,
                          make_fork, series_rows, setUpModule)  # noqa: F401  (setUpModule guards E:)

# --------------------------------------------------------------------------
# An issuer packet: every identifying fact appears in several forms.
# --------------------------------------------------------------------------

EQUITY_ASOF = "2025-08-28T12:00:00+00:00"
EQUITY_SCENARIO = """\
name: blind_equity_fixture
description: >
  Issuer guidance decision, then value-fund and long/short book responses.
headline_outcomes: [rerate_up]
rewards: {rerate_up: 1.0, flat: 0.5, derate_down: 0.0}
tree:
  actor: issuer_management
  actions:
    raise_guidance:
      child:
        actor: value_funds
        actions:
          accumulate: {outcome: rerate_up}
          hold: {outcome: flat}
          trim: {outcome: derate_down}
    hold_guidance: {outcome: flat}
"""
SEC = {  # series_id -> [(period end, value, knowledge time)]
    "SEC:NVDA:Revenues:USD:FY": [("2025-01-26", 130497e6, "2025-02-26T21:00:00"),
                                 ("2024-01-28", 60922e6, "2025-02-26T21:00:00"),
                                 ("2023-01-29", 26974e6, "2024-02-21T21:00:00")],
    "SEC:NVDA:NetIncomeLoss:USD:FY": [("2025-01-26", 72880e6, "2025-02-26T21:00:00"),
                                      ("2024-01-28", 29760e6, "2025-02-26T21:00:00"),
                                      ("2023-01-29", 4368e6, "2024-02-21T21:00:00")],
    "SEC:NVDA:EarningsPerShareDiluted:USD/shares:FY": [("2025-01-26", 2.94, "2025-02-26T21:00:00"),
                                                        ("2024-01-28", 1.19, "2025-02-26T21:00:00"),
                                                        ("2023-01-29", 0.17, "2024-02-21T21:00:00")],
}
UNITS = {"SEC:NVDA:EarningsPerShareDiluted:USD/shares:FY": "USD/shares"}
NVDA_BARS = [
    {"sec_id": 1, "primary_ticker": "NVDA", "event_date": "2025-08-26", "open": 178.35, "high": 182.62,
     "low": 177.44, "close": 181.77, "volume": 168688200.0, "currency": "USD",
     "knowledge_time": "2025-08-27T11:40:00", "revision_seq": 0, "source_id": "yahoo_eod",
     "pit_class": "OBSERVED_PIT"},
    {"sec_id": 1, "primary_ticker": "NVDA", "event_date": "2025-08-27", "open": 181.98, "high": 182.63,
     "low": 178.43, "close": 181.6, "volume": 235444900.0, "currency": "USD",
     "knowledge_time": "2025-08-28T11:40:00", "revision_seq": 0, "source_id": "yahoo_eod",
     "pit_class": "OBSERVED_PIT"},
]
MARKET_CAP = 3.215e12
IDENTITY_MEMO = (
    "NVIDIA Corporation (NASDAQ: NVDA, $NVDA) grew fiscal 2025 revenue to $130.5 billion from 60.9bn in "
    "FY2024 and 26,974,000,000 in FY23, on data center GPU and AI accelerator demand. Nvidia's market cap was "
    "about $3.215 trillion (3,215,000,000,000; 3.2T) at the August 27, 2025 close of 181.60 on 2025-08-27; "
    "diluted EPS 2.94 against 1.19. Semiconductor peers Advanced Micro Devices (AMD) and Broadcom (AVGO) "
    "trail; the stock sits in the Information Technology sector, Semiconductors industry. Q2 FY26 guide "
    "due 08/27/2025.")
UNIVERSE = [
    {"sec_id": 1, "ticker": "NVDA", "name": "NVIDIA Corporation", "sector": "Information Technology",
     "country": "US", "asset_class": "equity"},
    {"sec_id": 2, "ticker": "AMD", "name": "Advanced Micro Devices, Inc.", "sector": None, "country": None,
     "asset_class": "equity"},
    {"sec_id": 3, "ticker": "AVGO", "name": "Broadcom Inc.", "sector": None, "country": None,
     "asset_class": "equity"},
    {"sec_id": 36, "ticker": "SPY", "name": "State Street SPDR S&P 500 ETF Trust", "sector": None,
     "country": None, "asset_class": "etf"},
    {"sec_id": 42, "ticker": "USO", "name": "United States Oil Fund", "sector": None, "country": None,
     "asset_class": "equity"},
]
SEALED_AMOUNTS = sorted({MARKET_CAP} | {v for rows in SEC.values() for _d, v, _k in rows}
                        | {bar[k] for bar in NVDA_BARS for k in ("open", "high", "low", "close", "volume")})


def equity_series(series_id, asof, start_date, end_date, limit=250):
    rows = [{"series_id": series_id, "label": f"NVDA {series_id.split(':')[2]} (FY)",
             "unit": UNITS.get(series_id, "USD"), "event_time": f"{day}T00:00:00", "value_num": value,
             "value_str": None, "quality": "10-K", "knowledge_time": kt, "revision_seq": 0,
             "source_id": "sec_xbrl_facts", "pit_class": "TRUE_PIT"}
            for day, value, kt in sorted(SEC[series_id])]
    return {"asof": asof, "rows": rows, "row_count": len(rows)}


def issuer_facts(ticker, asof):
    rows = [{**row, "event_time": row["event_time"]} for sid in SEC if sid.startswith(f"SEC:{ticker}:")
            for row in equity_series(sid, asof, None, None)["rows"]]
    return {"asof": asof, "ticker": ticker, "rows": rows, "row_count": len(rows), "truncated": False}


def security_lookup(ticker, asof):
    info = {"NVDA": (1, "NVIDIA Corporation"), "USO": (42, "United States Oil Fund")}.get(ticker.upper())
    return {"securities": [] if info is None else [
        {"sec_id": info[0], "primary_ticker": ticker.upper(), "name": info[1], "currency": "USD"}]}


def price_history(sec_id, asof, start_date, end_date, limit=250):
    rows = [dict(USO_ROW)] if sec_id == 42 else [dict(bar) for bar in NVDA_BARS
                                                  if start_date <= bar["event_date"] <= end_date]
    return {"rows": rows, "row_count": len(rows)}


# --------------------------------------------------------------------------
# This file's own concept scanner.
# --------------------------------------------------------------------------


def strings_of(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_of(item)


#: Numeric fields that carry a value (day counts, row counts and revision
#: numbers are not amounts).
VALUE_KEYS = frozenset({"value", "open", "high", "low", "close", "volume"})


def numbers_of(value, key=None):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if key is None or key in VALUE_KEYS:
            yield float(value)
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from numbers_of(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from numbers_of(item, key)


MONTHS = ("january", "february", "march", "april", "june", "july", "august", "september", "october",
          "november", "december")


def concept_leaks(rendered, *, tickers, names, years, sectors, amounts) -> list[str]:
    """Every way this file knows a ticker, name, year, date, sector or absolute amount can be written."""
    found = []
    texts = list(strings_of(rendered))
    blob = "\n".join(texts)
    for ticker in tickers:
        for form in (ticker, ticker.lower(), f"${ticker}", f"{ticker}.US"):
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", blob):
                found.append(f"ticker {form}")
    for name in names:
        words = re.findall(r"[a-z0-9]+", name.casefold())
        if re.search(r"[\W_]+".join(words), blob, re.IGNORECASE):
            found.append(f"name {name}")
    for year in years:
        for form in (str(year), f"FY{year}", f"FY{str(year)[2:]}", f"'{str(year)[2:]}", f"FY {year}"):
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![0-9])", blob, re.IGNORECASE):
                found.append(f"year {form}")
        if any(n == year for n in numbers_of(rendered, "value")):
            found.append(f"year {year} as a number")
    if re.search(r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}", blob):
        found.append("ISO date")
    if re.search(r"(?<!\d)\d{1,2}/\d{1,2}/\d{2,4}(?!\d)", blob):
        found.append("numeric date")
    for month in MONTHS:
        if re.search(rf"(?<![a-z]){month}(?![a-z])", blob, re.IGNORECASE):
            found.append(f"month {month}")
    for sector in sectors:
        if re.search(rf"(?<![a-z]){re.escape(sector)}", blob, re.IGNORECASE):
            found.append(f"sector {sector}")
    printed = [(float(m.group(0).replace(",", "")), m.group(0)) for m in
               re.finditer(r"(?<![\w.])\d[\d,]*(?:\.\d+)?", blob)]
    values = [(n, repr(n)) for n in numbers_of(rendered)]
    for amount in amounts:
        for scale in (1, 1e3, 1e6, 1e9, 1e12):
            target = amount / scale
            if target < 1:
                continue
            for number, shown in printed + values:
                if number >= 1 and math.isclose(number, target, rel_tol=0.004):
                    found.append(f"amount {shown} ~ {amount:g}/{scale:g}")
    return found


class ScannerSelfTest(unittest.TestCase):
    """The scanner finds what it must find (so a clean result means something)."""

    def test_scanner_flags_every_concept_in_the_raw_memo(self):
        leaks = concept_leaks({"text": IDENTITY_MEMO}, tickers=["NVDA", "AMD", "AVGO"],
                              names=["NVIDIA", "Advanced Micro Devices", "Broadcom"], years=[2023, 2024, 2025],
                              sectors=["information technology", "semiconductor", "gpu", "data center"],
                              amounts=SEALED_AMOUNTS)
        for concept in ("ticker NVDA", "ticker $NVDA", "name NVIDIA", "year 2025", "year FY2024", "ISO date",
                        "numeric date", "month august", "sector information technology", "sector gpu"):
            self.assertIn(concept, leaks)
        self.assertTrue(any(item.startswith("amount 3.215") for item in leaks))
        self.assertTrue(any(item.startswith("amount 130.5") for item in leaks))


class BlindDecisionTest(unittest.TestCase):
    def test_auto_forced_and_invalid(self):
        now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
        old = datetime(2026, 8, 28, 12)
        recent = datetime(2026, 9, 25, 12)
        self.assertTrue(blinding.resolve_blind("auto", old, now)["blind"])
        self.assertFalse(blinding.resolve_blind("auto", recent, now)["blind"])
        self.assertTrue(blinding.resolve_blind("auto", recent, now, max_age_days=2)["blind"])
        self.assertTrue(blinding.resolve_blind(True, recent, now)["blind"])
        self.assertFalse(blinding.resolve_blind("false", old, now)["blind"])
        self.assertEqual(blinding.resolve_blind("auto", old, now)["mode"], "auto")
        for bad in ("maybe", "blind"):
            with self.assertRaises(ValueError):
                blinding.resolve_blind(bad, old, now)
        with self.assertRaises(ValueError):
            blinding.resolve_blind("auto", old, now, max_age_days=-1)


class BlindHarness(PacketHarness):
    """PacketHarness plus issuer fixtures and named securities."""

    def setUp(self):
        super().setUp()
        (self.sim / "config" / "scenario_blind_equity.yaml").write_text(EQUITY_SCENARIO, encoding="utf-8",
                                                                         newline="\n")
        self.equity_tree = fork_rules.parse_scenario(EQUITY_SCENARIO)

        def series(series_id, asof, start, end, limit=250):
            return equity_series(series_id, asof, start, end, limit) if series_id in SEC else \
                series_rows(series_id, asof, start, end, limit)

        for item in (patch.object(server, "security_universe", return_value=[dict(u) for u in UNIVERSE]),
                     patch.object(server, "pit_security", side_effect=security_lookup),
                     patch.object(server, "pit_price_history", side_effect=price_history),
                     patch.object(server, "pit_series_history", side_effect=series),
                     patch.object(server, "issuer_annual_facts", side_effect=issuer_facts)):
            item.start()
            self.addCleanup(item.stop)

    def freeze_equity(self, **overrides):
        arguments = dict(
            scenario_id="blind_equity", run_asof=EQUITY_ASOF, blind=True,
            series=[{"series_id": sid, "start_date": "2022-01-01", "end_date": "2025-08-28"} for sid in SEC],
            prices=[{"ticker": "NVDA", "start_date": "2025-08-20", "end_date": "2025-08-27"}],
            memos=[{"author": "fundamentals_evidence", "text": IDENTITY_MEMO, "citations": ["E1", "P2"]}],
            excluded_or_missing=[{"item": "SEC:NVDA:AssetsCurrent:USD:FY",
                                  "reason": "not ingested for NVIDIA before 2025-08-28 (FY2025 10-K)"}],
            interpretation_limits=["NVDA figures are as filed on 2025-02-26 for fiscal 2025."],
        )
        arguments.update(overrides)
        return server.freeze_evidence_packet(**arguments)

    def freeze_pilot_blind(self, **overrides):
        return self.freeze(blind=True, **overrides)


class BlindRenderTest(BlindHarness):
    def fork_visible(self, sha):
        return server.inspect_evidence_packet(sha, "role_fork")

    def test_issuer_render_leaks_no_ticker_name_year_sector_or_absolute_amount(self):
        sha = self.freeze_equity()["packet_sha256"]
        visible = self.fork_visible(sha)
        stored = json.loads((self.home / "actor_packets" / sha / "blind_view.json").read_text(encoding="utf-8"))
        for rendered in (visible, stored):
            leaks = concept_leaks(
                {k: v for k, v in rendered.items() if k != "packet_sha256"},
                tickers=["NVDA", "AMD", "AVGO"], names=["NVIDIA", "Advanced Micro Devices", "Broadcom"],
                years=[2022, 2023, 2024, 2025, 2026],
                sectors=["information technology", "semiconductor", "gpu", "data center", "ai accelerator"],
                amounts=SEALED_AMOUNTS)
            self.assertEqual(leaks, [])
        self.assertTrue(visible["blind"])
        self.assertNotIn("run_asof_utc", visible)
        self.assertIn("[AMOUNT]", visible["memos"][0]["text"])
        self.assertIn("[OTHER_SECURITY]", visible["memos"][0]["text"])
        self.assertIn("[EXCHANGE]: SECURITY_A", visible["memos"][0]["text"])
        self.assertTrue(visible["memos"][0]["text"].endswith("guide due [DATE]."))
        # evidence ids survive; values are rebased, never absolute
        self.assertEqual([o["evidence_id"] for o in visible["admitted_observations"]],
                         [f"E{i}" for i in range(1, 10)])
        revenue_t0 = next(o for o in visible["admitted_observations"]
                          if o["series"] == "S1" and o["period"] == "t-0")
        self.assertEqual(revenue_t0["value"], 100.0)
        income_t0 = next(o for o in visible["admitted_observations"]
                         if o["series"] == "S2" and o["period"] == "t-0")
        self.assertAlmostEqual(income_t0["value"], round(100 * 72880e6 / 130497e6, 2))  # ratio survives
        self.assertEqual({p["close"] for p in visible["admitted_prices"] if p["period"] == "t-0"}, {100.0})
        self.assertEqual({s["concept"] for s in visible["series"]},
                         {"Revenues", "NetIncomeLoss", "EarningsPerShareDiluted"})

    def test_render_hides_calendar_but_keeps_real_spacing(self):
        sha = self.freeze_equity()["packet_sha256"]
        visible = self.fork_visible(sha)
        periods = {(o["series"], o["period"]): o["years_before_t0"] for o in visible["admitted_observations"]}
        self.assertEqual(periods[("S1", "t-0")], 0.0)
        self.assertAlmostEqual(periods[("S1", "t-1")], 1.0, places=2)   # 364 days: a 52-week fiscal year
        self.assertAlmostEqual(periods[("S1", "t-2")], 1.99, places=2)  # 728 days

    def test_derived_style_scores_enter_blind_and_scale_free(self):
        sha = self.freeze_equity()["packet_sha256"]
        packet = server.load_packet(sha)
        style = {f"{r['family']}.{r['component']}": r for r in packet["derived_style_scores"]}
        self.assertEqual(style["defensive_value.pe_ratio"]["status"], "FAIL")
        self.assertAlmostEqual(style["defensive_value.pe_ratio"]["value"], 181.6 / 2.94)
        self.assertEqual(style["growth_at_reasonable_price.eps_growth"]["tier"], "FAST_GROWER")
        self.assertTrue(all(r["evidence_id"].startswith("Z") for r in packet["derived_style_scores"]))
        visible = self.fork_visible(sha)
        blind_rows = visible["derived_style_scores"]
        self.assertTrue(blind_rows and all(r["security"] == "SECURITY_A" for r in blind_rows))
        for row in blind_rows:
            self.assertNotIn("ticker", row)
            self.assertNotIn("inputs", row)
            self.assertNotIn("detail", row)
            self.assertNotIn("sealed_amounts", row)
        self.assertNotIn("graham", json.dumps(visible).lower())
        self.assertNotIn("lynch", json.dumps(visible).lower())

    def test_pilot_blind_render_scrubs_dates_and_the_fund_but_keeps_probabilities(self):
        sha = self.freeze_pilot_blind()["packet_sha256"]
        visible = self.fork_visible(sha)
        leaks = concept_leaks({k: v for k, v in visible.items() if k != "packet_sha256"},
                              tickers=["USO"], names=["United States Oil Fund"], years=[2025, 2026, 2027],
                              sectors=[], amounts=[USO_ROW[k] for k in ("open", "high", "low", "close", "volume")])
        self.assertEqual(leaks, [])
        self.assertEqual(sorted(o["value"] for o in visible["admitted_observations"]),
                         sorted(PILOT_SERIES.values()))
        descriptions = [s["description"] for s in visible["series"]]
        self.assertIn("POLY:us-x-iran-ceasefire-continues-through-[DATE]:Yes", descriptions)
        self.assertEqual(visible["decision_states"], self.tree.fork_view())
        self.assertEqual(visible["admitted_prices"][0]["close"], 100.0)

    def test_a_commodity_packet_keeps_other_names_and_its_scenario_words(self):
        labels = SCENARIO_YAML.replace("restraint:", "share_intel_in_september:")
        (self.sim / "config" / "scenario_iran_oil.yaml").write_text(labels, encoding="utf-8", newline="\n")
        memo = {"author": "geopolitical_evidence", "citations": ["E1"],
                "text": "US intel briefings cite Intel and NVDA supply chains; USO printed 130.01 at the close."}
        sha = self.freeze_pilot_blind(memos=[memo])["packet_sha256"]
        text = server.inspect_evidence_packet(sha, "role_fork")["memos"][0]["text"]
        self.assertEqual(text, "US intel briefings cite Intel and NVDA supply chains; SECURITY_A printed [AMOUNT] "
                               "at the close.")

    def test_scenario_naming_a_sealed_security_cannot_be_blinded(self):
        leaky = EQUITY_SCENARIO.replace("issuer_management", "nvda_management")
        (self.sim / "config" / "scenario_blind_equity.yaml").write_text(leaky, encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "blind rendering would leak.*ticker"):
            self.freeze_equity()
        self.assertFalse((self.home / "actor_packets").exists())
        dated = EQUITY_SCENARIO.replace("hold_guidance", "hold_guidance_2025")
        (self.sim / "config" / "scenario_blind_equity.yaml").write_text(dated, encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "year"):
            self.freeze_equity()
        self.assertTrue(self.freeze_equity(blind=False)["created"])

    def test_non_blind_and_auto_recent_packets_render_as_before(self):
        plain = self.freeze()
        self.assertEqual(plain["blind"], {"blind": False, "mode": "forced", "max_age_days": 7})
        view = self.fork_visible(plain["packet_sha256"])
        self.assertEqual(view["run_asof_utc"], "2026-08-28T12:00:00Z")
        self.assertIn(next(iter(PILOT_SERIES)), json.dumps(view))
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0).isoformat()
        self.assertFalse(self.freeze(run_asof=recent, blind="auto")["blind"]["blind"])
        self.assertTrue(self.freeze(blind="auto")["blind"]["blind"])  # the pilot as-of is weeks old


class SealedMappingTest(BlindHarness):
    def test_sealed_mapping_round_trips_every_alias_and_value(self):
        sha = self.freeze_equity()["packet_sha256"]
        packet = server.load_packet(sha)
        unsealed = server.unseal_evidence_packet(sha)
        mapping = unsealed["sealed_mapping"]
        view = server.inspect_evidence_packet(sha, "role_fork")
        self.assertEqual(unsealed["admitted_observations"], packet["admitted_observations"])
        self.assertEqual(mapping["truth"], {"tickers": ["NVDA"], "companies": ["NVIDIA Corporation"],
                                            "year": 2025})
        self.assertEqual({s["security"]: s["ticker"] for s in mapping["securities"]}, {"SECURITY_A": "NVDA"})
        alias_to_series = {s["series"]: s["series_id"] for s in mapping["series"]}
        bases = {}
        for note in mapping["rebase"]:
            for member in note.get("members") or [note["scope"]]:
                bases[member] = note["base"]
        raw = {o["evidence_id"]: o for o in packet["admitted_observations"]}
        for row in view["admitted_observations"]:
            original = raw[row["evidence_id"]]
            self.assertEqual(alias_to_series[row["series"]], original["series_id"])
            recovered = row["value"] * bases[row["series"]] / 100
            self.assertLessEqual(abs(recovered - original["value_num"]), bases[row["series"]] * 0.00005 + 1e-9)
        price_base = next(n["base"] for n in mapping["rebase"] if n["scope"] == "SECURITY_A price")
        raw_prices = {p["evidence_id"]: p for p in packet["admitted_prices"]}
        for row in view["admitted_prices"]:
            self.assertLessEqual(abs(row["close"] * price_base / 100 - raw_prices[row["evidence_id"]]["close"]),
                                 price_base * 0.00005 + 1e-9)
        self.assertEqual(json.loads((self.home / "actor_packets" / sha / "sealed_mapping.json")
                                    .read_text(encoding="utf-8")), mapping)

    def test_forks_cannot_unblind_and_tampering_is_detected(self):
        sha = self.freeze_equity()["packet_sha256"]
        with self.assertRaisesRegex(ValueError, "unseal_evidence_packet"):
            server.inspect_evidence_packet(sha, "full")
        path = self.home / "actor_packets" / sha / "blind_view.json"
        view = json.loads(path.read_text(encoding="utf-8"))
        view["memos"][0]["text"] = IDENTITY_MEMO
        path.write_text(json.dumps(view), encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(RuntimeError, "no longer matches its digest"):
            server.inspect_evidence_packet(sha, "role_fork")


class ProbeScoringTest(unittest.TestCase):
    TRUTH = {"tickers": ["NVDA"], "companies": ["NVIDIA Corporation"], "year": 2025}

    def score(self, **guess):
        return contamination.score_guess(contamination.validate_guess(guess), self.TRUTH)

    def test_exact_ticker_and_normalized_name(self):
        self.assertEqual(self.score(ticker="nvda")["verdict"], "IDENTIFIED")
        self.assertEqual(self.score(ticker="$NVDA.US")["verdict"], "IDENTIFIED")
        self.assertEqual(self.score(company="Nvidia")["verdict"], "IDENTIFIED")
        self.assertEqual(self.score(company="NVIDIA Corp.")["hits"]["company"], True)
        self.assertEqual(self.score(company="NVDA")["hits"]["ticker"], True)
        self.assertEqual(self.score(ticker="AMD", company="Advanced Micro Devices")["verdict"], "NOT_IDENTIFIED")
        self.assertEqual(self.score(company="Nvidia Graphics Holdings Foundation")["verdict"], "NOT_IDENTIFIED")

    def test_year_within_one(self):
        for year, verdict in ((2025, "IDENTIFIED"), ("FY2026", "IDENTIFIED"), ("2024", "IDENTIFIED"),
                              ("FY24", "IDENTIFIED"), (2023, "NOT_IDENTIFIED"), ("2027", "NOT_IDENTIFIED")):
            self.assertEqual(self.score(year=year)["verdict"], verdict, year)
        self.assertEqual(self.score()["verdict"], "NOT_IDENTIFIED")
        self.assertEqual(self.score()["fields_guessed"], [])

    def test_shotgun_guesses_are_refused(self):
        for guess in ({"year": "2023-2025"}, {"year": "2019 or 2025"}, {"ticker": "NVDA, AMD"},
                      {"company": "Nvidia or AMD"}, {"ticker": "NVDA", "sector": "chips"}, {"year": True}):
            with self.assertRaises(ValueError):
                contamination.validate_guess(guess)


class ContaminationFlowTest(BlindHarness):
    def fake_simulator(self, calls):
        def run(args, **kwargs):
            calls.append(args)
            out = Path(args[args.index("--out-dir") + 1])
            result = {"run_id": "r" * 24, "authority": "RESEARCH_PILOT_ONLY_UNCALIBRATED",
                      "validation": {"crosscheck_status": "PASS"}, "ensemble_mc": {"oil_range": 0.6},
                      "mc_wilson_95": {}, "model_form_range": {}, "expected_reward": {}, "limitations": []}
            (out / "pilot_result.json").write_text(json.dumps(result), encoding="utf-8", newline="\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return run

    def run_packet(self, sha):
        calls = []
        with patch.object(server.subprocess, "run", side_effect=self.fake_simulator(calls)):
            result = server.run_market_actor_sim(packet_sha256=sha, fork_order=PILOT_TEMPERAMENTS, rollouts=1000)
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        return result, manifest, calls[0]

    def submit_all(self, sha, mutate=None):
        for temperament in PILOT_TEMPERAMENTS:
            fork = make_fork(temperament, self.tree)
            if mutate and temperament == PILOT_TEMPERAMENTS[0]:
                mutate(fork)
            self.submit(sha, fork)

    def test_blind_run_feeds_the_simulator_the_blind_rendering_and_records_not_probed(self):
        sha = self.freeze_pilot_blind()["packet_sha256"]
        self.submit_all(sha)
        result, manifest, args = self.run_packet(sha)
        evidence = Path(args[args.index("--evidence") + 1])
        self.assertEqual(evidence, (self.home / "actor_packets" / sha / "blind_view.json").resolve())
        self.assertNotIn("USO", evidence.read_text(encoding="utf-8"))
        self.assertTrue(result["blind"])
        self.assertEqual(result["contamination"], {"status": "NOT_PROBED", "skill_eligible": True})
        self.assertTrue(manifest["blind"]["blind"])
        for key in ("blind_view_sha256", "sealed_mapping_sha256", "unblinded_packet_path"):
            self.assertIn(key, manifest["blind"])
        self.assertEqual(server.inspect_market_actor_run(result["run_key"])["run_manifest_summary"]["blind"], True)

    def test_probe_not_identified_then_identified_tags_the_run_contaminated(self):
        sha = self.freeze_pilot_blind()["packet_sha256"]
        miss = server.score_contamination_guess(sha, {"ticker": "XLE", "company": "Energy Select Sector SPDR",
                                                      "year": 2019}, "probe_small_model")
        self.assertEqual((miss["verdict"], miss["status"]), ("NOT_IDENTIFIED", "NOT_IDENTIFIED"))
        self.assertNotIn("USO", json.dumps(miss))
        self.submit_all(sha)
        _result, manifest, _args = self.run_packet(sha)
        self.assertEqual(manifest["contamination"]["status"], "NOT_IDENTIFIED")
        hit = server.score_contamination_guess(sha, {"year": "2026"}, "probe_large_model")
        self.assertEqual((hit["verdict"], hit["hits"]["year"], hit["status"]),
                         ("IDENTIFIED", True, "CONTAMINATED"))
        _result, manifest, _args = self.run_packet(sha)
        self.assertEqual(manifest["contamination"]["status"], "CONTAMINATED")
        self.assertFalse(manifest["contamination"]["skill_eligible"])

    def test_probe_is_write_once_and_only_for_blind_packets(self):
        sha = self.freeze_pilot_blind()["packet_sha256"]
        first = server.score_contamination_guess(sha, {"ticker": "USO"})
        self.assertEqual(first["verdict"], "IDENTIFIED")
        self.assertTrue(server.score_contamination_guess(sha, {"ticker": "USO"})["duplicate"])
        with self.assertRaisesRegex(ValueError, "one guess per probe"):
            server.score_contamination_guess(sha, {"ticker": "XOP"})
        plain = self.freeze()["packet_sha256"]
        with self.assertRaisesRegex(ValueError, "not blind"):
            server.score_contamination_guess(plain, {"ticker": "USO"})

    def test_a_fork_naming_the_sealed_identity_contaminates_the_run(self):
        sha = self.freeze_pilot_blind()["packet_sha256"]

        def name_it(fork):
            fork["memos"]["strike_iran/close_strait"]["analysis"] += " Funds already crowd into USO calls."
        self.submit_all(sha, mutate=name_it)
        _result, manifest, _args = self.run_packet(sha)
        self.assertEqual(manifest["contamination"]["status"], "CONTAMINATED")
        mentions = manifest["contamination"]["fork_identity_mentions"][PILOT_TEMPERAMENTS[0]]
        self.assertIn({"where": "strike_iran/close_strait/analysis", "concept": "ticker"}, mentions)

    def test_a_real_series_id_cited_in_a_blind_packet_resolves_but_counts_as_recognition(self):
        sha = self.freeze_pilot_blind()["packet_sha256"]
        fork = make_fork(PILOT_TEMPERAMENTS[0], self.tree)
        fork["memos"][""]["evidence"] = ["S1", "E4"]
        self.assertTrue(self.submit(sha, fork)["accepted"])
        other = make_fork(PILOT_TEMPERAMENTS[1], self.tree)
        other["memos"][""]["evidence"] = [next(iter(PILOT_SERIES))]
        self.assertTrue(self.submit(sha, other)["accepted"])
        state = server.contamination_state(sha, server.load_packet(sha))
        self.assertEqual(state["status"], "CONTAMINATED")
        self.assertEqual(sorted(state["fork_identity_mentions"]), [PILOT_TEMPERAMENTS[1]])

    def test_unblinded_runs_are_not_blind_and_legacy_runs_say_so(self):
        sha = self.freeze()["packet_sha256"]
        self.submit_all(sha)
        result, manifest, args = self.run_packet(sha)
        self.assertEqual(Path(args[args.index("--evidence") + 1]).name, "packet.json")
        self.assertEqual(result["contamination"]["status"], "NOT_BLIND")
        self.assertEqual(manifest["blind"]["blind"], False)


if __name__ == "__main__":
    unittest.main()
