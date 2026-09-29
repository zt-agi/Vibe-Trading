"""Role-fork templates: neutral investor checklists bound to scenario actors.

ZT add-on (2026-09-29). A template is a small YAML file in the canonical
project (``vt_addons/role_fork_templates/<template_id>.yaml``) that tells a
role fork how to play one security-buyer/seller role class of ZT's MCT
hierarchy -- which frozen evidence to read, what its state-local output must
contain, and which position directions the role may take. Templates were
adapted from ai-hedge-fund (MIT, commit 5d2c7ca2): its persona prompts and v1
numeric scorers became neutral checklists; persona names, signals, confidence
scores and scoring weights were dropped, because a role's incentives are
elicited from documents, never set as coefficients.

A scenario binds actors to templates with an optional top-level mapping the
simulator ignores::

    actor_roles:
      value_funds: {role_class: ActiveMutualFunds, sub_role: value, template: value_checklist}

At every state whose actor is bound to a template, a submitted fork must:

* carry ``memo.checklist`` with one entry per checklist item: either cited
  packet ids plus the finding, or ``MISSING: <what is absent>``;
* list every id its checklist cites in ``memo.evidence`` too;
* keep the vector near uniform over the permitted actions when every required
  item is MISSING (no role evidence, no departure from indifference);
* give an action its template forbids (a long-only role's ``short`` and
  ``cover`` actions) exactly ``fork_rules.FORBIDDEN_FLOOR``: the simulator
  needs every entry strictly above zero, so "no mass" is the smallest
  admissible entry.

An action's direction is read from its ``direction:`` in the scenario YAML,
else inferred from its label words (``sell_short`` -> short); an action whose
direction cannot be determined stays permitted and is reported as
``unspecified`` so the scenario author can declare it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:
    import fork_rules
except ImportError:  # imported as a package module rather than run as a script
    from . import fork_rules

TEMPLATE_SCHEMA = "vt.role_fork_template.v1"
TEMPLATES_REL = "vt_addons/role_fork_templates"

#: ZT's Tier 1 security buyers and sellers (investment-mct-setup). Mechanical
#: flows are NatureNodes driven by a stated rule, never role forks.
ROLE_CLASSES = ("ActiveMutualFunds", "LongShortHedgeFunds", "ForeignInstitutions",
                "DomesticInstitutions", "PensionSovereign", "Retail",
                "CorporateIssuerAsTrader", "InvestorMarket")
NATURE_NODE_CLASSES = ("LeveragedETFRebalance", "OptionsDealerHedging")
CLASS_KINDS = ("Aggregate", "Decider")
APPROACHES = ("long_only", "long_short")
#: Directions a long-only role may not take.
LONG_ONLY_FORBIDDEN = ("short", "cover")

#: Label words -> direction, checked in this order (``sell_short`` is short,
#: ``buy_to_cover`` is cover, ``trim_long`` is reduce).
DIRECTION_PRECEDENCE = ("cover", "short", "reduce", "long", "neutral")
DEFAULT_DIRECTION_WORDS = {
    "cover": ("cover", "covers", "covering"),
    "short": ("short", "shorts", "shorting", "shortsell", "shortselling"),
    "reduce": ("sell", "sells", "selling", "trim", "trims", "reduce", "reduces", "exit", "exits",
               "liquidate", "redeem", "redemption", "redemptions", "lighten", "underweight",
               "divest", "outflow", "outflows"),
    "long": ("buy", "buys", "buying", "accumulate", "add", "adds", "initiate", "long", "increase",
             "build", "overweight", "inflow", "inflows"),
    "neutral": ("hold", "holds", "wait", "maintain", "pass", "unchanged", "stay", "keep", "abstain"),
}

#: Persona names the templates must not carry (ZT: neutral role names, no
#: famous names), checked on every string of a template.
BANNED_NAMES = ("buffett", "munger", "graham", "lynch", "druckenmiller", "soros", "dalio",
                "ackman", "icahn", "burry", "templeton", "klarman", "greenblatt", "tepper",
                "einhorn", "damodaran", "jhunjhunwala", "cathie", "berkshire", "magellan")
#: Keys that would turn a checklist into a scoring model.
BANNED_KEYS = ("weight", "weights", "coefficient", "coefficients", "score_weight", "points")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_ROLE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_ITEM_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
#: What a template's output_schema must describe (the rules themselves are
#: fixed in fork_rules and here).
OUTPUT_KEYS = ("propensities", "evidence", "memo", "forbidden_actions")
_MISSING_RE = re.compile(r"^\s*MISSING\b\s*[:\-]?\s*(?P<reason>.*)$", re.IGNORECASE | re.DOTALL)
MAX_CHECKLIST_TEXT = 600


class TemplateError(ValueError):
    """A role-fork template file or binding is invalid."""


@dataclass
class Template:
    template_id: str
    body: dict
    sha256: str
    path: str  # relative to the project root

    @property
    def role_name(self) -> str:
        return self.body["role_name"]

    @property
    def checklist(self) -> list[dict]:
        return self.body["checklist"]

    @property
    def forbidden_directions(self) -> tuple[str, ...]:
        return tuple(self.body.get("forbidden_directions") or ())

    def words(self) -> dict[str, tuple[str, ...]]:
        custom = self.body.get("direction_words") or {}
        return {d: tuple(custom.get(d, DEFAULT_DIRECTION_WORDS[d])) for d in DIRECTION_PRECEDENCE}


@dataclass
class Binding:
    actor: str
    role_class: str
    sub_role: str | None
    template: Template | None
    #: state_key -> {action: direction}
    directions: dict[str, dict[str, str]] = field(default_factory=dict)


def templates_dir(project_root: Path) -> Path:
    return Path(project_root) / TEMPLATES_REL


def _strings(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key, f"{path}/{key}") if isinstance(key, str) else ()
            yield from _strings(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def validate_template(body, template_id: str) -> dict:
    """Check a parsed template; return it unchanged or raise TemplateError."""
    problems: list[str] = []
    if not isinstance(body, dict):
        raise TemplateError(f"template {template_id}: not a mapping")
    if body.get("schema") != TEMPLATE_SCHEMA:
        problems.append(f"schema must be {TEMPLATE_SCHEMA}")
    if body.get("template_id") != template_id:
        problems.append(f"template_id must equal the file name ({template_id})")
    if not _ROLE_NAME_RE.match(str(body.get("role_name") or "")):
        problems.append("role_name must be an upper-case neutral label such as VALUE_CHECKLIST_ALLOCATOR")
    role_class = body.get("role_class")
    if role_class in NATURE_NODE_CLASSES:
        problems.append(f"{role_class} is a mechanical flow (NatureNode), never a role fork")
    elif role_class not in ROLE_CLASSES:
        problems.append(f"role_class must be one of {ROLE_CLASSES}")
    if body.get("class_kind") not in CLASS_KINDS:
        problems.append(f"class_kind must be one of {CLASS_KINDS}")
    approach = body.get("investment_approach")
    if approach not in APPROACHES:
        problems.append(f"investment_approach must be one of {APPROACHES}")
    forbidden = body.get("forbidden_directions") or []
    if not isinstance(forbidden, list) or any(d not in fork_rules.DIRECTIONS for d in forbidden):
        problems.append(f"forbidden_directions must list directions from {fork_rules.DIRECTIONS}")
    elif approach == "long_only" and not set(LONG_ONLY_FORBIDDEN) <= set(forbidden):
        problems.append(f"a long_only role must forbid {list(LONG_ONLY_FORBIDDEN)}")
    elif approach == "long_short" and forbidden:
        problems.append("a long_short role forbids no direction")
    words = body.get("direction_words")
    if words is not None and (not isinstance(words, dict) or set(words) - set(DIRECTION_PRECEDENCE)
                              or any(not isinstance(v, list) or not all(isinstance(w, str) for w in v)
                                     for v in words.values())):
        problems.append(f"direction_words must map {DIRECTION_PRECEDENCE} to lists of words")
    checklist = body.get("checklist")
    if not isinstance(checklist, list) or not 3 <= len(checklist) <= 12:
        problems.append("checklist must list 3 to 12 items")
        checklist = []
    seen = set()
    for index, item in enumerate(checklist):
        where = f"checklist[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be a mapping")
            continue
        item_id = str(item.get("id") or "")
        if not _ITEM_ID_RE.match(item_id) or item_id in seen:
            problems.append(f"{where}.id must be a unique short upper-case id such as V1")
        seen.add(item_id)
        if not str(item.get("item") or "").strip():
            problems.append(f"{where}.item is required")
        read = item.get("read")
        if not isinstance(read, list) or not read or not all(isinstance(r, str) and r.strip() for r in read):
            problems.append(f"{where}.read must list the evidence to read")
        if not isinstance(item.get("required"), bool):
            problems.append(f"{where}.required must be true or false")
    if checklist and not any(isinstance(i, dict) and i.get("required") is True for i in checklist):
        problems.append("at least one checklist item must be required")
    output = body.get("output_schema")
    if not isinstance(output, dict) or any(not str(output.get(k) or "").strip() for k in OUTPUT_KEYS):
        problems.append(f"output_schema must describe {list(OUTPUT_KEYS)}")
    banned_keys = sorted({k for k in _keys(body) if k.lower() in BANNED_KEYS})
    if banned_keys:
        problems.append(f"no scoring keys {banned_keys}: incentives are elicited from documents, "
                        "never set as coefficients")
    for where, text in _strings(body):
        found = [name for name in BANNED_NAMES
                 if re.search(rf"(?<![A-Za-z]){name}(?![A-Za-z])", text, re.IGNORECASE)]
        if found:
            problems.append(f"{where or '/'} names {found}; templates use neutral role names")
        if _YEAR_RE.search(text):
            problems.append(f"{where or '/'} carries a calendar year; templates are period-free")
    if problems:
        raise TemplateError(f"template {template_id}: " + "; ".join(problems))
    return body


def load_template(project_root: Path, template_id: str) -> Template:
    """Read and validate ``vt_addons/role_fork_templates/<template_id>.yaml``."""
    if not _ID_RE.match(str(template_id or "")):
        raise TemplateError(f"invalid template id {template_id!r}")
    folder = templates_dir(project_root)
    path = folder / f"{template_id}.yaml"
    if not path.is_file():
        raise TemplateError(f"role-fork template {template_id!r} is not installed at "
                            f"{TEMPLATES_REL}/{template_id}.yaml")
    data = path.read_bytes()
    body = validate_template(yaml.safe_load(data.decode("utf-8")), template_id)
    return Template(template_id=template_id, body=body, sha256=hashlib.sha256(data).hexdigest(),
                    path=f"{TEMPLATES_REL}/{template_id}.yaml")


def list_templates(project_root: Path) -> list[Template]:
    folder = templates_dir(project_root)
    if not folder.is_dir():
        return []
    return [load_template(project_root, path.stem) for path in sorted(folder.glob("*.yaml"))]


def action_direction(label: str, declared: str | None,
                     words: dict[str, tuple[str, ...]] | None = None) -> str:
    """The action's position direction: declared, else inferred, else ``unspecified``."""
    if declared:
        return declared
    vocabulary = words or DEFAULT_DIRECTION_WORDS
    tokens = {word.casefold() for word in re.findall(r"[^\W\d_]+", fork_rules.split_identifier(label))}
    for direction in DIRECTION_PRECEDENCE:
        if tokens & set(vocabulary.get(direction, ())):
            return direction
    return "unspecified"


