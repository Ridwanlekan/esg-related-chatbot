# ESG Workspace Chatbot

A retrieval-augmented generation (RAG) chatbot platform where each user signs up into a **workspace** (category) — Finance or HR — and chats with a specialized assistant that answers strictly from that workspace's documents. Documents are chunked, embedded, and stored in per-workspace persistent vector indexes; each query is routed deterministically to the user's workspace index.

> Note: `data/demo/` files are placeholders (Jupiter, Ada Lovelace, CRISPR). `data/finance/` and `data/hr/` hold the real ESG documents (IFRS S1/S2, ESG-for-HR); drop files into a workspace folder and re-index.

## Features

- **User accounts & workspace routing**: signup/login (JWT) lets users pick a category (Finance or HR). The authenticated category selects the matching RAG workspace — no LLM supervisor needed, routing is deterministic and billable on the token.
- **Per-workspace vector indexes**: each category gets its own data folder + index (hard isolation). A Finance user can never retrieve HR chunks.
- **Friendly, day-aware greetings**: greetings ("hi", "good morning") are answered with a persona-specific, date/time-aware greeting (incl. emoji) based on the user's timezone — no LLM call.
- **Strict workspace scoping**: each bot's system prompt answers only from retrieved context and refuses out-of-scope topics; retrieval physically can't reach other workspaces.
- **Persistent vector index** (SQLite + sqlite-vec): embeddings are stored on disk, so startup is instant and the LLM/embedding models aren't re-run on every launch.
- **Incremental ingestion**: unchanged documents are skipped (content-hash based); edited files are re-indexed; deleted files are pruned from the index automatically.
- **Multi-format ingestion**: Docling parses PDF, Office, HTML, Markdown, AsciiDoc, CSV, images (OCR), audio/video (ASR), and more into clean Markdown before chunking.
- **Metadata-aware**: each chunk tracks its source file, chunk index, and document hash, with source-filtered retrieval.
- **Hybrid retrieval**: dense semantic search fused with lexical BM25 matches (SQLite FTS5) via reciprocal rank fusion — catches exact names and phrases the embedding model would miss.
- **Offline retrieval**: semantic search uses a local sentence-transformer model — the OpenAI API is only called to generate the final answer.
- **Conversational follow-ups**: follow-up questions are rewritten into standalone queries using chat history (`rewrite.py`) so turns like "which is the largest?" retrieve against the right context.
- **HTTP API + UI**: FastAPI service with streaming (SSE), per-user session memory (SQLite), signup/login UI, and a self-contained browser UI (`/ui`, no CDN).

## Project Structure

```
esg-chatbot/
├── src/chatbot/          # Python package
│   ├── rag.py            # RAGBot: orchestration, embedding, LLM answering
│   ├── workspaces.py     # workspace/category config, labels, per-workspace bots
│   ├── users.py          # UserStore: accounts, passwords (scrypt), JWT tokens
│   ├── smalltalk.py      # greeting detection + day/date-aware persona replies
│   ├── ingest.py         # chunking + incremental ingestion pipeline
│   ├── docling_loader.py # multi-format parsing via Docling (docs/images/ASR)
│   ├── vector_store.py   # persistent vector store (SQLite + sqlite-vec)
│   ├── rewrite.py        # query rewriting from chat history (python module)
│   ├── api.py            # FastAPI HTTP service (esg-api command)
│   ├── session_store.py  # per-user session chat memory (SQLite)
│   ├── view_index.py     # index inspector (view-index command)
│   ├── static/ui.html    # self-contained browser chat UI (signup/login)
│   └── cli.py            # terminal chatbot (esg-chatbot command)
├── tests/                # pytest suite (no model/network required)
├── data/
│   ├── finance/          # workspace: ESG financial reporting docs (IFRS S1/S2)
│   ├── hr/               # workspace: ESG people & HR docs
│   └── demo/             # placeholder docs (Jupiter, Ada, CRISPR)
├── evals/                # retrieval/answer eval set
├── doc/                  # reference docs, env template
│   └── env_example.txt   # copy to .env and fill in
├── models/               # local embedding model (downloaded, gitignored)
├── .index/               # generated vector indexes + sessions + users (gitignored)
├── pyproject.toml        # package metadata, commands, pytest config
└── requirements.txt      # pinned runtime dependencies
```

