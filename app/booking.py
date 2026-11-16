"""Call booking: finding open slots and turning a visitor's request into a pending calendar event.

The AI only ever *reads* availability. Booking happens through POST /book, which re-checks every rule
against the live calendar, so a visitor can't talk the model into booking anything.
"""

import hashlib
import hmac
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.calendar_api import GoogleCalendar, window
from app.config import Settings
from app.scheduling import Rules, open_slots, parse_hours, spread

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BookingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def rules_from(s: Settings) -> Rules:
    return Rules(
        hours=parse_hours(s.booking_hours),
        tz=ZoneInfo(s.owner_timezone),
        slot_minutes=s.slot_minutes,
        buffer_minutes=s.buffer_minutes,
        min_notice_hours=s.min_notice_hours,
        days_ahead=s.booking_days_ahead,
        max_per_day=s.max_bookings_per_day,
    )


def _calendar_state(cal: GoogleCalendar, rules: Rules, now: datetime):
    lo, hi = window(now, rules.days_ahead)
    bookings = cal.bookings(lo, hi)
    busy = cal.busy(lo, hi) + [(b.start, b.end) for b in bookings]  # pending events block too
    per_day: dict[date, int] = {}
    for b in bookings:
        d = b.start.astimezone(rules.tz).date()
        per_day[d] = per_day.get(d, 0) + 1
    return busy, per_day


def find_slots(
    cal: GoogleCalendar,
    rules: Rules,
    now: datetime,
    start_day: date | None = None,
    end_day: date | None = None,
    part_of_day: str = "any",
    limit: int = 8,
) -> list[datetime]:
    busy, per_day = _calendar_state(cal, rules, now)
    slots = open_slots(rules, now, busy, per_day, start_day, end_day, part_of_day)
    return spread(slots, limit)


def request_booking(
    cal: GoogleCalendar, rules: Rules, now: datetime, start: datetime, name: str, email: str, topic: str, review_url_for
):
    name, email, topic = name.strip()[:80], email.strip().lower()[:200], topic.strip()[:500]
    if not name:
        raise BookingError("Please enter your name.")
    if not EMAIL_RE.match(email):
        raise BookingError("Please enter a valid email address.")
    if start.tzinfo is None:
        raise BookingError("Invalid time.")

    lo, hi = window(now, rules.days_ahead)
    if cal.bookings(now, hi, email=email):
        raise BookingError(
            "You already have a call request with Salil. Please wait for that one, or email him to change it.", 409
        )

    local_day = start.astimezone(rules.tz).date()
    busy, per_day = _calendar_state(cal, rules, now)
    available = open_slots(rules, now, busy, per_day, local_day, local_day)
    if start.astimezone(timezone.utc) not in {s.astimezone(timezone.utc) for s in available}:
        raise BookingError("Sorry, that time is no longer available. Please pick another one.", 409)

    end = start + timedelta(minutes=rules.slot_minutes)
    return cal.create_pending(start.astimezone(rules.tz), end.astimezone(rules.tz), name, email, topic, review_url_for)


# ── signed review links ──────────────────────────────────────────────────────
def sign(secret: str, event_id: str) -> str:
    return hmac.new(secret.encode(), f"review:{event_id}".encode(), hashlib.sha256).hexdigest()[:32]


def verify(secret: str, event_id: str, sig: str) -> bool:
    return bool(secret) and hmac.compare_digest(sign(secret, event_id), sig or "")


def fmt(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    return local.strftime("%a %d %b, %I:%M %p").replace(" 0", " ") + f" {local.tzname()}"
