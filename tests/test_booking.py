import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

import app.main as main
from app.booking import BookingError, find_slots, request_booking, sign, verify
from app.calendar_api import Booking
from app.graph import build_graph
from app.scheduling import Rules, open_slots, parse_hours, spread

IST = ZoneInfo("Asia/Kolkata")
HOURS = "mon-fri 19:00-24:00; sat-sun 10:00-14:00, 19:00-24:00"
RULES = Rules(hours=parse_hours(HOURS), tz=IST)
# Monday 16 Nov 2026, 08:00 IST
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=IST).astimezone(timezone.utc)


def ist(d, hh, mm=0):
    return datetime(2026, 11, d, hh, mm, tzinfo=IST)


# ── rules ────────────────────────────────────────────────────────────────────
def test_parse_hours():
    h = parse_hours(HOURS)
    assert h[0] == [(19 * 60, 24 * 60)]                       # Monday
    assert h[5] == h[6] == [(600, 840), (1140, 1440)]          # weekend
    with pytest.raises(ValueError):
        parse_hours("mon 20:00-19:00")


def test_weekday_evening_slots_until_midnight():
    # Monday 08:00 + 12h notice = 20:00 earliest
    slots = open_slots(RULES, NOW, [], start_day=date(2026, 11, 16), end_day=date(2026, 11, 16))
    assert slots[0] == ist(16, 20, 0) and slots[-1] == ist(16, 23, 30)
    assert len(slots) == 8


def test_weekend_two_windows():
    slots = open_slots(RULES, NOW, [], start_day=date(2026, 11, 21), end_day=date(2026, 11, 21))  # Saturday
    times = [s.strftime("%H:%M") for s in slots]
    assert times[:8] == ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30"]
    assert times[8] == "19:00" and times[-1] == "23:30" and "14:00" not in times


def test_busy_and_buffer_block_slots():
    busy = [(ist(17, 20, 0), ist(17, 21, 0))]  # Tuesday 8-9 PM meeting
    times = {s.strftime("%H:%M") for s in open_slots(RULES, NOW, busy, start_day=date(2026, 11, 17), end_day=date(2026, 11, 17))}
    assert "19:00" in times
    assert not times & {"19:30", "20:00", "20:30", "21:00"}  # overlap or within 15 min buffer
    assert "21:30" in times


def test_daily_cap_and_horizon_and_part_of_day():
    full = {date(2026, 11, 17): 3}
    assert not open_slots(RULES, NOW, [], full, date(2026, 11, 17), date(2026, 11, 17))
    assert not open_slots(RULES, NOW, [], start_day=date(2026, 12, 25), end_day=date(2026, 12, 26))
    night = open_slots(RULES, NOW, [], start_day=date(2026, 11, 21), end_day=date(2026, 11, 21), part_of_day="morning")
    assert night and all(s.hour < 12 for s in night)


def test_spread_gives_varied_times():
    picked = spread(open_slots(RULES, NOW, []))
    by_day = {}
    for s in picked:
        by_day.setdefault(s.date(), []).append(s.strftime("%H:%M"))
    assert by_day[date(2026, 11, 16)] == ["20:00", "22:00", "23:30"]  # Mon after the 12 h notice
    assert by_day[date(2026, 11, 17)] == ["19:00", "21:00", "23:30"]
    assert len(by_day) == 3 and picked == sorted(picked)
    sat = spread(open_slots(RULES, NOW, [], start_day=date(2026, 11, 21), end_day=date(2026, 11, 21)))
    assert [s.strftime("%H:%M") for s in sat] == ["10:00", "19:00", "23:30"]  # both weekend windows


# ── fake calendar ────────────────────────────────────────────────────────────
class FakeCalendar:
    def __init__(self):
        self.events: dict[str, Booking] = {}
        self.busy_times: list = []
        self.emails: list = []
        self.deleted: list = []

    def busy(self, start, end):
        return list(self.busy_times)

    def bookings(self, start, end, email=None):
        return [b for b in self.events.values() if start <= b.start <= end and (email is None or b.email == email)]

    def get(self, event_id):
        return self.events.get(event_id)

    def create_pending(self, start, end, name, email, topic, review_url_for):
        eid = f"ev{len(self.events) + 1}"
        review_url_for(eid)
        self.events[eid] = Booking(eid, start, end, name, email, topic, "pending")
        return self.events[eid]

    def approve(self, event_id):
        self.events[event_id].state = "confirmed"

    def delete(self, event_id, notify=False):
        self.deleted.append((event_id, notify))
        self.events.pop(event_id)

    def send_email(self, to, subject, body):
        self.emails.append((to, subject, body))


