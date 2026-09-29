"""Blind renderings of frozen evidence packets and their sealed mapping.

ZT add-on (2026-09-29), adapted from ai-hedge-fund's blind snapshot
(``features/snapshot.py`` render(blind=True), MIT, commit 5d2c7ca2). That
render withholds the ticker, industry and calendar dates but still shows the
sector, the absolute market cap and per-share levels, and its personas carry
famous names -- each enough for a model to recall the company and so the
outcome it is scored on. Here a blind packet's fork-visible rendering keeps
the evidence ids and removes the rest:

* securities become ``SECURITY_A``..., series become ``S1``... (an XBRL
  series keeps only its generic concept tag and fiscal-period code; any other
  series keeps its label once dates, names and amounts are scrubbed out);
* calendar dates go: each series' rows are labelled ``t-0`` (latest known at
  the as-of) ... ``t-n``, with real spacing in years for fundamentals and bar
  counts for prices, never a date;
* currency sizes are rebased per security and currency so the security's
  reference size (its revenue if present, else its largest size) at t-0 is
  100 -- ratios between that security's sizes survive, absolute scale does
  not; per-share values, prices, volumes, counts and index levels are rebased
  to 100 at their own t-0 (deciles when t-0 is zero); probabilities and
  percentages stay as recorded;
* free text (memos, exclusions, labels, limits, source ids) is scrubbed of
  sealed tickers and names (and of the warehouse's other single-name
  securities), sealed series ids, dates, years, month names, amounts and --
  when the packet describes an issuer -- sector and industry words the
  scenario itself does not use;
* the rendering is then scanned with the same concept patterns and the freeze
  fails closed on any residue; a scenario whose own labels name a sealed
  security, a date or a year cannot be blinded.

The sealed mapping (aliases, identities, rebase bases, and the truth a
contamination probe is scored against) is written beside the packet and is
never returned to a role fork. Blinding reduces recall, it does not remove
it: distinctive ratios can still give a company away, which is what the
contamination probe measures.
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

BLIND_VIEW_SCHEMA = "vt.actor_packet.blind_view.v1"
SEALED_SCHEMA = "vt.actor_packet.sealed_mapping.v1"
DEFAULT_MAX_AGE_DAYS = 7
BLIND_MODES = ("auto", "true", "false")

#: Placeholders a scrubbed text may contain.
DATE, YEAR, MONTH, AMOUNT, SECTOR, OTHER, EXCHANGE = (
    "[DATE]", "[YEAR]", "[MONTH]", "[AMOUNT]", "[SECTOR]", "[OTHER_SECURITY]", "[EXCHANGE]")
PLACEHOLDERS = frozenset((DATE, YEAR, MONTH, AMOUNT, SECTOR, OTHER, EXCHANGE))
CONCEPTS = ("series", "date", "company", "ticker", "other_security", "sector", "exchange", "year", "amount",
            "month")
#: What a scenario's own labels are checked for: the packet's own securities,
#: dates and years. Its sector words are the scenario's subject, its numbers
#: are structure rather than sealed amounts, and other warehouse names or a
#: bare month name in a label do not identify the packet.
SCENARIO_CONCEPTS = ("series", "date", "company", "ticker", "year")

TIME_REFERENCE = (
    "Blind packet: securities, series identities, calendar dates, years, sectors and absolute "
    "amounts are withheld. Periods are labelled per series relative to the latest value known at "
    "the packet's present (t-0, t-1, ...). Treat t-0 as the present.")
BLIND_RULES = (
    "Currency sizes are rebased per security and currency: the security's reference size at t-0 "
    "is 100, so ratios between that security's sizes hold while absolute scale is hidden.",
    "Per-share values, prices, volumes, counts and index levels are rebased to 100 at their own "
    "t-0 (deciles 1-10 when t-0 is zero); probabilities and percentages are shown as recorded.",
    "years_before_t0 is the real spacing of a period from its series' t-0; use it for growth "
    "rates instead of assuming evenly spaced periods.",
    "Cite evidence ids (E, P, Z, M, X) or series aliases (S); do not try to name the security, the "
    "company, its sector or the calendar period, and write no date or year.",
)
#: Keys whose string values come from fixed vocabularies generated here.
_FIXED_KEYS = frozenset({
    "schema", "time_reference", "blind_rules", "transform", "price_transform", "volume_transform",
    "period", "unit_class", "filing", "pit_class", "security", "series", "evidence_id", "memo_id",
    "exclusion_id", "packet_sha256", "authority", "fiscal_period", "status", "unit", "family",
    "component", "source", "rule", "missing", "tier", "direction", "basis"})

_MONTHS_FULL = ("january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december")
_MONTHS_ABBR = ("jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec")
_MONTH = "(?:" + "|".join(sorted(_MONTHS_FULL + _MONTHS_ABBR, key=len, reverse=True)) + r")\.?"
_DAY = r"(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?"
_YEAR4 = r"(?:19|20)\d{2}"
_L, _R = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"

_DATE_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    # ISO dates and datetimes, compact dates, numeric day/month/year forms
    rf"(?<!\d){_YEAR4}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])"
    r"(?:[T ][0-2]\d:[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\d)",
    rf"(?<!\d){_YEAR4}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)",
    r"(?<![\d/.])(?:0?[1-9]|[12]\d|3[01])[/.](?:0?[1-9]|[12]\d|3[01])[/.](?:\d{4}|\d{2})(?![\d/]|\.\d)",
    # month-name dates, also inside slugs: september-30, december-31-2026, 30_sep_2026
    rf"{_L}{_MONTH}[\s\-_,]*{_DAY}(?:[\s\-_,]+{_YEAR4})?{_R}",
    rf"{_L}{_DAY}[\s\-_]+{_MONTH}(?:[\s\-_,]+{_YEAR4})?{_R}",
    rf"{_L}{_MONTH}[\s\-_,]+{_YEAR4}{_R}",
))
_YEAR_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"(?<![A-Za-z0-9])(?:FY|CY)\s?'?(?:\d{4}|\d{2})(?!\d)",            # FY2025, FY 25, CY'24
    r"(?<![A-Za-z0-9])[QH][1-4]\s?(?:FY)?'?(?:(?:19|20)\d{2}|\d{2})(?![\d])",  # Q3 2025, Q3'25, H1FY26
    r"(?<![A-Za-z0-9])[1-4][QH]\s?'?(?:(?:19|20)\d{2}|\d{2})(?!\d)",    # 3Q25, 1H 2026
    rf"(?<!\d){_YEAR4}\s?[-/]?\s?[QH][1-4](?![A-Za-z0-9])",             # 2025Q3, 2025-H1
    r"(?<![A-Za-z0-9'])'\d{2}(?![\d'])",                                 # '25
    rf"(?<![\d.,]){_YEAR4}(?:\d{{1,4}})?(?!\d)(?!\.\d)",                 # 2026, slug runs 2026062
))
_MONTH_WORD = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(m for m in _MONTHS_FULL if m != "may") + r")(?![A-Za-z])",
    re.IGNORECASE)
_CURRENCY_MARK = (r"(?:US\$|NT\$|HK\$|A\$|C\$|S\$|R\$|\$|€|£|¥|₩|₹|"
                  r"(?<![A-Za-z])(?:USD|EUR|JPY|KRW|TWD|CNY|RMB|GBP|HKD|CHF|CAD|AUD|INR|SGD)\s?)")
_MAGNITUDE = r"(?:trillion|billion|million|thousand|tn|bn|mn|mm|[kmbt])"
_AMOUNT_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    rf"{_CURRENCY_MARK}\s?\d[\d,]*(?:\.\d+)?(?:\s?{_MAGNITUDE}(?![A-Za-z]))?",
    rf"(?<![\w.])\d[\d,]*(?:\.\d+)?\s?{_MAGNITUDE}(?![A-Za-z])",
    r"(?<![\w.,])\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?(?![\d,])",           # 3,043,200
    r"(?<![\w.,])\d{6,}(?:\.\d+)?(?!\d)",                              # 130497000000
))
_NUMBER = re.compile(r"(?<![\w.,])(\d[\d,]*(?:\.\d+)?)(?![\d.,]*\d)(?!\s?%)")

#: Words that do not identify a company on their own (kept out of the
#: distinctive-token set built from security names).
_GENERIC_NAME_WORDS = frozenset("""
advanced micro devices applied materials research technology technologies semiconductor semiconductors
manufacturing systems platforms energy power services holdings holding international global united
states american national first general capital financial group super computer index composite fund
funds trust shares share gold silver dollar bond bonds treasury yield high corporate south north korea
taiwan japan china weighted capitalization stock daily bull bear long short ultra ultrapro volatility
exchange street state partners company corporation limited incorporated products solutions industries
enterprises networks network communications electric electronics digital data cloud software
resources pharmaceuticals therapeutics biosciences medical health healthcare bank bancorp insurance
""".split())
_LEGAL_SUFFIX = frozenset("""inc incorporated corp corporation co company ltd limited plc lp llp llc nv
sa ag se holdings holding group class""".split())
_FUND_WORDS = re.compile(r"(?<![A-Za-z])(?:etf|etn|fund|trust|index|ishares|proshares|spdr|composite|"
                         r"nikkei|kospi|nasdaq|s&p|direxion|graniteshares|invesco|vaneck)(?![A-Za-z])",
                         re.IGNORECASE)

#: Sector and industry vocabulary (GICS sectors and industries plus common
#: synonyms). Scrubbed only from packets that describe an issuer, and never
#: where the scenario's own labels use the word.
SECTOR_LEXICON = (
    "energy", "materials", "industrials", "consumer discretionary", "consumer staples", "health care",
    "healthcare", "financials", "information technology", "communication services", "utilities",
    "real estate", "semiconductor", "semiconductors", "semiconductor equipment", "chipmaker",
    "chipmakers", "chip maker", "chip makers", "chip", "chips", "foundry", "foundries", "memory chip",
    "memory chips", "dram", "nand", "hbm", "gpu", "gpus", "ai accelerator", "ai accelerators",
    "data center", "data centers", "datacenter", "datacenters", "software", "saas", "cloud",
    "hyperscaler", "hyperscalers", "it services", "internet", "e-commerce", "ecommerce",
    "search engine", "social media", "streaming", "technology hardware", "hardware", "servers",
    "networking", "smartphone", "smartphones", "telecom", "telecommunications", "media",
    "entertainment", "advertising", "bank", "banks", "banking", "insurance", "insurer", "insurers",
    "reinsurance", "asset management", "brokerage", "capital markets", "reit", "reits",
    "biotechnology", "biotech", "pharmaceutical", "pharmaceuticals", "pharma", "medical devices",
    "medtech", "automobile", "automobiles", "automaker", "automakers", "auto parts",
    "electric vehicle", "electric vehicles", "aerospace", "defense contractor", "airline",
    "airlines", "railroad", "railroads", "trucking", "shipping", "logistics", "oil and gas",
    "oil & gas", "exploration and production", "refining", "refiner", "refiners",
    "oilfield services", "midstream", "pipeline", "pipelines", "coal", "mining", "miner", "miners",
    "metals", "steel", "aluminum", "copper", "chemicals", "fertilizer", "construction",
    "homebuilder", "homebuilders", "building products", "machinery", "electrical equipment",
    "industrial conglomerate", "power producer", "independent power", "electric utility",
    "renewable", "renewables", "solar", "wind power", "nuclear", "retail", "retailer", "retailers",
    "restaurants", "hotels", "casinos", "gaming", "apparel", "beverages", "tobacco",
    "food products", "household products",
)

#: Listing venues: sealed with the sector words in packets that describe an issuer.
EXCHANGES = ("nasdaq", "nyse", "nyse american", "amex", "otc markets", "lse", "tsx", "tsxv", "hkex",
             "tse", "twse", "tpex", "krx", "kosdaq", "sse", "szse", "euronext", "xetra", "six swiss exchange",
             "asx", "nse", "bse")

_KEEP_UNITS = {"prob": "probability", "probability": "probability", "implied_probability": "probability",
               "pct": "percent", "percent": "percent", "%": "percent", "bps": "percent", "pp": "percent",
               "ratio": "ratio", "pure": "ratio"}
_CURRENCY_CODES = frozenset("usd eur jpy krw twd cny rmb gbp hkd chf cad aud inr sgd".split())
REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax")
_ANNUAL_FORMS = ("10-K", "20-F", "40-F")
_INTERIM_FORMS = ("10-Q", "6-K")
_SAFE_QUALITIES = frozenset("final prelim preliminary estimate flash nowcast revised advance second third".split())


class BlindLeak(ValueError):
    """The blind rendering would still carry sealed identity, time or scale."""

    def __init__(self, leaks: list[dict]):
        self.leaks = leaks
        shown = "; ".join(f"{leak['path']}: {leak['concept']} {leak['match']!r}" for leak in leaks[:12])
        super().__init__(f"blind rendering would leak {len(leaks)} item(s): {shown}")


def resolve_blind(blind, asof_naive_utc: datetime, now: datetime | None = None,
                  max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> dict:
    """Decide blind mode; ``auto`` blinds a run whose as-of is older than ``max_age_days``."""
    raw = "true" if blind is True else "false" if blind is False else str(blind or "false").strip().lower()
    raw = {"yes": "true", "1": "true", "on": "true", "no": "false", "0": "false", "off": "false"}.get(raw, raw)
    if raw not in BLIND_MODES:
        raise ValueError("blind must be true, false or auto")
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) or not 0 <= max_age_days <= 3650:
        raise ValueError("blind_max_age_days must be an integer 0..3650")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    age_days = (now - asof_naive_utc).total_seconds() / 86400.0
    blind_now = age_days > max_age_days if raw == "auto" else raw == "true"
    return {"blind": blind_now, "mode": "auto" if raw == "auto" else "forced",
            "max_age_days": max_age_days, "asof_age_days": round(age_days, 3)}


# --------------------------------------------------------------------------
# Names and the scrubber
# --------------------------------------------------------------------------


def name_tokens(name) -> list[str]:
    """Lower-case alphanumeric tokens of a security name without legal suffixes."""
    tokens = re.findall(r"[a-z0-9]+", str(name or "").casefold())
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    while tokens and (tokens[-1] in _LEGAL_SUFFIX or len(tokens[-1]) == 1):
        tokens.pop()
    return tokens


def normalize_name(name) -> str:
    return " ".join(name_tokens(name))


def is_fund_like(ticker, name) -> bool:
    """Index, ETF and fund securities: market context, not an issuer."""
    ticker = str(ticker or "")
    return ticker.startswith("^") or "." in ticker or bool(_FUND_WORDS.search(str(name or "")))


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for suffix in ("es", "s"):
        if len(word) > 4 and word.endswith(suffix) and not word.endswith("ss"):
            return word[: -len(suffix)]
    return word


def _words(text) -> set[str]:
    spelled = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    words = set()
    for word in re.findall(r"[a-z]+", spelled.casefold()):
        words |= {word, _stem(word)}
    return words


@dataclass
class Sealed:
    """Everything a blind rendering must not show."""

    aliases: dict[str, str] = field(default_factory=dict)   # packet ticker -> alias
    names: dict[str, str] = field(default_factory=dict)     # packet name -> alias
    others: dict[str, str] = field(default_factory=dict)    # other single-name ticker -> name
    series: dict[str, str] = field(default_factory=dict)    # sealed series id/label -> alias
    sectors: set[str] = field(default_factory=set)          # sealed sectors not used by the scenario
    lexicon: tuple[str, ...] = ()                           # active sector/industry words
    exchanges: tuple[str, ...] = ()                         # listing venues (issuer packets)
    amounts: list[float] = field(default_factory=list)      # sealed absolute values (abs, >= 1)


class Scrubber:
    """Replace, or report, the sealed concepts in a text."""

    def __init__(self, sealed: Sealed):
        self.sealed = sealed
        rules: list[tuple[str, re.Pattern, str]] = []
        for key, alias in sorted(sealed.series.items(), key=lambda kv: -len(kv[0])):
            rules.append(("series", re.compile(re.escape(key), re.IGNORECASE), alias))
        rules += [("date", pattern, DATE) for pattern in _DATE_PATTERNS]
        phrases = [(name_tokens(name), alias, False) for name, alias in sealed.names.items()]
        phrases += [(name_tokens(name), OTHER, True) for name in sealed.others.values()]
        for tokens, alias, other in sorted(phrases, key=lambda item: -len(" ".join(item[0]))):
            if not tokens:
                continue
            if len(tokens) == 1 and (len(tokens[0]) < (5 if other else 3)
                                     or (other and tokens[0] in _GENERIC_NAME_WORDS)):
                continue
            body = r"[\W_]+".join(re.escape(t) for t in tokens)
            suffix = r"(?:[\W_]+(?:" + "|".join(sorted(_LEGAL_SUFFIX)) + r")(?![A-Za-z0-9])\.?)*"
            rules.append(("other_security" if other else "company",
                          re.compile(rf"{_L}{body}{suffix}{_R}", re.IGNORECASE), alias))
        tickers = list(sealed.aliases.items()) + [(t, OTHER) for t in sealed.others]
        for ticker, alias in sorted(tickers, key=lambda item: -len(item[0])):
            bare = ticker.lstrip("^")
            if len(bare) < 2:
                continue
            flags = 0 if (len(bare) <= 3 or alias == OTHER) else re.IGNORECASE
            core = "(?:" + "|".join(sorted({re.escape(ticker), re.escape(bare)}, key=len, reverse=True)) + ")"
            rules.append(("other_security" if alias == OTHER else "ticker", re.compile(
                rf"(?<![A-Za-z0-9^])\$?{core}(?:[.:](?:US|O|N|OQ))?(?![A-Za-z0-9])", flags), alias))
        seen: set[str] = set()
        for name, alias in list(sealed.names.items()) + [(n, OTHER) for n in sealed.others.values()]:
            for token in name_tokens(name):
                if len(token) >= 5 and token.isalpha() and token not in _GENERIC_NAME_WORDS and token not in seen:
                    seen.add(token)
                    rules.append(("other_security" if alias == OTHER else "company",
                                  re.compile(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])", re.IGNORECASE),
                                  alias))
        sep = r"(?:\s*&\s*|[\s\-_/]+and[\s\-_/]+|[\s\-_/]+)"
        for term in sorted(set(sealed.sectors) | set(sealed.lexicon), key=len, reverse=True):
            parts = [re.escape(w) for w in re.findall(r"[A-Za-z0-9]+", term) if w.casefold() != "and"]
            if parts:
                rules.append(("sector", re.compile(rf"(?<![A-Za-z]){sep.join(parts)}(?:e?s)?(?![A-Za-z])",
                                                   re.IGNORECASE), SECTOR))
        for venue in sorted(sealed.exchanges, key=len, reverse=True):
            body = r"[\s\-_]+".join(re.escape(w) for w in venue.split())
            rules.append(("exchange", re.compile(rf"(?<![A-Za-z]){body}(?![A-Za-z])", re.IGNORECASE), EXCHANGE))
        rules += [("year", pattern, YEAR) for pattern in _YEAR_PATTERNS]
        rules += [("amount", pattern, AMOUNT) for pattern in _AMOUNT_PATTERNS]
        rules.append(("month", _MONTH_WORD, MONTH))
        self.rules = rules
        self._targets = sorted({amount / scale for amount in sealed.amounts
                                for scale in (1.0, 1e3, 1e6, 1e9, 1e12) if amount / scale >= 1})
        self._leak_cache: dict[tuple[str, tuple], list] = {}

    def _sealed_amount(self, token: str) -> bool:
        """Whether a printed number is a sealed amount at its printed precision.

        Each sealed value is also compared in thousands, millions, billions
        and trillions ("130.5" for 130,497,000,000); numbers with fewer than
        two significant digits never match.
        """
        digits = token.replace(",", "")
        try:
            value = float(digits)
        except ValueError:
            return False
        if len(digits.replace(".", "").lstrip("0")) < 2 or not math.isfinite(value):
            return False
        decimals = len(digits.split(".", 1)[1]) if "." in digits else 0
        tolerance = 0.5 * 10 ** (-decimals) * (1 + 1e-9)
        index = bisect.bisect_left(self._targets, value - tolerance)
        return index < len(self._targets) and self._targets[index] <= value + tolerance

    def scrub(self, text, concepts=CONCEPTS):
        if text is None:
            return None
        out = str(text)
        for concept, pattern, replacement in self.rules:
            if concept in concepts:
                out = pattern.sub(replacement, out)
        if "amount" in concepts and self.sealed.amounts:
            out = _NUMBER.sub(lambda m: AMOUNT if self._sealed_amount(m.group(1)) else m.group(0), out)
        return out

    def leaks(self, text, concepts=CONCEPTS) -> list[tuple[str, str]]:
        text = str(text)
        key = (text, tuple(concepts))
        if key in self._leak_cache:
            return self._leak_cache[key]
        found: list[tuple[str, str]] = []
        for concept, pattern, _replacement in self.rules:
            if concept in concepts:
                found += [(concept, m.group(0)) for m in pattern.finditer(text)
                          if m.group(0) not in PLACEHOLDERS]
        if "amount" in concepts and self.sealed.amounts:
            found += [("amount", m.group(0)) for m in _NUMBER.finditer(text) if self._sealed_amount(m.group(1))]
        self._leak_cache[key] = found
        return found


def scan(view, scrubber: Scrubber, path: str = "", concepts=CONCEPTS) -> list[dict]:
    """Every sealed concept left in the free-text strings of a rendering."""
    found: list[dict] = []
    if isinstance(view, dict):
        for key, value in view.items():
            if key == "decision_states":
                found += scan(value, scrubber, f"{path}/{key}", SCENARIO_CONCEPTS)
            elif key not in _FIXED_KEYS:
                found += scan(value, scrubber, f"{path}/{key}", concepts)
    elif isinstance(view, list):
        for index, value in enumerate(view):
            found += scan(value, scrubber, f"{path}[{index}]", concepts)
    elif isinstance(view, str):
        found += [{"path": path or "/", "concept": concept, "match": match}
                  for concept, match in scrubber.leaks(view, concepts)]
    return found


# --------------------------------------------------------------------------
# Numeric helpers
# --------------------------------------------------------------------------


def security_alias(index: int) -> str:
    letters, index = "", index + 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return f"SECURITY_{letters}"


def _naive(value) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _round(value: float) -> float:
    return round(float(value), 2) + 0.0


def unit_class(series_id, unit) -> str:
    """How a blind rendering treats a series' values."""
    raw = str(unit or "").strip().casefold()
    if raw in _KEEP_UNITS:
        return _KEEP_UNITS[raw]
    if str(series_id).upper().startswith(("POLY:", "KALSHI:")):
        return "probability"
    if raw in _CURRENCY_CODES:
        return "currency"
    if "/" in raw:
        return "per_unit"
    if raw in ("shares", "count", "thous", "units"):
        return "count"
    if raw == "index":
        return "index"
    if raw.startswith(tuple(_CURRENCY_CODES)) or raw.startswith("bn_") or raw.endswith(("_mn", "_bn")):
        return "currency_scaled"
    return "level"


