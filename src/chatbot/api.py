import json
import os
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from chatbot.session_store import SessionStore

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_SESSIONS_PATH = os.path.join(PROJECT_ROOT, ".index", "sessions.sqlite3")
UI_FILE = Path(__file__).with_name("static") / "ui.html"


def _default_bot_factory():
    from chatbot.rag import RAGBot

    return RAGBot()


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None
    k: int = Field(default=3, ge=1, le=20)
    source: str | None = None
    history_limit: int = Field(default=10, ge=0, le=50)


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: list[str] = []


class SearchRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    k: int = Field(default=3, ge=1, le=50)
    source: str | None = None


class SearchItem(BaseModel):
    chunk_id: str
    source: str
    chunk_index: int
    similarity: float
    score: float | None
    content: str


class SearchResponse(BaseModel):
    question: str
    results: list[SearchItem]


class SessionList(BaseModel):
    total: int
    sessions: list[dict]


class IngestResponse(BaseModel):
    documents_seen: int
    documents_reindexed: int
    chunks_upserted: int
    stale_chunks_removed: int


class SessionMessages(BaseModel):
    session_id: str
    messages: list[dict]


def create_app(bot=None, session_store=None):
    app = FastAPI(title="ESG RAG Chatbot API", version="0.1.0")
    app.state.bot = bot
    app.state.session_store = session_store or SessionStore(DEFAULT_SESSIONS_PATH)

    def get_bot(request: Request):
        if request.app.state.bot is None:
            request.app.state.bot = _default_bot_factory()
        return request.app.state.bot

    def get_sessions(request: Request):
        return request.app.state.session_store

    bot_dep = Depends(get_bot)
    store_dep = Depends(get_sessions)

    @app.get("/")
    def root():
        return {
            "service": "ESG RAG Chatbot API",
            "endpoints": [
                "GET /ui",
                "GET /health",
                "POST /chat",
                "POST /chat/stream",
                "POST /search",
                "POST /ingest",
                "GET /sessions",
                "GET /sessions/{session_id}",
                "DELETE /sessions/{session_id}",
            ],
        }

    @app.get("/ui", response_class=HTMLResponse)
    def ui():
        return UI_FILE.read_text() if UI_FILE.exists() else "<h1>UI not found</h1>"

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, bot=bot_dep, store=store_dep):
        session_id = req.session_id or store.new_id()
        store.create(session_id)
        history = store.history(session_id, limit=req.history_limit)
        answer = await run_in_threadpool(
            bot.ask, question=req.question, k=req.k, source=req.source, history=history
        )
        store.append(session_id, "user", req.question)
        store.append(session_id, "assistant", answer)
        sources = [r.source for r in getattr(bot, "last_results", []) or []]
        return ChatResponse(session_id=session_id, answer=answer, sources=sources)

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest, bot=bot_dep, store=store_dep):
        session_id = req.session_id or store.new_id()
        store.create(session_id)
        history = store.history(session_id, limit=req.history_limit)

        def event_stream():
            chunks = []
            try:
                for piece in bot.ask_stream(
                    question=req.question, k=req.k, source=req.source, history=history
                ):
                    chunks.append(piece)
                    yield f"data: {json.dumps({'delta': piece})}\n\n"
            finally:
                store.append(session_id, "user", req.question)
                store.append(session_id, "assistant", "".join(chunks))
            sources = [r.source for r in getattr(bot, "last_results", []) or []]
            yield f"data: {json.dumps({'sources': sources})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"X-Session-ID": session_id},
        )

    @app.post("/search", response_model=SearchResponse)
    async def search(req: SearchRequest, bot=bot_dep):
        results = await run_in_threadpool(
            bot.retrieve, question=req.question, k=req.k, source=req.source
        )
        items = [
            SearchItem(
                chunk_id=r.chunk_id,
                source=r.source,
                chunk_index=r.chunk_index,
                similarity=round(r.similarity, 4),
                score=round(r.score, 4) if r.score is not None else None,
                content=r.content,
            )
            for r in results
        ]
        return SearchResponse(question=req.question, results=items)

    @app.post("/ingest", response_model=IngestResponse)
    async def ingest(bot=bot_dep):
        stats = await run_in_threadpool(bot.read_and_embed_data)
        return IngestResponse(**stats.__dict__)

    @app.get("/sessions", response_model=SessionList)
    def list_sessions(limit: int = 20, offset: int = 0, store=store_dep):
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        return store.list_sessions(limit=limit, offset=offset)

    @app.get("/sessions/{session_id}", response_model=SessionMessages)
    def get_session(session_id: str, store=store_dep):
        return SessionMessages(session_id=session_id, messages=store.messages(session_id))

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str, store=store_dep):
        store.delete(session_id)
        return {"deleted": session_id}

    return app


app = create_app()


def run(host="0.0.0.0", port=8000, reload=False):
    uvicorn.run("chatbot.api:app", host=host, port=port, reload=reload)