def test_request_booking_rules():
    cal = FakeCalendar()
    b = request_booking(cal, RULES, NOW, ist(17, 20, 0), " Ada ", "Ada@Example.com", "Hiring", lambda i: "")
    assert b.state == "pending" and b.email == "ada@example.com" and b.end - b.start == timedelta(minutes=30)

    with pytest.raises(BookingError, match="already have"):
        request_booking(cal, RULES, NOW, ist(18, 20, 0), "Ada", "ada@example.com", "", lambda i: "")
    with pytest.raises(BookingError, match="no longer available"):  # pending event blocks the slot
        request_booking(cal, RULES, NOW, ist(17, 20, 0), "Bob", "bob@example.com", "", lambda i: "")
    with pytest.raises(BookingError, match="no longer available"):  # outside hours
        request_booking(cal, RULES, NOW, ist(17, 15, 0), "Bob", "bob@example.com", "", lambda i: "")
    with pytest.raises(BookingError, match="no longer available"):  # inside the 12h notice
        request_booking(cal, RULES, NOW, ist(16, 19, 30), "Bob", "bob@example.com", "", lambda i: "")
    with pytest.raises(BookingError, match="valid email"):
        request_booking(cal, RULES, NOW, ist(18, 20, 0), "Bob", "not-an-email", "", lambda i: "")


def test_find_slots_counts_pending_bookings_toward_daily_cap():
    cal = FakeCalendar()
    for i, h in enumerate((20, 21, 22)):
        request_booking(cal, RULES, NOW, ist(17, h, 0), f"P{i}", f"p{i}@x.dev", "", lambda e: "")
    assert all(s.date() != date(2026, 11, 17) for s in find_slots(cal, RULES, NOW, days=20, per_day=20))


def test_signed_links():
    assert verify("secret", "ev1", sign("secret", "ev1"))
    assert not verify("secret", "ev2", sign("secret", "ev1"))
    assert not verify("", "ev1", sign("", "ev1"))


# ── HTTP endpoints ───────────────────────────────────────────────────────────
@pytest.fixture
def api(monkeypatch):
    cal = FakeCalendar()
    monkeypatch.setattr(main, "get_calendar", lambda: cal)
    monkeypatch.setattr(main, "_now", lambda: NOW)
    monkeypatch.setattr(main.settings, "admin_token", "secret")
    monkeypatch.setattr(main.settings, "contact_email", "owner@x.dev")
    main.limiter.reset()
    return TestClient(main.app), cal


def test_book_review_approve(api):
    c, cal = api
    r = c.post("/book", json={"start": ist(17, 20, 0).isoformat(), "name": "Ada", "email": "ada@x.dev", "topic": "Role"})
    assert r.status_code == 200 and r.json()["ok"]
    assert cal.events["ev1"].state == "pending"
    to, subject, body = cal.emails[0]
    assert to == "owner@x.dev" and "Ada" in subject and "/bookings/ev1/review?sig=" in body

    assert c.get("/bookings/ev1/review?sig=wrong").status_code == 403
    page = c.get(f"/bookings/ev1/review?sig={sign('secret', 'ev1')}")
    assert page.status_code == 200 and "Approve" in page.text and "ada@x.dev" in page.text
    assert c.post(f"/bookings/ev1/approve?sig={sign('secret', 'ev1')}").status_code == 200
    assert cal.events["ev1"].state == "confirmed"


def test_book_decline_emails_visitor(api):
    c, cal = api
    c.post("/book", json={"start": ist(17, 20, 0).isoformat(), "name": "Ada", "email": "ada@x.dev"})
    r = c.post(f"/bookings/ev1/decline?sig={sign('secret', 'ev1')}")
    assert r.status_code == 200 and "ev1" not in cal.events and cal.deleted == [("ev1", False)]
    assert cal.emails[-1][0] == "ada@x.dev"


def test_cancelling_confirmed_call_notifies_attendee(api):
    c, cal = api
    c.post("/book", json={"start": ist(17, 20, 0).isoformat(), "name": "Ada", "email": "ada@x.dev"})
    c.post(f"/bookings/ev1/approve?sig={sign('secret', 'ev1')}")
    c.post(f"/bookings/ev1/decline?sig={sign('secret', 'ev1')}")
    assert cal.deleted == [("ev1", True)]


def test_book_rejects_bad_slot_and_honeypot(api):
    c, cal = api
    r = c.post("/book", json={"start": ist(17, 15, 0).isoformat(), "name": "Ada", "email": "ada@x.dev"})
    assert r.status_code == 409
    r = c.post("/book", json={"start": ist(17, 20, 0).isoformat(), "name": "Bot", "email": "b@x.dev", "website": "spam"})
    assert r.status_code == 200 and not cal.events


def test_book_unavailable_without_calendar(api, monkeypatch):
    c, _ = api
    monkeypatch.setattr(main, "get_calendar", lambda: None)
    assert c.post("/book", json={"start": ist(17, 20, 0).isoformat(), "name": "A", "email": "a@x.dev"}).status_code == 503