def bindings(tree: fork_rules.ScenarioTree, project_root: Path,
             frozen: dict[str, str] | None = None) -> dict[str, Binding]:
    """The scenario's actor -> role bindings, templates loaded and checked.

    ``frozen`` (template_id -> sha256 recorded at packet freeze) refuses a
    template file that changed afterwards.
    """
    result: dict[str, Binding] = {}
    for actor, role in tree.actor_roles.items():
        template = None
        if role.get("template"):
            template = load_template(project_root, role["template"])
            if frozen is not None and frozen.get(template.template_id) != template.sha256:
                raise TemplateError(f"role-fork template {template.template_id} changed after packet "
                                    "freeze; freeze a new packet")
            body = template.body
            if body["role_class"] != role["role_class"]:
                raise TemplateError(f"actor {actor!r} is bound as {role['role_class']} but template "
                                    f"{template.template_id} plays {body['role_class']}")
            if role.get("sub_role") and body.get("sub_role") and role["sub_role"] != body["sub_role"]:
                raise TemplateError(f"actor {actor!r} sub_role {role['sub_role']!r} differs from template "
                                    f"{template.template_id} sub_role {body['sub_role']!r}")
        binding = Binding(actor=actor, role_class=role["role_class"], sub_role=role.get("sub_role"),
                          template=template)
        words = template.words() if template else None
        for state_key, spec in tree.states.items():
            if spec.actor == actor:
                binding.directions[state_key] = {
                    action: action_direction(action, spec.directions.get(action), words)
                    for action in spec.actions}
        result[actor] = binding
    return result


