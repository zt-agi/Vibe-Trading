"""ZT add-on (2026-09-28): the concept-level ``probability`` figure role.

An LLM never sets an outcome probability. Every figure the answer presents as a
probability — declared ``probability`` or phrased as odds, likelihood, chance,
confidence, a scenario weight or a partition of 100% across scenarios — must
trace to an allowlisted model tool (actor simulation, prediction markets,
options tools, quantlib) or the draft is refused. Counts, weights and
thresholds keep their old path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent.grounding import GroundingLedger
from src.agent.grounding import probability
from src.agent.grounding.figures import parse_figures_block, scan_figures

pytestmark = pytest.mark.unit

# The 2026-08-28 pilot result, as run_market_actor_sim / inspect_market_actor_run
# return it (subset).
PILOT = {
    "run_key": "0123456789abcdef",
    "authority": "RESEARCH_PILOT_ONLY_UNCALIBRATED",
    "anchor_status": "elicited-only",
    "ensemble_mc": {
        "oil_down": 0.13385333333333332,
        "oil_range": 0.5768133333333333,
        "oil_spike_moderate": 0.26454333333333335,
        "oil_spike_severe": 0.02479,
    },
    "mc_wilson_95": {"oil_range": {"p": 0.5768133333333333, "wilson_halfwidth_95": 0.0017679759946954672}},
    "model_form_range": {"oil_range": {"min": 0.5348620000000001, "max": 0.61742}},
    "state_propensity_dispersion": {"": {"strike_iran": {"mean": 0.29333333333333333}}},
    "validation": {"fork_count": 3, "state_count": 9},
}


def _mcp(payload: dict[str, Any]) -> str:
    """How VT's MCP wrapper hands a structured tool result to the loop."""
    return json.dumps({"status": "ok", "data": payload, "structured_content": payload})


SIM = ("mcp_pit_actor_sim_run_market_actor_sim", {"packet_sha256": "a" * 64}, _mcp(PILOT), "sim1")
PM = (
    "prediction_market",
    {"mode": "market", "ids": ["1"]},
    json.dumps({"status": "ok", "markets": [{"question": "Ceasefire holds through Sep 30?", "outcomes": [
        {"outcome": "Yes", "implied_probability": 0.895, "implied_probability_pct": 89.5}]}]}),
    "pm1",
)
QL = (
    "quantlib_call",
    {"action": "call", "module": "risk", "function": "analyze_mc_results", "kwargs": {}},
    json.dumps({"ok": True, "result": {"prob_loss": 0.4213, "mean_return": 0.012, "var": 0.18}}),
    "q1",
)
CHAIN = (
    "get_options_chain",
    {"symbol": "USO.US"},
    json.dumps({"ok": True, "calls": [{"strike": 95.0, "last": 8.0}, {"strike": 105.0, "last": 3.0}]}),
    "oc1",
)
SEARCH = ("web_search", {"query": "iran odds"}, json.dumps({"ok": True, "results": [{"score": 0.7}]}), "ws1")


def _ledger(tmp_path: Path, *calls: tuple[str, dict[str, Any], str, str]) -> GroundingLedger:
    ledger = GroundingLedger(run_dir=tmp_path, user_message="How could the Iran situation play out?")
    for tool, arguments, result, call_id in calls:
        ledger.ingest_tool_result(
            tool_name=tool, arguments=arguments, result=result, call_id=call_id, success=True
        )
    return ledger


def _block(*rows: str) -> str:
    return "\n\n```figures\n" + "\n".join(rows) + "\n```"


def _reasons(result: Any) -> list[str]:
    return [str(issue.get("reason")) for issue in result.issues]


def _claims(text: str) -> list[str]:
    block = parse_figures_block(text)
    return [claim.text for claim in probability.find_claims(text, scan_figures(text, block), block)]


# ---------------------------------------------------------------------------
# Paraphrases go through the concept check: an invented probability fails
# however it is phrased.
# ---------------------------------------------------------------------------