def parse_sec_series(series_id) -> dict | None:
    """``SEC:<TICKER>:<Concept>:<Unit>:<FP>`` -> parts, else None."""
    parts = str(series_id or "").split(":")
    if len(parts) == 5 and parts[0] == "SEC" and parts[1] and parts[2]:
        return {"ticker": parts[1], "concept": parts[2], "unit": parts[3], "fp": parts[4]}
    return None


def _deciles(values: dict[str, float]) -> dict[str, int]:
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    if len(ordered) == 1:
        return {ordered[0][0]: 10}
    return {key: 1 + min(9, int(10 * rank / len(ordered))) for rank, (key, _v) in enumerate(ordered)}


def _filing(form) -> str | None:
    raw = str(form or "").strip()
    upper = raw.upper()
    amended = upper.endswith("/A")
    base = upper[:-2] if amended else upper
    if base in _ANNUAL_FORMS:
        kind = "annual_filing"
    elif base in _INTERIM_FORMS:
        kind = "interim_filing"
    elif base == "8-K":
        kind = "current_report"
    elif raw.casefold() in _SAFE_QUALITIES:
        return raw.casefold()
    else:
        return None
    return kind + ("+amended" if amended else "")


def _age_days(asof: datetime, moment) -> int | None:
    if moment in (None, ""):
        return None
    return max(0, int((asof - _naive(moment)).total_seconds() // 86400))


def scenario_words(states: list[dict]) -> set[str]:
    """Words the scenario's own labels use (never scrubbed as sector words)."""
    words: set[str] = set()
    for state in states:
        for key in ("state_key", "actor", "role_class", "sub_role", "role_template"):
            words |= _words(str(state.get(key, "")).replace("/", " ").replace("_", " "))
        for action in state.get("actions", []):
            words |= _words(str(action.get("action", "")).replace("_", " "))
    return words


def _scenario_uses(term: str, words: set[str]) -> bool:
    parts = [p for p in re.findall(r"[a-z]+", term.casefold()) if p != "and"]
    return bool(parts) and all(p in words or _stem(p) in words for p in parts)


# --------------------------------------------------------------------------
# The blind rendering
# --------------------------------------------------------------------------


def build_blind_packet(body: dict, packet_sha256: str, *, universe: list[dict] | None = None,
                       role_view: dict | None = None) -> tuple[dict, dict]:
    """(fork-visible blind view, sealed mapping) for a frozen packet body.

    ``body["securities"]`` lists the packet's securities with names and
    sectors (sealed); ``universe`` lists the warehouse's securities so other
    single names mentioned in free text are scrubbed too; ``role_view`` (the
    role-template contract) is added verbatim after the leak scan.

    Raises:
        BlindLeak: When a sealed ticker, name, series id, date, year, sector
            word or absolute amount would remain in the rendering.
    """
    asof = _naive(body["asof_utc_naive"])
    observations = list(body.get("admitted_observations") or [])
    prices = list(body.get("admitted_prices") or [])
    style = list(body.get("derived_style_scores") or [])
    by_ticker = {str(s["ticker"]): dict(s) for s in body.get("securities") or []}
    order: list[str] = []
    for ticker in ([p["ticker"] for p in map(parse_sec_series, (r.get("series_id") for r in observations)) if p]
                   + [str(r.get("primary_ticker")) for r in prices if r.get("primary_ticker")]
                   + [str(r.get("ticker")) for r in style if r.get("ticker")] + list(by_ticker)):
        if ticker not in order:
            order.append(ticker)
    aliases = {ticker: security_alias(i) for i, ticker in enumerate(order)}
    issuer = any(parse_sec_series(r.get("series_id")) for r in observations) or any(
        not is_fund_like(t, (by_ticker.get(t) or {}).get("name")) for t in order)
    words = scenario_words(body["scenario"]["states"])

    series_rows: dict[str, list[dict]] = {}
    series_alias: dict[str, str] = {}
    for row in observations:
        sid = str(row["series_id"])
        series_alias.setdefault(sid, f"S{len(series_alias) + 1}")
        series_rows.setdefault(sid, []).append(row)
    for rows in series_rows.values():
        rows.sort(key=lambda r: _naive(r["event_time"]), reverse=True)

    # ---- numeric transforms (and the sealed amounts they hide)
    amounts: list[float] = []
    rebase_notes: list[dict] = []
    t0 = {sid: rows[0].get("value_num") for sid, rows in series_rows.items()}
    shared_base: dict[str, tuple[float, str]] = {}
    groups: dict[tuple[str, str], list[str]] = {}
    for sid, rows in series_rows.items():
        if unit_class(sid, rows[0].get("unit")) == "currency":
            parsed = parse_sec_series(sid)
            groups.setdefault((parsed["ticker"] if parsed else sid, str(rows[0].get("unit")).upper()),
                              []).append(sid)
    for (owner, currency), members in groups.items():
        def preference(sid: str):
            parsed = parse_sec_series(sid) or {}
            value = t0[sid]
            return (parsed.get("concept") not in REVENUE_CONCEPTS, parsed.get("fp") != "FY",
                    -abs(value) if _finite(value) else math.inf)
        reference = sorted(members, key=preference)[0]
        base = t0[reference]
        if _finite(base) and base != 0:
            for sid in members:
                shared_base[sid] = (abs(float(base)), reference)
            rebase_notes.append({"scope": f"{aliases.get(owner, series_alias.get(owner, owner))} currency sizes",
                                 "sealed_currency": currency, "reference_series": series_alias[reference],
                                 "base": abs(float(base)), "members": [series_alias[m] for m in members]})
    mapped_values: dict[str, float | None] = {}
    descriptors: list[dict] = []
    for sid, rows in series_rows.items():
        klass = unit_class(sid, rows[0].get("unit"))
        values = {r["evidence_id"]: float(r["value_num"]) for r in rows if _finite(r.get("value_num"))}
        if klass in ("probability", "percent", "ratio"):
            transform, mapped = "as_recorded", dict(values)
        else:
            amounts.extend(values.values())
            if sid in shared_base:
                base = shared_base[sid][0]
                transform, mapped = "security_rebase_100", {k: _round(100 * v / base) for k, v in values.items()}
            elif _finite(t0[sid]) and t0[sid] != 0:
                base = abs(float(t0[sid]))
                transform, mapped = "series_rebase_100", {k: _round(100 * v / base) for k, v in values.items()}
                rebase_notes.append({"scope": series_alias[sid], "base": base})
            elif values and all(v == 0 for v in values.values()):
                transform, mapped = "as_recorded", dict(values)
            else:
                transform, mapped = "decile", {k: float(d) for k, d in _deciles(values).items()}
        mapped_values.update(mapped)
        parsed = parse_sec_series(sid)
        descriptor = {"series": series_alias[sid], "unit_class": klass, "transform": transform,
                      "rows": len(rows), "t0_age_days": _age_days(asof, rows[0]["event_time"])}
        if parsed:
            descriptor.update({"security": aliases[parsed["ticker"]], "concept": parsed["concept"],
                               "fiscal_period": parsed["fp"] if parsed["fp"] in ("FY", "Q1", "Q2", "Q3", "Q4")
                               else "unstated", "source": "issuer_filings"})
        descriptors.append(descriptor)
    bars: dict[str, list[dict]] = {}
    for row in prices:
        bars.setdefault(str(row.get("primary_ticker")), []).append(row)
    for rows in bars.values():
        rows.sort(key=lambda r: str(r["event_date"]), reverse=True)
        for row in rows:
            amounts.extend(float(row[k]) for k in ("open", "high", "low", "close", "volume") if _finite(row.get(k)))
    for row in style:
        amounts.extend(abs(float(v)) for v in (row.get("sealed_amounts") or []) if _finite(v))

    # ---- sealed terms; a series id or label is sealed only if it carries sealed content
    sealed = Sealed(aliases=dict(aliases))
    sealed.amounts = sorted({abs(a) for a in amounts if _finite(a) and abs(a) >= 1.0})
    for ticker in order:
        info = by_ticker.get(ticker) or {}
        if info.get("name") and normalize_name(info["name"]):
            sealed.names[str(info["name"])] = aliases[ticker]
        for part in re.split(r"[,;/]", str(info.get("sector") or "")) + [str(info.get("sector") or "")]:
            if part.strip() and not _scenario_uses(part, words):
                sealed.sectors.add(part.strip())
    for other in (universe or []) if issuer else []:
        # Peers narrow an issuer's identity; a macro or commodity packet keeps other names.
        ticker, name = str(other.get("ticker") or ""), str(other.get("name") or "")
        if ticker and ticker not in aliases and not is_fund_like(ticker, name):
            sealed.others[ticker] = name
    sealed.lexicon = tuple(t for t in SECTOR_LEXICON if not _scenario_uses(t, words)) if issuer else ()
    sealed.exchanges = tuple(v for v in EXCHANGES if not _scenario_uses(v, words)) if issuer else ()
    base_scrubber = Scrubber(sealed)
    for sid, rows in series_rows.items():
        for text in {sid, str(rows[0].get("label") or "")}:
            if len(text) >= 4 and base_scrubber.scrub(text) != text:
                sealed.series[text.casefold()] = series_alias[sid]
    scrubber = Scrubber(sealed)

    # ---- the rendering
    by_alias = {alias: sid for sid, alias in series_alias.items()}
    for descriptor in descriptors:
        if "concept" not in descriptor:
            sid = by_alias[descriptor["series"]]
            descriptor["description"] = base_scrubber.scrub(str(series_rows[sid][0].get("label") or sid))
    blind_obs = []
    for row in observations:
        rows = series_rows[str(row["series_id"])]
        k = next(i for i, r in enumerate(rows) if r["evidence_id"] == row["evidence_id"])
        spacing = (_naive(rows[0]["event_time"]) - _naive(row["event_time"])).total_seconds() / 86400 / 365.25
        blind_obs.append({
            "evidence_id": row["evidence_id"], "series": series_alias[str(row["series_id"])], "period": f"t-{k}",
            "years_before_t0": round(spacing, 2) + 0.0, "value": mapped_values.get(row["evidence_id"]),
            "value_text": scrubber.scrub(row.get("value_str")), "filing": _filing(row.get("quality")),
            "revision_seq": row.get("revision_seq"), "pit_class": row.get("pit_class"),
            "source_id": scrubber.scrub(row.get("source_id")),
            "knowledge_age_days": _age_days(asof, row.get("knowledge_time")),
        })
    blind_prices, price_securities = [], []
    for ticker, rows in bars.items():
        alias = aliases.get(ticker, OTHER)
        close0, volume0 = rows[0].get("close"), rows[0].get("volume")
        close_ok, volume_ok = _finite(close0) and close0 != 0, _finite(volume0) and volume0 != 0
        close_dec = {} if close_ok else _deciles({str(i): float(r["close"]) for i, r in enumerate(rows)
                                                  if _finite(r.get("close"))})
        volume_dec = {} if volume_ok else _deciles({str(i): float(r["volume"]) for i, r in enumerate(rows)
                                                    if _finite(r.get("volume"))})
        price_securities.append({"security": alias, "bars": len(rows),
                                 "t0_age_days": _age_days(asof, f"{rows[0]['event_date']}T00:00:00"),
                                 "price_transform": "close_t0_rebase_100" if close_ok else "decile",
                                 "volume_transform": "volume_t0_rebase_100" if volume_ok else "decile"})
        if close_ok:
            rebase_notes.append({"scope": f"{alias} price", "base": abs(float(close0))})
        if volume_ok:
            rebase_notes.append({"scope": f"{alias} volume", "base": abs(float(volume0))})
        for k, row in enumerate(rows):
            entry = {"evidence_id": row["evidence_id"], "security": alias, "period": f"t-{k}"}
            for key in ("open", "high", "low", "close"):
                value = row.get(key)
                if close_ok and _finite(value):
                    entry[key] = _round(100 * value / abs(float(close0)))
                else:
                    entry[key] = float(close_dec[str(k)]) if key == "close" and str(k) in close_dec else None
            volume = row.get("volume")
            if volume_ok and _finite(volume):
                entry["volume"] = _round(100 * volume / abs(float(volume0)))
            else:
                entry["volume"] = float(volume_dec[str(k)]) if str(k) in volume_dec else None
            entry.update({"revision_seq": row.get("revision_seq"), "pit_class": row.get("pit_class"),
                          "source_id": scrubber.scrub(row.get("source_id")),
                          "knowledge_age_days": _age_days(asof, row.get("knowledge_time"))})
            blind_prices.append(entry)
    price_order = {row["evidence_id"]: i for i, row in enumerate(prices)}
    blind_prices.sort(key=lambda r: price_order[r["evidence_id"]])
    blind_style = []
    for row in style:
        entry = {key: row.get(key) for key in ("evidence_id", "family", "component", "value", "unit",
                                               "status", "pit_class")}
        entry["security"] = aliases.get(str(row.get("ticker")), OTHER)
        entry["rule"] = row.get("rule")          # fixed rule text from zt_style
        entry["missing"] = list(row.get("missing") or [])  # XBRL concept names
        entry["periods_used"] = row.get("periods_used")
        entry["years_spanned"] = row.get("years_spanned")
        entry.update({key: row[key] for key in ("tier", "direction", "basis") if key in row})
        blind_style.append(entry)
    view = {
        "schema": BLIND_VIEW_SCHEMA,
        "blind": True,
        "packet_sha256": packet_sha256,
        "authority": body.get("authority"),
        "time_reference": TIME_REFERENCE,
        "blind_rules": list(BLIND_RULES),
        "warehouse_audit": {"status": (body.get("warehouse_audit") or {}).get("status")},
        "decision_states": body["scenario"]["states"],
        "securities": [{"security": aliases[t]} for t in order],
        "series": descriptors,
        "price_securities": price_securities,
        "admitted_observations": blind_obs,
        "admitted_prices": blind_prices,
        "derived_style_scores": blind_style,
        "memos": [{"memo_id": m["memo_id"], "author": scrubber.scrub(m.get("author", "")),
                   "text": scrubber.scrub(m.get("text", "")), "citations": list(m.get("citations") or [])}
                  for m in body.get("memos") or []],
        "excluded_or_missing": [{"exclusion_id": x["exclusion_id"], "item": scrubber.scrub(x.get("item", "")),
                                 "reason": scrubber.scrub(x.get("reason", ""))}
                                for x in body.get("excluded_or_missing") or []],
        "interpretation_limits": [scrubber.scrub(text) for text in body.get("interpretation_limits") or []],
    }
    leaks = scan(view, scrubber)
    if leaks:
        raise BlindLeak(leaks)
    if role_view:
        view.update(role_view)
    names = sorted({str((by_ticker.get(t) or {}).get("name")) for t in order if (by_ticker.get(t) or {}).get("name")})
    mapping = {
        "schema": SEALED_SCHEMA,
        "packet_sha256": packet_sha256,
        "run_asof_utc": body.get("run_asof_utc"),
        "securities": [{"security": aliases[t], "ticker": t,
                        **{k: (by_ticker.get(t) or {}).get(k) for k in ("sec_id", "name", "sector", "country")},
                        "fund_like": is_fund_like(t, (by_ticker.get(t) or {}).get("name"))} for t in order],
        "series": [{"series": series_alias[sid], "series_id": sid, "label": rows[0].get("label"),
                    "unit": rows[0].get("unit"),
                    "sealed": sid.casefold() in sealed.series
                    or str(rows[0].get("label") or "").casefold() in sealed.series}
                   for sid, rows in series_rows.items()],
        "rebase": rebase_notes,
        "scrub": {"issuer_packet": issuer, "lexicon_terms_active": len(sealed.lexicon),
                  "other_single_names": sorted(sealed.others), "sealed_sectors": sorted(sealed.sectors),
                  "sealed_series_texts": len(sealed.series), "sealed_amounts": len(sealed.amounts)},
        "truth": {"tickers": list(order), "companies": names, "year": asof.year},
    }
    return view, mapping