def frozen_hashes(bound: dict[str, Binding]) -> dict[str, dict]:
    """template_id -> {sha256, path} for the packet body."""
    return {b.template.template_id: {"sha256": b.template.sha256, "path": b.template.path}
            for b in bound.values() if b.template}


def forbidden_actions(bound: dict[str, Binding]) -> dict[str, set[str]]:
    """state_key -> actions the bound template forbids there."""
    result: dict[str, set[str]] = {}
    for binding in bound.values():
        if not binding.template:
            continue
        blocked = set(binding.template.forbidden_directions)
        for state_key, directions in binding.directions.items():
            actions = {a for a, d in directions.items() if d in blocked}
            if actions:
                result[state_key] = actions
    return result


def template_states(bound: dict[str, Binding]) -> dict[str, Template]:
    """state_key -> template for every template-bound state."""
    return {state_key: b.template for b in bound.values() if b.template for state_key in b.directions}


def fork_view(bound: dict[str, Binding]) -> dict:
    """What a role fork reads about the templates bound in its packet."""
    templates: dict[str, dict] = {}
    for binding in bound.values():
        if not binding.template:
            continue
        body = binding.template.body
        entry = templates.setdefault(binding.template.template_id, {
            "role_name": body["role_name"], "role_class": body["role_class"],
            "sub_role": body.get("sub_role"), "class_kind": body["class_kind"],
            "investment_approach": body["investment_approach"],
            "purpose": body.get("purpose", ""),
            "checklist": [{"id": i["id"], "item": i["item"], "read": list(i["read"]),
                           "required": i["required"]} for i in body["checklist"]],
            "output_schema": dict(body["output_schema"]),
            "forbidden_directions": list(binding.template.forbidden_directions),
            "applies_to": [],
        })
        for state_key, directions in binding.directions.items():
            blocked = sorted(a for a, d in directions.items() if d in binding.template.forbidden_directions)
            entry["applies_to"].append({
                "state_key": state_key, "actor": binding.actor,
                "action_directions": dict(directions),
                "forbidden_actions": blocked,
                "unspecified_direction_actions": sorted(a for a, d in directions.items() if d == "unspecified"),
            })
    if not templates:
        return {}
    return {
        "role_templates": templates,
        "role_template_contract": (
            "At a state listed under a template's applies_to: memo.checklist is an object with one "
            "entry per checklist id -- the packet ids you read and what they show, or 'MISSING: <what "
            "is absent>'; every id a checklist entry cites is also listed in memo.evidence; each "
            f"forbidden action gets exactly {fork_rules.FORBIDDEN_FLOOR}; when every required item is "
            f"MISSING the vector stays within {fork_rules.UNCITED_UNIFORM_TOLERANCE} of uniform over the "
            "permitted actions. The checklist names what to read; how far the evidence moves the "
            "actor's own tendency at this state is your judgment, argued in the memo. Nothing here "
            "asks about how the scenario ends."),
    }


