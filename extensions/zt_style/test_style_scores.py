"""Tests for the LLM-free investor-style factor pack (ZT add-on).

    cd extensions/zt_style && python -B -m unittest test_style_scores

With INVESTMENT_AI_PROJECT_ROOT set, SqlTest also runs pit_actor_sim's
ISSUER_FACTS_SQL against the project's own pitdb schema and macros in an
in-memory DuckDB.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402
import style_rules  # noqa: E402

ASOF = datetime(2025, 6, 30, 20, 0)
FILED = {2020: "2021-02-18T21:00:00", 2021: "2022-02-17T21:00:00", 2022: "2023-02-16T21:00:00",
         2023: "2024-02-22T21:00:00", 2024: "2025-02-20T21:00:00"}
FULL = {  # concept -> unit, {fiscal year: value}
    "Revenues": ("USD", {2020: 100e6, 2021: 110e6, 2022: 125e6, 2023: 150e6, 2024: 195e6}),
    "OperatingIncomeLoss": ("USD", {2020: 10e6, 2021: 11.5e6, 2022: 13.75e6, 2023: 18e6, 2024: 29.25e6}),
    "NetIncomeLoss": ("USD", {2020: 8e6, 2021: 9e6, 2022: 10e6, 2023: 12e6, 2024: 18e6}),
    "EarningsPerShareDiluted": ("USD/shares", {2020: 0.8, 2021: 0.9, 2022: 1.0, 2023: 1.2, 2024: 1.8}),
    "CommonStockSharesOutstanding": ("shares", {y: 10e6 for y in range(2020, 2025)}),
    "StockholdersEquity": ("USD", {2020: 50e6, 2021: 55e6, 2022: 60e6, 2023: 68e6, 2024: 80e6}),
    "AssetsCurrent": ("USD", {2023: 55e6, 2024: 60e6}),
    "LiabilitiesCurrent": ("USD", {2023: 30e6, 2024: 30e6}),
    "LongTermDebtNoncurrent": ("USD", {2023: 22e6, 2024: 20e6}),
    "NetCashProvidedByUsedInOperatingActivities": ("USD", {2020: 12e6, 2021: 13e6, 2022: 15e6, 2023: 18e6,
                                                           2024: 25e6}),
    "PaymentsToAcquirePropertyPlantAndEquipment": ("USD", {2020: 4e6, 2021: 4e6, 2022: 5e6, 2023: 6e6,
                                                           2024: 7e6}),
}
#: The concepts the warehouse's SecXbrlFacts connector ingests today.
LAKE_TAGS = ("Revenues", "NetIncomeLoss", "OperatingIncomeLoss", "LongTermDebtNoncurrent",
             "PaymentsToAcquirePropertyPlantAndEquipment")
PRICE = {"sec_id": 9, "primary_ticker": "ACME", "event_date": "2025-06-27", "close": 22.0, "currency": "USD",
         "knowledge_time": "2025-06-28T11:40:00", "revision_seq": 0, "source_id": "yahoo_eod",
         "pit_class": "OBSERVED_PIT"}


def fact(concept, unit, day, value, known, form="10-K", ticker="ACME"):
    return {"series_id": f"SEC:{ticker}:{concept}:{unit}:FY", "event_time": f"{day}T00:00:00", "value_num": value,
            "quality": form, "knowledge_time": known, "revision_seq": 0, "source_id": "sec_xbrl_facts",
            "unit": unit, "pit_class": "TRUE_PIT"}


def facts(table=FULL, only=None):
    rows = []
    for concept, (unit, values) in table.items():
        if only and concept not in only:
            continue
        for year, value in values.items():
            rows.append(fact(concept, unit, f"{year}-12-31", value, FILED[year]))
    return rows


def evaluate(rows, price=PRICE, asof=ASOF):
    return style_rules.evaluate("ACME", asof, rows, price, {"sec_id": 9, "name": "Acme Fixture Inc."})


def component(result, key):
    family, name = key.split(".")
    return result["components"][family][name]


class FullRecordTest(unittest.TestCase):
    def setUp(self):
        self.result = evaluate(facts())

    def test_every_rule_with_complete_filings(self):
        r = self.result
        pe = component(r, "graham.pe_ratio")
        self.assertAlmostEqual(pe["value"], 22.0 / 1.8)
        self.assertEqual(pe["status"], "PASS")
        self.assertEqual(component(r, "graham.current_ratio")["value"], 2.0)
        self.assertEqual(component(r, "graham.current_ratio")["status"], "PASS")
        self.assertEqual(component(r, "graham.earnings_positive_every_period")["status"], "PASS")
        self.assertEqual(component(r, "graham.debt_vs_net_current_assets")["status"], "PASS")
        growth = component(r, "lynch.eps_growth")
        self.assertAlmostEqual(growth["value"], (1.8 / 0.8) ** (1 / (1461 / 365.25)) - 1)
        self.assertEqual((growth["tier"], growth["status"], growth["basis"]), ("FAST_GROWER", "PASS", "diluted EPS"))
        peg = component(r, "lynch.peg_ratio")
        self.assertAlmostEqual(peg["value"], pe["value"] / (growth["value"] * 100))
        self.assertEqual(peg["status"], "PASS")
        roe = component(r, "quality.roe_consistency")
        self.assertEqual((roe["value"], roe["status"], roe["periods_used"]), (1.0, "PASS", 5))
        bvps = component(r, "quality.bvps_cagr")
        self.assertAlmostEqual(bvps["value"], (8 / 5) ** (1 / (1461 / 365.25)) - 1)
        self.assertEqual(bvps["status"], "PASS_LENIENT")
        self.assertEqual(component(r, "cash_flow.operating_cash_flow_positive_every_period")["status"], "PASS")
        self.assertEqual(component(r, "cash_flow.free_cash_flow_trend")["status"], "PASS")
        de = component(r, "leverage.debt_to_equity")
        self.assertEqual((de["value"], de["status"]), (0.25, "PASS"))
        self.assertIn("noncurrent debt only", de["detail"])
        acceleration = component(r, "inflection.revenue_growth_acceleration")
        self.assertEqual((acceleration["status"], acceleration["direction"]), ("PASS", "ACCELERATING"))
        margin = component(r, "inflection.operating_margin_acceleration")
        self.assertAlmostEqual(margin["value"], (0.15 - 0.12) - (0.12 - 0.11))
        self.assertEqual(margin["status"], "PASS")
        self.assertEqual(r["style_label"], "DEEP_VALUE_PROFILE")
        self.assertEqual(r["style_labels"], ["DEEP_VALUE_PROFILE", "QUALITY_COMPOUNDER_PROFILE", "GARP_PROFILE",
                                             "FAST_GROWER_PROFILE", "INFLECTION_UP_PROFILE"])
        self.assertEqual(r["missing_data"], [])

    def test_inputs_carry_knowledge_times_and_resolve_from_components(self):
        r = self.result
        ids = {item["input_id"]: item for item in r["inputs"]}
        for family in r["components"].values():
            for entry in family.values():
                for input_id in entry["inputs"]:
                    self.assertIn(input_id, ids)
        for item in r["inputs"]:
            self.assertLessEqual(datetime.fromisoformat(item["knowledge_time"]), ASOF)
        price = next(i for i in r["inputs"] if i["line_item"] == "price")
        self.assertEqual((price["event_date"], price["value"]), ("2025-06-27", 22.0))
        self.assertEqual(r["latest_knowledge_time"], "2025-06-28T11:40:00")

    def test_output_carries_no_probability_advice_or_trade_word(self):
        text = json.dumps({k: v for k, v in self.result.items() if k != "inputs"})
        text = re.sub(r"SEC:[^\"]+", "", text)
        forbidden = re.compile(r"(?<![A-Za-z])(?:probabilit\w*|likelihood|odds|chances?|confidence|bullish|"
                               r"bearish|buy|sell|long|short|overweight|underweight|recommend\w*|undervalued|"
                               r"overvalued|target price)(?![A-Za-z\-_])", re.IGNORECASE)
        self.assertEqual(forbidden.findall(text), [])


class RealPeriodSpacingTest(unittest.TestCase):
    def test_bvps_cagr_uses_real_dates_across_a_gap(self):
        # Book value per share 10 -> 12 -> 20 at fiscal years 2019, 2020 and 2023: two fiscal
        # years are missing. Evenly spaced periods would give 41.4% (two annual steps) or
        # 300% (the snapshot's quarter spacing); the real span is four years.
        filed = {2019: "2020-02-20T21:00:00", 2020: "2021-02-18T21:00:00", 2023: "2024-02-22T21:00:00"}
        rows = []
        for year, (equity, shares) in {2019: (100e6, 10e6), 2020: (120e6, 10e6), 2023: (200e6, 10e6)}.items():
            rows.append(fact("StockholdersEquity", "USD", f"{year}-12-31", equity, filed[year]))
            rows.append(fact("CommonStockSharesOutstanding", "shares", f"{year}-12-31", shares, filed[year]))
            rows.append(fact("EarningsPerShareDiluted", "USD/shares", f"{year}-12-31",
                             {2019: 1.0, 2020: 1.1, 2023: 1.5}[year], filed[year]))
        result = evaluate(rows, asof=datetime(2024, 6, 30))
        bvps = component(result, "quality.bvps_cagr")
        self.assertAlmostEqual(bvps["years_spanned"], 4.0, places=2)
        self.assertAlmostEqual(bvps["value"], 2 ** (1 / (1461 / 365.25)) - 1, places=6)
        self.assertLess(bvps["value"], 0.2)
        self.assertNotAlmostEqual(bvps["value"], 2 ** (1 / 2) - 1, places=2)
        self.assertEqual(bvps["periods_used"], 3)
        growth = component(result, "lynch.eps_growth")
        self.assertAlmostEqual(growth["value"], 1.5 ** (1 / (1461 / 365.25)) - 1, places=6)
        self.assertEqual(growth["tier"], "STALWART")  # 10.7% a year; two even steps would say 22.5%, FAST
        self.assertEqual(result["annual_periods"]["equity"], ["2023-12-31", "2020-12-31", "2019-12-31"])

    def test_52_53_week_years_and_quarter_rows_in_the_annual_series(self):
        rows = [fact("Revenues", "USD", day, value, known) for day, value, known in (
            ("2023-01-29", 27e9, "2024-02-21T21:00:00"), ("2024-01-28", 61e9, "2025-02-26T21:00:00"),
            ("2025-01-26", 130e9, "2025-02-26T21:00:00"),
            ("2024-10-27", 35e9, "2025-02-26T21:00:00"))]  # a quarter tagged in the annual filing
        facts_ = style_rules.Facts(rows, datetime(2025, 8, 28))
        self.assertEqual(facts_.anchor.isoformat(), "2025-01-26")
        self.assertEqual([r["period_end"].isoformat() for r in facts_.annual("revenue")],
                         ["2025-01-26", "2024-01-28", "2023-01-29"])


class MissingDataTest(unittest.TestCase):
    def test_todays_warehouse_tags_flag_what_is_missing(self):
        result = evaluate(facts(only=LAKE_TAGS))
        for key, needs in (("graham.pe_ratio", "EarningsPerShareDiluted"),
                           ("graham.current_ratio", "AssetsCurrent"),
                           ("quality.roe_consistency", "StockholdersEquity"),
                           ("quality.bvps_cagr", "StockholdersEquity"),
                           ("cash_flow.free_cash_flow_trend", "NetCashProvidedByUsedInOperatingActivities"),
                           ("leverage.debt_to_equity", "StockholdersEquity")):
            entry = component(result, key)
            self.assertEqual(entry["status"], "UNKNOWN", key)
            self.assertTrue(any(needs in item for item in entry["missing"]), (key, entry["missing"]))
        flagged = {item["component"] for item in result["missing_data"]}
        self.assertIn("graham.pe_ratio", flagged)
        growth = component(result, "lynch.eps_growth")
        self.assertEqual(growth["basis"], "net income (EPS not in the warehouse)")
        self.assertEqual(component(result, "graham.earnings_positive_every_period")["status"], "PASS")
        self.assertEqual(component(result, "inflection.revenue_growth_acceleration")["status"], "PASS")
        self.assertIn(result["style_label"], ("FAST_GROWER_PROFILE", "INFLECTION_UP_PROFILE"))

    def test_nothing_known_is_insufficient_data(self):
        result = evaluate([], price=None)
        self.assertEqual(result["style_label"], "INSUFFICIENT_DATA")
        self.assertTrue(all(c["status"] == "UNKNOWN" for f in result["components"].values() for c in f.values()))


class GuardTest(unittest.TestCase):
    def test_a_fact_known_after_the_asof_is_refused(self):
        rows = facts()
        rows[0]["knowledge_time"] = "2025-07-01T00:00:00"
        with self.assertRaisesRegex(ValueError, "known after the as-of"):
            evaluate(rows)

    def test_currency_mismatch_leaves_pe_unknown(self):
        table = dict(FULL)
        table["EarningsPerShareDiluted"] = ("TWD/shares", FULL["EarningsPerShareDiluted"][1])
        result = evaluate(facts(table))
        pe = component(result, "graham.pe_ratio")
        self.assertEqual(pe["status"], "UNKNOWN")
        self.assertIn("EPS in TWD, price in USD", pe["detail"])
        self.assertEqual(component(result, "lynch.peg_ratio")["status"], "UNKNOWN")

    def test_a_suspiciously_small_annual_revenue_is_flagged(self):
        table = dict(FULL)
        table["Revenues"] = ("USD", {**FULL["Revenues"][1], 2022: 30e6})
        result = evaluate(facts(table))
        self.assertTrue(any("2022-12-31" in w and "quarterly" in w for w in result["warnings"]))
        table["Revenues"] = ("USD", {**FULL["Revenues"][1], 2024: 40e6})  # a quarter as the latest year
        self.assertTrue(any("2024-12-31" in w for w in evaluate(facts(table))["warnings"]))

    def test_fast_growth_is_not_mistaken_for_a_quarterly_value(self):
        table = dict(FULL)
        table["Revenues"] = ("USD", {2022: 27e6, 2023: 61e6, 2024: 130e6})
        self.assertFalse(any("quarterly" in w for w in evaluate(facts(table))["warnings"]))

    def test_losses_and_turnarounds_are_not_growth_rates(self):
        table = dict(FULL)
        table["EarningsPerShareDiluted"] = ("USD/shares", {2020: -0.5, 2021: -0.1, 2022: 0.2, 2023: 0.4, 2024: 0.9})
        growth = component(evaluate(facts(table)), "lynch.eps_growth")
        self.assertEqual((growth["tier"], growth["status"], growth["value"]), ("TURNAROUND", "NOT_MEANINGFUL", None))


class PacketRowsTest(unittest.TestCase):
    def test_rows_use_neutral_family_names_and_keep_provenance(self):
        rows = style_rules.packet_rows(evaluate(facts()))
        self.assertEqual({r["family"] for r in rows}, {"defensive_value", "growth_at_reasonable_price", "quality",
                                                       "cash_flow", "leverage", "inflection"})
        pe = next(r for r in rows if r["component"] == "pe_ratio")
        self.assertEqual(pe["pit_class"], "OBSERVED_PIT")
        self.assertIn(22.0, pe["sealed_amounts"])
        self.assertIn(1.8, pe["sealed_amounts"])
        self.assertEqual(pe["knowledge_time"], "2025-06-28T11:40:00")
        self.assertNotIn("graham", json.dumps([{k: v for k, v in r.items() if k != "inputs"} for r in rows]).lower())


class ToolTest(unittest.TestCase):
    def fake_pit(self, rows=None, price=PRICE, securities=None):
        real = server.pit_sim()
        return SimpleNamespace(
            asof_utc=real.asof_utc,
            pit_security=lambda ticker, asof: {"securities": securities if securities is not None else [
                {"sec_id": 9, "primary_ticker": "ACME", "name": "Acme Fixture Inc.", "currency": "USD"}]},
            pit_price_history=lambda sec_id, asof, start, end, limit: {"rows": [price] if price else []},
            issuer_annual_facts=lambda ticker, asof: {"rows": facts() if rows is None else rows, "truncated": False})

    def test_tool_reads_through_the_sanctioned_readers(self):
        with patch.object(server, "pit_sim", return_value=self.fake_pit()):
            result = server.investor_style_scores("acme", "2025-06-30T20:00:00Z")
        self.assertEqual((result["schema"], result["ticker"], result["style_label"]),
                         (style_rules.SCHEMA, "ACME", "DEEP_VALUE_PROFILE"))
        self.assertEqual(result["security"]["name"], "Acme Fixture Inc.")
        self.assertIn("no advice", result["authority"])

    def test_tool_guards(self):
        with patch.object(server, "pit_sim", return_value=self.fake_pit()):
            with self.assertRaisesRegex(ValueError, "ticker is required"):
                server.investor_style_scores(" ", "2025-06-30T20:00:00Z")
            with self.assertRaisesRegex(ValueError, "timezone offset"):
                server.investor_style_scores("ACME", "2025-06-30T20:00:00")
        with patch.object(server, "pit_sim", return_value=self.fake_pit(securities=[], price=None)):
            result = server.investor_style_scores("ACME", "2025-06-30T20:00:00Z")
        self.assertTrue(any("resolved to 0 securities" in w for w in result["warnings"]))
        self.assertEqual(component(result, "graham.pe_ratio")["status"], "UNKNOWN")

    def test_the_facts_query_is_an_approved_pit_query(self):
        pit = server.pit_sim()
        worker = (Path(pit.__file__).with_name("query_worker.py")).read_text(encoding="utf-8")
        for sql in (pit.ISSUER_FACTS_SQL, pit.UNIVERSE_SQL):
            self.assertIn(hashlib.sha256(sql.encode("utf-8")).hexdigest(), worker)
        self.assertIn("obs_asof(?)", pit.ISSUER_FACTS_SQL)
        self.assertNotIn("fact_observation", pit.ISSUER_FACTS_SQL)


def _schema() -> Path | None:
    raw = os.environ.get("INVESTMENT_AI_PROJECT_ROOT", "").strip()
    path = Path(raw) / "implementation" / "pit_warehouse" / "pitdb" / "schema.sql" if raw else None
    return path if path and path.is_file() else None


@unittest.skipUnless(_schema(), "needs INVESTMENT_AI_PROJECT_ROOT with pitdb/schema.sql")
class SqlTest(unittest.TestCase):
    def test_issuer_facts_sql_cuts_at_the_asof_and_reads_only_fiscal_years(self):
        import duckdb
        con = duckdb.connect()
        con.execute(_schema().read_text(encoding="utf-8"))
        con.execute("INSERT INTO dim_source (source_id, pit_class) VALUES ('sec_xbrl_facts', 'TRUE_PIT')")
        for sid, unit in (("SEC:ACME:Revenues:USD:FY", "USD"), ("SEC:ACME:Revenues:USD:Q2", "USD"),
                          ("SEC:ACMEX:Revenues:USD:FY", "USD")):
            con.execute("INSERT INTO dim_series (series_id, source_id, unit, pit_class) VALUES (?, ?, ?, NULL)",
                        [sid, "sec_xbrl_facts", unit])
        for sid, event, known, value in (
                ("SEC:ACME:Revenues:USD:FY", "2023-12-31", "2024-02-22 21:00", 150e6),
                ("SEC:ACME:Revenues:USD:FY", "2023-12-31", "2025-02-20 21:00", 151e6),  # restated later
                ("SEC:ACME:Revenues:USD:FY", "2024-12-31", "2025-02-20 21:00", 195e6),
                ("SEC:ACME:Revenues:USD:Q2", "2024-06-30", "2024-07-30 21:00", 90e6),
                ("SEC:ACMEX:Revenues:USD:FY", "2024-12-31", "2025-02-20 21:00", 1e6)):
            con.execute("INSERT INTO fact_observation (series_id, event_time, knowledge_time, revision_seq, "
                        "value_num, quality, source_id) VALUES (?, ?, ?, 0, ?, '10-K', 'sec_xbrl_facts')",
                        [sid, event, known, value])
        sql = server.pit_sim().ISSUER_FACTS_SQL
        early = con.execute(sql, [datetime(2024, 6, 30), "SEC:ACME:"]).fetchall()
        late = con.execute(sql, [datetime(2025, 6, 30), "SEC:ACME:"]).fetchall()
        self.assertEqual([(r[0], r[2]) for r in early], [("SEC:ACME:Revenues:USD:FY", 150e6)])
        self.assertEqual([(r[1].date().isoformat(), r[2]) for r in late],
                         [("2023-12-31", 151e6), ("2024-12-31", 195e6)])
        self.assertEqual({r[8] for r in late}, {"TRUE_PIT"})  # the source's class when the series has none


if __name__ == "__main__":
    unittest.main()
