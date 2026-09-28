"""Role-fork admission rules for the evidence-frozen actor simulator.

ZT add-on (2026-09-28). This ports ``validate_forks`` from
``market_actor_sim/run_governed_pilot.py`` so a fork is checked when a swarm
worker submits it, not only when the simulator starts:

* at least three temperament forks, with unique labels;
* every scenario decision state is covered, with the node's own actor;
* each state carries a content memo of at least 120 characters;
* no memo names a terminal outcome;
* every propensity vector is state-local: exactly the node's actions, each
  strictly between 0 and 1, summing to one.

Two rules from the pilot's role-elicitation packet are added because a frozen
packet now exists to check them against: a memo's citations must resolve to
items in that packet, and a state whose memo cites nothing it can resolve must
name the missing observable and stay near uniform.

The terminal-outcome check works on concepts, not literal strings (ZT review
carry-over): outcome labels and memo text are split into words, stemmed and
mapped through a small market-vocabulary synonym table, so "a severe oil
spike", "crude surging severely" and "oil_spike_severe" are the same mention.
The simulator keeps its own literal check as the last line of defence.

The scenario tree is parsed here with the same rules as ``sim/model.py`` so
the MCP server does not import project code to validate a fork.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: Pilot rule: a content memo shorter than this is not an analysis.
MIN_MEMO_CHARS = 120
#: Pilot rule: at least three temperaments, one sampled per rollout.
MIN_FORKS = 3
#: Largest distance from uniform a state may keep when its memo cites nothing
#: in the frozen packet ("widen toward uniform", role_elicitation_packet.md).
UNCITED_UNIFORM_TOLERANCE = 0.10
#: Pilot rule: probabilities sum to one within this tolerance.
SUM_TOLERANCE = 1e-9
#: Extra content words allowed between the words of one outcome mention.
MENTION_SLACK = 2

_TEMPERAMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{2,79}$")

_STOPWORDS = frozenset(
    """a an the of to in on for with and or by at as is are was were be been being
    this that these those it its from into over under than then so if but not no
    will would could should may might can do does did has have had their there
    they them he she his her we our you your i me my via per vs versus about
    which who whom whose what when where why how all any each both either
    neither more most less least very much many such same other another""".split()
)

#: Stem -> concept. Generic market-move vocabulary so an outcome label written
#: as a snake_case identifier matches the words a memo uses for the same state.
_SYNONYMS = {
    "crude": "oil", "brent": "oil", "wti": "oil", "petroleum": "oil", "barrel": "oil",
    "surg": "spik", "jump": "spik", "soar": "spik", "spik": "spik",
    "sever": "sever", "extrem": "sever", "sharp": "sever", "drastic": "sever",
    "moderat": "moderat", "modest": "moderat", "mild": "moderat",
    "fall": "down", "drop": "down", "declin": "down", "slump": "down", "collaps": "down",
    "plung": "down", "tumbl": "down", "down": "down",
    "rang": "rang", "rangebound": "rang", "sideway": "rang",
    "maximum": "max", "maximal": "max", "minimum": "min", "minimal": "min",
}


def _stem(word: str) -> str:
    """A light English stemmer: plural, -ing/-ed/-ly and a final -e."""
    w = word
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith(("sses", "ches", "shes", "xes", "zes")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suffix in ("ingly", "edly", "ing", "ed", "ly"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            w = w[: -len(suffix)]
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    if len(w) > 4 and w.endswith("y"):
        w = w[:-1]
    return w


def _concept(word: str) -> str:
    stem = _stem(word)
    return _SYNONYMS.get(stem, _SYNONYMS.get(word, stem))


def split_identifier(text: str) -> str:
    """Spell identifiers as words: ``oil_spike-severe`` / ``oilSpikeSevere`` -> words."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.sub(r"[_\-:/.]+", " ", text)


def concept_tokens(text: str) -> list[tuple[str, int, int]]:
    """Content tokens as ``(concept, start, end)`` over the original text."""
    spelled = split_identifier(text)
    tokens: list[tuple[str, int, int]] = []
    # split_identifier keeps offsets except for camelCase spaces; map by scan.
    for match in re.finditer(r"[^\W\d_]+", spelled):
        word = match.group(0).casefold()
        if word in _STOPWORDS or len(word) < 2:
            continue
        tokens.append((_concept(word), match.start(), match.end()))
    return tokens


