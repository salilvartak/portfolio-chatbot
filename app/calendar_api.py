"""Google Calendar + email access for call booking.

Booking requests become *pending* events on the owner's calendar (which blocks the slot). The owner
approves or declines from an emailed review link; approving invites the visitor with a Meet link.
All booking state lives on the event itself (extendedProperties), so no extra database is needed.

Two ways to reach the owner's Google account (same booking logic on top):
- Apps Script (recommended): a small script deployed as a web app in the owner's account
  (see apps_script/Code.gs). No OAuth client, consent screen or expiring tokens.
- OAuth: a refresh token for an OAuth client (needs the consent screen published).
"""

import base64
import json
import logging
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import lru_cache

from app.config import get_settings

log = logging.getLogger("chatbot")

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


# ── transports: the handful of raw operations booking needs ───────────────────
class AppsScriptTransport:
    """Calls the owner's Apps Script web app (apps_script/Code.gs) with a shared secret."""

    def __init__(self, url: str, secret: str, timeout: float = 25):
        self.url, self.secret, self.timeout = url, secret, timeout

    def __call__(self, op: str, **args):
        data = json.dumps({"secret": self.secret, "op": op, "args": args}).encode()
        req = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"})
        # Apps Script answers POSTs with a redirect to the result; urllib follows it as a GET.
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode()
        try:
            out = json.loads(raw)
        except ValueError:
            raise RuntimeError(f"Apps Script returned non-JSON (is it deployed for 'Anyone'?): {raw[:200]}")
        if "error" in out:
            raise RuntimeError(f"Apps Script error on {op}: {out['error']}")
        return out.get("result")


class GoogleApiTransport:
    """Calls Google's APIs directly with an OAuth refresh token."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, calendar_id: str):
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
        self.cal_id = calendar_id
        self._cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def __call__(self, op: str, **a):
        from googleapiclient.errors import HttpError

        ev = self._cal.events()
        if op == "freebusy":
            return self._cal.freebusy().query(
                body={"timeMin": a["timeMin"], "timeMax": a["timeMax"], "items": [{"id": self.cal_id}]}
            ).execute()
        if op == "list":
            return ev.list(
                calendarId=self.cal_id, timeMin=a["timeMin"], timeMax=a["timeMax"],
                privateExtendedProperty=f"source={SOURCE}", singleEvents=True, maxResults=250,
            ).execute()
        if op == "get":
            try:
                return ev.get(calendarId=self.cal_id, eventId=a["eventId"]).execute()
            except HttpError as err:
                if err.resp.status in (404, 410):
                    return None
                raise
        if op == "insert":
            return ev.insert(calendarId=self.cal_id, sendUpdates=a["sendUpdates"], body=a["body"]).execute()
        if op == "patch":
            return ev.patch(
                calendarId=self.cal_id, eventId=a["eventId"], sendUpdates=a["sendUpdates"],
                conferenceDataVersion=a.get("conferenceDataVersion", 0), body=a["body"],
            ).execute()
        if op == "delete":
            return ev.delete(calendarId=self.cal_id, eventId=a["eventId"], sendUpdates=a["sendUpdates"]).execute()
        if op == "email":
            msg = EmailMessage()
            msg["To"], msg["Subject"] = a["to"], a["subject"]
            msg.set_content(a["body"])
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            return self._gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        raise ValueError(f"unknown op {op}")


# ── booking operations ───────────────────────────────────────────────────────
class GoogleCalendar:
    def __init__(self, call, calendar_id: str = "primary"):
        self._call = call
        self.calendar_id = calendar_id

    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        resp = self._call("freebusy", timeMin=start.isoformat(), timeMax=end.isoformat()) or {}
        cals = resp.get("calendars", {})
        periods = (cals.get(self.calendar_id) or next(iter(cals.values()), {})).get("busy", [])
        return [(_parse(p["start"]), _parse(p["end"])) for p in periods]

    def bookings(self, start: datetime, end: datetime, email: str | None = None) -> list[Booking]:
        """Calls booked through the site (pending or confirmed) in a time range."""
        resp = self._call("list", timeMin=start.isoformat(), timeMax=end.isoformat()) or {}
        out = [b for e in resp.get("items", []) if e.get("status") != "cancelled" and (b := _booking(e))]
        return [b for b in out if b.email == email.lower()] if email else out

    def get(self, event_id: str) -> Booking | None:
        e = self._call("get", eventId=event_id)
        return _booking(e) if e and e.get("status") != "cancelled" else None

    def create_pending(self, start: datetime, end: datetime, name: str, email: str, topic: str, review_url_for) -> Booking:
        event_id = uuid.uuid4().hex  # hex is valid in Calendar's base32hex id alphabet
        review_url = review_url_for(event_id)
        self._call(
            "insert",
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
                "extendedProperties": {"private": _props(name, email, topic, "pending")},
            },
        )
        return Booking(event_id, start, end, name, email.lower(), topic, "pending")

    def approve(self, event_id: str) -> None:
        b = self.get(event_id)
        self._call(
            "patch",
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
                "extendedProperties": {"private": _props(b.name, b.email, b.topic, "confirmed")},
            },
        )

    def delete(self, event_id: str, notify: bool = False) -> None:
        """Remove a booking. notify=True sends the attendee a cancellation (for already-confirmed calls)."""
        self._call("delete", eventId=event_id, sendUpdates="all" if notify else "none")

    def send_email(self, to: str, subject: str, body: str) -> None:
        self._call("email", to=to, subject=subject, body=body)


def _props(name: str, email: str, topic: str, state: str) -> dict:
    return {"source": SOURCE, "state": state, "visitor_email": email.lower(), "visitor_name": name, "topic": topic[:500]}


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _booking(e: dict) -> Booking | None:
    p = (e.get("extendedProperties") or {}).get("private") or {}
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
    if not s.admin_token.strip():
        log.warning("call booking is OFF: missing ADMIN_TOKEN")
        return None
    if s.apps_script_url.strip() and s.apps_script_secret.strip():
        log.info("call booking is ON (via Apps Script)")
        return GoogleCalendar(AppsScriptTransport(s.apps_script_url.strip(), s.apps_script_secret.strip()))
    oauth = [s.google_oauth_client_id, s.google_oauth_client_secret, s.google_oauth_refresh_token]
    if all(v.strip() for v in oauth):
        log.info("call booking is ON (via OAuth, calendar=%s)", s.calendar_id)
        transport = GoogleApiTransport(*(v.strip() for v in oauth), s.calendar_id)
        return GoogleCalendar(transport, s.calendar_id)
    log.warning("call booking is OFF: set APPS_SCRIPT_URL + APPS_SCRIPT_SECRET (or the GOOGLE_OAUTH_* values)")
    return None


def window(now: datetime, days: int) -> tuple[datetime, datetime]:
    return now - timedelta(days=1), now + timedelta(days=days + 2)
