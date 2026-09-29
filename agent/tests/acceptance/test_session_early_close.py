import datetime as dt
import pandas as pd
import pytest
from backtest.asof_guard import US_EQUITY, CA_EQUITY, drop_unfinished_bars


@pytest.mark.parametrize("day, close", [("2025-11-28", "18:00"), ("2025-07-03", "17:00"),
                                      ("2025-12-24", "18:00")])
def test_early_close_before_and_after(day, close):
    frame = pd.DataFrame({"close": [100]}, index=pd.DatetimeIndex([day]))
    instant = pd.Timestamp(f"{day}T{close}:00Z")
    assert US_EQUITY.close_utc([day])[0] == instant
    assert len(drop_unfinished_bars({"NVDA.US": frame}, "1D", instant-pd.Timedelta(minutes=5))[0]["NVDA.US"]) == 0
    assert len(drop_unfinished_bars({"NVDA.US": frame}, "1D", instant+pd.Timedelta(minutes=5))[0]["NVDA.US"]) == 1


def test_regular_dst_non_us_and_order_preserved():
    days = ["2025-11-28", "2025-07-02", "2025-11-28"]
    assert list(US_EQUITY.close_utc(days)) == list(pd.to_datetime([
        "2025-11-28T18:00Z", "2025-07-02T20:00Z", "2025-11-28T18:00Z"]))
    assert CA_EQUITY.close_utc(["2025-11-28"])[0] == pd.Timestamp("2025-11-28T21:00Z")
