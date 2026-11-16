"""Booking rules: turn weekly hours + busy times into open call slots. Pure logic, no I/O."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Parts of the day a visitor might ask for, in the owner's local time (minutes from midnight).
PARTS_OF_DAY = {
    "morning": (0, 12 * 60),
    "afternoon": (12 * 60, 17 * 60),
    "evening": (17 * 60, 21 * 60),
    "night": (21 * 60, 24 * 60),
}


@dataclass(frozen=True)
class Rules:
    hours: dict[int, list[tuple[int, int]]]  # weekday (0=Mon) -> [(start_min, end_min)]
    tz: ZoneInfo
    slot_minutes: int = 30
    buffer_minutes: int = 15
    min_notice_hours: int = 12
    days_ahead: int = 14
    max_per_day: int = 3


def parse_hours(spec: str) -> dict[int, list[tuple[int, int]]]:
    """Parse e.g. "mon-fri 19:00-24:00; sat-sun 10:00-14:00, 19:00-24:00"."""
    hours: dict[int, list[tuple[int, int]]] = {d: [] for d in range(7)}
    for part in filter(None, (p.strip() for p in spec.split(";"))):
        m = re.match(r"^([a-z]{3})(?:-([a-z]{3}))?\s+(.+)$", part.lower())
        if not m:
            raise ValueError(f"Can't parse booking hours: {part!r}")
        first, last = DAYS.index(m.group(1)), DAYS.index(m.group(2) or m.group(1))
        days = range(first, last + 1) if first <= last else [*range(first, 7), *range(0, last + 1)]
        for win in m.group(3).split(","):
            a, b = (x.strip() for x in win.split("-"))
            start, end = _minutes(a), _minutes(b)
            if not 0 <= start < end <= 24 * 60:
                raise ValueError(f"Bad time window: {win!r}")
            for d in days:
                hours[d].append((start, end))
    return hours


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def open_slots(
    rules: Rules,
    now: datetime,
    busy: list[tuple[datetime, datetime]],
    booked_per_day: dict[date, int] | None = None,
    start_day: date | None = None,
    end_day: date | None = None,
    part_of_day: str = "any",
) -> list[datetime]:
    """Open slot start times (timezone-aware, owner's timezone), earliest first.

    busy: (start, end) intervals already taken on the calendar.
    booked_per_day: how many calls were already booked through the site on each local date.
    """
    booked_per_day = booked_per_day or {}
    local_now = now.astimezone(rules.tz)
    earliest = now + timedelta(hours=rules.min_notice_hours)
    first_day = local_now.date()
    last_day = first_day + timedelta(days=rules.days_ahead)
    lo = max(start_day or first_day, first_day)
    hi = min(end_day or last_day, last_day)
    part = PARTS_OF_DAY.get(part_of_day)
    pad = timedelta(minutes=rules.buffer_minutes)
    length = timedelta(minutes=rules.slot_minutes)

    out = []
    day = lo
    while day <= hi:
        if booked_per_day.get(day, 0) < rules.max_per_day:
            midnight = datetime(day.year, day.month, day.day, tzinfo=rules.tz)
            for start_min, end_min in rules.hours[day.weekday()]:
                m = start_min
                while m + rules.slot_minutes <= end_min:
                    if part is None or part[0] <= m < part[1]:
                        start = midnight + timedelta(minutes=m)
                        end = start + length
                        if start >= earliest and not any(start < b_end + pad and end > b_start - pad for b_start, b_end in busy):
                            out.append(start)
                    m += rules.slot_minutes
        day += timedelta(days=1)
    return out


def spread(slots: list[datetime], limit: int = 8) -> list[datetime]:
    """Pick up to `limit` slots spread across days, so visitors see options on several days."""
    by_day: dict[date, list[datetime]] = {}
    for s in slots:
        by_day.setdefault(s.date(), []).append(s)
    picked: list[datetime] = []
    rnd = 0
    while len(picked) < limit and any(len(v) > rnd for v in by_day.values()):
        for day_slots in by_day.values():
            if rnd < len(day_slots) and len(picked) < limit:
                picked.append(day_slots[rnd])
        rnd += 1
    return sorted(picked)
