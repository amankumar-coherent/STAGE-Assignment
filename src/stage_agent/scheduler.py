"""Time handling: UTC parsing, IST calendar days, quiet hours and message slot scheduling.

All datetimes inside the agent are timezone-aware UTC. IST is derived from Asia/Kolkata when the platform
has a tz database; otherwise a fixed +05:30 offset is used, which is exact because India has observed no
daylight-saving time since 1945.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

UTC = timezone.utc
try:  # pragma: no cover - depends on the host tz database
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    IST_SOURCE = "zoneinfo:Asia/Kolkata"
except Exception:  # ZoneInfoNotFoundError on Windows without the tzdata package
    IST = timezone(timedelta(hours=5, minutes=30), "IST")
    IST_SOURCE = "fixed:+05:30"


def parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime. Returns None for empty input."""
    if value is None or value == "":
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp not allowed: {value!r}")
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_day(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def ist_clock(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class QuietHours:
    """Quiet window in IST, e.g. 22:00-08:00 (wraps midnight). Start inclusive, end exclusive."""

    def __init__(self, start: str = "22:00", end: str = "08:00"):
        self.start = _parse_hhmm(start)
        self.end = _parse_hhmm(end)

    def contains(self, dt: datetime) -> bool:
        t = dt.astimezone(IST).time()
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def next_allowed(self, dt: datetime) -> datetime:
        """Earliest instant >= dt that is outside quiet hours."""
        if not self.contains(dt):
            return dt
        local = dt.astimezone(IST)
        candidate = local.replace(hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)


def ist_at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC)


def plan_message_slot(
    earliest: datetime,
    deadline: datetime,
    quiet: QuietHours,
    taken_days: set[date],
    morning_hour_ist: int,
    max_days_ahead: int = 3,
) -> datetime | None:
    """First send time >= earliest, outside quiet hours, on an IST day with no message yet, before deadline.

    When the earliest instant falls in quiet hours the message moves to `morning_hour_ist` of the next allowed
    day (later, never earlier). Days already holding a push/WhatsApp are skipped (combined daily cap).
    """
    candidate = earliest
    if quiet.contains(candidate):
        nxt = quiet.next_allowed(candidate)
        candidate = max(nxt, ist_at(ist_day(nxt), morning_hour_ist))
    for _ in range(max_days_ahead + 1):
        if candidate >= deadline:
            return None
        day = ist_day(candidate)
        if day not in taken_days and not quiet.contains(candidate):
            return candidate
        candidate = ist_at(day + timedelta(days=1), morning_hour_ist)
    return None
