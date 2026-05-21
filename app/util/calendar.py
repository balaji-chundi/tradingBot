"""NSE trading-day calendar.

For Phase 6 we hard-code the 2026 NSE equity-segment holiday list (published
by NSE annually as a circular). Update the dict each January when the
following year's calendar is released. Weekends are always non-trading.

The runtime check `is_trading_day(d)` takes an IST date and returns whether
the equity segment is open. Used by:
  - orchestrator: skip feed thread + scheduler on non-trading days
  - scripts/morning_start.sh: skip auth refresh + uvicorn restart
  - scheduler.RegimeScheduler._guarded_task: skip out-of-band Tier 1 calls
  - scripts/eod_report.py: skip on non-trading days

Source: NSE circular `Holiday List for the Calendar Year 2026` (verify in Jan).
"""

from __future__ import annotations

from datetime import date

# NSE Equity holidays 2026 — placeholder list pending the official circular.
# These are typical national holidays that fall on weekdays; the actual NSE
# list may shift a date or add/remove one. Update once the official circular
# lands. Weekends are filtered separately.
NSE_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 26),  # Republic Day (Mon)
        date(2026, 3, 4),  # Holi (Wed)
        date(2026, 3, 31),  # Eid-ul-Fitr (Tue)
        date(2026, 4, 3),  # Good Friday (Fri)
        date(2026, 4, 14),  # Dr. Ambedkar Jayanti (Tue)
        date(2026, 5, 1),  # Maharashtra Day (Fri)
        date(2026, 5, 27),  # Buddha Pournima (Wed)
        date(2026, 8, 15),  # Independence Day (Sat — already weekend)
        date(2026, 8, 26),  # Ganesh Chaturthi (Wed)
        date(2026, 10, 2),  # Gandhi Jayanti (Fri)
        date(
            2026, 11, 8
        ),  # Diwali / Laxmi Pujan (Sun — already weekend; muhurat session typically held)
        date(2026, 11, 9),  # Balipratipada (Mon)
        date(2026, 11, 25),  # Guru Nanak Jayanti (Wed)
        date(2026, 12, 25),  # Christmas (Fri)
    }
)


def is_trading_day(d: date) -> bool:
    """True iff NSE equity is open on `d`. False for weekends and holidays."""
    if d.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return d not in NSE_HOLIDAYS_2026


def reason_for_closure(d: date) -> str | None:
    """Human-readable reason `d` is closed, or None if it's a trading day."""
    if d.weekday() == 5:
        return "weekend_saturday"
    if d.weekday() == 6:
        return "weekend_sunday"
    if d in NSE_HOLIDAYS_2026:
        return "nse_holiday"
    return None
