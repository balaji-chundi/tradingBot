from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.engine.interfaces import Dialect


class UtcDateTime(TypeDecorator[datetime]):
    """Timestamp column that round-trips with tzinfo=UTC.

    SQLite has no TIMESTAMPTZ, so DateTime(timezone=True) silently strips tzinfo
    on read. This wrapper rejects naive datetimes on write and reattaches UTC on
    read so the rest of the app can treat all stored timestamps as aware.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime rejected; pass a UTC-aware datetime.")
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"Expected datetime from DB, got {type(value).__name__}")
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