def validate_template_states(fork: dict, tree: fork_rules.ScenarioTree, bound: dict[str, Binding],
                             citation_ids: dict[str, str], absence_ids: set[str]) -> list[str]:
    """Checklist and permitted-uniform rules for every template-bound state."""
    problems: list[str] = []
    memos = fork.get("memos") if isinstance(fork.get("memos"), dict) else {}
    props = fork.get("propensities") if isinstance(fork.get("propensities"), dict) else {}
    temperament = str(fork.get("temperament") or "").strip()
    blocked_by_state = forbidden_actions(bound)
    for state_key, template in template_states(bound).items():
        where = f"{temperament}/{state_key or '<root>'}"
        memo = memos.get(state_key)
        if not isinstance(memo, dict):
            continue  # fork_rules reports the missing state
        checklist = memo.get("checklist")
        wanted = [item["id"] for item in template.checklist]
        if not isinstance(checklist, dict):
            problems.append(f"{where}: role template {template.template_id} needs memo.checklist with "
                            f"entries {wanted}")
            continue
        missing_ids = [i for i in wanted if i not in checklist]
        extra_ids = sorted(str(k) for k in checklist if k not in wanted)
        if missing_ids or extra_ids:
            problems.append(f"{where}: memo.checklist must have exactly the entries {wanted}"
                            + (f"; missing {missing_ids}" if missing_ids else "")
                            + (f"; not in the template {extra_ids}" if extra_ids else ""))
        evidence = memo.get("evidence") if isinstance(memo.get("evidence"), list) else []
        memo_cited = {fork_rules.resolve_citation(str(c), citation_ids) for c in evidence} - {None}
        required_missing, required = 0, 0
        for item in template.checklist:
            text = checklist.get(item["id"])
            if text is None:
                continue
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHECKLIST_TEXT:
                problems.append(f"{where}: checklist {item['id']} must be text of 1..{MAX_CHECKLIST_TEXT} "
                                "characters")
                continue
            for outcome, matched in fork_rules.outcome_mentions(text, tree.outcomes):
                problems.append(f"{where}: checklist {item['id']} mentions terminal outcome {outcome!r} "
                                f"via {matched!r}; score only this actor's own decision")
            required += bool(item["required"])
            missing = _MISSING_RE.match(text)
            if missing:
                if not missing.group("reason").strip():
                    problems.append(f"{where}: checklist {item['id']} says MISSING without naming what "
                                    "is absent")
                required_missing += bool(item["required"])
                continue
            cited = set()
            for token in re.findall(r"[^\s,;()\[\]{}\"'`<>]+", text):
                target = fork_rules.resolve_citation(token, citation_ids)
                if target:
                    cited.add(target)
                elif token.strip(".:").casefold() in absence_ids:
                    cited.add(token.strip(".:").upper())
            if not cited:
                problems.append(f"{where}: checklist {item['id']} cites no packet id; cite the evidence "
                                "ids you read or write 'MISSING: <what is absent>'")
                continue
            not_listed = sorted(c for c in cited if c not in memo_cited
                                and c.casefold() not in absence_ids)
            if not_listed:
                problems.append(f"{where}: checklist {item['id']} cites {not_listed}; list them in "
                                "memo.evidence too")
        dist = props.get(state_key)
        if required and required_missing == required and isinstance(dist, dict):
            spec = tree.states[state_key]
            blocked = blocked_by_state.get(state_key, set())
            permitted = [a for a in spec.actions if a not in blocked] or list(spec.actions)
            try:
                uniform = 1.0 / len(permitted)
                spread = max(abs(float(dist[a]) - uniform) for a in permitted)
            except (KeyError, TypeError, ValueError):
                continue  # fork_rules reports malformed vectors
            if spread > fork_rules.UNCITED_UNIFORM_TOLERANCE + 1e-12:
                problems.append(
                    f"{where}: every required checklist item is MISSING, so the vector must stay within "
                    f"{fork_rules.UNCITED_UNIFORM_TOLERANCE} of uniform over the permitted actions "
                    f"({uniform:.3f}); widest gap {spread:.3f}")
    return problems
