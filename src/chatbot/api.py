import base64
import csv
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, File, Form, HTTPException, FastAPI, Header, Request, UploadFile

# Load .env before any config is read (create_app() runs at import below).
# Without this, AUTH_SECRET/API_KEY in .env were silently ignored because
# load_dotenv() in chatbot.rag only fires when a RAG bot is first built.
load_dotenv()
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from chatbot import smalltalk, telemetry
from chatbot.admin_security import (
    admin_gate_config,
    make_basic_auth_check,
    make_totp_dep,
)
from chatbot.admin_store import AdminStore, validate_category
from chatbot.ingest import INGEST_PROGRESS
from chatbot.security import RateLimiter, int_env, make_auth_check, make_rate_limit
from chatbot.session_store import SessionStore
from chatbot.users import UserStore, verify_jwt
from chatbot.workspaces import (
    base_workspace_names,
    default_workspace_config,
    index_dir,
    make_workspace_bot,
    reload_extra_workspaces,
    root_data_dir,
    system_prompt_for,
    workspace_meta,
    workspace_names,
)

logger = logging.getLogger("esg.api")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX_DIR = os.environ.get("INDEX_DIR", os.path.join(PROJECT_ROOT, ".index"))
DEFAULT_SESSIONS_PATH = os.path.join(INDEX_DIR, "sessions.sqlite3")
DEFAULT_USERS_PATH = os.path.join(INDEX_DIR, "users.sqlite3")
UI_FILE = Path(__file__).with_name("static") / "ui.html"
ADMIN_FILE = Path(__file__).with_name("static") / "admin.html"
DEFAULT_WORKSPACES_DB = os.path.join(INDEX_DIR, "workspaces.sqlite3")

_DEFAULT = object()


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
    documents_failed: int = 0
    duration_seconds: float | None = None


class SessionMessages(BaseModel):
    session_id: str
    messages: list[dict]


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=40)
    invite: str | None = Field(default=None, max_length=256)


class InviteCreateRequest(BaseModel):
    category: str = Field(min_length=1, max_length=40)
    label: str | None = Field(default=None, max_length=120)
    max_uses: int = Field(default=1, ge=1, le=1000)
    ttl_hours: int = Field(default=168, ge=1, le=8760)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    category: str
    created_at: str


class AuthResponse(BaseModel):
    token: str
    user: UserResponse
    workspace: dict


class MeResponse(BaseModel):
    user: UserResponse
    workspace: dict


class WorkspaceCreateRequest(BaseModel):
    category: str = Field(min_length=2, max_length=40)
    label: str | None = None
    blurb: str | None = None
    emoji: str | None = None


class WorkspaceUpdateRequest(BaseModel):
    label: str | None = None
    blurb: str | None = None
    emoji: str | None = None


class AdminUserCreateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=40)


class AdminUserUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    category: str | None = Field(default=None, min_length=1, max_length=40)
    password: str | None = Field(default=None, min_length=8, max_length=256)


class DocumentDeleteRequest(BaseModel):
    category: str
    filename: str


def tz_offset_minutes(request: Request) -> int:
    try:
        return int(request.headers.get("X-Timezone-Offset", "0"))
    except (TypeError, ValueError):
        return 0


