import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

import app.main as main
from app.graph import build_graph
from app.store import Chunk
from ingest.chunking import split_markdown, split_text
from ingest.sources import blog_chunks, project_chunks

CHUNKS = [
    Chunk("1", "Clush is a dating app built with Flutter.", "Project: Clush"),
    Chunk("2", "More on Clush.", "Project: Clush"),
    Chunk("3", "Salil writes about memory layers.", "Blog: Memory", "https://x.dev/blog/memory"),
]


@pytest.fixture
def client():
    seen = []

    def retriever(q):
        seen.append(q)
        return CHUNKS

    llm = GenericFakeChatModel(messages=iter([AIMessage("Salil built Clush with Flutter.")]))
    main._graph = build_graph(llm, retriever, "Salil")
    yield TestClient(main.app), seen
    main._graph = None


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_chat_streams_sources_then_tokens(client):
    c, seen = client
    r = c.post(
        "/chat",
        json={"messages": [
            {"role": "user", "content": "What projects?"},
            {"role": "assistant", "content": "Clush."},
            {"role": "user", "content": "What stack?"},
        ]},
    )
    assert r.status_code == 200
    events = parse_sse(r.text)
    assert events[0] == ("sources", [
        {"label": "Project: Clush", "url": ""},
        {"label": "Blog: Memory", "url": "https://x.dev/blog/memory"},
    ])
    answer = "".join(d for e, d in events if e == "token")
    assert answer == "Salil built Clush with Flutter."
    assert events[-1] == ("done", None)
    # Follow-up retrieval includes the previous user turn.
    assert seen == ["What projects?\nWhat stack?"]


def test_rejects_non_user_last_message(client):
    c, _ = client
    r = c.post("/chat", json={"messages": [{"role": "assistant", "content": "hi"}]})
    assert parse_sse(r.text)[0][0] == "error"


def test_rejects_empty(client):
    c, _ = client
    assert c.post("/chat", json={"messages": []}).status_code == 422


def test_split_text_respects_limit():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(30))
    parts = split_text(text, max_chars=500, overlap=50)
    assert len(parts) > 1 and all(len(p) <= 500 + 50 + 2 for p in parts)


def test_split_markdown_headings():
    md = "intro\n# About\nI build things\n## Skills\nPython"
    assert split_markdown(md) == [("", "intro"), ("About", "I build things"), ("Skills", "Python")]


def test_project_and_blog_chunks():
    p = project_chunks([{"title": "Clush", "stack": ["Flutter"], "highlights": [{"bold": "10k", "text": "users"}],
                         "links": [{"label": "Live", "href": "https://clush.app"}]}])
    assert p[0].source == "Project: Clush" and "Flutter" in p[0].text and p[0].url == "https://clush.app"
    b = blog_chunks([{"title": "T", "slug": "t", "excerpt": "e", "body": "# H\nbody"}], "https://site.dev/")
    assert [c.url for c in b] == ["https://site.dev/blog/t"] * 2
    assert len({c.id for c in b}) == 2
