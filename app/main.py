"""FastAPI app. POST /chat streams the answer as Server-Sent Events; POST /book requests a call."""

import asyncio
import html
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.booking import BookingError, fmt, request_booking, rules_from, sign, verify
from app.calendar_api import get_calendar
from app.config import get_settings
from app.suggestions import pick_suggestions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("chatbot")
settings = get_settings()


def _client_ip(request: Request) -> str:
    # Render sits behind a proxy; the real client is the first X-Forwarded-For hop.
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else get_remote_address(request)


limiter = Limiter(key_func=_client_ip)
app = FastAPI(title="Portfolio Chatbot")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)

_graph = None


def get_graph():
    global _graph
    if _graph is None:
        from app.graph import default_graph

        _graph = default_graph()
    return _graph


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=settings.max_message_chars * 4)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/admin/reindex")
@limiter.limit("5/hour")
async def reindex(request: Request, authorization: str = Header(default="")):
    """Re-embed all content. Call after editing projects or blog posts in Firestore."""
    token = settings.admin_token
    if not token or not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    from ingest.run import reindex as run_reindex

    return {"chunks": await run_in_threadpool(run_reindex)}


@app.post("/chat")
@limiter.limit(settings.rate_limit)
async def chat(request: Request, body: ChatRequest):
    msgs = body.messages[-settings.max_history_messages :]
    if msgs[-1].role != "user":
        return StreamingResponse(iter([_sse("error", "Last message must be from the user.")]), media_type="text/event-stream")
    last = msgs[-1].content.strip()[: settings.max_message_chars]
    history = [HumanMessage(m.content) if m.role == "user" else AIMessage(m.content) for m in msgs[:-1]]

    async def stream():
        t0 = time.monotonic()
        first_token = None
        n_tokens = 0
        log.info("chat start: model=%s question=%r", settings.chat_model, last[:80])
        try:
            async for mode, payload in get_graph().astream(
                {"messages": [*history, HumanMessage(last)], "chunks": []},
                stream_mode=["updates", "messages"],
            ):
                if mode == "updates" and "retrieve" in payload:
                    log.info("chat retrieved %d chunks in %.1fs", len(payload["retrieve"]["chunks"]), time.monotonic() - t0)
                    seen, sources = set(), []
                    for c in payload["retrieve"]["chunks"]:
                        if c.source not in seen:
                            seen.add(c.source)
                            sources.append({"label": c.source, "url": c.url})
                    yield _sse("sources", sources)
                    asked = [m.content for m in msgs if m.role == "user"]
                    yield _sse("suggestions", pick_suggestions(payload["retrieve"]["chunks"], asked))
                elif mode == "updates" and "tools" in payload:
                    for m in payload["tools"].get("messages", []):
                        if isinstance(m, ToolMessage) and m.artifact:
                            log.info("chat offered %d slots", len(m.artifact))
                            yield _sse("slots", m.artifact)
                elif mode == "messages":
                    chunk, meta = payload
                    # AIMessageChunk when streamed; a whole AIMessage if the model returned it in one go
                    if meta.get("langgraph_node") == "generate" and isinstance(chunk, AIMessage):
                        if text := chunk.text:  # text blocks only, never internal reasoning
                            if first_token is None:
                                first_token = time.monotonic() - t0
                                log.info("chat first token after %.1fs", first_token)
                            n_tokens += 1
                            yield _sse("token", text)
            if n_tokens == 0:
                log.warning("chat produced no answer text (model=%s)", settings.chat_model)
            log.info("chat done in %.1fs, %d token chunks", time.monotonic() - t0, n_tokens)
            yield _sse("done", None)
        except (asyncio.CancelledError, GeneratorExit):
            log.warning("chat cancelled after %.1fs (client disconnected)", time.monotonic() - t0)
            raise
        except Exception as e:
            log.exception("chat failed after %.1fs", time.monotonic() - t0)
            busy = any(k in str(e) for k in ("503", "UNAVAILABLE", "high demand", "429", "RESOURCE_EXHAUSTED")) or (
                "timeout" in type(e).__name__.lower()
            )
            yield _sse(
                "error",
                "The AI is getting a lot of requests right now. Please try again in a minute."
                if busy
                else "Sorry, something went wrong. Please try again.",
            )

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ── Call booking ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BookRequest(BaseModel):
    start: str = Field(max_length=40)  # ISO 8601 with timezone, from a slot the chat offered
    name: str = Field(max_length=200)
    email: str = Field(max_length=200)
    topic: str = Field(default="", max_length=2000)
    website: str = Field(default="", max_length=200)  # honeypot: humans never see or fill this


def _review_url(event_id: str) -> str:
    return f"{settings.api_url}/bookings/{event_id}/review?sig={sign(settings.admin_token, event_id)}"