## Setup

Requires Python 3.10+.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"
```

Create `.env` from the template:

```bash
cp doc/env_example.txt .env
```

Fill in your Azure OpenAI credentials:

```env
AZURE_OPENAI_API_KEY=<your key>
AZURE_OPENAI_ENDPOINT=<your endpoint>
OPENAI_API_VERSION=<api version>
MODEL_NAME=<model deployment name>
TOKENIZERS_PARALLELISM=False
```

For user accounts and workspace routing set a stable signing secret:

```env
AUTH_SECRET=<random 32+ char secret>   # required in prod; ephemeral if unset
# WORKSPACES=finance,hr                # default categories
```

## Observability (Step 6)

**Request tracing** — every response carries an `X-Request-ID` (the incoming one is honored, otherwise generated). All server logs include it, so a log line can be correlated to a specific request:

```
2026-09-21 15:39:01,989 INFO esg.api [d6344f706020] GET /health -> 200 (0.3 ms, rid=d6344f706020)
```

**Metrics** — `GET /metrics` exposes Prometheus-format metrics (`esg_*`):
- `esg_http_requests_total` / `esg_http_request_duration_seconds` (by method + route + status)
- `esg_llm_requests_total` / `esg_llm_duration_seconds` / `esg_llm_tokens_total` / `esg_llm_errors_total` (by kind: `rewrite`, `answer`, `stream`; token types prompt/completion)
- `esg_retrieval_requests_total` / `esg_retrieval_duration_seconds` (embed + hybrid search)
- `esg_index_chunks` / `esg_sessions` (state gauges)

Point Prometheus at `/metrics` to scrape; also works with a simple `curl`. `LOG_LEVEL` env controls verbosity.

## Users, workspaces & routing

Users sign up with basic info and a **category** (Finance or HR). The category is stamped as a claim in their JWT, and at request time the API reads it — deterministically — to pick the matching RAG workspace. No LLM "supervisor" is involved: routing is a lookup, not a model decision (cheap, fast, and can't misroute).

```
Login → JWT { sub, email, name, category=finance }
POST /chat (Authorization: Bearer <jwt>, X-Timezone-Offset: <minutes>) →
  verify JWT → bot_registry[category] →
  is_smalltalk? → day/date-aware persona reply (no RAG, no LLM)
  else         → rewrite → hybrid search → rerank → LLM(workspace prompt) → answer
