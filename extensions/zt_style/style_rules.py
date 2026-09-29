"""LLM-free investor-style descriptors from point-in-time SEC XBRL facts.

ZT add-on (2026-09-29). The numeric checks behind ai-hedge-fund's investor
personas (MIT, commit 5d2c7ca2; the v1 scorers ben_graham.py, peter_lynch.py,
warren_buffett.py, charlie_munger.py, stanley_druckenmiller.py) recomputed as
plain rules over warehouse facts, with two corrections:

* every growth rate uses the real spacing of the period-end dates. The
  original snapshot CAGR assumed evenly spaced periods (``years =
  (len(values) - 1) / 4`` after dropping missing values), so a gap in the
  record inflated the rate; here a missing fiscal year widens the span;
* nothing is aggregated into a score, a signal or a confidence: each rule
  reports its value, a PASS / PASS_LENIENT / FAIL / NOT_MEANINGFUL / UNKNOWN
  status, the inputs with their knowledge times and what was missing. The
  style label is a descriptor of which rule families pass; it carries no
  advice and no forecast.

Inputs are the issuer's fiscal-year (``SEC:<TICKER>:<Concept>:<Unit>:FY``)
facts as ``obs_asof`` returned them at the as-of, plus the latest close
``price_asof`` knew. ``obs_asof`` does not expose a fact's period start, so a
three-month value filed at a fiscal-year end cannot be told from the annual
value; annual periods are therefore the fiscal-year-end dates of the latest
annual filing (within 10 days, for 52/53-week years), one per year, and a
revenue value far below its neighbours is flagged.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

SCHEMA = "zt.investor_style_scores.v1"
AUTHORITY = ("RESEARCH_ONLY_STYLE_DESCRIPTORS: mechanical rule outcomes over point-in-time filings; "
             "descriptive only, no forecast and no advice")
STATUSES = ("PASS", "PASS_LENIENT", "FAIL", "NOT_MEANINGFUL", "UNKNOWN")

#: line item -> XBRL concepts, most preferred first (merged per period).
LINE_ITEMS = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax"),
    "net_income": ("NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"),
    "operating_income": ("OperatingIncomeLoss",),
    "eps_diluted": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"),
    "shares": ("CommonStockSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding",
               "WeightedAverageNumberOfSharesOutstandingBasic"),
    "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "long_term_debt": ("LongTermDebt", "LongTermDebtNoncurrent"),
    "long_term_debt_current": ("LongTermDebtCurrent",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
}
#: Rule thresholds (from the personas' own numeric rules).
PE_STRICT, PE_LENIENT = 15.0, 20.0
CURRENT_RATIO_FLOOR = 1.5
FAST_GROWER, STALWART_LOW, STALWART_HIGH = 0.20, 0.10, 0.12
PEG_STRICT, PEG_LENIENT = 1.0, 2.0
ROE_BAR, ROE_SHARE_STRICT, ROE_SHARE_LENIENT = 0.15, 0.8, 0.6
BVPS_STRICT, BVPS_LENIENT = 0.15, 0.10
DE_STRICT, DE_LENIENT = 0.5, 1.0
GROWTH_ACCELERATION, MARGIN_ACCELERATION = 0.02, 0.01
MAX_SPAN_YEARS = 5.25
MIN_SPAN_YEARS = 0.9
FYE_TOLERANCE_DAYS = 10
STALE_AFTER_DAYS = 456  # about 15 months after the latest fiscal-year end
_ANNUAL_FORMS = ("10-K", "20-F", "40-F")
_FAMILIES = ("graham", "lynch", "quality", "cash_flow", "leverage", "inflection")

RULES = {
    "graham.pe_ratio": f"P/E on the latest fiscal-year diluted EPS: <= {PE_STRICT:g} PASS, <= {PE_LENIENT:g} "
                       "PASS_LENIENT, higher FAIL; non-positive EPS FAIL",
    "graham.current_ratio": f"current assets / current liabilities at the latest fiscal-year end >= "
                            f"{CURRENT_RATIO_FLOOR:g} PASS, else FAIL",
    "graham.earnings_positive_every_period": "net income positive in every fiscal year on record "
                                             "(at least two) PASS, else FAIL",
    "graham.debt_vs_net_current_assets": "long-term debt <= current assets - current liabilities at the latest "
                                         "fiscal-year end PASS, else FAIL",
    "lynch.eps_growth": f"annualized EPS growth over up to {MAX_SPAN_YEARS:g} years of real period spacing: "
                        f">= {FAST_GROWER:.0%} FAST_GROWER, {STALWART_LOW:.0%}-{STALWART_HIGH:.0%} STALWART, "
                        f"between them INTERMEDIATE_GROWER, 0 to {STALWART_LOW:.0%} SLOW_GROWER, below 0 "
                        "DECLINING, non-positive start and positive end TURNAROUND, non-positive end "
                        "after a non-positive start LOSS_MAKING; PASS for FAST, INTERMEDIATE and STALWART",
    "lynch.peg_ratio": f"P/E / (annualized EPS growth in percent): <= {PEG_STRICT:g} PASS, <= {PEG_LENIENT:g} "
                       "PASS_LENIENT, higher FAIL; needs positive growth",
    "quality.roe_consistency": f"share of fiscal years with return on average equity above {ROE_BAR:.0%} "
                               f"(at least three years): >= {ROE_SHARE_STRICT:g} PASS, >= "
                               f"{ROE_SHARE_LENIENT:g} PASS_LENIENT, else FAIL",
    "quality.bvps_cagr": f"book value per share CAGR over real period spacing (up to {MAX_SPAN_YEARS:g} "
                         f"years): > {BVPS_STRICT:.0%} PASS, > {BVPS_LENIENT:.0%} PASS_LENIENT, else FAIL; "
                         "a non-positive end NOT_MEANINGFUL",
    "cash_flow.operating_cash_flow_positive_every_period": "operating cash flow positive in every fiscal "
                                                           "year on record (at least two) PASS, else FAIL",
    "cash_flow.free_cash_flow_trend": "free cash flow (operating cash flow - capital expenditure) higher at "
                                      "the latest fiscal year than at the start of the span and positive "
                                      "PASS, else FAIL",
    "leverage.debt_to_equity": f"long-term debt / equity at the latest fiscal-year end: <= {DE_STRICT:g} PASS, "
                               f"<= {DE_LENIENT:g} PASS_LENIENT, higher FAIL; non-positive equity NOT_MEANINGFUL",
    "inflection.revenue_growth_acceleration": f"annualized revenue growth t-1 to t-0 minus t-2 to t-1 (real "
                                              f"spacing) >= {GROWTH_ACCELERATION:.0%} PASS, else FAIL",
    "inflection.operating_margin_acceleration": f"operating-margin change t-1 to t-0 minus change t-2 to t-1 "
                                                f">= {MARGIN_ACCELERATION:.0%} PASS, else FAIL",
}

LABEL_RULES = {
    "DEEP_VALUE_PROFILE": "graham pe_ratio PASS, current_ratio PASS, earnings_positive_every_period PASS "
                          "and debt_vs_net_current_assets PASS",
    "QUALITY_COMPOUNDER_PROFILE": "quality roe_consistency PASS and bvps_cagr PASS or PASS_LENIENT",
    "GARP_PROFILE": "lynch peg_ratio PASS and eps_growth tier FAST_GROWER, INTERMEDIATE_GROWER or STALWART",
    "FAST_GROWER_PROFILE": "lynch eps_growth tier FAST_GROWER",
    "INFLECTION_UP_PROFILE": "inflection revenue_growth_acceleration PASS and operating_margin_acceleration PASS",
    "INFLECTION_DOWN_PROFILE": "inflection revenue growth decelerating by at least 2 points and operating margin "
                               "change decelerating by at least 1 point",
}


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _moment(value) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def years_between(start: date, end: date) -> float:
    return (end - start).days / 365.25


def cagr(first: float, last: float, start: date, end: date) -> float | None:
    """Annualized growth between two dated values using their real spacing."""
    years = years_between(start, end)
    if years < MIN_SPAN_YEARS or not (_finite(first) and _finite(last)) or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1.0 / years) - 1.0


def _circular_gap(a: date, b: date) -> int:
    gap = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(gap, 366 - gap)


def _form_base(form) -> str:
    raw = str(form or "").upper().strip()
    return raw[:-2] if raw.endswith("/A") else raw


def parse_series(series_id) -> dict | None:
    parts = str(series_id or "").split(":")
    if len(parts) == 5 and parts[0] == "SEC":
        return {"ticker": parts[1], "concept": parts[2], "unit": parts[3], "fp": parts[4]}
    return None


class Facts:
    """Annual fiscal-year facts per line item, with provenance."""

    def __init__(self, rows: list[dict], asof: datetime):
        self.asof = asof
        self.warnings: list[str] = []
        parsed = []
        for row in rows:
            meta = parse_series(row.get("series_id"))
            if not meta or meta["fp"] != "FY" or not _finite(row.get("value_num")):
                continue
            if _moment(row["knowledge_time"]) > asof:
                raise ValueError(f"fact {row['series_id']} known after the as-of")
            parsed.append({**row, **meta, "period_end": _day(row["event_time"])})
        self.rows = parsed
        self.anchor = self._anchor()

    def _anchor(self) -> date | None:
        annual = [r for r in self.rows if _form_base(r.get("quality")) in _ANNUAL_FORMS]
        pool = annual or self.rows
        if not pool:
            return None
        if not annual:
            self.warnings.append("no fact carries an annual-report form; fiscal-year ends taken from the "
                                 "latest period end")
            return max(r["period_end"] for r in pool)
        latest_kt = max(_moment(r["knowledge_time"]) for r in annual)
        return max(r["period_end"] for r in annual if _moment(r["knowledge_time"]) == latest_kt)

    def annual(self, item: str) -> list[dict]:
        """One value per fiscal year for a line item, newest first (most preferred concept wins)."""
        if self.anchor is None:
            return []
        concepts = LINE_ITEMS[item]
        candidates = [r for r in self.rows if r["concept"] in concepts
                      and _circular_gap(r["period_end"], self.anchor) <= FYE_TOLERANCE_DAYS
                      and r["period_end"] <= self.anchor]
        if not candidates:
            return []
        units = {}
        for r in candidates:
            units.setdefault(r["unit"], []).append(r)
        unit = max(units, key=lambda u: (max(r["period_end"] for r in units[u]), len(units[u])))
        if len(units) > 1:
            self.warnings.append(f"{item}: facts in {sorted(units)}; used {unit}")
        by_year: dict[int, dict] = {}
        for r in units[unit]:
            k = round((self.anchor - r["period_end"]).days / 365.25)
            if k < 0:
                continue
            rank = (concepts.index(r["concept"]), abs((self.anchor - r["period_end"]).days - 365.25 * k))
            if k not in by_year or rank < by_year[k]["_rank"]:
                by_year[k] = {**r, "_rank": rank}
        return [by_year[k] for k in sorted(by_year)]


class Evaluation:
    def __init__(self, ticker: str, asof: datetime, facts: Facts, price: dict | None):
        self.ticker, self.asof, self.facts, self.price = ticker, asof, facts, price
        self.inputs: list[dict] = []
        self._input_ids: dict[tuple, str] = {}
        self.components: dict[str, dict] = {}
        self.missing: list[dict] = []

    def use(self, row: dict, item: str) -> str:
        key = (row["series_id"], str(row["period_end"]))
        if key not in self._input_ids:
            input_id = f"I{len(self.inputs) + 1}"
            self._input_ids[key] = input_id
            self.inputs.append({"input_id": input_id, "line_item": item, "series_id": row["series_id"],
                                "concept": row["concept"], "period_end": row["period_end"].isoformat(),
                                "value": row["value_num"], "unit": row["unit"],
                                "knowledge_time": _moment(row["knowledge_time"]).isoformat(),
                                "revision_seq": row.get("revision_seq"), "form": row.get("quality"),
                                "pit_class": row.get("pit_class")})
        return self._input_ids[key]

    def use_price(self) -> str:
        key = ("price", str(self.price["event_date"]))
        if key not in self._input_ids:
            input_id = f"I{len(self.inputs) + 1}"
            self._input_ids[key] = input_id
            self.inputs.append({"input_id": input_id, "line_item": "price", "sec_id": self.price.get("sec_id"),
                                "event_date": str(_day(self.price["event_date"])), "value": self.price["close"],
                                "unit": self.price.get("currency"),
                                "knowledge_time": _moment(self.price["knowledge_time"]).isoformat(),
                                "revision_seq": self.price.get("revision_seq"),
                                "source_id": self.price.get("source_id"), "pit_class": self.price.get("pit_class")})
        return self._input_ids[key]

    def put(self, key: str, *, value=None, unit="ratio", status="UNKNOWN", inputs=(), missing=(),
            periods_used=None, years_spanned=None, detail="", **extra) -> None:
        family, component = key.split(".", 1)
        entry = {"family": family, "component": component, "value": None if value is None else float(value),
                 "unit": unit, "status": status, "rule": RULES[key], "detail": detail,
                 "inputs": list(inputs), "missing": list(missing), "periods_used": periods_used,
                 "years_spanned": None if years_spanned is None else round(years_spanned, 2), **extra}
        if status == "UNKNOWN" and missing:
            self.missing.append({"component": key, "needs": list(missing)})
        self.components[key] = entry


def _latest(rows: list[dict]) -> dict | None:
    return rows[0] if rows else None


def _at(rows: list[dict], period_end: date) -> dict | None:
    return next((r for r in rows if r["period_end"] == period_end), None)


def _span(rows: list[dict]) -> list[dict]:
    """Newest-first annual rows within MAX_SPAN_YEARS of the latest."""
    if not rows:
        return []
    latest = rows[0]["period_end"]
    return [r for r in rows if years_between(r["period_end"], latest) <= MAX_SPAN_YEARS]


def _band(value: float, strict: float, lenient: float, higher_is_better: bool = False) -> str:
    if higher_is_better:
        return "PASS" if value > strict else "PASS_LENIENT" if value > lenient else "FAIL"
    return "PASS" if value <= strict else "PASS_LENIENT" if value <= lenient else "FAIL"


def _currency(unit) -> str:
    return str(unit or "").split("/")[0].upper()


def evaluate(ticker: str, asof: datetime, fact_rows: list[dict], price: dict | None,
             security: dict | None = None) -> dict:
    """Investor-style rule outcomes for one issuer at one as-of (naive UTC ``asof``)."""
    facts = Facts(fact_rows, asof)
    ev = Evaluation(ticker, asof, facts, price)
    items = {item: facts.annual(item) for item in LINE_ITEMS}
    _graham(ev, items)
    _lynch(ev, items)
    _quality(ev, items)
    _cash_flow(ev, items)
    _leverage(ev, items)
    _inflection(ev, items)
    warnings = list(dict.fromkeys(facts.warnings))
    warnings += _duration_warnings(items["revenue"])
    if facts.anchor and (asof.date() - facts.anchor).days > STALE_AFTER_DAYS:
        warnings.append(f"latest fiscal-year end {facts.anchor.isoformat()} is more than 15 months before the as-of")
    if any(i.get("pit_class") in (None, "NON_PIT") for i in ev.inputs):
        warnings.append("an input is NON_PIT or has no PIT class")
    labels = _labels(ev.components)
    known = sum(1 for c in ev.components.values() if c["status"] != "UNKNOWN")
    style_label = labels[0] if labels else ("NO_STYLE_MATCH" if known * 2 >= len(ev.components)
                                            else "INSUFFICIENT_DATA")
    knowledge = [i["knowledge_time"] for i in ev.inputs]
    return {
        "schema": SCHEMA,
        "authority": AUTHORITY,
        "ticker": ticker,
        "asof_utc": asof.isoformat(timespec="seconds") + "Z",
        "security": security,
        "fiscal_year_end_anchor": facts.anchor.isoformat() if facts.anchor else None,
        "annual_periods": {item: [r["period_end"].isoformat() for r in rows] for item, rows in items.items() if rows},
        "components": {family: {key.split(".", 1)[1]: comp for key, comp in ev.components.items()
                                if key.startswith(family + ".")} for family in _FAMILIES},
        "inputs": ev.inputs,
        "latest_knowledge_time": max(knowledge) if knowledge else None,
        "missing_data": ev.missing,
        "warnings": warnings,
        "style_label": style_label,
        "style_labels": labels,
        "label_rules": LABEL_RULES,
        "limitations": [
            "Descriptors of how the filed numbers sit against fixed investor-style rules: no forecast "
            "and no advice.",
            "Annual values come from fiscal-year facts known at the as-of; obs_asof carries no period start, "
            "so a three-month value filed at a fiscal-year end would be indistinguishable (see warnings).",
            "Growth rates use the real spacing between period ends; a missing fiscal year widens the span.",
        ],
    }


def _graham(ev: Evaluation, items: dict) -> None:
    eps = items["eps_diluted"]
    latest_eps = _latest(eps)
    basis_rows, basis = eps, "diluted EPS"
    if latest_eps is None and items["net_income"] and items["shares"]:
        ni, shares = _latest(items["net_income"]), _at(items["shares"], items["net_income"][0]["period_end"])
        if ni and shares and shares["value_num"] > 0:
            latest_eps = {**ni, "value_num": ni["value_num"] / shares["value_num"], "unit": ni["unit"] + "/shares"}
            basis_rows, basis = [ni, shares], "net income / shares"
    if ev.price is None or latest_eps is None:
        missing = ([] if latest_eps else list(LINE_ITEMS["eps_diluted"][:1]) + ["or NetIncomeLoss with shares"]) \
            + ([] if ev.price else ["price_asof close"])
        ev.put("graham.pe_ratio", missing=missing)
    elif _currency(latest_eps["unit"]) != _currency(ev.price.get("currency")):
        ev.put("graham.pe_ratio", missing=["EPS and price in the same currency"],
               detail=f"EPS in {_currency(latest_eps['unit'])}, price in {_currency(ev.price.get('currency'))}")
    else:
        inputs = [ev.use(r, "eps_diluted" if basis == "diluted EPS" else r["concept"]) for r in
                  (basis_rows[:1] if basis == "diluted EPS" else basis_rows)] + [ev.use_price()]
        e = latest_eps["value_num"]
        if e <= 0:
            ev.put("graham.pe_ratio", status="FAIL", inputs=inputs, detail=f"{basis} not positive")
        else:
            pe = ev.price["close"] / e
            ev.put("graham.pe_ratio", value=pe, status=_band(pe, PE_STRICT, PE_LENIENT), inputs=inputs,
                   detail=f"close {ev.price['close']:g} / {basis} {e:g} (fiscal year ending "
                          f"{latest_eps['period_end'].isoformat()})")
    ca, cl = items["current_assets"], items["current_liabilities"]
    both = [(a, _at(cl, a["period_end"])) for a in ca if _at(cl, a["period_end"])]
    if both and both[0][1]["value_num"] > 0:
        a, l = both[0]
        ratio = a["value_num"] / l["value_num"]
        ev.put("graham.current_ratio", value=ratio, status="PASS" if ratio >= CURRENT_RATIO_FLOOR else "FAIL",
               inputs=[ev.use(a, "current_assets"), ev.use(l, "current_liabilities")],
               detail=f"at the fiscal-year end {a['period_end'].isoformat()}")
    else:
        ev.put("graham.current_ratio", missing=[c for c, rows in (("AssetsCurrent", ca), ("LiabilitiesCurrent", cl))
                                                if not rows] or ["AssetsCurrent and LiabilitiesCurrent at one date"])
    ni = items["net_income"]
    if len(ni) >= 2:
        negatives = [r for r in ni if r["value_num"] <= 0]
        ev.put("graham.earnings_positive_every_period", value=len(ni) - len(negatives), unit="periods",
               status="FAIL" if negatives else "PASS", inputs=[ev.use(r, "net_income") for r in ni],
               periods_used=len(ni), years_spanned=years_between(ni[-1]["period_end"], ni[0]["period_end"]),
               detail=f"{len(ni) - len(negatives)} of {len(ni)} fiscal years positive")
    else:
        ev.put("graham.earnings_positive_every_period", missing=["NetIncomeLoss for at least two fiscal years"])
    debt = items["long_term_debt"]
    if both and debt:
        a, l = both[0]
        d = _at(debt, a["period_end"])
        if d is not None:
            ncav = a["value_num"] - l["value_num"]
            ev.put("graham.debt_vs_net_current_assets", value=d["value_num"] / ncav if ncav > 0 else None,
                   unit="ratio", status="PASS" if d["value_num"] <= ncav else "FAIL",
                   inputs=[ev.use(d, "long_term_debt"), ev.use(a, "current_assets"),
                           ev.use(l, "current_liabilities")],
                   detail="long-term debt against net current assets"
                          + ("; net current assets not positive" if ncav <= 0 else ""))
            return
    ev.put("graham.debt_vs_net_current_assets",
           missing=[c for c, rows in (("LongTermDebt or LongTermDebtNoncurrent", debt), ("AssetsCurrent", ca),
                                      ("LiabilitiesCurrent", cl)) if not rows] or ["facts at one fiscal-year end"])


def _growth(ev: Evaluation, items: dict) -> tuple[dict | None, list[dict], str]:
    """(growth result, rows used, basis) for EPS, falling back to net income."""
    for item, basis in (("eps_diluted", "diluted EPS"), ("net_income", "net income (EPS not in the warehouse)")):
        rows = _span(items[item])
        if len(rows) < 2:
            continue
        latest, oldest = rows[0], rows[-1]
        years = years_between(oldest["period_end"], latest["period_end"])
        if years < MIN_SPAN_YEARS:
            continue
        rate = cagr(oldest["value_num"], latest["value_num"], oldest["period_end"], latest["period_end"])
        if rate is None:
            tier = ("TURNAROUND" if oldest["value_num"] <= 0 < latest["value_num"] else
                    "DECLINING" if oldest["value_num"] > 0 else "LOSS_MAKING")
        elif rate >= FAST_GROWER:
            tier = "FAST_GROWER"
        elif rate > STALWART_HIGH:
            tier = "INTERMEDIATE_GROWER"
        elif rate >= STALWART_LOW:
            tier = "STALWART"
        elif rate >= 0:
            tier = "SLOW_GROWER"
        else:
            tier = "DECLINING"
        return {"rate": rate, "tier": tier, "years": years, "item": item}, rows, basis
    return None, [], ""


def _lynch(ev: Evaluation, items: dict) -> None:
    result, rows, basis = _growth(ev, items)
    if result is None:
        ev.put("lynch.eps_growth", unit="annual_rate",
               missing=["EarningsPerShareDiluted or NetIncomeLoss for two fiscal years at least 0.9 years apart"])
    else:
        ev.put("lynch.eps_growth", value=result["rate"], unit="annual_rate",
               status="PASS" if result["tier"] in ("FAST_GROWER", "INTERMEDIATE_GROWER", "STALWART")
               else "NOT_MEANINGFUL" if result["tier"] in ("TURNAROUND", "LOSS_MAKING") else "FAIL",
               inputs=[ev.use(r, result["item"]) for r in rows], periods_used=len(rows),
               years_spanned=result["years"], tier=result["tier"], basis=basis,
               detail=f"{basis} over {result['years']:.2f} years of real period spacing")
    pe = ev.components.get("graham.pe_ratio", {})
    if result is None or pe.get("value") is None:
        ev.put("lynch.peg_ratio", missing=["P/E"] if pe.get("value") is None else ["EPS growth"])
    elif result["rate"] is None or result["rate"] <= 0:
        ev.put("lynch.peg_ratio", status="NOT_MEANINGFUL", inputs=pe["inputs"],
               detail="growth not positive; PEG undefined")
    else:
        peg = pe["value"] / (result["rate"] * 100.0)
        ev.put("lynch.peg_ratio", value=peg, status=_band(peg, PEG_STRICT, PEG_LENIENT),
               inputs=sorted(set(pe["inputs"]) | set(ev.components["lynch.eps_growth"]["inputs"]),
                             key=lambda i: int(i[1:])),
               detail=f"P/E {pe['value']:.2f} over growth {result['rate'] * 100:.2f} percent")


def _quality(ev: Evaluation, items: dict) -> None:
    ni, equity = items["net_income"], items["equity"]
    roes, used = [], []
    for row in ni:
        end = _at(equity, row["period_end"])
        prior = next((e for e in equity if e["period_end"] < row["period_end"]
                      and 0.9 <= years_between(e["period_end"], row["period_end"]) <= 1.1), None)
        if end is None:
            continue
        base = (end["value_num"] + prior["value_num"]) / 2 if prior else end["value_num"]
        if base <= 0:
            continue
        roes.append(row["value_num"] / base)
        used += [row, end] + ([prior] if prior else [])
    if len(roes) >= 3:
        share = sum(1 for r in roes if r > ROE_BAR) / len(roes)
        ev.put("quality.roe_consistency", value=share, unit="share_of_years",
               status="PASS" if share >= ROE_SHARE_STRICT else "PASS_LENIENT" if share >= ROE_SHARE_LENIENT
               else "FAIL", inputs=[ev.use(r, r["concept"]) for r in used], periods_used=len(roes),
               detail=f"{sum(1 for r in roes if r > ROE_BAR)} of {len(roes)} fiscal years above {ROE_BAR:.0%}; "
                      f"mean {sum(roes) / len(roes):.3f}")
    else:
        ev.put("quality.roe_consistency", unit="share_of_years",
               missing=["StockholdersEquity and NetIncomeLoss for at least three fiscal years"])
    shares = items["shares"]
    bvps = []
    for row in _span(equity):
        count = _at(shares, row["period_end"])
        if count and count["value_num"] > 0:
            bvps.append((row["period_end"], row["value_num"] / count["value_num"], row, count))
    if len(bvps) >= 2 and years_between(bvps[-1][0], bvps[0][0]) >= MIN_SPAN_YEARS:
        (end_day, end_value, *_), (start_day, start_value, *_) = bvps[0], bvps[-1]
        years = years_between(start_day, end_day)
        inputs = [ev.use(r, r["concept"]) for _d, _v, e, c in bvps for r in (e, c)]
        if end_value <= 0 or start_value <= 0:
            ev.put("quality.bvps_cagr", unit="annual_rate", status="NOT_MEANINGFUL", inputs=inputs,
                   periods_used=len(bvps), years_spanned=years,
                   detail="book value per share not positive at one end of the span")
        else:
            rate = cagr(start_value, end_value, start_day, end_day)
            ev.put("quality.bvps_cagr", value=rate, unit="annual_rate",
                   status=_band(rate, BVPS_STRICT, BVPS_LENIENT, higher_is_better=True), inputs=inputs,
                   periods_used=len(bvps), years_spanned=years,
                   detail=f"from the fiscal year ending {start_day.isoformat()} to {end_day.isoformat()}")
    else:
        ev.put("quality.bvps_cagr", unit="annual_rate",
               missing=["StockholdersEquity and a share count at two fiscal-year ends at least 0.9 years apart"])


def _cash_flow(ev: Evaluation, items: dict) -> None:
    ocf, capex = items["operating_cash_flow"], items["capex"]
    if len(ocf) >= 2:
        negatives = [r for r in ocf if r["value_num"] <= 0]
        ev.put("cash_flow.operating_cash_flow_positive_every_period", value=len(ocf) - len(negatives),
               unit="periods", status="FAIL" if negatives else "PASS",
               inputs=[ev.use(r, "operating_cash_flow") for r in ocf], periods_used=len(ocf),
               years_spanned=years_between(ocf[-1]["period_end"], ocf[0]["period_end"]),
               detail=f"{len(ocf) - len(negatives)} of {len(ocf)} fiscal years positive")
    else:
        ev.put("cash_flow.operating_cash_flow_positive_every_period", unit="periods",
               missing=["NetCashProvidedByUsedInOperatingActivities for at least two fiscal years"])
    fcf = []
    for row in _span(ocf):
        spend = _at(capex, row["period_end"])
        if spend is not None:
            fcf.append((row["period_end"], row["value_num"] - abs(spend["value_num"]), row, spend))
    if len(fcf) >= 2 and years_between(fcf[-1][0], fcf[0][0]) >= MIN_SPAN_YEARS:
        (end_day, end_value, *_), (start_day, start_value, *_) = fcf[0], fcf[-1]
        rate = cagr(start_value, end_value, start_day, end_day)
        ev.put("cash_flow.free_cash_flow_trend", value=rate, unit="annual_rate",
               status="PASS" if end_value > start_value and end_value > 0 else "FAIL",
               inputs=[ev.use(r, r["concept"]) for _d, _v, o, c in fcf for r in (o, c)], periods_used=len(fcf),
               years_spanned=years_between(start_day, end_day),
               detail="annualized free-cash-flow growth" if rate is not None
               else "free cash flow not positive at one end; growth rate undefined")
    else:
        ev.put("cash_flow.free_cash_flow_trend", unit="annual_rate",
               missing=[c for c, rows in (("NetCashProvidedByUsedInOperatingActivities", ocf),
                                          ("PaymentsToAcquirePropertyPlantAndEquipment", capex)) if not rows]
               or ["both at two fiscal-year ends at least 0.9 years apart"])


def _leverage(ev: Evaluation, items: dict) -> None:
    debt, equity, current = items["long_term_debt"], items["equity"], items["long_term_debt_current"]
    for row in debt:
        eq = _at(equity, row["period_end"])
        if eq is None:
            continue
        cur = _at(current, row["period_end"]) if row["concept"] == "LongTermDebtNoncurrent" else None
        total = row["value_num"] + (cur["value_num"] if cur else 0.0)
        inputs = [ev.use(row, "long_term_debt"), ev.use(eq, "equity")] + ([ev.use(cur, "long_term_debt_current")]
                                                                          if cur else [])
        note = "noncurrent debt only (LongTermDebtCurrent not on record)" \
            if row["concept"] == "LongTermDebtNoncurrent" and cur is None else "long-term debt"
        if eq["value_num"] <= 0:
            ev.put("leverage.debt_to_equity", status="NOT_MEANINGFUL", inputs=inputs,
                   detail=f"equity not positive at {row['period_end'].isoformat()}")
        else:
            ratio = total / eq["value_num"]
            ev.put("leverage.debt_to_equity", value=ratio, status=_band(ratio, DE_STRICT, DE_LENIENT),
                   inputs=inputs, detail=f"{note} at the fiscal-year end {row['period_end'].isoformat()}")
        return
    ev.put("leverage.debt_to_equity",
           missing=[c for c, rows in (("LongTermDebt or LongTermDebtNoncurrent", debt), ("StockholdersEquity", equity))
                    if not rows] or ["debt and equity at one fiscal-year end"])


def _pair_growth(newer: dict, older: dict) -> float | None:
    years = years_between(older["period_end"], newer["period_end"])
    if years < MIN_SPAN_YEARS or older["value_num"] <= 0 or newer["value_num"] <= 0:
        return None
    return (newer["value_num"] / older["value_num"]) ** (1.0 / years) - 1.0


def _inflection(ev: Evaluation, items: dict) -> None:
    revenue = items["revenue"]
    if len(revenue) >= 3:
        r0, r1, r2 = revenue[:3]
        recent, older = _pair_growth(r0, r1), _pair_growth(r1, r2)
        inputs = [ev.use(r, "revenue") for r in (r0, r1, r2)]
        if recent is None or older is None:
            ev.put("inflection.revenue_growth_acceleration", unit="rate_difference", status="NOT_MEANINGFUL",
                   inputs=inputs, detail="revenue not positive in a period, or periods closer than 0.9 years")
        else:
            change = recent - older
            direction = ("ACCELERATING" if change >= GROWTH_ACCELERATION else
                         "DECELERATING" if change <= -GROWTH_ACCELERATION else "STEADY")
            ev.put("inflection.revenue_growth_acceleration", value=change, unit="rate_difference",
                   status="PASS" if change >= GROWTH_ACCELERATION else "FAIL", inputs=inputs, periods_used=3,
                   years_spanned=years_between(r2["period_end"], r0["period_end"]), direction=direction,
                   recent_growth=recent, older_growth=older,
                   detail=f"annualized growth {recent:.4f} (t-1 to t-0) against {older:.4f} (t-2 to t-1)")
    else:
        ev.put("inflection.revenue_growth_acceleration", unit="rate_difference",
               missing=["Revenues for three fiscal years"])
    margins = []
    for row in revenue[:3]:
        op = _at(items["operating_income"], row["period_end"])
        if op is not None and row["value_num"] > 0:
            margins.append((row, op, op["value_num"] / row["value_num"]))
    if len(margins) == 3 and margins[2][0]["period_end"] == revenue[2]["period_end"]:
        (m0r, m0o, m0), (m1r, m1o, m1), (m2r, m2o, m2) = margins
        change = (m0 - m1) - (m1 - m2)
        direction = ("EXPANDING" if m0 - m1 >= MARGIN_ACCELERATION else
                     "CONTRACTING" if m0 - m1 <= -MARGIN_ACCELERATION else "STEADY")
        ev.put("inflection.operating_margin_acceleration", value=change, unit="rate_difference",
               status="PASS" if change >= MARGIN_ACCELERATION else "FAIL",
               inputs=[ev.use(r, r["concept"]) for pair in margins for r in pair[:2]], periods_used=3,
               years_spanned=years_between(m2r["period_end"], m0r["period_end"]), direction=direction,
               recent_margin_change=m0 - m1, older_margin_change=m1 - m2,
               detail=f"operating margin {m2:.4f}, {m1:.4f}, {m0:.4f} (t-2, t-1, t-0)")
    else:
        ev.put("inflection.operating_margin_acceleration", unit="rate_difference",
               missing=["OperatingIncomeLoss and Revenues at three fiscal-year ends"])


def _duration_warnings(revenue: list[dict]) -> list[str]:
    """Annual revenue values that look like a three-month value filed at a fiscal-year end.

    A quarter stored as the year is about a quarter of its neighbours: flag an
    interior value under 45 percent of both neighbours, or a latest value under
    30 percent of the year before. The oldest value is never flagged on its own
    (fast growth looks the same).
    """
    warnings = []
    for k, row in enumerate(revenue):
        value = row["value_num"]
        if value <= 0:
            continue
        newer = revenue[k - 1]["value_num"] if k > 0 else None
        older = revenue[k + 1]["value_num"] if k + 1 < len(revenue) else None
        interior = newer is not None and older is not None and value < 0.45 * min(newer, older)
        collapse = k == 0 and older is not None and older > 0 and value < 0.30 * older
        if interior or collapse:
            warnings.append(f"annual revenue for the fiscal year ending {row['period_end'].isoformat()} is far "
                            "below its neighbouring years; check that a quarterly value was not filed at the "
                            "fiscal-year end")
    return warnings


def _labels(components: dict) -> list[str]:
    def status(key):
        return components.get(key, {}).get("status")

    tier = components.get("lynch.eps_growth", {}).get("tier")
    labels = []
    if all(status(f"graham.{k}") == "PASS" for k in ("pe_ratio", "current_ratio",
                                                    "earnings_positive_every_period",
                                                    "debt_vs_net_current_assets")):
        labels.append("DEEP_VALUE_PROFILE")
    if status("quality.roe_consistency") == "PASS" and status("quality.bvps_cagr") in ("PASS", "PASS_LENIENT"):
        labels.append("QUALITY_COMPOUNDER_PROFILE")
    if status("lynch.peg_ratio") == "PASS" and tier in ("FAST_GROWER", "INTERMEDIATE_GROWER", "STALWART"):
        labels.append("GARP_PROFILE")
    if tier == "FAST_GROWER":
        labels.append("FAST_GROWER_PROFILE")
    if status("inflection.revenue_growth_acceleration") == "PASS" \
            and status("inflection.operating_margin_acceleration") == "PASS":
        labels.append("INFLECTION_UP_PROFILE")
    growth = components.get("inflection.revenue_growth_acceleration", {}).get("value")
    margin = components.get("inflection.operating_margin_acceleration", {}).get("value")
    if growth is not None and margin is not None and growth <= -GROWTH_ACCELERATION \
            and margin <= -MARGIN_ACCELERATION:
        labels.append("INFLECTION_DOWN_PROFILE")
    return labels


#: Neutral family names used when rule outcomes enter a role fork's evidence
#: packet (templates and forks never see persona names).
PACKET_FAMILIES = {"graham": "defensive_value", "lynch": "growth_at_reasonable_price", "quality": "quality",
                   "cash_flow": "cash_flow", "leverage": "leverage", "inflection": "inflection"}
_PIT_ORDER = ("TRUE_PIT", "OBSERVED_PIT", "RECONSTRUCTED_PIT", "NON_PIT")


def score_issuer(ticker: str, asof: str, *, asof_utc, pit_security, pit_price_history,
                 issuer_annual_facts, price_window_days: int = 14) -> dict:
    """Resolve, read and evaluate one issuer through injected PIT readers.

    The readers are pit_actor_sim's sanctioned functions (``pit_security``,
    ``pit_price_history``, ``issuer_annual_facts``); nothing here opens the
    warehouse itself.
    """
    from datetime import timedelta

    at = asof_utc(asof)
    symbol = str(ticker or "").strip().upper()
    securities = pit_security(symbol, asof)["securities"]
    sec_ids = sorted({row["sec_id"] for row in securities})
    price, notes = None, []
    security = None
    if len(sec_ids) == 1:
        first = next(row for row in securities if row["sec_id"] == sec_ids[0])
        security = {"sec_id": sec_ids[0], "name": first.get("name"), "primary_ticker": first.get("primary_ticker"),
                    "currency": first.get("currency")}
        start = (at - timedelta(days=price_window_days)).date().isoformat()
        rows = pit_price_history(sec_ids[0], asof, start, at.date().isoformat(), 50)["rows"]
        closes = [row for row in rows if _finite(row.get("close"))]
        if closes:
            price = closes[-1]
        else:
            notes.append(f"no close known at the as-of in the last {price_window_days} days; no price-based rule")
    else:
        notes.append(f"ticker resolved to {len(sec_ids)} securities at the as-of; no price-based rule")
    facts = issuer_annual_facts(symbol, asof)
    result = evaluate(symbol, at, facts["rows"], price, security)
    if facts.get("truncated"):
        notes.append("issuer facts truncated at 5000 rows")
    if not facts["rows"]:
        notes.append(f"no SEC:{symbol}:*:FY fact known at the as-of")
    result["warnings"] = notes + result["warnings"]
    return result


def packet_rows(result: dict) -> list[dict]:
    """One derived-evidence row per rule outcome, for a frozen evidence packet.

    Family names are neutral; ``inputs`` keep the provenance and
    ``sealed_amounts`` the absolute inputs a blind rendering must hide.
    """
    inputs = {item["input_id"]: item for item in result.get("inputs") or []}
    rows = []
    for family, components in result["components"].items():
        for component, entry in components.items():
            used = [inputs[i] for i in entry.get("inputs") or [] if i in inputs]
            classes = [u.get("pit_class") for u in used]
            worst = max(classes, key=lambda c: _PIT_ORDER.index(c) if c in _PIT_ORDER else len(_PIT_ORDER)) \
                if classes else None
            row = {"ticker": result["ticker"], "family": PACKET_FAMILIES[family], "component": component,
                   "value": entry.get("value"), "unit": entry.get("unit"), "status": entry.get("status"),
                   "rule": entry.get("rule"), "detail": entry.get("detail"), "missing": entry.get("missing") or [],
                   "periods_used": entry.get("periods_used"), "years_spanned": entry.get("years_spanned"),
                   "pit_class": worst,
                   "knowledge_time": max((u["knowledge_time"] for u in used), default=None),
                   "inputs": [{k: u.get(k) for k in ("series_id", "line_item", "period_end", "event_date",
                                                     "knowledge_time", "pit_class")} for u in used],
                   "sealed_amounts": [abs(float(u["value"])) for u in used if _finite(u.get("value"))]}
            for key in ("tier", "direction", "basis"):
                if key in entry:
                    row[key] = entry[key]
            rows.append(row)
    return rows
