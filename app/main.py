"""FastAPI app. POST /chat streams the answer as Server-Sent Events."""

import json
import logging
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import get_settings

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


def _text(content) -> str:
    # Gemini may return content as a list of parts.
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/chat")
@limiter.limit(settings.rate_limit)
async def chat(request: Request, body: ChatRequest):
    msgs = body.messages[-settings.max_history_messages :]
    if msgs[-1].role != "user":
        return StreamingResponse(iter([_sse("error", "Last message must be from the user.")]), media_type="text/event-stream")
    last = msgs[-1].content.strip()[: settings.max_message_chars]
    history = [HumanMessage(m.content) if m.role == "user" else AIMessage(m.content) for m in msgs[:-1]]

    async def stream():
        try:
            async for mode, payload in get_graph().astream(
                {"messages": [*history, HumanMessage(last)], "chunks": []},
                stream_mode=["updates", "messages"],
            ):
                if mode == "updates" and "retrieve" in payload:
                    seen, sources = set(), []
                    for c in payload["retrieve"]["chunks"]:
                        if c.source not in seen:
                            seen.add(c.source)
                            sources.append({"label": c.source, "url": c.url})
                    yield _sse("sources", sources)
                elif mode == "messages":
                    chunk, meta = payload
                    if meta.get("langgraph_node") == "generate" and isinstance(chunk, AIMessageChunk):
                        if text := _text(chunk.content):
                            yield _sse("token", text)
            yield _sse("done", None)
        except Exception:
            log.exception("chat failed")
            yield _sse("error", "Sorry, something went wrong. Please try again.")

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