```

- **Per-workspace data + index**: `data/finance/` → `.index/vectors_finance.sqlite3`, `data/hr/` → `.index/vectors_hr.sqlite3`. Workspace bots are constructed once per category and cached (`api.py` `get_bot`). A question from a Finance user physically queries only the Finance index.
- **Strict scope**: each workspace passes its own system prompt (see `workspaces.system_prompt_for`) that answers only from retrieved context and refuses unrelated topics. Combined with per-index isolation, out-of-workspace queries return "out of scope" rather than guessing.
- **Small talk**: the API intercepts greetings/thanks/help before RAG (`smalltalk.handle`). Greetings are answered with the *user's* time-of-day (from `X-Timezone-Offset`), today's date and weekday, the workspace persona, and an emoji — zero token cost.
- **Sessions are per-user**: sessions are owned by the authenticated user; listing, loading, and deleting only ever touch the caller's sessions.

### Accounts

| Endpoint | Purpose |
| -------- | ------- |
| `POST /auth/signup` | create account (`email`, `password` ≥8 chars, `name`, `category`) → `{token, user, workspace}` |
| `POST /auth/login` | verify credentials → new token (default 7-day TTL, configurable via `TOKEN_TTL_SECONDS`) |
| `GET /me` | current user profile + workspace meta (label, emoji, blurb) |

User chat endpoints require `Authorization: Bearer <jwt>`. `AUTH_SECRET` signs the tokens — set it in production (tokens are invalidated on restart if it's missing, since an ephemeral secret is used).

- Account passwords are hashed with PBKDF2-HMAC-SHA256 (200k iterations, per-user salt).

## Retrieval pipeline

Query → **rewrite** (only with chat history) → **hybrid search** (dense + BM25 fused by reciprocal rank over `CANDIDATE_K` candidates, default 30) → **cross-encoder rerank** (local `cross-encoder/ms-marco-MiniLM-L-6-v2`, offloadable via `RERANKER_ENABLED=0`) → **MMR diversity** (`MMR_LAMBDA`, default 0.5; 0 disables) → final `k` chunks.

The reranker and MMR are fully local (no API cost), so the whole pipeline still runs offline for eval and `/search`.

## Docker

```bash
cp doc/env_example.txt .env   # fill in Azure + AUTH_SECRET + API_KEY
docker compose up --build     # bake models, then serve on :8000
```

- The image **pre-bakes** the embedding + cross-encoder models at build time — the container runs fully offline (no runtime model downloads).
- `./data` and `./.index` are mounted, so docs, the vector indexes, sessions, and user accounts persist across restarts; `POST /ingest` reindexes every workspace (or `POST /ingest/{category}` for one).
- Bare usage: `docker build -t esg-chatbot . && docker run -p 8000:8000 --env-file .env esg-chatbot`.

## Deployment (CI/CD)

**Container publishing** — `.github/workflows/container.yml` builds the image on every PR (validation only) and, on `main`/version tags, pushes to GitHub Container Registry:

```
ghcr.io/<owner>/esg-related-chatbot:latest      # default branch
ghcr.io/<owner>/esg-related-chatbot:sha-<sha>
ghcr.io/<owner>/esg-related-chatbot:<version>   # from a v* tag
```

The image is public by default on GHCR; make it private under package settings if needed.

**Self-host (compose, pulls the published image):**

```bash
ESG_IMAGE=ghcr.io/<owner>/esg-related-chatbot:latest \
  docker compose -f compose.prod.yaml up -d
```

The vector index lives in a named volume (`esg-index`); `data/` is mounted for re-indexing.

**Azure Web App for Containers** — `.github/workflows/deploy.yml` deploys the published image after the Container workflow succeeds on `main` (also runnable manually). It is inert until you configure the target, so it won't fail your pipeline:

1. Create a Web App (Linux, Container) and note its name/resource group.
2. Repo **Variables**: `AZURE_WEBAPP_NAME` (required to enable), `AZURE_RESOURCE_GROUP` (optional, sets `WEBSITES_PORT=8000`).
3. Repo **Secrets** (OIDC federated login): `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.
4. Give the Web App read access to GHCR (Registry settings → `https://ghcr.io`, user = GitHub username, password = PAT with `read:packages`), then set the app's Azure OpenAI/`API_KEY` env vars in Configuration.
5. Enable a persistent path for `/app/.index` (App Service storage) so sessions and the index survive restarts.

`PORT` is honored if the platform injects it; otherwise the app listens on `8000`. Set `INDEX_DIR` to a writable persistent path (e.g. `/home/esg-index` on App Service) to keep the vector index and sessions across restarts.

## Security

Built-in controls (all env-driven, see `doc/env_example.txt`):