def label_concepts(label: str) -> list[str]:
    """Distinct content concepts of an outcome or action label, in order."""
    seen: list[str] = []
    for concept, _, _ in concept_tokens(label):
        if concept not in seen:
            seen.append(concept)
    return seen


def outcome_mentions(text: str, outcomes: list[str]) -> list[tuple[str, str]]:
    """Terminal outcomes a text refers to, with the matched stretch.

    A mention is every concept of the outcome label inside a window of
    ``len(concepts) + MENTION_SLACK`` content tokens, in any order.
    """
    tokens = concept_tokens(text)
    spelled = split_identifier(text)
    found: list[tuple[str, str]] = []
    for outcome in outcomes:
        wanted = label_concepts(outcome)
        if not wanted:
            continue
        width = len(wanted) + MENTION_SLACK
        for start in range(len(tokens)):
            window = tokens[start : start + width]
            present = {concept for concept, _, _ in window}
            if all(concept in present for concept in wanted):
                positions = [
                    (s, e) for concept, s, e in window if concept in wanted
                ]
                first = min(s for s, _ in positions)
                last = max(e for _, e in positions)
                found.append((outcome, spelled[first:last]))
                break
    return found


def _token_matches(wanted: str, have: str) -> bool:
    if wanted == have:
        return True
    shorter, longer = sorted((wanted, have), key=len)
    return len(shorter) >= 3 and longer.startswith(shorter)


def action_discussed(text: str, action: str) -> bool:
    """Whether a memo discusses an action: every concept of its label appears."""
    have = {concept for concept, _, _ in concept_tokens(text)}
    return all(
        any(_token_matches(wanted, concept) for concept in have)
        for wanted in label_concepts(action)
    )


# --------------------------------------------------------------------------
# Scenario tree (same rules as market_actor_sim/sim/model.py)
# --------------------------------------------------------------------------


@dataclass
class StateSpec:
    state_key: str
    actor: str
    #: action -> ("state", child_state_key) or ("outcome", outcome_name)
    actions: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass
class ScenarioTree:
    name: str
    description: str
    rewards: dict[str, float]
    headline_outcomes: list[str]
    states: dict[str, StateSpec]

    @property
    def outcomes(self) -> list[str]:
        return sorted(self.rewards)

    def fork_view(self) -> list[dict]:
        """Decision states as a role fork may see them: no outcome labels."""
        return [
            {
                "state_key": spec.state_key,
                "actor": spec.actor,
                "actions": [
                    {"action": action, "leads_to_state": target}
                    if kind == "state"
                    else {"action": action, "terminal": True}
                    for action, (kind, target) in spec.actions.items()
                ],
            }
            for spec in self.states.values()
        ]


def _build(spec: dict, state_key: str, states: dict[str, StateSpec]) -> None:
    if not isinstance(spec, dict) or "actor" not in spec or "actions" not in spec:
        raise ValueError(f"scenario node {state_key!r} needs actor and actions")
    node = StateSpec(state_key=state_key, actor=str(spec["actor"]))
    states[state_key] = node  # pre-order: the fork view reads root first
    for action, action_spec in spec["actions"].items():
        child_key = f"{state_key}/{action}" if state_key else str(action)
        if "outcome" in action_spec:
            node.actions[str(action)] = ("outcome", str(action_spec["outcome"]))
        elif "child" in action_spec:
            node.actions[str(action)] = ("state", child_key)
            _build(action_spec["child"], child_key, states)
        else:
            raise ValueError(f"action {child_key!r} needs 'outcome' or 'child'")


