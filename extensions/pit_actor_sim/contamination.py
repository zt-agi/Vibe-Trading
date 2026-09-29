"""Contamination probe scoring for blind evidence packets.

ZT add-on (2026-09-29). A blind packet hides the security, the company and
the calendar period so a role fork cannot recall how the story ended. Whether
that worked is measured, not assumed: before the forks run, a probe agent
reads the same blind rendering and names its single best guess of the
ticker, the company and the year. The guess is compared mechanically with the
sealed truth (ticker and normalized company name must match exactly; the year
may be off by one) and the verdict is recorded beside the packet. Role-fork
memos of a blind packet are also scanned for the sealed identities: a fork
that writes the true ticker, name, a sealed series id or a year within one of
the truth has recognised the packet too.

Any IDENTIFIED probe or identity mention tags the run CONTAMINATED in its run
manifest; the forecast ledger keeps such runs visible but never counts them
toward skill. No LLM judges anything here and the verdict never reveals the
truth to the caller.
"""
from __future__ import annotations

import re

try:
    import blinding
except ImportError:  # imported as a package module rather than run as a script
    from . import blinding

PROBE_SCHEMA = "vt.contamination_probe.v1"
VERDICTS = ("IDENTIFIED", "NOT_IDENTIFIED")
#: Run-level status recorded in run manifests and the forecast ledger.
STATUSES = ("CONTAMINATED", "NOT_IDENTIFIED", "NOT_PROBED", "NOT_BLIND")
YEAR_TOLERANCE = 1
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_TICKER_RE = re.compile(r"^\$?[A-Za-z0-9^][A-Za-z0-9.\-/^]{0,14}$")
_TICKER_SUFFIX = re.compile(r"[.:](?:US|O|N|OQ|NQ)$", re.IGNORECASE)


def normalize_ticker(value) -> str:
    raw = str(value or "").strip().upper().lstrip("$")
    raw = _TICKER_SUFFIX.sub("", raw)
    return re.sub(r"[./]", "-", raw)


def _years_in(text) -> list[int]:
    """Calendar years a text names: 2026, FY2026, FY26, Q3'25, '25 ..."""
    years: list[int] = []
    for pattern in blinding._YEAR_PATTERNS:
        for match in pattern.finditer(str(text)):
            token = match.group(0)
            four = re.search(r"(?:19|20)\d{2}", token)
            if four and len(re.sub(r"\D", "", token)) in (4, 5, 6):
                years.append(int(four.group(0)))
                continue
            two = re.findall(r"\d{2}", token)
            if two and not four:
                years.append(2000 + int(two[-1]))
    for pattern in blinding._DATE_PATTERNS:
        for match in pattern.finditer(str(text)):
            four = re.search(r"(?:19|20)\d{2}", match.group(0))
            if four:
                years.append(int(four.group(0)))
    return years


def parse_year(value) -> int | None:
    """One guessed year, or None when not guessed; several distinct years are refused."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("year must be a single year such as 2026")
    if isinstance(value, int):
        if not 1900 <= value <= 2099:
            raise ValueError("year must be between 1900 and 2099")
        return value
    found = sorted(set(_years_in(value)))
    if len(found) != 1:
        raise ValueError("year must name exactly one year (a single best guess, no ranges or lists)")
    return found[0]


def validate_guess(guess) -> dict:
    """A probe's guess as {ticker, company, year}; fields left empty were not guessed."""
    if not isinstance(guess, dict):
        raise ValueError("guess must be an object {ticker?, company?, year?}")
    unknown = sorted(set(guess) - {"ticker", "company", "year"})
    if unknown:
        raise ValueError(f"guess has unknown fields {unknown}; use ticker, company, year")
    ticker = str(guess.get("ticker") or "").strip()
    if ticker and not _TICKER_RE.match(ticker):
        raise ValueError("ticker must be one symbol (a single best guess, no lists)")
    company = str(guess.get("company") or "").strip()
    if len(company) > 120 or any(sep in company for sep in (";", "|", "\n", " or ", " / ")):
        raise ValueError("company must be one name of at most 120 characters (a single best guess)")
    return {"ticker": ticker or None, "company": company or None, "year": parse_year(guess.get("year"))}


