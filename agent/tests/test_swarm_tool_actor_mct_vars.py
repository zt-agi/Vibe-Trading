"""ZT add-on (2026-09-28): run_swarm variables for the actor_mct_team user preset.

The preset's only variables are scenario_id and run_asof. A prompt's tickers
must not reach user_vars, or the swarm would pre-fetch today's bars into an
as-of run and hand them to the role forks as a Ground Truth block.
"""

from __future__ import annotations

from src.swarm.grounding import extract_symbols_from_user_vars
from src.tools.swarm_tool import _build_variables


def test_only_the_scenario_and_the_asof_are_passed() -> None:
    prompt = (
        "Run actor_mct_team for scenario_id=iran_oil as of 2026-08-28T12:00:00Z; "
        "compare with USO and XLE.US and NVDA pricing."
    )

    variables = _build_variables("actor_mct_team", prompt)

    assert variables == {"scenario_id": "iran_oil", "run_asof": "2026-08-28T12:00:00Z"}
    assert extract_symbols_from_user_vars(variables) == []


def test_a_scenario_path_and_an_offset_asof_are_understood() -> None:
    variables = _build_variables(
        "actor_mct_team", "Use config/scenario_iran_oil.yaml at 2026-08-28T08:00:00-04:00."
    )

    assert variables == {"scenario_id": "iran_oil", "run_asof": "2026-08-28T08:00:00-04:00"}


def test_a_missing_asof_is_left_missing_not_guessed() -> None:
    variables = _build_variables("actor_mct_team", "scenario: iran_oil, as of today please")

    assert variables == {"scenario_id": "iran_oil"}


def test_other_presets_keep_their_builders() -> None:
    assert set(_build_variables("geopolitical_war_room", "Hormuz blockade")) == {"crisis", "market"}
