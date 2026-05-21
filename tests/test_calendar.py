from __future__ import annotations

from datetime import date

from app.util.calendar import NSE_HOLIDAYS_2026, is_trading_day, reason_for_closure


def test_regular_weekday_is_trading_day() -> None:
    # 2026-05-21 is a Thursday and not in the holiday list
    d = date(2026, 5, 21)
    assert is_trading_day(d) is True
    assert reason_for_closure(d) is None


def test_saturday_is_not_trading_day() -> None:
    d = date(2026, 5, 23)  # Saturday
    assert is_trading_day(d) is False
    assert reason_for_closure(d) == "weekend_saturday"


def test_sunday_is_not_trading_day() -> None:
    d = date(2026, 5, 24)  # Sunday
    assert is_trading_day(d) is False
    assert reason_for_closure(d) == "weekend_sunday"


def test_known_holiday_is_not_trading_day() -> None:
    d = date(2026, 1, 26)  # Republic Day
    assert d in NSE_HOLIDAYS_2026
    assert is_trading_day(d) is False
    assert reason_for_closure(d) == "nse_holiday"


def test_holidays_list_is_a_frozenset() -> None:
    assert isinstance(NSE_HOLIDAYS_2026, frozenset)
    for d in NSE_HOLIDAYS_2026:
        assert isinstance(d, date)
        assert d.year == 2026
