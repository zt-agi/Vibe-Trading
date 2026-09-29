"""Tradeable entries require both independent information inputs to be PIT."""
import pytest
import core


@pytest.mark.parametrize("field", ["pit_class", "sue_pit_class"])
@pytest.mark.parametrize("value", [None, "", "MISSING", "UNKNOWN", "NON_PIT", "RECONSTRUCTED_PIT"])
def test_entry_refuses_each_unverified_input(field, value):
    row = {"pit_class": "TRUE_PIT", "sue_pit_class": "OBSERVED_PIT", field: value}
    assert field in core.entry_pit_reason(row)
    with pytest.raises(ValueError, match=field):
        core.proposal_arguments(row, "entry", 0.1, 20)


@pytest.mark.parametrize("event", ["TRUE_PIT", "OBSERVED_PIT"])
@pytest.mark.parametrize("sue", ["TRUE_PIT", "OBSERVED_PIT"])
def test_verified_inputs_eligible(event, sue):
    assert core.entry_pit_reason({"pit_class": event, "sue_pit_class": sue}) is None


def test_exit_remains_available_for_held_position():
    row = {"ticker": "AAA", "accession": "acc", "pit_class": "NON_PIT", "sue_pit_class": None,
           "direction": "LONG", "entry_tag": "prior", "exit_open_utc": "2026-09-29T13:30:00Z"}
    assert core.proposal_arguments(row, "exit", 0.0, 20)["targets"] == {"AAA": 0.0}