- **User auth (JWTs)**: `/chat`, `/chat/stream`, `/search`, `/sessions*`, `/me` require `Authorization: Bearer <jwt>` from signup/login. Tokens are signed with HMAC-SHA256, carry the user's category (workspace), and expire (`TOKEN_TTL_SECONDS`, default 7 days). If `AUTH_SECRET` is unset the server warns and uses an ephemeral secret (dev only).
- **Admin API key**: set `API_KEY` to protect all `/admin*` and `/ingest*` endpoints (re-indexing, document upload/delete, workspace and user management are ops actions). If unset, the server logs a startup warning that admin endpoints are open and the workspace/user-management endpoints return 503 — do not expose it beyond localhost without a key.
- **Per-user sessions**: chat sessions are owned by the issuer; cross-user access returns 404.
- **Rate limiting**: per-IP sliding window on `/chat`, `/chat/stream`, `/search`, `/auth/*` (`RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`, default 60/60).
- **CORS**: `CORS_ORIGINS` restricts browser origins that can call the API (empty = same-origin only).
- **Azure call hardening**: explicit timeouts (`AZURE_OPENAI_TIMEOUT`, default 120s) and retries (`OPENAI_MAX_RETRIES`, default 3); generation capped at `MAX_GENERATION_TOKENS`.
- **Missing config fails fast**: startup raises a clear error if Azure credentials are absent.
- **Graceful failures**: empty index / retrieval errors surface as HTTP 503 (or an SSE `error` event), not 500 crashes.

## Usage

### Chat

```bash
source venv/bin/activate
esg-chatbot
```

The first run builds the index (embeds the documents). Subsequent runs reuse it and only re-index what changed. Type `exit` to quit. Note: the CLI indexes every workspace folder into one combined index for local inspection — the HTTP service keeps workspaces isolated.

### Inspect the index

```bash
view-index                                   # summary + chunk rows (default legacy combined index)
view-index --db .index/vectors_finance.sqlite3 --source "10. IFRS S1.pdf" --limit 10
view-index --db .index/vectors_finance.sqlite3 --full <chunk-id>   # full text + metadata of one chunk
view-index --db .index/vectors_hr.sqlite3 --search "diversity metrics" --k 3
```

### Ingest a new corpus

Drop any supported file into a workspace folder (`data/finance/`, `data/hr/`, …) and re-index. The HTTP API does this with admin auth: `POST /ingest` (all workspaces) or `POST /ingest/finance` (one). From Python:

```python
from chatbot.workspaces import make_workspace_bot
bot = make_workspace_bot("finance")   # data/finance -> .index/vectors_finance.sqlite3
bot.read_and_embed_data()             # incremental: only new/changed files are embedded
```

### Supported file types (Docling)