def score_guess(guess: dict, truth: dict) -> dict:
    """Compare a validated guess with the sealed truth; the truth is never returned."""
    tickers = {normalize_ticker(t) for t in truth.get("tickers") or [] if t}
    names = {blinding.normalize_name(n) for n in truth.get("companies") or [] if blinding.normalize_name(n)}
    candidates = [c for c in (guess.get("ticker"), guess.get("company")) if c]
    ticker_hit = any(normalize_ticker(c) in tickers for c in candidates)
    company_hit = any(blinding.normalize_name(c) in names for c in candidates if blinding.normalize_name(c))
    year = guess.get("year")
    truth_year = truth.get("year")
    year_hit = year is not None and truth_year is not None and abs(int(year) - int(truth_year)) <= YEAR_TOLERANCE
    hits = {"ticker": ticker_hit, "company": company_hit, "year": year_hit}
    return {"verdict": "IDENTIFIED" if any(hits.values()) else "NOT_IDENTIFIED", "hits": hits,
            "fields_guessed": [k for k in ("ticker", "company", "year") if guess.get(k) is not None]}


def identity_scrubber(mapping: dict) -> blinding.Scrubber:
    """Patterns for the sealed identities of one packet (no sector, no amounts)."""
    sealed = blinding.Sealed()
    for security in mapping.get("securities") or []:
        sealed.aliases[str(security["ticker"])] = str(security["security"])
        if security.get("name") and blinding.normalize_name(security["name"]):
            sealed.names[str(security["name"])] = str(security["security"])
    for series in mapping.get("series") or []:
        if series.get("sealed"):
            for text in (series.get("series_id"), series.get("label")):
                if text and len(str(text)) >= 4:
                    sealed.series[str(text).casefold()] = str(series["series"])
    return blinding.Scrubber(sealed)


def identity_mentions(texts: dict[str, str], mapping: dict) -> list[dict]:
    """Sealed identities a fork wrote: ticker, company, sealed series id, or a year near the truth."""
    scrubber = identity_scrubber(mapping)
    truth_year = (mapping.get("truth") or {}).get("year")
    found: list[dict] = []
    for where, text in texts.items():
        for concept, _match in scrubber.leaks(text, ("series", "company", "ticker")):
            found.append({"where": where, "concept": concept})
        if truth_year is not None and any(abs(y - int(truth_year)) <= YEAR_TOLERANCE for y in _years_in(text)):
            found.append({"where": where, "concept": "year"})
    unique = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique


def fork_texts(fork: dict) -> dict[str, str]:
    """Every free-text field of a canonical fork, keyed by where it sits."""
    texts: dict[str, str] = {}
    for state_key, memo in (fork.get("memos") or {}).items():
        where = state_key or "<root>"
        texts[f"{where}/analysis"] = str(memo.get("analysis", ""))
        texts[f"{where}/evidence"] = "\n".join(map(str, memo.get("evidence") or []))
        texts[f"{where}/missing_observables"] = "\n".join(map(str, memo.get("missing_observables") or []))
        for item, text in (memo.get("checklist") or {}).items():
            texts[f"{where}/checklist/{item}"] = str(text)
    return texts


def summarize(blind: bool, probes: list[dict], fork_mentions: dict[str, list[dict]]) -> dict:
    """Run-level contamination status for a run manifest and the forecast ledger."""
    mentions = {label: items for label, items in fork_mentions.items() if items}
    if not blind:
        status = "NOT_BLIND"
    elif any(p.get("verdict") == "IDENTIFIED" for p in probes) or mentions:
        status = "CONTAMINATED"
    elif probes:
        status = "NOT_IDENTIFIED"
    else:
        status = "NOT_PROBED"
    return {
        "status": status,
        "skill_eligible": status != "CONTAMINATED",
        "probes": [{k: p.get(k) for k in ("probe_label", "verdict", "hits", "fields_guessed", "scored_at_utc")}
                   for p in probes],
        "fork_identity_mentions": {label: [{k: m[k] for k in ("where", "concept")} for m in items]
                                   for label, items in sorted(mentions.items())},
        "rule": "CONTAMINATED when a probe IDENTIFIED the packet or a fork memo named a sealed identity; "
                "a contaminated run is recorded but never counts toward skill.",
    }