PARAPHRASES = [
    "There is a 70% probability of escalation.",
    "The odds of escalation are 70%.",
    "Escalation is 70% likely.",
    "A 70% chance that Iran escalates.",
    "The likelihood of a strike is 0.3.",
    "Base case (50%): the crisis is contained.",
    "Base case: 50%.",
    "Bull 25% / base 50% / bear 25%.",
    "P(escalation) = 0.30",
    "We see a 30% risk of recession.",
    "3-to-1 odds against escalation.",
    "Only a 1 in 4 chance of a deal.",
    "Only a one in four chance of a deal.",
    "A deal is a coin flip.",
    "A 70 percent chance of a strike.",
    "Confidence 70% that the uptrend holds.",
    "Scenario weights: escalation 30%, status quo 50%, de-escalation 20%.",
    "| Scenario | Probability |\n|---|---|\n| Escalation | 30% |\n| Status quo | 70% |",
    "| Scenario | Weight |\n|---|---|\n| Escalation | 30% |\n| Status quo | 70% |",
    "- Escalation: 30%\n- Status quo: 50%\n- De-escalation: 20%",
    "明日上涨概率 70%。",
    "有70%的可能会升级。",
    "我们有七成把握。",
    "百分之七十的概率会谈判。",
    "三分之一的可能性会破裂。",
    "基准情形（50%）：局势受控。",
]


@pytest.mark.parametrize("text", PARAPHRASES)
def test_an_invented_probability_fails_however_it_is_phrased(tmp_path: Path, text: str) -> None:
    result = _ledger(tmp_path, SIM).validate_final_answer(text)

    assert result.valid is False
    assert set(_reasons(result)) == {"probability_not_from_model_tool"}, result.issues


@pytest.mark.parametrize(
    ("prose", "row"),
    [
        ("There is a 70% probability of escalation.", "70% | count | my estimate"),
        ("There is a 70% probability of escalation.", "70% | cited | analyst consensus"),
        ("There is a 70% probability of escalation.", "70% | observed | desk view"),
        ("There is a 70% probability of escalation.", "70% | probability | judgment"),
        ("Escalation looks plausible at 70%.", "70% | count | subjective probability"),
        ("Escalation looks plausible at 70%.", "70% | probability | judgment"),
    ],
)
def test_no_declared_role_launders_an_invented_probability(tmp_path: Path, prose: str, row: str) -> None:
    result = _ledger(tmp_path, SIM, PM).validate_final_answer(prose + _block(row))

    assert result.valid is False
    assert _reasons(result) == ["probability_not_from_model_tool"], result.issues


def test_a_probability_quoted_from_a_non_model_tool_is_refused(tmp_path: Path) -> None:
    result = _ledger(tmp_path, SIM, SEARCH).validate_final_answer(
        "A 70% chance of escalation." + _block("70% | probability | search result score | ws1")
    )

    assert result.valid is False
    assert _reasons(result) == ["probability_ref_not_model_tool"]


def test_elicited_propensities_are_not_outcome_probabilities(tmp_path: Path) -> None:
    """The simulator returns the forks' mean action propensities; they are inputs."""
    result = _ledger(tmp_path, SIM).validate_final_answer(
        "The probability that the US strikes is 29.3%." + _block("29.3% | probability | fork mean | sim1")
    )

    assert result.valid is False
    assert _reasons(result) == ["probability_not_from_model_tool"]


# ---------------------------------------------------------------------------
# Allowlisted model tools ground probabilities.
# ---------------------------------------------------------------------------


def test_actor_simulation_probabilities_pass_declared_and_phrased(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, SIM)
    declared = ledger.validate_final_answer(
        "The actor simulation puts the range outcome at 57.7% (53.5%–61.7% across temperaments)."
        + _block(
            "57.7% | probability | oil_range, ensemble | sim1",
            "53.5% | probability | model_form_range min | model_form_range.oil_range.min",
            "61.7% | probability | model_form_range max | sim1",
        )
    )
    phrased = ledger.validate_final_answer("P(oil_range) = 0.577 and P(oil_down) = 0.134 in the simulation.")
    rounded = ledger.validate_final_answer("The simulated probability of the range outcome is 58%.")

    assert declared.valid is True, declared.issues
    assert phrased.valid is True, phrased.issues
    assert rounded.valid is True, rounded.issues
    assert ledger.validate_final_answer("The simulated probability of the range outcome is 60%.").valid is False


def test_market_implied_and_mechanical_probabilities_pass(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, PM, QL)
    market = ledger.validate_final_answer(
        "Polymarket implies an 89.5% probability that the ceasefire holds."
        + _block("89.5% | probability | implied_probability | pm1")
    )
    mechanical = ledger.validate_final_answer("The GBM simulation gives a 42.1% probability of a loss.")
    wrong_call = ledger.validate_final_answer(
        "Polymarket implies an 89.5% probability that the ceasefire holds."
        + _block("89.5% | probability | implied | q1")
    )

    assert market.valid is True, market.issues
    assert mechanical.valid is True, mechanical.issues
    assert wrong_call.valid is False