Ingestion runs files through [Docling](https://github.com/docling-project/docling), which parses them into clean Markdown (layout, reading order, and tables preserved) before chunking and embedding:

| Category  | Extensions |
| --------- | ---------- |
| Documents | `pdf`, `docx`, `doc`, `pptx`, `ppt`, `odt`, `ods`, `odp`, `rtf`, `epub`, `pages`, `boxnote`, `dclx` |
| Markup    | `md`, `adoc`/`asciidoc`, `tex`/`latex`, `html`/`xhtml`, `xml` (JATS/USPTO/XBRL/DocLang), `json` |
| Data      | `csv`, `xlsx`, `xls` |
| Images    | `png`, `jpg`/`jpeg`, `tiff`, `bmp`, `webp` (OCR) |
| Audio     | `wav`, `mp3`, `m4a`, `aac`, `ogg`, `flac` (ASR) |
| Video     | `mp4`, `avi`, `mov`, `mkv`, `webm` (audio track transcribed) |
| Captions / mail | `vtt`, `eml`, `msg` |
| Plain text | `txt`, `log` |

Legacy Office formats (`doc`, `xls`, `ppt`) and Apple Pages need LibreOffice / an extra — see Docling's docs.

**Audio & video (ASR)** uses Whisper. Enable it with the extra and the `ffmpeg` binary:

```bash
pip install "docling[asr]"   # or: pip install -e ".[ingest-asr]"
brew install ffmpeg          # apt-get install ffmpeg on Debian/Ubuntu
```

In Docker: `docker build --build-arg INSTALL_ASR=true .`

Environment knobs (all optional, see `doc/env_example.txt`): `DOCLING_ENABLED=0` disables Docling (plain-text only), `DOCLING_OCR=0` disables OCR for scanned PDFs/images, `DOCLING_ASR=0` disables transcription, `DOCLING_ASR_MODEL` picks a Whisper spec (default `WHISPER_TURBO`).

**Vetting Docling before it enters the index.** Before chunking/embedding, every Docling-converted document is cleaned (right-trimmed lines, collapse of repeated blank lines) and the cleaned Markdown is saved so you can eyeball extraction quality — this is exactly the text that gets chunked:

```
.docling_vet/overview_of_esg.md      # cleaned extraction, exactly what's chunked
```

The folder sits at the project root (alongside the generated `.index/`), is gitignored, and is excluded from ingestion (it is never re-ingested). Point `DOCLING_VET_DIR` elsewhere or set it to `0` to disable.

If Docling isn't installed or a conversion fails, the loader logs a warning and falls back to plain-text decoding for text-like formats; binary files that can't be parsed are skipped (reported as `documents_failed` in the ingest response).

## API

Run the HTTP service (uses the same index + chat memory):

```bash
esg-api                       # uvicorn on 0.0.0.0:8000
# dev reload: uvicorn chatbot.api:app --reload
```

Interactive docs at `http://localhost:8000/docs`.

**Browser UI**: open `http://localhost:8000/ui` — a self-contained UI with signup/login (category pills are loaded live from `GET /workspaces`, so admin-created workspaces appear automatically), streaming answers, day-aware greetings, per-user session history (in `localStorage`), sources, and retrieval-only search. No CDN dependencies. Pass your browser's UTC offset to the API via `X-Timezone-Offset` automatically.

**Admin UI**: open `http://localhost:8000/admin` — a full admin console with four tabs, protected by the `API_KEY` (entered once in the page, stored in `localStorage`, sent as `Authorization: Bearer <API_KEY>`):

- **Overview** — live totals (workspaces, indexed chunks, users, sessions), per-workspace cards with quick **↑ Upload** and **Re-index** buttons, and a live progress bar while an ingestion run is in progress (polls `/ingest/status`).
- **Documents** — drag-and-drop (or click-to-browse) uploads to any workspace (saved to `data/<category>/` and the index is rebuilt in place) plus a per-workspace, filterable file table showing size and indexed state, with safe delete (file removed + index rebuilt to drop stale chunks).
- **Workspaces** — create custom workspaces (category + label + emoji + blurb), edit metadata of any workspace, and delete custom ones. Built-in (`WORKSPACES` env) workspaces can be edited but not deleted. Cards show file and chunk counts. New categories instantly appear in the `/ui` signup pills and become routable/indexable.
- **Users** — create users, edit name/category, reset passwords (generates a random one), and delete accounts, with per-user session counts, **last login**, name/email search and load-more pagination.

| Endpoint            | Auth   | Purpose                                                          |
| ------------------- | ------ | ---------------------------------------------------------------- |
| `GET /health`       | public | liveness check (+ auth/workspaces status)                        |
| `GET /workspaces`   | public | signup categories + label/emoji/blurb (no secrets)               |
| `POST /auth/signup` | public | create account → `{token, user, workspace}`                      |
| `POST /auth/login`  | public | sign in → fresh token                                            |
| `GET /me`           | user   | profile + workspace meta                                         |
| `POST /chat`        | user   | ask in your workspace; small talk returns day-aware greetings     |
| `POST /chat/stream` | user   | same, streaming SSE (`data: {"delta": "text"}`, trailing `{"sources": [...]}` event) |
| `POST /search`      | user   | retrieval only — no LLM call (scoped to your workspace)          |
| `GET /admin`        | public | admin console HTML page                                          |
| `GET /admin/status` | admin  | index status + totals (workspaces/chunks/users/sessions)         |
| `GET /admin/workspaces` | admin | list workspaces with built-in/custom type                    |
| `POST /admin/workspaces` | admin | create a custom workspace (`category`, `label`, `blurb`, `emoji`) |
| `PATCH /admin/workspaces/{cat}` | admin | edit label/blurb/emoji                          |
| `DELETE /admin/workspaces/{cat}` | admin | delete a custom workspace (built-ins → 403)         |
| `GET /admin/documents` | admin | documents per workspace (name, size, indexed state)           |
| `POST /admin/upload` | admin | multipart upload: `category` + `files` → save + re-index       |
| `POST /admin/documents/delete` | admin | delete one file + rebuild index (prunes stale chunks) |
| `GET /admin/users`   | admin | list users (`?limit=&offset=`) with session counts              |
| `POST /admin/users`  | admin | create a user (`email`, `password`, `name`, `category`)          |
| `PATCH /admin/users/{id}` | admin | update name/category, reset password                          |
| `DELETE /admin/users/{id}` | admin | delete a user account                                        |
| `POST /ingest`      | admin  | run incremental ingestion of every workspace                      |
| `POST /ingest/{category}` | admin | ingest one workspace (e.g. `/ingest/finance`)                 |
| `GET /ingest/status`| admin  | live ingestion progress                                          |
| `GET /sessions`     | user   | your paginated sessions (`?limit=&offset=`); returns `{total, sessions}` |
| `GET /sessions/{id}` | user  | full message thread for one of your sessions                     |
| `DELETE /sessions/{id}` | user | clear one of your sessions' history                          |

Custom workspaces live in a small SQLite registry (`.index/workspaces.sqlite3`) managed by the admin console; base categories come from the `WORKSPACES` env variable. Admin metadata overrides are applied live without restarting the server.

Sessions are owned per user and capped at `MAX_RETAINED_SESSIONS` (default 500, oldest auto-pruned); the UI loads them 12 at a time with a "Load more" button.

### Example

```bash
# 1) sign up as a Finance user
TOKEN=$(curl -s -X POST localhost:8000/auth/signup \
  -H 'content-type: application/json' \
  -d '{"email":"ada@bank.com","password":"correcthorsebatterystaple","name":"Ada","category":"finance"}' \
  | python -c 'import sys,json; print(json.load(sys.stdin)["token"])')

# 2) greet it (no LLM call) — day/time-aware reply
curl -s -X POST localhost:8000/chat \
  -H "authorization: Bearer $TOKEN" \
  -H "x-timezone-offset: -60" \
  -H 'content-type: application/json' \
  -d '{"question": "good morning!"}'

# 3) ask a workspace question (scoped to finance docs only)
curl -s -X POST localhost:8000/chat \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"question": "What does IFRS S1 require regarding climate disclosures?"}'

# 4) continue the same conversation
curl -s -X POST localhost:8000/chat \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"session_id": "<id from first call>", "question": "which disclosures are most material?"}'

# retrieval only (no OpenAI call) — scoped to your workspace
curl -X POST localhost:8000/search \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"question": "IFRS S2 transition plan", "k": 3}'
```

Chat history is stored per user in `.index/sessions.sqlite3`.

## Debugging & maintenance

Quick commands for checking the backend state day-to-day. All paths relative to the project root, all SQLite DBs live in `.index/` (gitignored).

### Logs & verbosity

```bash
LOG_LEVEL=DEBUG esg-api              # DEBUG-level logs (default INFO)
LOG_LEVEL=DEBUG uvicorn chatbot.api:app --reload   # dev server + auto-reload
curl -s localhost:8000/metrics | grep esg_ | head    # Prometheus counters
curl -s localhost:8000/health                        # {"status":"ok","auth":true,"workspaces":[...]}
```

Every log line carries the `X-Request-ID` so you can correlate a request with its entries:

```
2026-09-21 15:39:01,989 INFO esg.api [d6344f706020] GET /health -> 200 (0.3 ms, rid=d6344f706020)
```

Set `LOG_LEVEL` in `.env` for a persistent level.

### Re-ingesting documents

HTTP (admin `API_KEY`; same as the `/admin` page buttons):

```bash
API_KEY=your-key
curl -X POST -H "Authorization: Bearer $API_KEY" localhost:8000/ingest            # all workspaces
curl -X POST -H "Authorization: Bearer $API_KEY" localhost:8000/ingest/finance     # one workspace
curl -H "Authorization: Bearer $API_KEY" localhost:8000/ingest/status               # progress of a running run
curl -H "Authorization: Bearer $API_KEY" localhost:8000/admin/status                # per-workspace chunk/doc counts
```

Ingestion is incremental (content-hash): only new/edited files are embedded, deleted files are pruned. A response of `"chunks_upserted": N` is healthy; `"documents_failed": M` (M>0) means files Docling couldn't parse — check `.docling_vet/` for the extracted text and the server log for the reason.

CLI / Python (no token needed):

```bash
python -c "from chatbot.workspaces import make_workspace_bot; b = make_workspace_bot('finance'); print(b.read_and_embed_data())"
```

`esg-chatbot` runs the *combined* local index for quick interactive probing: it ingests `data/` then answers in a REPL.

After changing `data/`, remember that the **running server** re-reads the same index files — no restart needed for the index to be visible, but restart to pick up `.env` changes (e.g. `AUTH_SECRET`).

### Inspect the SQLite stores from the terminal

The `chunks` table on every vector DB is plain SQLite, so `sqlite3` works for text metadata. The actual float vectors live in a `vec0` virtual table loaded via the `sqlite-vec` extension — the plain `sqlite3` CLI can't see them; use `view-index` for vector-level checks (it loads the extension).

**Users** (`.index/users.sqlite3`):

```bash
sqlite3 -header -column .index/users.sqlite3 "SELECT email, name, category, substr(created_at,1,19) AS created, coalesce(substr(last_login,1,19),'never') AS last_login FROM users ORDER BY created;"
sqlite3 .index/users.sqlite3 "SELECT category, COUNT(*) FROM users GROUP BY category;"
# delete a user (chat sessions keep their user_id and just become unlisted)
sqlite3 .index/users.sqlite3 "DELETE FROM users WHERE email='smoketest@fin.com';"
```

**Sessions** (`.index/sessions.sqlite3`; user accounts live in the separate `users.sqlite3`, so `ATTACH` them for a join):

```bash
sqlite3 .index/sessions.sqlite3 \
  "ATTACH '.index/users.sqlite3' AS ok;
   SELECT s.id AS session_id, u.email, u.category, COUNT(m.id) AS msgs, substr(s.created_at,1,19) AS created
   FROM sessions s LEFT JOIN ok.users u ON u.id = s.user_id
   LEFT JOIN messages m ON m.session_id = s.id GROUP BY s.id ORDER BY s.created_at DESC LIMIT 20;"
sqlite3 -header -column .index/sessions.sqlite3 \
  "SELECT role, substr(content,1,90) AS text FROM messages WHERE session_id='<session_id>' ORDER BY id;"
sqlite3 .index/sessions.sqlite3 "SELECT COUNT(*) FROM sessions;"              # total
sqlite3 .index/sessions.sqlite3 "SELECT COUNT(*) FROM sessions WHERE user_id IS NULL;"  # legacy/unowned (invisible)
```

Note the join syntax above needs a non-root schema name (`ok.users`); in raw `sqlite3` one-liners keep it on a single line.

**Vector index** (`.index/vectors_<workspace>.sqlite3`, e.g. `vectors_finance.sqlite3`, `vectors_hr.sqlite3`):

```bash
sqlite3 .index/vectors_finance.sqlite3 "SELECT COUNT(*) FROM chunks;"                  # total chunks
sqlite3 -header -column .index/vectors_finance.sqlite3 \
  "SELECT source, COUNT(*) AS n FROM chunks GROUP BY source ORDER BY n DESC;"          # per-document chunks
sqlite3 -header -column .index/vectors_finance.sqlite3 \
  "SELECT chunk_index, substr(content,1,100) AS preview FROM chunks WHERE source LIKE '%IFRS S1%' LIMIT 8;"  # peek at content
# vector-valued checks, semantic search, full chunk dumps (loads sqlite-vec):
view-index --db .index/vectors_finance.sqlite3 --source "10. IFRS S1.pdf" --limit 10
view-index --db .index/vectors_finance.sqlite3 --full <chunk-id>
view-index --db .index/vectors_finance.sqlite3 --search "transition plan climate risks" --k 5
```

`view-index` with no `--db` reads the *legacy combined* `.index/vectors.sqlite3`; point it at a workspace DB to inspect an isolated index.

### Runtime sanity checks

```bash
# token, then say hi (day-aware, no LLM) — proves auth + routing + smalltalk
TOKEN=$(curl -s -X POST localhost:8000/auth/login -H 'content-type: application/json' \
  -d '{"email":"ada@bank.com","password":"correcthorsebatterystaple"}' | python -c 'import sys,json; print(json.load(sys.stdin)["token"])')
curl -s -X POST localhost:8000/chat -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"question": "hi"}'
# retrieval-only (exercises embed + hybrid search + rerank, never touches Azure)
curl -s -X POST localhost:8000/search -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"question": "IFRS S2 transition plan", "k": 3}'
```

- **503 on a chat question** → workspace index empty or retrieval failed; check `/admin/status` chunks vs data files and re-ingest.
- **401 on chat** → expired/invalid JWT (restart without `AUTH_SECRET` invalidates tokens).
- **Retrieval empty but index non-empty** → query and corpus don't overlap; sanity-check with `view-index --search` and raise `CANDIDATE_K`/`MMR_LAMBDA`.

### Resetting state

```bash
rm -f .index/vectors_*.sqlite3      # drop all vector indexes (re-ingest rebuilds them)
rm -f .index/sessions.sqlite3       # wipe all chat history (accounts remain)
# or targeted: sqlite3 .index/sessions.sqlite3 "DELETE FROM sessions; DELETE FROM messages;"
sqlite3 .index/sessions.sqlite3 "DELETE FROM messages WHERE session_id='<id>';"   # clear one thread
```

## Testing

```bash
pytest
```

The suite covers chunking, ingestion idempotency, deleted-file pruning, Docling format routing/fallbacks, vector-store round-trips, retrieval, the security layer, and eval metrics — none of it needs a model download or network.

## Evaluation (Step 5)

Golden-set harness to measure retrieval and (optionally) answer quality. Questions live in `evals/eval_set.json` as `{"question", "expected_sources", "answer_hint?"}` — replace/augment them with ESG-specific Q&A as your real corpus lands.

```bash
# retrieval metrics only (no Azure credentials required — CI safe)
esg-eval --k 3
# with thresholds (non-zero exit gates the build)
esg-eval --k 3 --min-hit-rate 0.8 --min-recall 0.8 --min-mrr 0.7
# additionally score answer faithfulness with the LLM as judge (needs Azure setup)
esg-eval --judge --min-faithfulness 0.8
# write a machine-readable report
esg-eval --k 3 --json eval-report.json
```

Metrics: **hit_rate@k** (≥1 expected source retrieved), **recall@k** (coverage of expected sources), **MRR** (rank quality), and optional **faithfulness** (answer fully supported by retrieved context). Every question is printed as OK/MISS so failures are actionable.

## CI (GitHub Actions)

`.github/workflows/ci.yml` runs on every push/PR to `main`:

- **Unit tests** — installs from `requirements.txt` and runs `pytest`.
- **Retrieval eval** — builds the index from `data/` (downloads the local embedding model) and gates on `hit_rate ≥ 0.8`, `recall ≥ 0.8`, `mrr ≥ 0.7`; uploads `eval-report.json` as an artifact. No LLM is used, so the gate has no network/secret dependency on Azure.