@app.post("/book")
@limiter.limit("5/hour")
async def book(request: Request, body: BookRequest):
    cal = get_calendar()
    if cal is None or not settings.admin_token:
        raise HTTPException(status_code=503, detail="Booking isn't available right now.")
    ok = {
        "ok": True,
        "message": f"Request sent! {settings.owner_name.split()[0]} will review it, and if he can make it you'll "
        "get a calendar invite with a Google Meet link at your email.",
    }
    if body.website:  # bot filled the honeypot: pretend it worked
        return ok
    try:
        start = datetime.fromisoformat(body.start.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid time.")

    rules = rules_from(settings)
    try:
        booking = await run_in_threadpool(
            request_booking, cal, rules, _now(), start, body.name, body.email, body.topic, _review_url
        )
    except BookingError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    log.info("booking requested for %s", booking.start.isoformat())

    owner = settings.notify_email or settings.contact_email
    if owner:
        when = fmt(booking.start, rules.tz)
        try:
            await run_in_threadpool(
                cal.send_email,
                owner,
                f"Call request: {booking.name}, {when}",
                f"New call request from your portfolio chatbot.\n\nWhen: {when}\nName: {booking.name}\n"
                f"Email: {booking.email}\nTopic: {booking.topic or '(none)'}\n\n"
                f"Approve or decline: {_review_url(booking.id)}\n\n"
                "It's on your calendar as [Pending] until you decide.",
            )
        except Exception:
            log.exception("couldn't send booking notification email")
    return ok


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font-family:system-ui,sans-serif;background:#07070A;color:#ECECEE;max-width:520px;margin:48px auto;padding:0 16px;line-height:1.5}}
.card{{background:#111116;border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:24px}}
h1{{font-size:20px;font-weight:500;margin:0 0 16px}} dt{{color:#8A8A93;font-size:13px;margin-top:10px}} dd{{margin:2px 0 0}}
form{{display:inline}} button{{font:inherit;border-radius:999px;padding:10px 20px;margin:20px 8px 0 0;cursor:pointer;border:1px solid rgba(255,255,255,.2);background:transparent;color:#ECECEE}}
button.go{{background:#B8FF5C;color:#07070A;border-color:#B8FF5C}}
</style></head><body><div class="card"><h1>{html.escape(title)}</h1>{body}</div></body></html>"""
    )


def _load(event_id: str, sig: str):
    if not verify(settings.admin_token, event_id, sig):
        raise HTTPException(status_code=403, detail="Invalid or expired link.")
    cal = get_calendar()
    if cal is None:
        raise HTTPException(status_code=503, detail="Booking isn't configured.")
    return cal


@app.get("/bookings/{event_id}/review", response_class=HTMLResponse)
async def review(event_id: str, sig: str = ""):
    cal = _load(event_id, sig)
    b = await run_in_threadpool(cal.get, event_id)
    if b is None:
        return _page("Request not found", "<p>This request was declined or removed.</p>")
    tz = rules_from(settings).tz
    e = html.escape
    details = (
        f"<dl><dt>When</dt><dd>{e(fmt(b.start, tz))}</dd><dt>Name</dt><dd>{e(b.name)}</dd>"
        f"<dt>Email</dt><dd>{e(b.email)}</dd><dt>Topic</dt><dd>{e(b.topic or '(none)')}</dd></dl>"
    )
    if b.state == "confirmed":
        return _page("Call confirmed", details + "<p>Already approved. The invite was sent.</p>")
    q = f"?sig={e(sig)}"
    return _page(
        "Call request",
        details
        + f'<form method="post" action="/bookings/{e(event_id)}/approve{q}"><button class="go">Approve &amp; send invite</button></form>'
        + f'<form method="post" action="/bookings/{e(event_id)}/decline{q}"><button>Decline</button></form>',
    )


@app.post("/bookings/{event_id}/approve", response_class=HTMLResponse)
async def approve(event_id: str, sig: str = ""):
    cal = _load(event_id, sig)
    b = await run_in_threadpool(cal.get, event_id)
    if b is None:
        return _page("Request not found", "<p>This request was declined or removed.</p>")
    if b.state != "confirmed":
        await run_in_threadpool(cal.approve, event_id)
        log.info("booking approved")
    return _page("Approved", f"<p>{html.escape(b.name)} has been sent a calendar invite with a Google Meet link.</p>")


@app.post("/bookings/{event_id}/decline", response_class=HTMLResponse)
async def decline(event_id: str, sig: str = ""):
    cal = _load(event_id, sig)
    b = await run_in_threadpool(cal.get, event_id)
    if b is None:
        return _page("Already removed", "<p>This request was already declined or removed.</p>")
    if b.state == "confirmed":
        # The visitor already has an invite: let Calendar send them a cancellation.
        await run_in_threadpool(cal.delete, event_id, True)
        log.info("confirmed booking cancelled")
        return _page("Cancelled", f"<p>The call was removed and {html.escape(b.name)} was sent a cancellation.</p>")
    await run_in_threadpool(cal.delete, event_id)
    log.info("booking declined")
    when = fmt(b.start, rules_from(settings).tz)
    try:
        await run_in_threadpool(
            cal.send_email,
            b.email,
            f"Your call request with {settings.owner_name}",
            f"Hi {b.name},\n\nThanks for requesting a call through {settings.site_url}. Unfortunately "
            f"{settings.owner_name} can't make {when}. Feel free to pick another time on the site, or just reply "
            f"to this email.\n\nBest,\n{settings.owner_name}",
        )
    except Exception:
        log.exception("couldn't send decline email")
    return _page("Declined", f"<p>The slot is free again, and {html.escape(b.name)} has been emailed.</p>")
