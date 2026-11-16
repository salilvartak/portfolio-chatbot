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

## Call booking (Google Calendar)

Visitors ask the bot for a call; the agent checks your calendar (read-only tool) and shows open times as
buttons. The visitor picks one and fills in a short form, which creates a **[Pending]** event on your calendar
and emails you a review link. **Approve** sends the visitor a calendar invite with a Google Meet link;
**Decline** removes the event and emails them a polite note. The model never books anything itself.

Rules (all overridable with env vars, see `.env.example`): 30-minute calls; weekdays 7 PM-midnight, weekends
10 AM-2 PM and 7 PM-midnight (Asia/Kolkata); 12 h minimum notice; 14 days ahead; 15 min buffer; max 3 a day;
one open request per email; 5 booking attempts per hour per IP.

### One-time setup: Apps Script bridge (recommended, ~5 minutes)

The backend reaches your calendar through a tiny script running in your own Google account
([`apps_script/Code.gs`](apps_script/Code.gs)). It only touches events the chatbot created, and every request
needs a shared secret. No OAuth client, consent screen or expiring tokens.

1. Make a secret: in Cloud Shell or any terminal run `openssl rand -hex 32` and copy the output.
2. Go to [script.google.com](https://script.google.com) → **New project**. Name it "Portfolio bot".
3. Replace everything in `Code.gs` with the contents of [`apps_script/Code.gs`](apps_script/Code.gs), and set
   `SECRET` to your secret. Save.
4. Left sidebar **Services (+)** → **Google Calendar API** → **Add**.
5. Pick the `authorize` function in the toolbar → **Run** → **Review permissions** → choose your account →
   *Advanced* → *Go to Portfolio bot (unsafe)* → **Allow**. (It's your own script.)
6. **Deploy → New deployment** → gear → **Web app**. *Execute as:* **Me**. *Who has access:* **Anyone**.
   **Deploy**, then copy the **Web app URL** (ends in `/exec`).
7. **Render → Environment:** `APPS_SCRIPT_URL` = that URL, `APPS_SCRIPT_SECRET` = your secret. Also make sure
   `ADMIN_TOKEN` and `CONTACT_EMAIL` are set. Save.
8. Check `https://YOUR-SERVICE.onrender.com/health` shows `"booking": true`.

If you change the script later, use **Deploy → Manage deployments → Edit → Version: New version** so the
URL stays the same.

### Alternative: OAuth refresh token

Set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and `GOOGLE_OAUTH_REFRESH_TOKEN` instead (OAuth
client of type *Web application*, refresh token from the OAuth Playground with scopes `calendar.events`,
`calendar.freebusy` and `gmail.send`). The consent screen must be **published**: in *Testing* mode tokens expire
after 7 days. Apps Script is used when both are configured.

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
