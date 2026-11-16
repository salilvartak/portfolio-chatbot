"""FastAPI app. POST /chat streams the answer as Server-Sent Events."""

import asyncio
import json
import logging
import secrets
import time
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import get_settings

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
                elif mode == "messages":
                    chunk, meta = payload
                    if meta.get("langgraph_node") == "generate" and isinstance(chunk, AIMessageChunk):
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
        except Exception:
            log.exception("chat failed after %.1fs", time.monotonic() - t0)
            yield _sse("error", "Sorry, something went wrong. Please try again.")

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
