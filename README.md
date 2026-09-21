# ESG RAG Chatbot

A retrieval-augmented generation (RAG) chatbot that answers questions grounded in a local document corpus. Documents are chunked, embedded, and stored in a persistent vector index; a query retrieves the most relevant chunks and an Azure OpenAI model answers using only that context.

> Note: current `data/` files are placeholder documents (Jupiter, Ada Lovelace, CRISPR). These will be replaced with ESG-related documents.

## Features

- **Persistent vector index** (SQLite + sqlite-vec): embeddings are stored on disk, so startup is instant and the LLM/embedding models aren't re-run on every launch.
- **Incremental ingestion**: unchanged documents are skipped (content-hash based); edited files are re-indexed; deleted files are pruned from the index automatically.
- **Metadata-aware**: each chunk tracks its source file, chunk index, and document hash, with source-filtered retrieval.
- **Hybrid retrieval**: dense semantic search fused with lexical BM25 matches (SQLite FTS5) via reciprocal rank fusion — catches exact names and phrases the embedding model would miss.
- **Offline retrieval**: semantic search uses a local sentence-transformer model — the OpenAI API is only called to generate the final answer.
- **Conversational follow-ups**: follow-up questions are rewritten into standalone queries using chat history (`rewrite.py`) so turns like "which is the largest?" retrieve against the right context.
- **HTTP API + UI**: FastAPI service with streaming (SSE), per-session chat memory, and a self-contained browser UI (`/ui`, no CDN).

## Project Structure

```
esg-chatbot/
├── src/chatbot/          # Python package
│   ├── rag.py            # RAGBot: orchestration, embedding, LLM answering
│   ├── ingest.py         # chunking + incremental ingestion pipeline
│   ├── vector_store.py   # persistent vector store (SQLite + sqlite-vec)
│   ├── rewrite.py        # query rewriting from chat history (python module)
│   ├── api.py            # FastAPI HTTP service (esg-api command)
│   ├── session_store.py  # per-session chat memory (SQLite)
│   ├── view_index.py     # index inspector (view-index command)
│   ├── static/ui.html    # self-contained browser chat UI
│   └── cli.py            # terminal chatbot (esg-chatbot command)
├── tests/                # pytest suite (no model/network required)
├── data/                 # source documents (txt)
├── doc/                  # reference docs, env template
│   └── env_example.txt   # copy to .env and fill in
├── models/               # local embedding model (downloaded, gitignored)
├── .index/               # generated vector index (gitignored)
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

## Retrieval pipeline

Query → **rewrite** (only with chat history) → **hybrid search** (dense + BM25 fused by reciprocal rank over `CANDIDATE_K` candidates, default 30) → **cross-encoder rerank** (local `cross-encoder/ms-marco-MiniLM-L-6-v2`, offloadable via `RERANKER_ENABLED=0`) → **MMR diversity** (`MMR_LAMBDA`, default 0.5; 0 disables) → final `k` chunks.

The reranker and MMR are fully local (no API cost), so the whole pipeline still runs offline for eval and `/search`.

## Docker

```bash
cp doc/env_example.txt .env   # fill in Azure + API_KEY
docker compose up --build     # bake models, then serve on :8000
```

- The image **pre-bakes** the embedding + cross-encoder models at build time — the container runs fully offline (no runtime model downloads).
- `./data` and `./.index` are mounted, so docs and the vector index (plus sessions) persist across restarts; `POST /ingest` reindexes changed docs.
- Bare usage: `docker build -t esg-chatbot . && docker run -p 8000:8000 --env-file .env esg-chatbot`.

## Security

Built-in controls (all env-driven, see `doc/env_example.txt`):

- **API key auth**: set `API_KEY` to require `Authorization: Bearer <key>` on every endpoint except `/health` and `/ui`. If unset, the server logs a startup warning that authentication is disabled — do not expose it beyond localhost without a key.
- **Rate limiting**: per-IP sliding window on `/chat`, `/chat/stream`, `/search` (`RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`, default 60/60).
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

The first run builds the index (embeds the documents). Subsequent runs reuse it and only re-index what changed. Type `exit` to quit.

### Inspect the index

```bash
view-index                                   # summary + chunk rows
view-index --source jupiter.txt --limit 10   # filter to one file
view-index --full <chunk-id>                 # full text + metadata of one chunk
view-index --search "moons of jupiter" --k 3 # semantic search
```

### Ingest a new corpus

Either drop new `.txt` files into `data/` and run `esg-chatbot`, or from Python:

```python
from chatbot.rag import RAGBot
rag = RAGBot()
rag.read_and_embed_data()   # incremental: only new/changed files are embedded
```

## API

Run the HTTP service (uses the same index + chat memory):

```bash
esg-api                       # uvicorn on 0.0.0.0:8000
# dev reload: uvicorn chatbot.api:app --reload
```

Interactive docs at `http://localhost:8000/docs`.

**Browser UI**: open `http://localhost:8000/ui` for a self-contained chat UI — streaming answers, per-session memory (stored in `localStorage`), sources, retrieval-only search, and re-indexing. No CDN dependencies.

| Endpoint            | Purpose                                                          |
| ------------------- | ---------------------------------------------------------------- |
| `GET /health`       | liveness check                                                   |
| `POST /chat`        | ask with per-session memory; returns answer + sources            |
| `POST /chat/stream` | same, streaming SSE (`data: {"delta": "text"}`, trailing `{"sources": [...]}` event) |
| `POST /search`      | retrieval only — no LLM call (debugging)                         |
| `POST /ingest`      | run incremental ingestion of `data/`                             |
| `GET /sessions`     | paginated list of past sessions (`?limit=&offset=`); returns `{total, sessions}` |
| `GET /sessions/{id}` | full message thread for a session                              |
| `DELETE /sessions/{id}` | clear a session's chat history                             |

Sessions are capped at `MAX_RETAINED_SESSIONS` (default 500, oldest auto-pruned) and the UI loads them 12 at a time with a "Load more" button.

### Example

```bash
# ask with auto-created session
curl -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"question": "What are Jupiter\u2019s Galilean moons?"}'

# continue the same conversation
curl -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"session_id": "<id from first call>", "question": "which is the largest?"}'

# retrieval only (no OpenAI call)
curl -X POST localhost:8000/search \
  -H 'content-type: application/json' \
  -d '{"question": "Jupiter moons", "k": 3}'
```

Chat history is stored per session in `.index/sessions.sqlite3`.

## Testing

```bash
pytest
```

The suite covers chunking, ingestion idempotency, deleted-file pruning, vector-store round-trips, retrieval, the security layer, and eval metrics — none of it needs a model download or network.

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