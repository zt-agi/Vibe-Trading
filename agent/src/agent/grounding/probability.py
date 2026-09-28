"""The ``probability`` figure role: a probability may only come from a model tool.

ZT add-on (2026-09-28). An LLM never elicits or sets an outcome probability;
one may only be quoted from actor simulation (``run_market_actor_sim`` /
``inspect_market_actor_run``), a market-implied source (``prediction_market``,
the options tools) or a mechanical model (``quantlib_call``). A figure the
model declares ``probability`` is held to that, and so is every figure the
prose *presents* as a probability whatever role it was declared under — the
declared role alone would let "70% | count | my estimate" through.

What makes a number a probability is decided on concepts, not on one literal
phrase (ZT review carry-over: no gate matches a string when a concept check is
possible). Words are normalised the way :mod:`evidence` normalises tool field
names — ``probability``/``prob``/``概率`` are one kind there — and extended
with the paraphrases prose uses for the same concept: odds, likelihood,
chance, likely, confidence, ``P(x)``, "risk of", scenario weights ("base case
50%"), odds forms ("3-to-1", "1 in 4", "one in four", "a coin flip") and the
Chinese forms (概率, 可能性, 胜算, 七成把握, 百分之七十). A figure binds to the
nearest concept word in its clause; parameter words (confidence *interval* or
*level*, VaR, quantile, threshold, p-value) and measured-quantity words
(return, drawdown, win rate, weight without a scenario) keep counts, weights
and thresholds out. A set of figures that partitions 100% across scenario
labels is a distribution even with no probability word in sight.

The module reads text and tool evidence; :mod:`policies` decides and reports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from src.agent.grounding.evidence import _metric_kind_for_path
from src.agent.grounding.figures import (
    Declaration,
    Figure,
    FiguresBlock,
    _fenced_blocks,
    _lines_with_offsets,
    _table_cells,
    segment_bounds,
)

#: Tools whose results may ground a probability, by base name. An MCP-wrapped
#: tool is ``mcp_<server>_<tool>``, so the base is matched as a suffix.
PROBABILITY_TOOLS = frozenset(
    {
        "run_market_actor_sim",
        "inspect_market_actor_run",
        "prediction_market",
        "get_options_chain",
        "options_pricing",
        "options_payoff",
        "quantlib_call",
    }
)

_MCT_TOOLS = frozenset({"run_market_actor_sim", "inspect_market_actor_run"})

#: Result sections of an actor simulation that hold computed outcome
#: probabilities. Elicited action propensities (state_propensity_dispersion)
#: are deliberately absent: they are role inputs, not outcomes.
_MCT_SECTIONS = frozenset(
    {"ensemble_mc", "ensemble_exact", "mc_wilson_95", "model_form_range", "exact_by_temperament"}
)

_PROBABILITY_FIELD_TOKENS = frozenset(
    {"probability", "probabilities", "prob", "odds", "likelihood", "pbo", "psr", "pd", "edf"}
)

#: Concept classes. P: probability. S: scenario (a figure bound to one is a
#: scenario weight). W: weight (a probability only beside a scenario). X: a
#: parameter or threshold. Q: a measured quantity. L: a scenario label, which
#: counts only when several labelled figures partition 100%.
_P, _S, _W, _X, _Q, _L = "P", "S", "W", "X", "Q", "L"

_WORDS = {
    _P: (
        "probability probabilities probable probably prob probs odds likelihood likelihoods "
        "likely unlikely chance chances possibility possibilities confidence confident pd edf"
    ),
    # "cases" is left out: "in 30% of cases" states a frequency, not a weight.
    _S: "case scenario scenarios outcome outcomes",
    _W: "weight weights weighting weighted",
    _X: (
        "interval intervals level levels var cvar es shortfall quantile quantiles percentile "
        "percentiles significance significant threshold thresholds cutoff band ci pvalue "
        "tolerance"
    ),
    _Q: (
        "return returns growth yield yields drawdown drawdowns margin margins vol volatility "
        "upside downside gain gains loss losses change changes move moves rally decline declines "
        "drop drops rise rises increase increases decrease decreases cut cuts hike hikes price "
        "prices spread spreads inflation cpi gdp eps revenue sales earnings allocation "
        "allocations exposure position positions share shares stake premium discount disruption "
        "supply demand utilization turnover ratio beta correlation overweight underweight "
        "tariff cost costs capacity output production coverage adoption impact contribution "
        "budget cap limit stop target rate rates frequency"
    ),
    _L: (
        "bull bear base baseline tail stress escalation escalate deescalation contained "
        "containment hold ease easing tightening dovish hawkish recession expansion"
    ),
}

#: Two-word concepts, checked before single words.
_BIGRAMS = {
    ("risk", "of"): _P,
    ("risk", "that"): _P,
    ("base", "rate"): _P,
    ("p", "value"): _X,
    ("confidence", "interval"): _X,
    ("confidence", "level"): _X,
    ("confidence", "band"): _X,
    ("expected", "shortfall"): _X,
    ("win", "rate"): _Q,
    ("hit", "rate"): _Q,
    ("fill", "rate"): _Q,
    ("success", "rate"): _Q,
    ("default", "rate"): _Q,
    ("interest", "rate"): _Q,
    ("status", "quo"): _L,
    ("soft", "landing"): _L,
    ("hard", "landing"): _L,
    ("no", "landing"): _L,
    ("lose", "control"): _L,
    ("de", "escalation"): _L,
}

_CJK_WORDS = {
    _P: ("概率", "几率", "机率", "可能性", "的可能", "胜算", "把握", "赔率", "或然率", "置信度"),
    _S: ("情景", "情形", "场景", "结局"),
    _W: ("权重", "加权"),
    _X: ("置信区间", "置信水平", "分位数", "分位", "显著", "阈值", "门槛"),
    _Q: (
        "收益率", "收益", "增长", "增速", "回撤", "涨幅", "跌幅", "仓位", "占比", "比例",
        "利率", "通胀", "波动率", "胜率", "命中率", "成功率", "溢价", "折价", "份额",
    ),
    _L: ("乐观", "悲观", "中性", "基准", "升级", "缓和", "维持", "衰退", "软着陆", "硬着陆", "鹰派", "鸽派"),
}

_WORD_CLASS = {word: kind for kind, words in _WORDS.items() for word in words.split()}

_CJK_CLASS = {term: kind for kind, terms in _CJK_WORDS.items() for term in terms}
#: Longest term first, so 置信区间 is read before 置信度 could be and 收益率 before 收益.
_CJK_RE = re.compile("|".join(re.escape(term) for term in sorted(_CJK_CLASS, key=len, reverse=True)))

_STOPWORDS = frozenset(
    "a an the of to in on for with and or by at as is are was were be been being this that "
    "its it from into over than then so about around roughly approximately approx near nearly "
    "some about est estimated".split()
)

#: Function characters that do not separate a CJK figure from its concept word.
_CJK_FUNCTION = frozenset("的地得了是为有约在与和及或其之将会被对于以从到也都就还而且则即仍已达近超过左右大概")

#: Unambiguous probability words, for reading a declaration's note.
_NOTE_P_RE = re.compile(
    r"\b(?:probabilit(?:y|ies)|prob|odds|likelihood|chances?|base\s+rate)\b"
    r"|概率|几率|机率|可能性|胜算|赔率",
    re.IGNORECASE,
)

#: How many content tokens may stand between a figure and the concept it binds to.
_MAX_GAP = 3

_ENGLISH_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "twenty": 20, "hundred": 100, "a": 1,
}
_CJK_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_WORD_TOKEN_RE = re.compile(r"[A-Za-z]+")
_P_NOTATION_RE = re.compile(r"(?<![A-Za-z])[Pp]\s*\(\s*[^()\n]{1,60}\)")
_ODDS_RE = re.compile(r"(?<![\d.:])(\d{1,3})\s*(?::|-to-|\s+to\s+)\s*(\d{1,3})(?![\d.:])")
#: Words that make an adjacent "N to M" / "N:M" a statement of odds.
_ODDS_WORD_RE = re.compile(r"\b(?:odds|against|in\s+favou?r|chance|chances)\b|赔率", re.IGNORECASE)
_IN_RE = re.compile(r"(?<![\d.])(\d{1,3})\s+(?:in|out\s+of)\s+(\d{1,3})(?![\d.])", re.IGNORECASE)
_WORD_IN_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine)\s+(?:in|out\s+of)\s+"
    r"(two|three|four|five|six|seven|eight|nine|ten|twenty|a\s+hundred|hundred)\b",
    re.IGNORECASE,
)
_EVEN_RE = re.compile(
    r"\b(?:coin[\s-]?(?:flip|toss)|fifty[\s-]fifty|50\s*/\s*50|even\s+(?:odds|money|chance)|toss[\s-]?up)\b"
    r"|五五开|对半开",
    re.IGNORECASE,
)
_CJK_TENTHS_RE = re.compile(r"([一二两三四五六七八九十])成(?=\s*(?:把握|可能|概率|胜算|机会|的?可能性))")
_CJK_PERCENT_RE = re.compile(r"百分之([一二两三四五六七八九十百零\d]+)")
_CJK_FRACTION_RE = re.compile(r"([一二三四五六七八九十])分之([一二三四五六七八九十])")
_WORD_PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*(?:percent|per\s+cent|pct)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Claim:
    """A span the answer presents as a probability.

    ``readings`` are the fractions it can mean: one for "70%", two for "3-to-1"
    odds (for or against). ``located`` is True when the span is a number the
    figure scan found, so the release path can cut it.
    """

    start: int
    end: int
    text: str
    readings: tuple[float, ...]
    cue: str
    located: bool


def tool_base(name: str) -> str | None:
    """The allowlisted base name of a tool, or None when it may not ground a probability."""
    name = str(name or "")
    if name in PROBABILITY_TOOLS:
        return name
    if name.startswith("mcp_"):
        for base in PROBABILITY_TOOLS:
            if name.endswith("_" + base):
                return base
    return None


def is_probability_field(base: str, path: str) -> bool:
    """Whether a leaf of an allowlisted tool's result holds a probability.

    An actor simulation's outcome sections hold probabilities whatever their
    leaf is called (``ensemble_mc.oil_range``); elsewhere the field name must
    carry the concept, the way :func:`evidence._metric_kind_for_path` reads it.
    """
    segments = re.split(r"[.\[\]]+", str(path or ""))
    if base in _MCT_TOOLS:
        return any(segment in _MCT_SECTIONS for segment in segments)
    tokens = {token for token in re.split(r"[^a-z0-9]+", str(path).casefold()) if token}
    if tokens & _PROBABILITY_FIELD_TOKENS or "transition_matrix" in str(path):
        return True
    return _metric_kind_for_path(str(path)) == "probability"


def concept_of_text(text: str) -> set[str]:
    """Concept classes named anywhere in a short text (a note, a header, a label)."""
    return {kind for kind, _, _ in _cues(text, 0)}


#: A table header that is a probability by notation alone: "p", "MCT p", "p(x)", "Pr".
_HEADER_P_RE = re.compile(r"^\s*(?:[A-Za-z][\w-]*\s+)?(?:p|pr|p\s*\(.*\)|p̂)\s*$", re.IGNORECASE)


def _header_kinds(text: str) -> set[str]:
    kinds = concept_of_text(text)
    if _HEADER_P_RE.match(text or ""):
        kinds.add(_P)
    return kinds


def _cues(text: str, offset: int) -> list[tuple[str, int, int]]:
    """``(class, start, end)`` of every concept word in ``text``, in document offsets."""
    hits: list[tuple[str, int, int]] = []
    words = [(match.group(0).casefold(), match.start(), match.end()) for match in _WORD_TOKEN_RE.finditer(text)]
    index = 0
    while index < len(words):
        word, start, end = words[index]
        if index + 1 < len(words):
            pair = _BIGRAMS.get((word, words[index + 1][0]))
            if pair is not None:
                hits.append((pair, offset + start, offset + words[index + 1][2]))
                index += 2
                continue
        kind = _WORD_CLASS.get(word) or _WORD_CLASS.get(word.rstrip("s"))
        if kind is not None:
            hits.append((kind, offset + start, offset + end))
        index += 1
    for match in _CJK_RE.finditer(text):
        hits.append((_CJK_CLASS[match.group(0)], offset + match.start(), offset + match.end()))
    for match in _P_NOTATION_RE.finditer(text):
        hits.append((_P, offset + match.start(), offset + match.end()))
    return sorted(hits, key=lambda hit: hit[1])


def _gap(text: str) -> int:
    """Content tokens in the text between a figure and a concept word."""
    count = sum(1 for word in _WORD_TOKEN_RE.findall(text) if word.casefold() not in _STOPWORDS)
    count += sum(1 for char in text if "㐀" <= char <= "鿿" and char not in _CJK_FUNCTION)
    count += len(re.findall(r"\d+(?:\.\d+)?", text))
    return count


def _bind(content: str, start: int, end: int, cues: Sequence[tuple[str, int, int]]) -> str | None:
    """The concept class a figure at ``start:end`` binds to within its clause.

    A parameter word within reach wins outright (a VaR confidence, an interval
    level); otherwise the nearest concept word wins, the one after the figure on
    a tie, because a number modifies the noun that follows it ("30% supply
    disruption"). Scenario labels do not bind a single figure.
    """
    reachable: list[tuple[int, int, str]] = []
    for kind, cue_start, cue_end in cues:
        if kind == _L:
            continue
        if cue_end <= start:
            gap, after = _gap(content[cue_end:start]), 0
        elif cue_start >= end:
            gap, after = _gap(content[end:cue_start]), 1
        else:
            continue
        if gap <= _MAX_GAP:
            reachable.append((gap, -after, kind))
    if not reachable:
        return None
    if any(kind == _X for gap, _, kind in reachable if gap <= 2):
        return _X
    return min(reachable)[2]


def _fraction(figure: Figure) -> float | None:
    """The figure as a probability fraction, or None when it cannot be one."""
    if figure.currency or figure.sign:
        return None
    if figure.percent:
        return figure.value / 100.0 if 0.0 <= figure.value <= 100.0 else None
    if "." in (figure.digits or "") and 0.0 <= figure.value <= 1.0:
        return figure.value
    return None


def _cjk_number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text in _CJK_NUMBERS:
        return _CJK_NUMBERS[text]
    if text.startswith("十") and len(text) == 2 and text[1] in _CJK_NUMBERS:
        return 10 + _CJK_NUMBERS[text[1]]
    if len(text) == 2 and text[1] == "十" and text[0] in _CJK_NUMBERS:
        return _CJK_NUMBERS[text[0]] * 10
    if len(text) == 3 and text[1] == "十" and text[0] in _CJK_NUMBERS and text[2] in _CJK_NUMBERS:
        return _CJK_NUMBERS[text[0]] * 10 + _CJK_NUMBERS[text[2]]
    if text == "一百" or text == "百":
        return 100
    return None


def _tables(content: str) -> list[tuple[list[str], list[tuple[int, list[tuple[str, int, int]]]]]]:
    """Each Markdown table as ``(header texts, [(line index, cells)])``."""
    positions = _lines_with_offsets(content)
    tables = []
    index = 0
    while index < len(positions):
        if positions[index][0].count("|") < 2:
            index += 1
            continue
        start = index
        while index < len(positions) and positions[index][0].count("|") >= 2:
            index += 1
        header = [text for text, _, _ in _table_cells(*positions[start])]
        body = []
        for line_index in range(start + 1, index):
            cells = _table_cells(*positions[line_index])
            if cells and not all(set(text.replace(" ", "")) <= {"-", ":"} and "-" in text for text, _, _ in cells):
                body.append((line_index, cells))
        tables.append((header, body))
    return tables


def _sums_to_whole(values: Sequence[float]) -> bool:
    return len(values) >= 2 and abs(sum(values) - 1.0) <= 0.011


def find_claims(content: str, figures: Sequence[Figure], block: FiguresBlock) -> list[Claim]:
    """Every probability the answer states, located or phrased.

    Args:
        content: The candidate answer.
        figures: Its scanned figures.
        block: Its figures block (declared roles and notes).

    Returns:
        One claim per probability, in document order.
    """
    fenced = [(start, end) for start, end, info, _ in _fenced_blocks(content) if info]
    fenced.extend(block.spans or ())
    claims: dict[tuple[int, int], Claim] = {}

    def add(start: int, end: int, readings: Iterable[float], cue: str, located: bool) -> None:
        if any(low <= start < high for low, high in fenced):
            return
        claims.setdefault(
            (start, end),
            Claim(start, end, content[start:end], tuple(readings), cue, located),
        )

    eligible = [
        (figure, _fraction(figure))
        for figure in figures
        # A tagged fence is already "exempt"; an untagged one is prose.
        if figure.shape in ("measured", "bare")
    ]
    by_span = {(figure.start, figure.end): figure for figure, _ in eligible}

    # 1. Declared: the model's own role or note names the concept. A note is
    # read for the unambiguous words only ("confidence" there is usually a
    # VaR level), and never against a parameter the prose binds the figure to.
    for figure, fraction in eligible:
        declaration = block.match(figure.value, figure.percent, figure.digits)
        if declaration is None:
            continue
        note = f"{declaration.note} {declaration.ref}"
        noted = bool(_NOTE_P_RE.search(note)) and _X not in concept_of_text(note)
        if noted and fraction is not None:
            left, right = segment_bounds(content, figure.start, figure.end)
            noted = _bind(content, figure.start, figure.end, _cues(content[left:right], left)) != _X
        if declaration.role == "probability" or (noted and fraction is not None):
            reading = fraction
            if reading is None:
                reading = figure.value / 100.0 if 1.0 < figure.value <= 100.0 else figure.value
            add(figure.start, figure.end, (reading,), f"declared {declaration.role}", True)

    # 2. Prose: a figure bound to a probability or scenario concept in its clause.
    table_cells = {
        (cell_start, cell_end)
        for _, body in _tables(content)
        for _, cells in body
        for _, cell_start, cell_end in cells
    }
    bound: dict[tuple[int, int], str | None] = {}
    for figure, fraction in eligible:
        if fraction is None:
            continue
        cell = next(((a, b) for a, b in table_cells if a <= figure.start and figure.end <= b), None)
        left, right = cell if cell else segment_bounds(content, figure.start, figure.end)
        cues = _cues(content[left:right], left)
        kind = _bind(content, figure.start, figure.end, cues)
        bound[(figure.start, figure.end)] = kind
        segment_kinds = {hit[0] for hit in cues}
        if kind in (_P, _S) or (kind == _W and segment_kinds & {_S, _P}):
            add(figure.start, figure.end, (fraction,), f"{kind} concept in clause", True)

    # 3. Tables: a column headed by a probability concept, or scenario weights.
    for header, body in _tables(content):
        header_kinds = [_header_kinds(text) for text in header]
        row_label_kinds = [concept_of_text(cells[0][0]) if cells else set() for _, cells in body]
        labelled_rows = sum(bool(kinds & {_S, _L}) for kinds in row_label_kinds)
        # Scenario weights partition the ROWS; scenarios as columns over asset
        # rows are allocations per scenario, not probabilities.
        scenario_table = bool(header_kinds[:1] and header_kinds[0] & {_S, _L}) or labelled_rows >= 2
        columns: dict[int, list[tuple[Figure, float]]] = {}
        for (_, cells), row_kinds in zip(body, row_label_kinds):
            for position, (_, cell_start, cell_end) in enumerate(cells):
                inside = [
                    (figure, fraction)
                    for figure, fraction in eligible
                    if fraction is not None and cell_start <= figure.start and figure.end <= cell_end
                ]
                if len(inside) != 1:
                    continue
                figure, fraction = inside[0]
                kinds = header_kinds[position] if position < len(header_kinds) else set()
                if _X in kinds or _Q in kinds:
                    continue
                if _P in kinds or _P in row_kinds or (_W in kinds and (scenario_table or row_kinds & {_S, _L})):
                    add(figure.start, figure.end, (fraction,), "probability column", True)
                columns.setdefault(position, []).append((figure, fraction))
        for position, entries in columns.items():
            kinds = header_kinds[position] if position < len(header_kinds) else set()
            if _X in kinds or _Q in kinds:
                continue
            if scenario_table and _sums_to_whole([f for _, f in entries]):
                for figure, fraction in entries:
                    add(figure.start, figure.end, (fraction,), "scenario column sums to 100%", True)

    # 4. Lines and list blocks: labelled figures that partition 100%.
    positions = _lines_with_offsets(content)
    blocks: list[list[tuple[str, int]]] = []
    for line, offset in positions:
        bullet = bool(re.match(r"\s*(?:[-*+]|\d+[.)])\s+", line))
        if bullet and blocks and blocks[-1] and blocks[-1][-1][0] == "bullet":
            blocks[-1].append(("bullet", offset))
        else:
            blocks.append([("bullet" if bullet else "line", offset)])
    line_spans = {offset: offset + len(line) for line, offset in positions}
    for group in blocks:
        low = group[0][1]
        high = line_spans[group[-1][1]]
        text = content[low:high]
        if "|" in text:
            continue
        # A figure bound to a parameter or a measured quantity is no part of a
        # distribution ("VaR at 99% confidence is 2.1%" does not sum to 100%).
        members = [
            (figure, fraction)
            for figure, fraction in eligible
            if fraction is not None and low <= figure.start and figure.end <= high
            and bound.get((figure.start, figure.end)) not in (_X, _Q)
        ]
        if not _sums_to_whole([fraction for _, fraction in members]):
            continue
        kinds = [hit[0] for hit in _cues(text, low)]
        labels = kinds.count(_L) + kinds.count(_S)
        if _X in kinds and _P not in kinds:
            continue
        if _P in kinds or labels >= 2:
            for figure, fraction in members:
                add(figure.start, figure.end, (fraction,), "labelled figures sum to 100%", True)

    # 5. Odds and phrased forms.
    def beside(start: int, end: int, pattern: re.Pattern[str], reach: int = 16) -> bool:
        left, right = segment_bounds(content, start, end)
        return bool(pattern.search(content[max(left, start - reach):start])) or bool(
            pattern.search(content[end:min(right, end + reach)])
        )

    probability_word = re.compile(
        r"\b(?:chance|chances|odds|probability|likelihood|likely|risk)\b|概率|可能性|几率|机率|胜算",
        re.IGNORECASE,
    )
    for match in _ODDS_RE.finditer(content):
        if not beside(match.start(), match.end(), _ODDS_WORD_RE):
            continue
        a, b = int(match.group(1)), int(match.group(2))
        if a + b == 0:
            continue
        readings = (a / (a + b), b / (a + b))
        for group_index in (1, 2):
            span = (match.start(group_index), match.end(group_index))
            add(*span, readings, "odds", span in by_span)
    for match in _IN_RE.finditer(content):
        a, b = int(match.group(1)), int(match.group(2))
        if b == 0 or a > b or not beside(match.start(), match.end(), probability_word):
            continue
        for group_index in (1, 2):
            span = (match.start(group_index), match.end(group_index))
            add(*span, (a / b,), "N in M", span in by_span)
    for match in _WORD_PERCENT_RE.finditer(content):
        left, right = segment_bounds(content, match.start(), match.end())
        cues = _cues(content[left:right], left)
        if _bind(content, match.start(), match.end(), cues) in (_P, _S):
            span = (match.start(1), match.end(1))
            add(*span, (float(match.group(1)) / 100.0,), "word percent", span in by_span)
    for match in _WORD_IN_RE.finditer(content):
        if not beside(match.start(), match.end(), probability_word):
            continue
        a = _ENGLISH_NUMBERS[match.group(1).casefold()]
        b = _ENGLISH_NUMBERS[match.group(2).casefold().split()[-1]]
        add(match.start(), match.end(), (a / b,), "N in M words", False)
    for match in _EVEN_RE.finditer(content):
        add(match.start(), match.end(), (0.5,), "even odds", False)
    for match in _CJK_TENTHS_RE.finditer(content):
        add(match.start(), match.end(), (_CJK_NUMBERS[match.group(1)] / 10.0,), "成", False)
    for match in _CJK_PERCENT_RE.finditer(content):
        number = _cjk_number(match.group(1))
        left, right = segment_bounds(content, match.start(), match.end())
        cue = _P in {hit[0] for hit in _cues(content[left:right], left)}
        if number is None or not (cue or content[match.end():match.end() + 3].lstrip("的").startswith("可能")):
            continue
        add(match.start(), match.end(), (number / 100.0,), "百分之", False)
    for match in _CJK_FRACTION_RE.finditer(content):
        left, right = segment_bounds(content, match.start(), match.end())
        if _P not in {hit[0] for hit in _cues(content[left:right], left)}:
            continue
        denominator, numerator = _CJK_NUMBERS[match.group(1)], _CJK_NUMBERS[match.group(2)]
        if numerator <= denominator:
            add(match.start(), match.end(), (numerator / denominator,), "分之", False)
    return sorted(claims.values(), key=lambda claim: (claim.start, claim.end))


def matches(written: Figure | None, text: str, readings: Sequence[float], target: float) -> bool:
    """Whether a claim restates ``target`` to the precision it was written with.

    A percent is compared in points and a decimal as a fraction, each within
    half a unit of the last digit written, capped at half a percentage point: a
    probability quoted coarser than a whole point ("0.3") cannot be told apart
    from its neighbours and is not recognised as a model's value. A phrased
    form ("3-to-1", "七成") is compared within half a percentage point.
    """
    if written is not None and written.percent:
        digits = written.digits or text
        half = 0.5 * 10.0 ** (-len(digits.split(".", 1)[1])) if "." in digits else 0.5
        return abs(written.value - target * 100.0) <= min(half, 0.5) + 1e-9
    if written is not None and "." in (written.digits or ""):
        half = 0.5 * 10.0 ** (-len(written.digits.split(".", 1)[1]))
        return abs(written.value - target) <= min(half, 0.005) + 1e-9
    return any(abs(reading - target) <= 0.005 + 1e-9 for reading in readings)


def declared(block: FiguresBlock, figure: Figure | None) -> Declaration | None:
    """The declaration covering a located figure, if any."""
    if figure is None:
        return None
    return block.match(figure.value, figure.percent, figure.digits)