def parse_scenario(text: str) -> ScenarioTree:
    spec = yaml.safe_load(text)
    states: dict[str, StateSpec] = {}
    _build(spec["tree"], "", states)
    rewards = {str(k): float(v) for k, v in spec["rewards"].items()}
    reached = {target for s in states.values() for kind, target in s.actions.values() if kind == "outcome"}
    if reached - set(rewards):
        raise ValueError(f"outcomes without a reward: {sorted(reached - set(rewards))}")
    return ScenarioTree(
        name=str(spec.get("name", "")),
        description=str(spec.get("description", "")),
        rewards=rewards,
        headline_outcomes=[str(o) for o in spec.get("headline_outcomes", [])],
        states=states,
    )


def load_scenario(path: Path) -> ScenarioTree:
    return parse_scenario(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Packet citations
# --------------------------------------------------------------------------


def packet_citation_ids(packet: dict) -> dict[str, str]:
    """Citable identifiers in a frozen packet -> the evidence id they resolve to."""
    ids: dict[str, str] = {}
    for item in packet.get("admitted_observations", []):
        ids[item["evidence_id"].casefold()] = item["evidence_id"]
        ids[str(item["series_id"]).casefold()] = item["evidence_id"]
    for item in packet.get("admitted_prices", []):
        ids[item["evidence_id"].casefold()] = item["evidence_id"]
    for item in packet.get("memos", []):
        ids[item["memo_id"].casefold()] = item["memo_id"]
    return ids


def packet_absence_ids(packet: dict) -> set[str]:
    return {item["exclusion_id"].casefold() for item in packet.get("excluded_or_missing", [])}


_ID_TOKEN_RE = re.compile(r"[^\s,;()\[\]{}\"'`<>]+")


def resolve_citation(citation: str, ids: dict[str, str]) -> str | None:
    """The packet evidence id a free-text citation names, or None.

    A citation may carry commentary ("POLY:...:Yes = 0.795 at kt ..."); any
    whitespace-delimited token that is exactly a packet identifier resolves it.
    """
    for token in _ID_TOKEN_RE.findall(str(citation)):
        key = token.strip(".:").casefold()
        if key in ids:
            return ids[key]
        if token.casefold() in ids:
            return ids[token.casefold()]
    return None


# --------------------------------------------------------------------------
# Fork validation
# --------------------------------------------------------------------------


class ForkRejected(ValueError):
    """A role fork failed admission; ``problems`` lists every failed rule."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _is_probability(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_fork(fork: dict, tree: ScenarioTree, packet: dict | None = None) -> dict:
    """Validate one temperament fork; return it in canonical scenario order.

    Args:
        fork: ``{"temperament", "memos", "propensities"}``.
        tree: The scenario the fork answers.
        packet: The frozen evidence packet, or None to skip citation rules
            (the legacy path-based simulator inputs have no packet).

    Raises:
        ForkRejected: With every failed rule, so one resubmission can fix all.
    """
    problems: list[str] = []
    temperament = str(fork.get("temperament") or "").strip()
    if not _TEMPERAMENT_RE.match(temperament):
        problems.append("temperament must be 3-80 characters of letters, digits, '_' or '-'")
    memos = fork.get("memos")
    props = fork.get("propensities")
    if not isinstance(memos, dict) or not isinstance(props, dict):
        raise ForkRejected(problems + ["memos and propensities must be objects keyed by state_key"])
    states = set(tree.states)
    if set(memos) != states or set(props) != states:
        missing = sorted(states - set(memos)) + sorted(states - set(props))
        extra = sorted((set(memos) | set(props)) - states)
        problems.append(
            f"{temperament}: state coverage differs from scenario"
            + (f"; missing {missing}" if missing else "")
            + (f"; not in the scenario {extra}" if extra else "")
        )
        raise ForkRejected(problems)
    ids = packet_citation_ids(packet) if packet is not None else {}
    absence = packet_absence_ids(packet) if packet is not None else set()
    canonical_memos: dict[str, dict] = {}
    canonical_props: dict[str, dict] = {}
    for state_key, spec in tree.states.items():
        where = f"{temperament}/{state_key or '<root>'}"
        memo = memos[state_key]
        if not isinstance(memo, dict):
            problems.append(f"{where}: memo must be an object")
            continue
        if memo.get("actor") != spec.actor:
            problems.append(f"{where}: actor mismatch (expected {spec.actor})")
        analysis = str(memo.get("analysis", ""))
        if len(analysis) < MIN_MEMO_CHARS:
            problems.append(f"{where}: content memo too short ({len(analysis)} < {MIN_MEMO_CHARS} characters)")
        evidence = memo.get("evidence", [])
        missing_obs = memo.get("missing_observables", [])
        if not isinstance(evidence, list) or not isinstance(missing_obs, list):
            problems.append(f"{where}: evidence and missing_observables must be lists")
            evidence = evidence if isinstance(evidence, list) else []
            missing_obs = missing_obs if isinstance(missing_obs, list) else []
        text = "\n".join([analysis, *map(str, evidence), *map(str, missing_obs)])
        for outcome, matched in outcome_mentions(text, tree.outcomes):
            problems.append(
                f"{where}: terminal outcome mentioned ({outcome!r} via {matched!r}); "
                "score only this actor's own decision"
            )
        undiscussed = [a for a in spec.actions if not action_discussed(analysis, a)]
        if undiscussed:
            problems.append(f"{where}: memo does not discuss action(s) {undiscussed}")
        dist = props[state_key]
        if not isinstance(dist, dict) or set(dist) != set(spec.actions):
            problems.append(
                f"{where}: action mismatch; propensities must cover exactly {list(spec.actions)}"
            )
            continue
        values = [dist[action] for action in spec.actions]
        if any(not _is_probability(p) or p <= 0 or p >= 1 for p in values):
            problems.append(f"{where}: probabilities must be strictly between 0 and 1")
            continue
        if not math.isclose(sum(values), 1.0, abs_tol=SUM_TOLERANCE):
            problems.append(f"{where}: probabilities do not sum to one ({sum(values)!r})")
        if packet is not None:
            resolved = []
            unresolved = []
            for citation in evidence:
                target = resolve_citation(str(citation), ids)
                if target is not None:
                    resolved.append(target)
                elif any(tok.strip(".:").casefold() in absence for tok in _ID_TOKEN_RE.findall(str(citation))):
                    continue
                else:
                    unresolved.append(str(citation)[:80])
            if unresolved:
                problems.append(
                    f"{where}: citation(s) not in the frozen packet {unresolved}; "
                    "cite evidence ids, series_id values or memo ids from the packet"
                )
            if not resolved:
                uniform = 1.0 / len(values)
                spread = max(abs(p - uniform) for p in values)
                if not missing_obs:
                    problems.append(
                        f"{where}: no packet evidence cited; name the missing observable "
                        "that would sharpen this state"
                    )
                if spread > UNCITED_UNIFORM_TOLERANCE + 1e-12:
                    problems.append(
                        f"{where}: no packet evidence cited, so the vector must stay within "
                        f"{UNCITED_UNIFORM_TOLERANCE} of uniform ({uniform:.3f}); widest gap {spread:.3f}"
                    )
        canonical_memos[state_key] = {
            "actor": memo.get("actor"),
            "analysis": analysis,
            "evidence": [str(item) for item in evidence],
            "missing_observables": [str(item) for item in missing_obs],
        }
        canonical_props[state_key] = {action: float(dist[action]) for action in spec.actions}
    if problems:
        raise ForkRejected(problems)
    return {"temperament": temperament, "memos": canonical_memos, "propensities": canonical_props}


def validate_fork_set(forks: list[dict], tree: ScenarioTree, packet: dict | None = None) -> list[dict]:
    """Whole-ensemble rules: at least three forks with unique temperaments."""
    problems: list[str] = []
    if len(forks) < MIN_FORKS:
        problems.append(f"at least {MIN_FORKS} temperament forks are required; have {len(forks)}")
    labels = [str(fork.get("temperament") or "").strip() for fork in forks]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates or any(not label for label in labels):
        problems.append(f"temperament labels must be non-empty and unique; duplicates {duplicates}")
    accepted = []
    for fork in forks:
        try:
            accepted.append(validate_fork(fork, tree, packet))
        except ForkRejected as rejected:
            problems.extend(rejected.problems)
    if problems:
        raise ForkRejected(problems)
    return accepted