# ── agent: tool call -> slots event ──────────────────────────────────────────
class ToolCallingFake(GenericFakeChatModel):
    """Returns whole messages (the generic fake can't stream a message that only holds a tool call)."""

    disable_streaming: bool = True

    def bind_tools(self, tools, **kw):
        return self


def test_agent_offers_slots():
    calls = []

    def finder(lo, hi, part):
        calls.append((lo, hi, part))
        return [{"start": "2026-11-17T14:30:00+00:00", "label": "Tue 17 Nov, 8:00 PM IST"}]

    llm = ToolCallingFake(messages=iter([
        AIMessage("", tool_calls=[{"name": "find_open_slots", "args": {"from_date": "2026-11-17", "time_of_day": "evening"}, "id": "t1"}]),
        AIMessage("Here are some open times. Pick one below."),
    ]))
    main._graph = build_graph(lambda tools: llm, lambda q: [], "Salil", slot_finder=finder,
                              scheduling_info=lambda: {"now": "x", "tz": "IST", "minutes": 30, "hours": HOURS, "days": "x"})
    try:
        text = TestClient(main.app).post("/chat", json={"messages": [{"role": "user", "content": "Can we talk Tuesday evening?"}]}).text
    finally:
        main._graph = None
    events = [(b.split("\n")[0][7:], json.loads(b.split("\n")[1][6:])) for b in text.strip().split("\n\n")]
    assert calls == [(date(2026, 11, 17), date(2026, 11, 17), "evening")]
    assert ("slots", [{"start": "2026-11-17T14:30:00+00:00", "label": "Tue 17 Nov, 8:00 PM IST"}]) in events
    assert "".join(d for e, d in events if e == "token") == "Here are some open times. Pick one below."
    assert events[-1] == ("done", None)


# ── Apps Script transport ────────────────────────────────────────────────────
def test_apps_script_transport_follows_redirect_and_checks_errors():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from app.calendar_api import AppsScriptTransport, GoogleCalendar

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # like Apps Script: accept POST, redirect to the result
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["post"] = body
            self.send_response(302)
            self.send_header("Location", f"/result?op={body['op']}&ok={int(body['secret'] == 's3cret')}")
            self.end_headers()

        def do_GET(self):
            ok = "ok=1" in self.path
            if not ok:
                out = {"error": "unauthorized"}
            elif "op=list" in self.path:
                out = {"result": {"items": [
                    {"id": "a", "status": "confirmed", "start": {"dateTime": "2026-11-17T19:00:00+05:30"},
                     "end": {"dateTime": "2026-11-17T19:30:00+05:30"},
                     "extendedProperties": {"private": {"source": "portfolio-bot", "visitor_email": "ada@x.dev",
                                                        "visitor_name": "Ada", "state": "pending"}}},
                    {"id": "b", "status": "confirmed", "start": {"dateTime": "2026-11-18T19:00:00+05:30"},
                     "end": {"dateTime": "2026-11-18T19:30:00+05:30"},
                     "extendedProperties": {"private": {"source": "portfolio-bot", "visitor_email": "bob@x.dev"}}},
                ]}}
            else:
                out = {"result": {"calendars": {"primary": {"busy": [
                    {"start": "2026-11-17T14:00:00Z", "end": "2026-11-17T15:00:00Z"}]}}}}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/exec"
    try:
        cal = GoogleCalendar(AppsScriptTransport(url, "s3cret"))
        busy = cal.busy(NOW, NOW + timedelta(days=2))
        assert busy == [(datetime(2026, 11, 17, 14, tzinfo=timezone.utc), datetime(2026, 11, 17, 15, tzinfo=timezone.utc))]
        assert seen["post"]["op"] == "freebusy" and seen["post"]["secret"] == "s3cret"
        assert [b.id for b in cal.bookings(NOW, NOW + timedelta(days=3))] == ["a", "b"]
        assert [b.name for b in cal.bookings(NOW, NOW + timedelta(days=3), email="ADA@x.dev")] == ["Ada"]
        with pytest.raises(RuntimeError, match="unauthorized"):
            GoogleCalendar(AppsScriptTransport(url, "wrong")).busy(NOW, NOW)
    finally:
        srv.shutdown()


def test_get_calendar_prefers_apps_script(monkeypatch):
    from app import calendar_api

    s = calendar_api.get_settings()
    calendar_api.get_calendar.cache_clear()
    monkeypatch.setattr(s, "admin_token", "t")
    monkeypatch.setattr(s, "apps_script_url", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setattr(s, "apps_script_secret", "s")
    try:
        cal = calendar_api.get_calendar()
        assert isinstance(cal._call, calendar_api.AppsScriptTransport)
        calendar_api.get_calendar.cache_clear()
        monkeypatch.setattr(s, "apps_script_secret", "")
        assert calendar_api.get_calendar() is None  # no OAuth values either
    finally:
        calendar_api.get_calendar.cache_clear()