def test_a_derived_probability_needs_model_tool_operands(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, CHAIN, PM)
    digital = ledger.validate_final_answer(
        "The call spread implies roughly a 50% probability that USO.US finishes above 100."
        + _block("50% | derived | (8.0 - 3.0) / (105 - 95)")
    )
    complement = ledger.validate_final_answer(
        "The market prices a 10.5% chance that the ceasefire breaks." + _block("10.5% | derived | 1 - 0.895")
    )
    invented = ledger.validate_final_answer(
        "Escalation odds are 70%." + _block("70% | derived | 0.9 - 0.2")
    )

    assert digital.valid is True, digital.issues
    assert complement.valid is True, complement.issues
    assert _reasons(invented) == ["probability_derivation_not_from_model_tool"]


def test_an_invented_probability_is_cut_and_the_model_one_released(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, SIM)
    draft = (
        "The simulation puts the range outcome at 57.7%. My own view: a 70% chance of escalation."
        + _block("57.7% | probability | oil_range | sim1")
    )
    validation = ledger.validate_final_answer(draft)

    released = ledger.redacted_release(draft, validation)

    assert released is not None
    assert "57.7%" in released
    assert "70%" not in released
    assert "(omitted※)" in released


# ---------------------------------------------------------------------------
# Negative controls: counts, weights, thresholds and measured frequencies.
# ---------------------------------------------------------------------------

NEGATIVE_CONTROLS = [
    "Use a 20-day window across 5 hotspots.",
    "Portfolio weight 30% in energy.",
    "Equities 60%, bonds 30%, cash 10%.",
    "| Asset | Weight |\n|---|---|\n| Equities | 60% |\n| Bonds | 40% |",
    "| Asset | Base case | Bear case |\n|---|---|---|\n| Equities | 60% | 30% |\n| Bonds | 40% | 70% |",
    "VaR at 99% confidence is 2.1% of NAV.",
    "The 95% confidence interval is 0.8 to 1.2.",
    "The p-value is 0.03, below the 0.05 threshold.",
    "Signal threshold 0.7; stop at -8%.",
    "Backtest win rate 55%, hit rate 0.62, max drawdown 20%.",
    "In the base case, EPS grows 12%.",
    "Escalation (30% prob) implies a ~10% supply disruption.",
    "胜率 55%，仓位上限 5%，置信区间 95%。",
    "In 3 of 5 scenarios the book loses money.",
]


@pytest.mark.parametrize("text", NEGATIVE_CONTROLS)
def test_counts_weights_and_thresholds_are_not_probabilities(text: str) -> None:
    claims = _claims(text)
    if text.startswith("Escalation (30% prob)"):
        assert claims == ["30%"]  # the probability, not the disruption size
    else:
        assert claims == []


def test_negative_controls_keep_their_old_verdicts(tmp_path: Path) -> None:
    """Declared as before, a weight and a threshold still pass."""
    ledger = _ledger(tmp_path, SIM)
    result = ledger.validate_final_answer(
        "Portfolio weight 30% in energy; alert threshold 0.7."
        + _block("30% | count | weight", "0.7 | count | threshold")
    )

    assert result.valid is True, result.issues


def test_the_allowlist_matches_mcp_wrapped_tools_by_suffix() -> None:
    assert probability.tool_base("mcp_pit_actor_sim_run_market_actor_sim") == "run_market_actor_sim"
    assert probability.tool_base("mcp_other_inspect_market_actor_run") == "inspect_market_actor_run"
    assert probability.tool_base("prediction_market") == "prediction_market"
    assert probability.tool_base("web_search") is None
    assert probability.tool_base("mcp_pit_actor_sim_submit_role_fork") is None
    assert probability.is_probability_field("run_market_actor_sim", "data.ensemble_mc.oil_range")
    assert not probability.is_probability_field(
        "inspect_market_actor_run", "data.state_propensity_dispersion..strike_iran.mean"
    )
    assert probability.is_probability_field("prediction_market", "markets[0].outcomes[0].implied_probability")
    assert not probability.is_probability_field("prediction_market", "markets[0].outcomes[0].price_usd")
    assert probability.is_probability_field("quantlib_call", "result.prob_loss")