def create_app(
    bot=None,
    session_store=None,
    api_key=None,
    cors_origins=None,
    rate_limit=None,
    rate_window=None,
    user_store=_DEFAULT,
    auth_secret=None,
    workspaces=None,
    admin_store=_DEFAULT,
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
            "API_KEY not set - admin endpoints (ingest) are open. Set API_KEY "
            "before exposing this service beyond localhost."
        )

    if admin_store is _DEFAULT:
        admin_store = AdminStore(DEFAULT_WORKSPACES_DB) if config_api_key else None
    if admin_store is not None:
        reload_extra_workspaces(admin_store.db_path)

    if user_store is _DEFAULT:
        user_store = UserStore(DEFAULT_USERS_PATH, secret=auth_secret)
    auth_enabled = user_store is not None

    app = FastAPI(title="ESG Workspace Chatbot API", version="1.0.0")
    app.state.bot = bot
    app.state.user_store = user_store
    app.state.session_store = session_store or SessionStore(DEFAULT_SESSIONS_PATH)
    app.state.workspace_registry = dict(workspaces or {})
    app.state.admin_store = admin_store
    app.state.workspace_config = (
        default_workspace_config() if bot is None and not workspaces else {}
    )
    app.state.category_bots = {}
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
    auth_deps = [Depends(d) for d in (auth,) if d is not None]
    rate_deps = [Depends(d) for d in (ratelimit,) if d is not None]

    # Optional extra admin-console gates (Basic auth + TOTP). Active only when
    # ADMIN_BASIC_USER/ADMIN_BASIC_PASS and ADMIN_TOTP_SECRET are configured.
    # Combined into a single dependency so the two layers form an OR gate:
    # a valid API key OR (valid Basic credentials AND a fresh TOTP code).
    config_admin_basic_user = os.environ.get("ADMIN_BASIC_USER", "")
    config_admin_basic_pass = os.environ.get("ADMIN_BASIC_PASS", "")
    config_admin_totp = os.environ.get("ADMIN_TOTP_SECRET", "")
    basic_check = make_basic_auth_check(config_admin_basic_user, config_admin_basic_pass)
    totp_dep = make_totp_dep(config_admin_totp)

    def require_admin(
        request: Request,
        apikey: str | None = Header(default=None, alias="Authorization"),
    ):
        """Gate for /admin/* API routes.

        When ADMIN_BASIC_* is configured, the Basic credentials fully replace
        the API key for the console; otherwise a valid Bearer API key is
        required. If ADMIN_TOTP_SECRET is set, a current TOTP code in the
        X-Admin-TOTP header is also mandatory.
        """
        if basic_check is not None:
            basic_check(request)
        else:
            bearer = apikey.removeprefix("Bearer ").strip() if apikey else ""
            if not (config_api_key and bearer == config_api_key):
                raise HTTPException(status_code=401, detail="Invalid or missing API key")
        if totp_dep is not None:
            totp_dep(request)

    admin_gate_deps = [Depends(require_admin)]

    app.state.admin_gate = admin_gate_config()

    def log_audit(request, event, object_type=None, object_id=None, detail=""):
        """Best-effort append-only audit record for an admin action."""
        store = app.state.admin_store
        if store is None:
            return
        actor = ""
        if config_api_key:
            actor = "key:" + hashlib.sha256(config_api_key.encode()).hexdigest()[:8]
        if config_admin_basic_user:
            actor = "user:" + config_admin_basic_user
        try:
            store.log_event(actor, event, object_type, object_id, detail[:2000])
        except Exception:
            logger.exception("audit log write failed (%s)", event)

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

    if auth_enabled:

        async def require_user(
            authorization: str | None = Header(default=None),
        ) -> dict:
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing or invalid token")
            payload = verify_jwt(user_store.secret, authorization[len("Bearer ") :])
            if not payload or not payload.get("sub"):
                raise HTTPException(status_code=401, detail="Missing or invalid token")
            return {
                "id": payload["sub"],
                "email": payload.get("email", ""),
                "name": payload.get("name", ""),
                "category": payload.get("category", ""),
            }

    else:

        async def require_user() -> dict:
            return {
                "id": "dev",
                "email": "dev@localhost",
                "name": "Developer",
                "category": (workspace_names() or ["finance"])[0],
                "dev": True,
            }

    user_dep = Depends(require_user)

    def get_sessions(request: Request):
        return request.app.state.session_store

    store_dep = Depends(get_sessions)

    def get_admin_store() -> AdminStore:
        store = app.state.admin_store
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Admin store not configured (set API_KEY to enable admin "
                "console features).",
            )
        return store

    admin_store_dep = Depends(get_admin_store)

    def get_bot(request: Request, category: str):
        app_ = request.app
        if category and category in app_.state.workspace_registry:
            return app_.state.workspace_registry[category]
        if category and category in app_.state.workspace_config:
            cached = app_.state.category_bots.get(category)
            if cached is None:
                cached = make_workspace_bot(
                    category, app_.state.workspace_config[category]
                )
                app_.state.category_bots[category] = cached
            return cached
        if app_.state.bot is not None:
            return app_.state.bot
        raise HTTPException(status_code=403, detail=f"Unknown workspace: {category}")

    def resolve_session(request: Request, session_id, user_id, store):
        if session_id:
            owner = store.owner_of(session_id)
            if owner != user_id:
                raise HTTPException(status_code=404, detail="Session not found")
        else:
            session_id = store.new_id()
        store.create(session_id, user_id=user_id)
        return session_id

    def workspace_summary(category):
        return {
            "category": category,
            "categories": workspace_names(),
            **workspace_meta(category),
        }

    @app.get("/metrics")
    def metrics(store=store_dep):
        total_chunks = 0
        if app.state.bot is not None:
            total_chunks += app.state.bot.store.count()
        for b in list(app.state.workspace_registry.values()) + list(
            app.state.category_bots.values()
        ):
            total_chunks += b.store.count()
        telemetry.sessions.set(store.count())
        telemetry.index_chunks.set(total_chunks)
        return Response(
            telemetry.render_metrics(),
            media_type=telemetry.METRICS_CONTENT_TYPE,
        )

    @app.get("/")
    def root():
        return {
            "service": "ESG Workspace Chatbot API",
            "workspaces": workspace_names(),
            "endpoints": [
                "GET /ui",
                "GET /admin",
                "GET /admin/status",
                "GET /admin/workspaces",
                "POST /admin/workspaces",
                "GET /admin/documents",
                "POST /admin/upload",
                "GET /admin/users",
                "GET /workspaces",
                "GET /health",
                "GET /metrics",
                "POST /auth/signup",
                "POST /auth/login",
                "GET /me",
                "POST /chat",
                "POST /chat/stream",
                "POST /search",
                "POST /ingest",
                "POST /ingest/{category}",
                "GET /ingest/status",
                "GET /sessions",
                "GET /sessions/{session_id}",
                "DELETE /sessions/{session_id}",
            ],
        }

    @app.get("/workspaces")
    def public_workspaces():
        """Categories available for signup (public, no secrets)."""
        return [
            {**meta, "category": cat}
            for cat in workspace_names()
            for meta in [workspace_meta(cat)]
        ]

    @app.get("/ui", response_class=HTMLResponse)
    def ui():
        return UI_FILE.read_text() if UI_FILE.exists() else "<h1>UI not found</h1>"

    @app.get("/admin", response_class=HTMLResponse)
    def admin():
        return ADMIN_FILE.read_text() if ADMIN_FILE.exists() else "<h1>Admin UI not found</h1>"

    @app.get("/admin/config")
    def admin_public_config():
        """Public, secret-free shape of the optional admin auth layers."""
        return app.state.admin_gate

    @app.get("/admin/status", dependencies=admin_gate_deps)
    def admin_status(request: Request):
        registry = app.state.workspace_registry
        config_based = bool(app.state.workspace_config)
        cats = (
            list(registry)
            if registry
            else workspace_names()
        )
        statuses = []
        total_chunks = 0
        for cat in cats:
            meta = workspace_meta(cat)
            if config_based:
                db = Path(index_dir()) / f"vectors_{cat}.sqlite3"
                chunks = sources = 0
                if db.exists():
                    try:
                        conn = sqlite3.connect(db)
                        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                        sources = conn.execute(
                            "SELECT COUNT(DISTINCT source) FROM chunks"
                        ).fetchone()[0]
                        conn.close()
                    except sqlite3.Error:
                        pass
            else:
                bot = get_bot(request, cat)
                try:
                    chunks = bot.store.count()
                except (AttributeError, TypeError):
                    chunks = 0
                sources = 0
            total_chunks += chunks or 0
            data = Path(root_data_dir()) / cat
            files = sorted(p.name for p in data.iterdir()) if data.is_dir() else []
            statuses.append(
                {
                    "category": cat,
                    "label": meta.get("label", cat.title()),
                    "emoji": meta.get("emoji"),
                    "blurb": meta.get("blurb"),
                    "chunks": chunks,
                    "sources": sources,
                    "data_file_count": len(files),
                    "data_files": files,
                    "indexed": (chunks or 0) > 0,
                }
            )
        totals = {
            "workspaces": len(cats),
            "chunks": total_chunks,
            "users": user_store.count_users() if user_store is not None else 0,
            "sessions": app.state.session_store.count(),
        }
        return {"totals": totals, "workspaces": statuses, "progress": INGEST_PROGRESS.snapshot()}

    @app.get("/admin/workspaces", dependencies=admin_gate_deps)
    def admin_workspaces(get_store: AdminStore = admin_store_dep):
        base = set(base_workspace_names())
        rows = []
        for cat in workspace_names():
            meta = workspace_meta(cat)
            folder = Path(root_data_dir()) / cat
            file_count = len([p for p in folder.iterdir() if p.is_file()]) if folder.is_dir() else 0
            _, chunk_count = _vector_stats(cat)
            rows.append(
                {
                    "category": cat,
                    "label": meta.get("label", cat.title()),
                    "emoji": meta.get("emoji"),
                    "blurb": meta.get("blurb"),
                    "type": "built-in" if cat in base else "custom",
                    "custom": cat not in base,
                    "data_file_count": file_count,
                    "chunks": chunk_count,
                }
            )
        return {"workspaces": rows}

    @app.post("/admin/workspaces", dependencies=admin_gate_deps)
    def admin_create_workspace(
        req: WorkspaceCreateRequest, request: Request, get_store: AdminStore = admin_store_dep
    ):
        try:
            cat = validate_category(req.category)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        row = get_store.upsert_workspace(
            cat, label=req.label, blurb=req.blurb, emoji=req.emoji
        )
        reload_extra_workspaces(get_store.db_path)
        os.makedirs(Path(root_data_dir()) / cat, exist_ok=True)
        os.makedirs(Path(index_dir()), exist_ok=True)
        log_audit(request, "workspace.create", "workspace", cat,
                  f"label={row.get('label')}")
        return {**row, "type": "custom"}

    @app.patch("/admin/workspaces/{category}", dependencies=admin_gate_deps)
    def admin_update_workspace(
        category: str,
        req: WorkspaceUpdateRequest,
        request: Request,
        get_store: AdminStore = admin_store_dep,
    ):
        if category not in workspace_names():
            raise HTTPException(status_code=404, detail="Unknown workspace")
        row = get_store.upsert_workspace(
            category, label=req.label, blurb=req.blurb, emoji=req.emoji
        )
        reload_extra_workspaces(get_store.db_path)
        log_audit(request, "workspace.update", "workspace", category,
                  "label/blurb/emoji changed")
        meta = workspace_meta(category)
        return {
            "category": category,
            "label": meta.get("label"),
            "emoji": meta.get("emoji"),
            "blurb": meta.get("blurb"),
            "type": "custom" if category not in base_workspace_names() else "built-in",
            "custom": category not in base_workspace_names(),
        }

    @app.delete("/admin/workspaces/{category}", dependencies=admin_gate_deps)
    def admin_delete_workspace(
        category: str, request: Request, get_store: AdminStore = admin_store_dep
    ):
        if category not in workspace_names():
            raise HTTPException(status_code=404, detail="Unknown workspace")
        if category in base_workspace_names():
            raise HTTPException(
                status_code=403,
                detail="Built-in workspaces cannot be deleted; exclude them via the "
                "WORKSPACES env variable instead.",
            )
        if not get_store.delete_workspace(category):
            raise HTTPException(status_code=404, detail="Workspace not in admin store")
        reload_extra_workspaces(get_store.db_path)
        log_audit(request, "workspace.delete", "workspace", category)
        return {"deleted": category}

    def _vector_stats(cat):
        db = Path(index_dir()) / f"vectors_{cat}.sqlite3"
        if not db.exists():
            return set(), 0
        try:
            conn = sqlite3.connect(db)
            source_names = {
                r[0]
                for r in conn.execute("SELECT DISTINCT source FROM chunks").fetchall()
            }
            chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            conn.close()
            return source_names, chunk_count
        except sqlite3.Error:
            return set(), 0

    def _indexed_sources(cat):
        return _vector_stats(cat)[0]

    @app.get("/admin/documents", dependencies=admin_gate_deps)
    def admin_documents(limit: int = 100, offset: int = 0, category: str | None = None):
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        registry = app.state.workspace_registry
        if category is not None:
            if category not in (list(registry) if registry else workspace_names()):
                raise HTTPException(status_code=404, detail="Unknown workspace")
            cats = [category]
        else:
            cats = list(registry) if registry else workspace_names()
        groups = []
        for cat in cats:
            indexed = _indexed_sources(cat)
            folder = Path(root_data_dir()) / cat
            all_files = []
            if folder.is_dir():
                for p in sorted(folder.iterdir()):
                    if not p.is_file():
                        continue
                    all_files.append(
                        {
                            "name": p.name,
                            "size": p.stat().st_size,
                            "indexed": p.name in indexed,
                        }
                    )
            page = all_files[offset : offset + limit]
            groups.append(
                {
                    "category": cat,
                    "files": page,
                    "total_files": len(all_files),
                    "has_more": offset + len(page) < len(all_files),
                }
            )
        return {"documents": groups, "limit": limit, "offset": offset}

    @app.post(
        "/admin/upload",
        dependencies=admin_gate_deps,
        response_model=dict,
    )
    async def admin_upload(
        request: Request,
        category: str = Form(...),
        files: list[UploadFile] = File(default=[]),
    ):
        if category not in workspace_names():
            raise HTTPException(status_code=422, detail="Unknown workspace")
        folder = Path(root_data_dir()) / category
        os.makedirs(folder, exist_ok=True)
        saved = []
        for f in files:
            name = os.path.basename((f.filename or "").replace("\\", "/"))
            if not name:
                continue
            dest = folder / name
            dest.write_bytes(await f.read())
            saved.append(name)
        if not saved:
            raise HTTPException(status_code=422, detail="No files were uploaded")
        started = time.time()
        bot = get_bot(request, category)
        stats = await run_in_threadpool(bot.read_and_embed_data)
        log_audit(request, "document.upload", "workspace", category,
                  ",".join(saved))
        return {
            "saved_files": saved,
            **stats.__dict__,
            "duration_seconds": round(time.time() - started, 1),
        }

    @app.post("/admin/documents/delete", dependencies=admin_gate_deps)
    async def admin_delete_document(
        req: DocumentDeleteRequest, request: Request
    ):
        if req.category not in workspace_names():
            raise HTTPException(status_code=422, detail="Unknown workspace")
        folder = Path(root_data_dir()) / req.category
        name = os.path.basename(req.filename.replace("\\", "/"))
        target = folder / name
        if not folder.is_dir() or not target.is_file():
            raise HTTPException(status_code=404, detail="Document not found")
        target.unlink()
        bot = get_bot(request, req.category)
        stats = {}
        if bot is not None:
            result = await run_in_threadpool(bot.read_and_embed_data)
            stats = result.__dict__
        log_audit(request, "document.delete", "workspace", req.category, name)
        return {
            "deleted": name,
            "workspace": req.category,
            "stale_chunks_removed": stats.get("stale_chunks_removed", 0),
            "chunks_upserted": stats.get("chunks_upserted", 0),
        }

    @app.get("/admin/users", dependencies=admin_gate_deps)
    def admin_users(limit: int = 100, offset: int = 0):
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        users = user_store.list_users(limit=max(1, min(limit, 500)), offset=max(0, offset))
        session_store = app.state.session_store
        for u in users:
            u["session_count"] = session_store.count(user_id=u["id"])
        return {"total": user_store.count_users(), "users": users}

    @app.post("/admin/users", dependencies=admin_gate_deps)
    def admin_create_user(req: AdminUserCreateRequest, request: Request):
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        try:
            user = user_store.create_user(
                req.email, req.password, req.name, req.category
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        user["session_count"] = app.state.session_store.count(user_id=user["id"])
        log_audit(request, "user.create", "user", user.get("id"), req.email)
        return user

    @app.patch("/admin/users/{user_id}", dependencies=admin_gate_deps)
    def admin_update_user(
        user_id: str, req: AdminUserUpdateRequest, request: Request
    ):
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        try:
            user = user_store.update_user(
                user_id, name=req.name, category=req.category, password=req.password
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        user["session_count"] = app.state.session_store.count(user_id=user["id"])
        log_audit(request, "user.update", "user", user_id)
        return user

    @app.delete("/admin/users/{user_id}", dependencies=admin_gate_deps)
    def admin_delete_user(user_id: str, request: Request):
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        if not user_store.delete_user(user_id):
            raise HTTPException(status_code=404, detail="User not found")
        log_audit(request, "user.delete", "user", user_id)
        return {"deleted": user_id}

    @app.get("/admin/audit", dependencies=admin_gate_deps)
    def admin_audit(limit: int = 100, offset: int = 0):
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store is disabled")
        return store.list_events(
            limit=max(1, min(limit, 500)), offset=max(0, offset)
        )

    @app.get("/admin/users/{user_id}/sessions", dependencies=admin_gate_deps)
    def admin_user_sessions(user_id: str, limit: int = 50, offset: int = 0):
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        if user_store.get(user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        sessions = app.state.session_store.list_sessions(
            limit=max(1, min(limit, 200)), offset=max(0, offset), user_id=user_id
        )
        return sessions

    @app.get("/admin/sessions/{session_id}", dependencies=admin_gate_deps)
    def admin_session_messages(session_id: str):
        if not app.state.session_store.exists(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session_id": session_id, "messages": app.state.session_store.messages(session_id)}

    @app.get("/admin/users/export.csv", dependencies=admin_gate_deps)
    def admin_users_csv():
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        users = user_store.list_users(limit=100_000, offset=0)
        session_store = app.state.session_store
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["id", "email", "name", "category", "created_at", "last_login", "session_count"]
        )
        for u in users:
            writer.writerow(
                [
                    u.get("id", ""),
                    u.get("email", ""),
                    u.get("name", ""),
                    u.get("category", ""),
                    u.get("created_at", ""),
                    u.get("last_login") or "",
                    session_store.count(user_id=u["id"]),
                ]
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="users.csv"',
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
        )

    @app.get("/admin/workspaces/export.csv", dependencies=admin_gate_deps)
    def admin_workspaces_csv():
        registry = app.state.workspace_registry
        cats = list(registry) if registry else workspace_names()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["category", "label", "type", "blurb", "files", "chunks", "users"])
        base = base_workspace_names()
        session_store = app.state.session_store
        user_store_local = user_store
        for cat in cats:
            meta = workspace_meta(cat)
            sources = _indexed_sources(cat)
            folder = Path(root_data_dir()) / cat
            file_count = (
                sum(1 for p in folder.iterdir() if p.is_file()) if folder.is_dir() else 0
            )
            user_count = (
                user_store_local.count_for_category(cat)
                if user_store_local is not None
                else 0
            )
            writer.writerow(
                [
                    cat,
                    meta.get("label", cat),
                    "built-in" if cat in base else "custom",
                    (meta.get("blurb") or "") if meta else "",
                    file_count,
                    len(sources),
                    user_count,
                ]
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="workspaces.csv"',
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
        )

    @app.post("/admin/invites", dependencies=admin_gate_deps)
    def admin_create_invite(req: InviteCreateRequest, request: Request):
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store is disabled")
        try:
            invite = store.create_invite(
                req.category,
                label=req.label,
                max_uses=req.max_uses,
                ttl_hours=req.ttl_hours,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        log_audit(request, "invite.create", "workspace", req.category,
                  f"max_uses={req.max_uses} ttl_hours={req.ttl_hours}")
        return invite

    @app.get("/admin/invites", dependencies=admin_gate_deps)
    def admin_list_invites():
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store is disabled")
        return {"invites": store.list_invites()}

    @app.delete("/admin/invites/{token}", dependencies=admin_gate_deps)
    def admin_delete_invite(token: str, request: Request):
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store is disabled")
        if not store.delete_invite(token):
            raise HTTPException(status_code=404, detail="Invite not found")
        log_audit(request, "invite.delete", "workspace", None, "invite revoked")
        return {"deleted": True}

    @app.get("/invites/{token}")
    def public_invite_check(token: str):
        """Public, rate-limited check: stable + callable before/while signing up."""
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=404, detail="Invites are not enabled")
        try:
            return store.peek_invite(token)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/health")
    def health():
        return {"status": "ok", "auth": auth_enabled, "workspaces": workspace_names()}

    @app.post("/auth/signup", response_model=AuthResponse, dependencies=rate_deps)
    def signup(req: SignupRequest, request: Request):
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        admin_store = app.state.admin_store
        invite_meta = None
        if req.invite:
            if admin_store is None:
                raise HTTPException(status_code=403, detail="Invites are not enabled")
            try:
                invite_meta = admin_store.peek_invite(req.invite)
            except ValueError as e:
                raise HTTPException(status_code=403, detail=str(e))
        try:
            user = user_store.create_user(req.email, req.password, req.name, req.category)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if invite_meta is not None:
            try:
                admin_store.redeem_invite(req.invite)
            except ValueError as e:
                user_store.delete_user(user["id"])
                raise HTTPException(status_code=403, detail=str(e))
        log_audit(request, "auth.signup", "user", user.get("id"),
                  f"email={req.email} category={req.category} invite={bool(req.invite)}")
        token = user_store.token_for(user)
        return AuthResponse(token=token, user=user, workspace=workspace_summary(user["category"]))

    @app.post("/auth/login", response_model=AuthResponse, dependencies=rate_deps)
    def login(req: LoginRequest):
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        user = user_store.verify(req.email, req.password)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        user_store.mark_login(user["id"])
        user["last_login"] = user_store.get(user["id"])["last_login"]
        token = user_store.token_for(user)
        return AuthResponse(token=token, user=user, workspace=workspace_summary(user["category"]))

    @app.get("/me", response_model=MeResponse)
    def me(user: dict = user_dep):
        profile = user
        if user_store is not None:
            full = user_store.get(user["id"])
            if full:
                profile = full
        return MeResponse(user=profile, workspace=workspace_summary(profile["category"]))

    @app.post("/chat", response_model=ChatResponse, dependencies=rate_deps)
    async def chat(
        req: ChatRequest,
        request: Request,
        user: dict = user_dep,
        store=store_dep,
    ):
        session_id = resolve_session(request, req.session_id, user["id"], store)
        history = store.history(session_id, limit=req.history_limit)
        bot = get_bot(request, user["category"])
        tz = tz_offset_minutes(request)
        reply = smalltalk.handle(req.question, user["name"], user["category"], tz)
        sources = []
        if reply is None:
            try:
                reply = await run_in_threadpool(
                    bot.ask,
                    question=req.question,
                    k=req.k,
                    source=req.source,
                    history=history,
                )
            except RuntimeError as e:
                raise HTTPException(status_code=503, detail=str(e))
            sources = [r.source for r in getattr(bot, "last_results", []) or []]
        store.append(session_id, "user", req.question)
        store.append(session_id, "assistant", reply)
        return ChatResponse(session_id=session_id, answer=reply, sources=sources)

    @app.post("/chat/stream", dependencies=rate_deps)
    async def chat_stream(
        req: ChatRequest,
        request: Request,
        user: dict = user_dep,
        store=store_dep,
    ):
        session_id = resolve_session(request, req.session_id, user["id"], store)
        history = store.history(session_id, limit=req.history_limit)
        bot = get_bot(request, user["category"])
        tz = tz_offset_minutes(request)
        reply = smalltalk.handle(req.question, user["name"], user["category"], tz)

        def event_stream():
            chunks = []
            try:
                if reply is not None:
                    chunks.append(reply)
                    yield f"data: {json.dumps({'delta': reply})}\n\n"
                else:
                    for piece in bot.ask_stream(
                        question=req.question,
                        k=req.k,
                        source=req.source,
                        history=history,
                    ):
                        chunks.append(piece)
                        yield f"data: {json.dumps({'delta': piece})}\n\n"
            except RuntimeError as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            finally:
                store.append(session_id, "user", req.question)
                store.append(session_id, "assistant", "".join(chunks))
            if reply is not None:
                sources = []
            else:
                sources = [r.source for r in getattr(bot, "last_results", []) or []]
            yield f"data: {json.dumps({'sources': sources})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"X-Session-ID": session_id},
        )

    @app.post("/search", response_model=SearchResponse, dependencies=rate_deps)
    async def search(
        req: SearchRequest,
        request: Request,
        user: dict = user_dep,
    ):
        bot = get_bot(request, user["category"])
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

    def iter_workspace_bots(request: Request):
        app_ = request.app
        if app_.state.workspace_config:
            categories = list(app_.state.workspace_config)
        elif app_.state.workspace_registry:
            categories = list(app_.state.workspace_registry)
        else:
            if app_.state.bot is not None:
                yield app_.state.bot
            return
        for category in categories:
            yield get_bot(request, category)

    @app.post("/ingest", response_model=IngestResponse, dependencies=auth_deps)
    async def ingest(request: Request):
        started = time.time()
        totals = {
            "documents_seen": 0,
            "documents_reindexed": 0,
            "chunks_upserted": 0,
            "stale_chunks_removed": 0,
            "documents_failed": 0,
        }
        for bot in iter_workspace_bots(request):
            stats = await run_in_threadpool(bot.read_and_embed_data)
            for key in totals:
                totals[key] += getattr(stats, key, 0)
        return IngestResponse(
            **totals, duration_seconds=round(time.time() - started, 1)
        )

    @app.post("/ingest/{category}", response_model=IngestResponse, dependencies=auth_deps)
    async def ingest_category(category: str, request: Request):
        bot = get_bot(request, category)
        started = time.time()
        stats = await run_in_threadpool(bot.read_and_embed_data)
        return IngestResponse(
            **stats.__dict__, duration_seconds=round(time.time() - started, 1)
        )

    @app.get("/ingest/status", dependencies=auth_deps)
    def ingest_status():
        return INGEST_PROGRESS.snapshot()

    @app.get("/sessions", response_model=SessionList)
    def list_sessions(
        limit: int = 20, offset: int = 0, user: dict = user_dep, store=store_dep
    ):
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        return store.list_sessions(limit=limit, offset=offset, user_id=user["id"])

    @app.get("/sessions/{session_id}", response_model=SessionMessages)
    def get_session(session_id: str, user: dict = user_dep, store=store_dep):
        if store.owner_of(session_id) != user["id"]:
            raise HTTPException(status_code=404, detail="Session not found")
        return SessionMessages(session_id=session_id, messages=store.messages(session_id))

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str, user: dict = user_dep, store=store_dep):
        if not store.delete(session_id, user_id=user["id"]):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"deleted": session_id}

    return app


app = create_app()


def run(host="0.0.0.0", port=None, reload=False):
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    telemetry.configure_logging(getattr(logging, level, logging.INFO))
    if port is None:
        port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("chatbot.api:app", host=host, port=port, reload=reload)