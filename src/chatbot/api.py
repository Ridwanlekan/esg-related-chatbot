import json
import logging
import os
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import Depends, HTTPException, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from chatbot import telemetry
from chatbot.security import RateLimiter, int_env, make_auth_check, make_rate_limit
from chatbot.session_store import SessionStore

logger = logging.getLogger("esg.api")

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


def create_app(
    bot=None,
    session_store=None,
    api_key=None,
    cors_origins=None,
    rate_limit=None,
    rate_window=None,
):
    config_api_key = os.environ.get("API_KEY", "") if api_key is None else api_key
    config_cors = (
        [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
        if cors_origins is None
        else cors_origins
    )
    rate_limit_req = (
        int_env("RATE_LIMIT_REQUESTS", 60) if rate_limit is None else rate_limit
    )
    rate_window_sec = (
        int_env("RATE_LIMIT_WINDOW_SECONDS", 60) if rate_window is None else rate_window
    )

    if not config_api_key:
        logger.warning(
            "API_KEY not set - authentication is DISABLED. Set API_KEY before "
            "exposing this service beyond localhost."
        )

    app = FastAPI(title="ESG RAG Chatbot API", version="0.1.0")
    app.state.bot = bot
    app.state.session_store = session_store or SessionStore(DEFAULT_SESSIONS_PATH)
    if config_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config_cors,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    limiter = RateLimiter(max_requests=rate_limit_req, window_seconds=rate_window_sec)
    app.state.rate_limiter = limiter
    auth = make_auth_check(config_api_key)
    ratelimit = make_rate_limit(limiter, rate_limit_req, rate_window_sec)
    guarded_deps = [Depends(d) for d in (auth, ratelimit) if d is not None]
    auth_deps = [Depends(d) for d in (auth,) if d is not None]

    @app.middleware("http")
    async def observability(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or telemetry.new_request_id()
        token = telemetry.set_request_id(rid)
        started = time.monotonic()
        route = getattr(request.scope.get("route", None), "path", request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            telemetry.reset_request_id(token)
            duration = (time.monotonic() - started) * 1000
            telemetry.observe_http(request.method, route, 500, duration)
            logger.exception("%s %s failed", request.method, route)
            raise
        telemetry.reset_request_id(token)
        duration = (time.monotonic() - started) * 1000
        telemetry.observe_http(request.method, route, response.status_code, duration)
        response.headers["X-Request-ID"] = rid
        logger.info(
            "%s %s -> %s (%.1f ms, rid=%s)",
            request.method,
            route,
            response.status_code,
            duration,
            rid,
        )
        return response

    def get_bot(request: Request):
        if request.app.state.bot is None:
            request.app.state.bot = _default_bot_factory()
        return request.app.state.bot

    def get_sessions(request: Request):
        return request.app.state.session_store

    bot_dep = Depends(get_bot)
    store_dep = Depends(get_sessions)

    @app.get("/metrics")
    def metrics(store=store_dep):
        telemetry.sessions.set(store.count())
        if app.state.bot is not None:
            telemetry.index_chunks.set(app.state.bot.store.count())
        return Response(
            telemetry.render_metrics(),
            media_type=telemetry.METRICS_CONTENT_TYPE,
        )

    @app.get("/")
    def root():
        return {
            "service": "ESG RAG Chatbot API",
            "endpoints": [
                "GET /ui",
                "GET /health",
                "GET /metrics",
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

    @app.post("/chat", response_model=ChatResponse, dependencies=guarded_deps)
    async def chat(req: ChatRequest, bot=bot_dep, store=store_dep):
        session_id = req.session_id or store.new_id()
        store.create(session_id)
        history = store.history(session_id, limit=req.history_limit)
        try:
            answer = await run_in_threadpool(
                bot.ask, question=req.question, k=req.k, source=req.source, history=history
            )
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        store.append(session_id, "user", req.question)
        store.append(session_id, "assistant", answer)
        sources = [r.source for r in getattr(bot, "last_results", []) or []]
        return ChatResponse(session_id=session_id, answer=answer, sources=sources)

    @app.post("/chat/stream", dependencies=guarded_deps)
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
            except RuntimeError as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
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

    @app.post("/search", response_model=SearchResponse, dependencies=guarded_deps)
    async def search(req: SearchRequest, bot=bot_dep):
        try:
            results = await run_in_threadpool(
                bot.retrieve, question=req.question, k=req.k, source=req.source
            )
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
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

    @app.post("/ingest", response_model=IngestResponse, dependencies=auth_deps)
    async def ingest(bot=bot_dep):
        stats = await run_in_threadpool(bot.read_and_embed_data)
        telemetry.index_chunks.set(bot.store.count())
        return IngestResponse(**stats.__dict__)

    @app.get("/sessions", response_model=SessionList, dependencies=auth_deps)
    def list_sessions(limit: int = 20, offset: int = 0, store=store_dep):
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        return store.list_sessions(limit=limit, offset=offset)

    @app.get("/sessions/{session_id}", response_model=SessionMessages, dependencies=auth_deps)
    def get_session(session_id: str, store=store_dep):
        return SessionMessages(session_id=session_id, messages=store.messages(session_id))

    @app.delete("/sessions/{session_id}", dependencies=auth_deps)
    def delete_session(session_id: str, store=store_dep):
        store.delete(session_id)
        return {"deleted": session_id}

    return app


app = create_app()


def run(host="0.0.0.0", port=8000, reload=False):
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    telemetry.configure_logging(getattr(logging, level, logging.INFO))
    uvicorn.run("chatbot.api:app", host=host, port=port, reload=reload)