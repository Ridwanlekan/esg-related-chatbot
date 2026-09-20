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

The suite covers chunking, ingestion idempotency, deleted-file pruning, vector-store round-trips, and retrieval — it uses a fake embedding function, so no model download or network is needed.