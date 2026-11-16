# Portfolio Chatbot (backend)

A RAG chatbot that answers visitors' questions about Salil, for the portfolio site.

- **FastAPI** server, streaming answers over Server-Sent Events
- **LangGraph** agent (`retrieve → generate`), structured so a calendar-booking branch can be added later
- **Gemini** for chat (`gemini-3.8-flash`) and embeddings (`gemini-embedding-001`, 768 dims)
- **Firestore vector search**, in the same Firebase project the portfolio already uses

## How it works

```
content/*.md ─┐
Firestore projects ─┼─► ingest (chunk + embed) ─► Firestore `rag_chunks` (with vectors)
Firestore blog/list ─┘                                          │
                                                                ▼
visitor question ─► /chat ─► LangGraph: retrieve (vector search) ─► generate (Gemini) ─► streamed answer + sources
```

## Deploy (Render)

1. **Keys**
   - Gemini API key: https://aistudio.google.com/apikey
   - Firebase service account: Firebase Console → Project settings → Service accounts → *Generate new private key*.
     Paste the JSON as one line into `FIREBASE_SERVICE_ACCOUNT_JSON`. Never commit it.
2. **Create the vector index** (one time). Open [Google Cloud Shell](https://shell.cloud.google.com)
   (gcloud is preinstalled, so nothing to install locally) and run:
   ```bash
   gcloud firestore indexes composite create --project=YOUR_PROJECT_ID \
     --collection-group=rag_chunks --query-scope=COLLECTION \
     --field-config=field-path=embedding,vector-config='{"dimension":"768","flat":"{}"}'
   ```
3. **Render:** New → Blueprint → pick this repo. Fill in `GOOGLE_API_KEY`, `FIREBASE_SERVICE_ACCOUNT_JSON`,
   `FIREBASE_PROJECT_ID`, `CONTACT_EMAIL` and `ALLOWED_ORIGINS` (e.g. `https://vartak.online,https://www.vartak.online`).
   `ADMIN_TOKEN` is generated for you.
4. Every deploy re-indexes `content/` plus your Firestore projects and blog posts (it's part of the build command),
   so editing `content/` and pushing is all it takes to update the bot.
5. After changing projects or blog posts in Firebase without a deploy, re-index on demand:
   ```bash
   curl -X POST https://YOUR-SERVICE.onrender.com/admin/reindex -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
   ```

The free plan sleeps when idle, so the first request after a quiet spell takes a while to start.

## Run locally (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env              # fill it in
python -m ingest.run --dry-run    # inspect chunks; drop --dry-run to embed + write
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

## Safety

- Rate-limited per IP (`RATE_LIMIT`, default 20/minute); message length and history are capped.
- CORS limited to `ALLOWED_ORIGINS`.
- The system prompt restricts answers to the retrieved context and tells the model to ignore instructions inside it.

## Tests

```bash
pytest
```
Tests use a fake LLM and retriever, so they need no keys or network.
