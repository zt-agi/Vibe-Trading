"""The ZT playbooks load through VT's own loader and keep the bundled contract.

They differ from the bundled catalogue in one deliberate way: they name the
zt-dashboards tools, because they exist to read those tools. Every snake_case
token in a body must be one of those tool names or a documented envelope field.
"""
import os
import re
from datetime import datetime, timezone

import pytest

from conftest import EXTENSION, load_extension_module

PLAYBOOK_DIR = EXTENSION / "playbooks"
SCHEDULES = {"alpha-monitor-brief": "45 8 * * 1-5", "asm-state": "0 9 * * 1-5",
             "pit-health": "15 9 * * *"}
# Monday 2026-09-28 08:00 New York time.
NOW_MS = int(datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
FIRST_FIRE_UTC = {"alpha-monitor-brief": "2026-09-28T12:45:00+00:00",
                  "asm-state": "2026-09-28T13:00:00+00:00",
                  "pit-health": "2026-09-28T13:15:00+00:00"}
FIELD_NAMES = {"as_of", "as_of_basis", "source_path", "pit_label", "missing_files",
               "alpha_ready", "hurdle_t"}
STATES = {
    "alpha-monitor-brief": {"FRESH", "STALE", "PARTIAL", "UNDATED", "BLOCKED", "MISSING"},
    "asm-state": {"FRESH", "STALE", "MISSING"},
    "pit-health": {"FRESH", "STALE", "MISSING", "PASS", "FAIL", "CLEAN", "ERRORS", "UNKNOWN"},
}
SAMPLE_VERDICTS = {
    "alpha-monitor-brief": ("- EXPORT: FRESH - export for the run date read\n"
                            "- BROKER_POSITIONS: STALE - 69.4 days old\n"
                            "- COMPONENT_MEMORY_PRICES: UNDATED - no observation date\n"
                            "- INTRADAY_OHLCV_TA: BLOCKED - retention policy\n"
                            "- MACRO_FRED: PARTIAL - partial bundle\n"
                            "- EDGAR_13F_INDEX: FRESH - weekly index current"),
    "asm-state": ("- SIGNAL-STATE: MISSING - file absent\n"
                  "- LANE-BOARD: STALE - dated by build log\n"
                  "- TEST-LEDGER: STALE - last run on the monitor date\n"
                  "- PROMOTION-BOARD: STALE - no verdicts yet\n"
                  "- KR-SEMI-EXP: FRESH - collector live\n"
                  "- PCU334413334413: FRESH - within 45-day limit\n"
                  "- OCC-EQ-PCR: STALE - collector not built"),
    "pit-health": ("- PIT-DAILY: FRESH - run started 14:38 UTC\n"
                   "- PIT-AUDIT: PASS - 10 of 10 checks\n"
                   "- PIT-EXIT: UNKNOWN - no exit line yet\n"
                   "- INGEST-24H: CLEAN - 11 runs ok\n"
                   "- AUDIT-RECEIPT: STALE - older than one hour\n"
                   "- INDEX-RECEIPT: MISSING - VIBE_TRADING_HOME not set"),
}
_SNAKE_RE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")


@pytest.fixture(scope="module")
def tool_names():
    server = load_extension_module("zt_dashboards_server", "server.py")
    return {f"mcp_zt_dashboards_{name}" for name in server.TOOL_NAMES}


def _playbook(slug):
    from src.scheduled_research.playbooks import get_playbook

    return get_playbook(slug, PLAYBOOK_DIR)


def test_catalogue_loads_with_vt_loader():
    from src.scheduled_research.playbooks import list_playbooks

    playbooks = list_playbooks(PLAYBOOK_DIR)
    assert {p.slug: p.suggested_schedule for p in playbooks} == SCHEDULES
    assert {p.suggested_timezone for p in playbooks} == {"America/New_York"}


def test_found_through_vibe_trading_playbook_dir(monkeypatch):
    from src.config.accessor import reset_env_config
    from src.scheduled_research.playbooks import list_playbooks

    monkeypatch.setenv("VIBE_TRADING_PLAYBOOK_DIR", str(PLAYBOOK_DIR))
    reset_env_config()
    try:
        slugs = {p.slug for p in list_playbooks()}
    finally:
        monkeypatch.delenv("VIBE_TRADING_PLAYBOOK_DIR")
        reset_env_config()
    assert set(SCHEDULES) <= slugs
    assert "premarket-brief" in slugs  # bundled playbooks stay available


@pytest.mark.parametrize("slug", sorted(SCHEDULES))
def test_body_keeps_the_bundled_contract(slug):
    body = _playbook(slug).body
    unwrapped = " ".join(body.split())
    for required in ("## When data is missing", "Data gaps", "from memory",
                     "third-party summary", "## Output", "## Boundaries", "## Verdict",
                     "run environment"):
        assert required in body, required
    for rule in ("No buy, sell, or hold calls", "no price targets",
                 "Do not place, modify, or cancel any order",
                 "Do not state, estimate, elicit or revise the probability"):
        assert rule in unwrapped, rule
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", body)


@pytest.mark.parametrize("slug", sorted(SCHEDULES))
def test_names_only_zt_tools_and_envelope_fields(slug, tool_names):
    playbook = _playbook(slug)
    tokens = set(_SNAKE_RE.findall(playbook.body))
    used_tools = {t for t in tokens if t.startswith("mcp_")}
    assert used_tools and used_tools <= tool_names
    assert tokens - used_tools <= FIELD_NAMES
    for capability in playbook.data_capabilities:
        assert not _SNAKE_RE.search(capability), capability


@pytest.mark.parametrize("slug", sorted(SCHEDULES))
def test_builds_a_job_that_first_fires_on_the_authored_time(slug):
    from src.scheduled_research.executor import next_due
    from src.scheduled_research.models import JobStatus
    from src.scheduled_research.playbooks import build_job

    job = build_job(slug, PLAYBOOK_DIR, now_ms=NOW_MS)
    assert job.status is JobStatus.PENDING and job.timezone == "America/New_York"
    assert "{{" not in job.prompt and job.config["playbook"] == slug
    first = datetime.fromtimestamp(job.next_run_at / 1000, timezone.utc).isoformat()
    assert first == FIRST_FIRE_UTC[slug]
    assert next_due(job.schedule, NOW_MS, job.timezone) == job.next_run_at


@pytest.mark.parametrize("slug", sorted(SCHEDULES))
def test_declared_verdict_vocabulary_parses(slug):
    from src.scheduled_research.verdict import PARSE_OK, parse_verdict_section

    body = _playbook(slug).body
    for state in STATES[slug]:
        assert f"`{state}`" in body, state
    briefing = "## Data gaps\nnone\n\n## Verdict\n" + SAMPLE_VERDICTS[slug] + "\n"
    parse, items = parse_verdict_section(briefing)
    assert parse == PARSE_OK
    assert {item.state for item in items} <= STATES[slug]


@pytest.mark.skipif(not os.environ.get("ZT_PLAYBOOK_DEPLOY_DIR"),
                    reason="set ZT_PLAYBOOK_DEPLOY_DIR to compare a deployed copy")
@pytest.mark.parametrize("slug", sorted(SCHEDULES))
def test_deployed_copy_is_byte_identical(slug):
    from pathlib import Path

    deployed = Path(os.environ["ZT_PLAYBOOK_DEPLOY_DIR"]) / f"{slug}.md"
    # Line endings may differ (git autocrlf on the Windows checkout vs LF on G:).
    def _lf(b: bytes) -> bytes:
        return b.replace(b"\r\n", b"\n")
    assert _lf(deployed.read_bytes()) == _lf((PLAYBOOK_DIR / f"{slug}.md").read_bytes())
