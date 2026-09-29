"""The zt-pead-scan playbook loads through VT's own loader and keeps its contract (ZT add-on).

Like the zt_dashboards playbooks it names tools on purpose: the zt-events scan
and the zt-approvals proposal tools. Every snake_case token in its body must be
one of those tool names or a field the scan really returns.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import conftest as C
import server

PLAYBOOK_DIR = Path(__file__).resolve().parents[1] / "playbooks"
SLUG = "zt-pead-scan"
APPROVAL_TOOLS = {"mcp_zt_approvals_propose_orders", "mcp_zt_approvals_list_order_proposals"}
STATES = {"PROPOSED", "DUPLICATE", "BLOCKED", "WATCH", "PENDING", "SUE-LATE", "STALE"}
# Monday 2026-09-28 12:00 New York time: the first fire is that evening at 17:45.
NOW_MS = int(datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc).timestamp() * 1000)
_SNAKE_RE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")


def _playbook():
    from src.scheduled_research.playbooks import get_playbook

    return get_playbook(SLUG, PLAYBOOK_DIR)


def test_loads_with_vts_loader_and_fires_after_the_close():
    from src.scheduled_research.executor import next_due
    from src.scheduled_research.models import JobStatus
    from src.scheduled_research.playbooks import build_job, list_playbooks

    assert [p.slug for p in list_playbooks(PLAYBOOK_DIR)] == [SLUG]
    playbook = _playbook()
    assert playbook.suggested_schedule == "45 17 * * 1-5"
    assert playbook.suggested_timezone == "America/New_York" and playbook.markets == ("us",)
    job = build_job(SLUG, PLAYBOOK_DIR, now_ms=NOW_MS)
    assert job.status is JobStatus.PENDING and job.timezone == "America/New_York"
    assert "{{" not in job.prompt and job.config["playbook"] == SLUG
    first = datetime.fromtimestamp(job.next_run_at / 1000, timezone.utc).isoformat()
    assert first == "2026-09-28T21:45:00+00:00"
    assert next_due(job.schedule, NOW_MS, job.timezone) == job.next_run_at


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
    assert SLUG in slugs and "earnings-season-tracker" in slugs   # bundled playbooks stay available


def test_body_keeps_the_contract():
    body = _playbook().body
    unwrapped = " ".join(body.split())
    for required in ("## When data is missing", "Data gaps", "from memory", "third-party summary",
                     "## Output", "## Boundaries", "## Verdict", "run environment"):
        assert required in body, required
    for rule in ("Orders are proposals only", "a human approves or rejects every proposal",
                 "Do not place, modify, or cancel any order", "do not approve or reject a proposal",
                 "with their `proposal` fields unchanged", "No price targets",
                 "Do not state, estimate, elicit or revise the probability of any outcome"):
        assert rule in unwrapped, rule
    assert unwrapped.lower().count("probabilit") == 1        # only in the boundary itself
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", body)
    assert body.rstrip().endswith("Report the state; the human decides.")


def test_names_only_its_tools_and_scan_fields():
    playbook = _playbook()
    tokens = set(_SNAKE_RE.findall(playbook.body))
    tools = {t for t in tokens if t.startswith("mcp_")}
    assert tools == {"mcp_zt_events_pead_candidates", "mcp_zt_events_sue"} | APPROVAL_TOOLS
    assert tools - APPROVAL_TOOLS <= {f"mcp_zt_events_{name}" for name in server.TOOL_NAMES}
    for capability in playbook.data_capabilities:
        assert not _SNAKE_RE.search(capability), capability


def test_every_field_it_names_is_in_the_scan_output(installed):
    keys: set = set()
    for asof in (C.ASOF_ENTRY, "2026-08-26T22:00:00Z"):         # an entry evening, an exit evening
        out = server.pead_candidates(asof=asof)
        keys |= set(out)
        for row in out["candidates"] + out["watchlist"] + out["exits"]:
            keys |= set(row) | set(row.get("proposal") or {})
    fields = {t for t in _SNAKE_RE.findall(_playbook().body) if not t.startswith("mcp_")}
    assert fields and fields <= keys, fields - keys


def test_the_verdict_vocabulary_parses():
    from src.scheduled_research.verdict import PARSE_OK, parse_verdict_section

    body = _playbook().body
    for state in STATES:
        assert f"`{state}`" in body, state
    briefing = ("## Data gaps\nnone\n\n## Verdict\n"
                "- AAA: PROPOSED - entry, proposal op-1\n"
                "- DDD: DUPLICATE - entry proposal already pending\n"
                "- BBB: WATCH - bottom bucket, shorts not enabled\n"
                "- EEE: PENDING - no 10-Q EPS yet\n"
                "- NVDA: SUE-LATE - 10-Q two sessions after the release\n"
                "- CCC: STALE - entry open passed\n"
                "- FFF: BLOCKED - approvals tool unavailable\n")
    parse, items = parse_verdict_section(briefing)
    assert parse == PARSE_OK and {item.state for item in items} == STATES


@pytest.mark.skipif(not os.environ.get("ZT_PLAYBOOK_DEPLOY_DIR"),
                    reason="set ZT_PLAYBOOK_DEPLOY_DIR to compare a deployed copy")
def test_deployed_copy_is_byte_identical():
    deployed = Path(os.environ["ZT_PLAYBOOK_DEPLOY_DIR"]) / f"{SLUG}.md"

    def lf(data: bytes) -> bytes:   # git autocrlf on the Windows checkout vs LF on G:
        return data.replace(b"\r\n", b"\n")

    assert lf(deployed.read_bytes()) == lf((PLAYBOOK_DIR / f"{SLUG}.md").read_bytes())
