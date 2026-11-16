# Portfolio Chatbot (backend)

A RAG chatbot that answers visitors' questions about Salil, for the portfolio site.

- **FastAPI** server, streaming answers over Server-Sent Events
- **LangGraph** agent (`retrieve → generate`), structured so a calendar-booking branch can be added later
- **Gemini** for chat (`gemini-2.5-flash`) and embeddings (`gemini-embedding-001`, 768 dims)
- **Firestore vector search**, in the same Firebase project the portfolio already uses

## How it works

```
content/*.md ─┐
Firestore projects ─┼─► ingest (chunk + embed) ─► Firestore `rag_chunks` (with vectors)
Firestore blog/list ─┘                                          │
                                                                ▼
visitor question ─► /chat ─► LangGraph: retrieve (vector search) ─► generate (Gemini) ─► streamed answer + sources
```

## Setup

1. **Python env**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements-dev.txt
   cp .env.example .env   # then fill it in
   ```
2. **Keys**
   - Gemini API key from https://aistudio.google.com/apikey
   - Firebase Admin service account: Firebase Console → Project settings → Service accounts → *Generate new private key*. Put the JSON on one line in `FIREBASE_SERVICE_ACCOUNT_JSON`. Never commit it.
3. **Create the vector index** (one time):
   ```bash
   gcloud firestore indexes composite create --project=YOUR_PROJECT_ID \
     --collection-group=rag_chunks --query-scope=COLLECTION \
     --field-config=field-path=embedding,vector-config='{"dimension":"768","flat":"{}"}'
   ```
4. **Add your knowledge doc** to `content/` (see `content/README.md`), then build the index:
   ```bash
   python -m ingest.run --dry-run   # inspect chunks
   python -m ingest.run             # embed + write to Firestore
   ```
   Re-run this whenever your doc, projects or blog posts change.
5. **Run locally**
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

## API

`POST /chat` with `{"messages": [{"role": "user", "content": "What has Salil built?"}]}`.
Send the recent conversation (the server keeps the last 10 messages). The response is `text/event-stream`:

| event     | data                                    |
|-----------|-----------------------------------------|
| `sources` | `[{"label": "Project: Clush", "url": ""}]` |
| `token`   | a piece of the answer text              |
| `done`    | `null`                                  |
| `error`   | message string                          |

`GET /health` returns `{"ok": true}`.

## Deploy (Render)

New → Blueprint → pick this repo; `render.yaml` sets everything up. Fill in the secret env vars in the
dashboard, and set `ALLOWED_ORIGINS` to your portfolio domain(s).
The free plan sleeps when idle, so the first request after a quiet spell takes a while to start.

## Safety

- Rate-limited per IP (`RATE_LIMIT`, default 20/minute); message length and history are capped.
- CORS limited to `ALLOWED_ORIGINS`.
- The system prompt restricts answers to the retrieved context and tells the model to ignore instructions inside it.

## Tests

```bash
pytest
```
Tests use a fake LLM and retriever, so they need no keys or network.
