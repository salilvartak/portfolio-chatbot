"""Google Calendar + Gmail access for call booking, using the owner's OAuth refresh token.

Booking requests become *pending* events on the owner's calendar (which blocks the slot). The owner
approves or declines from an emailed review link; approving invites the visitor with a Meet link.
All booking state lives on the event itself (extendedProperties), so no extra database is needed.
"""

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import lru_cache

from app.config import get_settings

SOURCE = "portfolio-bot"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/gmail.send",
]


@dataclass
class Booking:
    id: str
    start: datetime
    end: datetime
    name: str
    email: str
    topic: str
    state: str  # "pending" | "confirmed"


class GoogleCalendar:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, calendar_id: str = "primary"):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        self.calendar_id = calendar_id
        self._cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ── reading ──────────────────────────────────────────────────────────────
    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        resp = self._cal.freebusy().query(
            body={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": self.calendar_id}]}
        ).execute()
        periods = resp["calendars"].get(self.calendar_id, {}).get("busy", [])
        return [(_parse(p["start"]), _parse(p["end"])) for p in periods]

    def bookings(self, start: datetime, end: datetime, email: str | None = None) -> list[Booking]:
        """Calls booked through the site (pending or confirmed) in a time range."""
        props = [f"source={SOURCE}"] + ([f"visitor_email={email.lower()}"] if email else [])
        resp = self._cal.events().list(
            calendarId=self.calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            privateExtendedProperty=props,
            singleEvents=True,
            maxResults=250,
        ).execute()
        return [b for e in resp.get("items", []) if e.get("status") != "cancelled" and (b := _booking(e))]

    def get(self, event_id: str) -> Booking | None:
        from googleapiclient.errors import HttpError

        try:
            e = self._cal.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as err:
            if err.resp.status in (404, 410):
                return None
            raise
        return _booking(e) if e.get("status") != "cancelled" else None

    # ── writing ──────────────────────────────────────────────────────────────
    def create_pending(self, start: datetime, end: datetime, name: str, email: str, topic: str, review_url_for) -> Booking:
        event_id = uuid.uuid4().hex  # hex is valid in Calendar's base32hex id alphabet
        review_url = review_url_for(event_id)
        self._cal.events().insert(
            calendarId=self.calendar_id,
            sendUpdates="none",
            body={
                "id": event_id,
                "summary": f"[Pending] Call with {name}",
                "description": f"Requested via the portfolio chatbot.\n\nName: {name}\nEmail: {email}\n"
                f"Topic: {topic or '(none)'}\n\nApprove or decline: {review_url}",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
                "status": "tentative",
                "transparency": "opaque",
                "extendedProperties": {
                    "private": {
                        "source": SOURCE,
                        "state": "pending",
                        "visitor_email": email.lower(),
                        "visitor_name": name,
                        "topic": topic[:500],
                    }
                },
            },
        ).execute()
        return Booking(event_id, start, end, name, email.lower(), topic, "pending")

    def approve(self, event_id: str) -> None:
        b = self.get(event_id)
        self._cal.events().patch(
            calendarId=self.calendar_id,
            eventId=event_id,
            sendUpdates="all",
            conferenceDataVersion=1,
            body={
                "summary": f"Call with {b.name}",
                "status": "confirmed",
                "attendees": [{"email": b.email, "displayName": b.name}],
                "conferenceData": {
                    "createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}
                },
                "extendedProperties": {
                    "private": {
                        "source": SOURCE,
                        "state": "confirmed",
                        "visitor_email": b.email,
                        "visitor_name": b.name,
                        "topic": b.topic,
                    }
                },
            },
        ).execute()

    def delete(self, event_id: str, notify: bool = False) -> None:
        """Remove a booking. notify=True sends the attendee a cancellation (for already-confirmed calls)."""
        self._cal.events().delete(
            calendarId=self.calendar_id, eventId=event_id, sendUpdates="all" if notify else "none"
        ).execute()

    def send_email(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self._gmail.users().messages().send(userId="me", body={"raw": raw}).execute()


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _booking(e: dict) -> Booking | None:
    p = e.get("extendedProperties", {}).get("private", {})
    if p.get("source") != SOURCE or "dateTime" not in e.get("start", {}):
        return None
    return Booking(
        id=e["id"],
        start=_parse(e["start"]["dateTime"]),
        end=_parse(e["end"]["dateTime"]),
        name=p.get("visitor_name", ""),
        email=p.get("visitor_email", ""),
        topic=p.get("topic", ""),
        state=p.get("state", "pending"),
    )


@lru_cache
def get_calendar() -> GoogleCalendar | None:
    """The owner's calendar, or None when booking isn't configured."""
    s = get_settings()
    if not (s.google_oauth_client_id and s.google_oauth_client_secret and s.google_oauth_refresh_token):
        return None
    return GoogleCalendar(s.google_oauth_client_id, s.google_oauth_client_secret, s.google_oauth_refresh_token, s.calendar_id)


def window(now: datetime, days: int) -> tuple[datetime, datetime]:
    return now - timedelta(days=1), now + timedelta(days=days + 2)
