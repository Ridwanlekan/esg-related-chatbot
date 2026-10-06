import base64
import csv
import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
import urllib.parse
import uuid
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import (
    Cookie,
    Depends,
    File,
    Form,
    HTTPException,
    FastAPI,
    Header,
    Query,
    Request,
    UploadFile,
)

# Load .env before any config is read (create_app() runs at import below).
# Without this, AUTH_SECRET/API_KEY in .env were silently ignored because
# load_dotenv() in chatbot.rag only fires when a RAG bot is first built.
load_dotenv()
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from chatbot import content_library, shared_docs, smalltalk, telemetry, voice
from chatbot.admin_security import (
    admin_gate_config,
    make_basic_auth_check,
    make_totp_dep,
    verify_totp,
)
from chatbot.admin_store import AdminStore, validate_category
from chatbot.documents import DocumentNotFound, resolve_document
from chatbot.download_links import (
    TOKEN_QUERY_PARAM,
    content_disposition,
    delivery_for,
    download_ttl_seconds,
    is_download_token,
    mint_download_token,
    page_fragment,
    verify_download_token,
)
from chatbot.ingest import INGEST_PROGRESS
from chatbot.security import RateLimiter, int_env, make_auth_check, make_rate_limit
from chatbot.session_store import SessionStore
from chatbot.users import SAMPLE_ORGANISATION_ID, UserStore, verify_jwt
from chatbot.workspaces import (
    base_workspace_names,
    default_workspace_config,
    index_dir,
    make_workspace_bot,
    organisation_workspace_config,
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


DOCUMENT_DOWNLOAD_PATH = "/documents/download"
DOCUMENT_LINK_PATH = "/documents/link"


class SourceRef(BaseModel):
    """One verifiable citation: where the claim came from, and how to open it.

    `similarity` is the retrieval distance-to-cosine score, NOT a confidence
    figure. It is shown for transparency about why a document was retrieved and
    must never be presented to users as a percentage of correctness.

    `page_start`/`page_end` are the 1-based page range the quoted chunk sits on,
    or None when the document has no pagination or predates page tracking. They
    are part of the citation, not a hint: "p. 42" is what makes a claim checkable
    against a 200-page report.
    """

    source: str
    url: str
    chunk_id: str
    chunk_index: int
    similarity: float
    page_start: int | None = None
    page_end: int | None = None


def document_url(source, token=None, page_start=None):
    """Citation link for a retrieved source.

    The reference travels as a query parameter rather than a path segment on
    purpose: ASGI percent-decodes scope["path"] before routing, so a %2F-encoded
    filename arrives as a real "/" and gets split into extra path segments.
    A query parameter cannot be re-split.

    `token` is a single-document download capability rather than the session
    token, so that the link survives being opened natively in a new tab, which
    sends no Authorization header. `page_start` becomes a `#page=` fragment,
    which only does something for the formats served inline.
    """
    query = {"source": source}
    if token:
        query[TOKEN_QUERY_PARAM] = token
    encoded = urllib.parse.urlencode(query, safe="", quote_via=urllib.parse.quote)
    return f"{DOCUMENT_DOWNLOAD_PATH}?{encoded}{page_fragment(page_start)}"


def citation_refs(results, token_for=None):
    """Turn retrieval results into citation objects for an API response.

    `token_for(source)` mints the download capability; it is called at most once
    per distinct document because several retrieved chunks usually share a file.
    """
    tokens = {}
    refs = []
    for r in results or []:
        token = None
        if token_for is not None:
            if r.source not in tokens:
                tokens[r.source] = token_for(r.source)
            token = tokens[r.source]
        refs.append(
            SourceRef(
                source=r.source,
                url=document_url(r.source, token, r.page_start),
                chunk_id=r.chunk_id,
                chunk_index=r.chunk_index,
                similarity=round(float(r.similarity), 4),
                page_start=r.page_start,
                page_end=r.page_end,
            )
        )
    return refs


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None
    k: int = Field(default=3, ge=1, le=20)
    source: str | None = None
    history_limit: int = Field(default=10, ge=0, le=50)


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: list[SourceRef] = []


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
    page_start: int | None = None
    page_end: int | None = None


class SearchResponse(BaseModel):
    question: str
    results: list[SearchItem]


class DocumentLinkRequest(BaseModel):
    """Mint a fresh download link for a document the caller can already reach."""

    source: str = Field(min_length=1, max_length=512)
    page_start: int | None = Field(default=None, ge=1)


class DocumentLinkResponse(BaseModel):
    source: str
    url: str
    expires_in: int


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
    # Absent for tokens minted before organisations existed, so optional to keep
    # those sessions working rather than failing response validation.
    organisation_id: str | None = None
    # Optional for the same reason: tokens predating the membership table carry
    # no claim, and the server falls back to `category` for those users.
    workspaces: list[str] = []
    created_at: str


class AuthResponse(BaseModel):
    token: str
    user: UserResponse
    workspace: dict


class MeResponse(BaseModel):
    user: UserResponse
    workspace: dict
    # Every workspace this user may reach, primary first. More than one means
    # the frontend should offer a switcher; exactly one means it must not.
    workspaces: list[dict] = []


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
    # Omitted means the free self-service path: the account lands in the shared
    # sample organisation on the launch pack (Q22, Section 3.5). A paying
    # account is created by an Administrator directly into its organisation.
    organisation_id: str | None = Field(default=None, min_length=1, max_length=64)


class AdminUserUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    category: str | None = Field(default=None, min_length=1, max_length=40)
    password: str | None = Field(default=None, min_length=8, max_length=256)


class DocumentDeleteRequest(BaseModel):
    category: str
    filename: str


class OrganisationCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=160)


class OrganisationDeleteRequest(BaseModel):
    organisation_id: str = Field(min_length=1, max_length=64)
    # Must echo the organisation id. See admin_delete_organisation.
    confirm: str = Field(min_length=1, max_length=64)


class ContentAssignRequest(BaseModel):
    organisation_id: str = Field(min_length=1, max_length=64)
    categories: list[str] = Field(default_factory=list)
    filenames: list[str] = Field(min_length=1)


class ContentUnassignRequest(BaseModel):
    organisation_id: str = Field(min_length=1, max_length=64)
    categories: list[str] = Field(default_factory=list)
    filenames: list[str] = Field(min_length=1)


class SharedLinkRequest(BaseModel):
    filename: str
    categories: list[str] = Field(default_factory=list)
    link_names: dict[str, str] | None = None


class SharedUnlinkRequest(BaseModel):
    filename: str
    category: str


class CostRatesRequest(BaseModel):
    price_input_per_m: float = Field(ge=0)
    price_output_per_m: float = Field(ge=0)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    totp: str | None = Field(default=None, max_length=8)


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

    if user_store is not None and user_store.get_organisation(SAMPLE_ORGANISATION_ID):
        # Q2: the free tier must work the moment someone signs up, with no
        # administrator action. Idempotent, so this is safe on every start and
        # never overwrites an Administrator's later edits to the sample pack.
        try:
            seeded = content_library.seed_sample_pack(
                SAMPLE_ORGANISATION_ID, categories=workspace_names()
            )
            if seeded:
                logger.info("Sample pack seeded for %s: %d document(s).",
                            SAMPLE_ORGANISATION_ID, len(seeded))
        except (OSError, ValueError):
            logger.exception("Sample pack seeding failed; free tier may be empty.")

    # Signs and verifies citation download links. With auth on this is AUTH_SECRET,
    # so rotating it revokes every outstanding link. With auth off there is no
    # secret to borrow, so links get a per-process one: dev mode already serves
    # documents to anyone who asks, and native new-tab links keep working there.
    link_secret = user_store.secret if user_store is not None else secrets.token_hex(32)
    app = FastAPI(title="ESG Workspace Chatbot API", version="1.0.0")
    app.state.bot = bot
    app.state.user_store = user_store
    app.state.link_secret = link_secret
    app.state.session_store = session_store or SessionStore(DEFAULT_SESSIONS_PATH)
    app.state.workspace_registry = dict(workspaces or {})
    app.state.admin_store = admin_store
    app.state.workspace_config = (
        default_workspace_config() if bot is None and not workspaces else {}
    )
    app.state.category_bots = {}
    # Per-organisation bots, keyed by (organisation_id, workspace). Empty unless
    # auth is on; the cache exists so a workspace's live vector-store handle
    # survives between requests instead of being rebuilt each time.
    app.state.org_bots = {}
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

    # Admin console sign-in sessions (HttpOnly cookie). The cookie carries an
    # opaque token whose hash lives in the admin store; the backend is the
    # only party that can mint/validate it.
    admin_session_cookie = os.environ.get("ADMIN_SESSION_COOKIE", "esg_admin_session")
    admin_session_ttl_seconds = int_env("ADMIN_SESSION_TTL_HOURS", 12) * 3600
    admin_session_secure = os.environ.get("ADMIN_SESSION_SECURE", "") == "1"

    def require_admin(
        request: Request,
        apikey: str | None = Header(default=None, alias="Authorization"),
        session: str | None = Cookie(default=None, alias=admin_session_cookie),
    ):
        """Gate for /admin/* API routes.

        A valid admin sign-in session cookie alone authorizes the request
        (no per-request credentials). Otherwise: when ADMIN_BASIC_* is
        configured, the Basic credentials fully replace the API key for the
        console; else a valid Bearer API key is required. If
        ADMIN_TOTP_SECRET is set, a current TOTP code in the X-Admin-TOTP
        header is also mandatory on the legacy path.
        """
        if session and admin_store is not None and admin_store.get_admin_session(session):
            return
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

    def actor_label(request):
        """Identify the caller for audit rows and registry bookkeeping."""
        actor = ""
        if config_api_key:
            actor = "key:" + hashlib.sha256(config_api_key.encode()).hexdigest()[:8]
        if config_admin_basic_user:
            actor = "user:" + config_admin_basic_user
        return actor

    def log_audit(request, event, object_type=None, object_id=None, detail="",
                  actor=None):
        """Best-effort append-only audit record for an admin action.

        `actor` overrides the console-derived label for events raised by an end
        user (a citation download), which have no admin credential to derive one
        from.
        """
        store = app.state.admin_store
        if store is None:
            return
        try:
            store.log_event(
                actor or actor_label(request), event, object_type, object_id,
                detail[:2000],
            )
        except Exception:
            logger.exception("audit log write failed (%s)", event)

    @app.post("/admin/login", dependencies=rate_deps)
    def admin_login(req: AdminLoginRequest, request: Request, response: Response):
        """Password sign-in for the admin console -> HttpOnly session cookie.

        Uses the ADMIN_BASIC_USER/PASS identity (single source of truth). On
        success the server stores a session hash and hands the client an
        opaque, short-lived, HttpOnly SameSite=strict cookie, so credentials
        are never persisted client-side and nothing is re-sent per request.
        """
        if admin_store is None:
            raise HTTPException(
                status_code=503, detail="Admin console is not enabled (set API_KEY)"
            )
        if not (config_admin_basic_user and config_admin_basic_pass):
            raise HTTPException(
                status_code=503,
                detail="Password sign-in is not configured "
                "(set ADMIN_BASIC_USER/ADMIN_BASIC_PASS)",
            )
        user_ok = secrets.compare_digest(
            (req.username or "").strip(), config_admin_basic_user
        )
        pass_ok = secrets.compare_digest(req.password or "", config_admin_basic_pass)
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=401,
                detail="Invalid username or password",
            )
        if config_admin_totp and not verify_totp(config_admin_totp, req.totp):
            raise HTTPException(
                status_code=401, detail="Authenticator code required or expired"
            )
        created = admin_store.create_admin_session(
            config_admin_basic_user, admin_session_ttl_seconds
        )
        response.set_cookie(
            admin_session_cookie,
            created["token"],
            max_age=admin_session_ttl_seconds,
            httponly=True,
            samesite="strict",
            secure=admin_session_secure,
            path="/",
        )
        log_audit(request, "admin.login", "user", config_admin_basic_user,
                  f"expires_at={created['expires_at']}")
        return {"ok": True, "actor": config_admin_basic_user, "expires_at": created["expires_at"]}

    @app.post("/admin/logout", dependencies=rate_deps)
    def admin_logout(
        request: Request,
        response: Response,
        session: str | None = Cookie(default=None, alias=admin_session_cookie),
    ):
        """Invalidate the server-side session and clear the cookie."""
        if admin_store is not None and session:
            existing = admin_store.get_admin_session(session)
            admin_store.delete_admin_session(session)
            if existing:
                log_audit(request, "admin.logout", "user", existing["actor"])
        response.delete_cookie(admin_session_cookie, path="/")
        return {"ok": True}

    @app.get("/admin/session")
    def admin_session_status(
        session: str | None = Cookie(default=None, alias=admin_session_cookie),
    ):
        """Public, secret-free sign-in state for the admin UI."""
        info = None
        if admin_store is not None and session:
            info = admin_store.get_admin_session(session)
        cfg = admin_gate_config()
        return {
            "authenticated": info is not None,
            "actor": info["actor"] if info else None,
            "expires_at": info["expires_at"] if info else None,
            "login": cfg["basic"],
            "basic": cfg["basic"],
            "totp": cfg["totp"],
        }

    def record_usage_events(request, usage_events, *, category, user_id, session_id):
        """Persist per-request LLM usage rows (best-effort, never raises).

        Shared chatbot instances make request-scoped attribution impossible on
        the bot, so the api layer owns the attribution context. `usage_events`
        are collected by passing a usage_sink list through the bot's call path.
        """
        store = app.state.admin_store
        if store is None or not usage_events:
            return
        rid = telemetry.get_request_id()
        try:
            for ev in usage_events:
                store.record_usage(
                    kind=ev.get("kind") or "llm",
                    model=ev.get("model"),
                    prompt_tokens=ev.get("prompt_tokens"),
                    completion_tokens=ev.get("completion_tokens"),
                    duration_s=ev.get("duration_s"),
                    category=category,
                    user_id=user_id,
                    session_id=session_id,
                    request_id=rid,
                )
        except Exception:
            logger.exception("usage log write failed")

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

    def dev_identity():
        """The single caller dev mode pretends there is when auth is off."""
        return {
            "id": "dev",
            "email": "dev@localhost",
            "name": "Developer",
            "category": (workspace_names() or ["finance"])[0],
            "dev": True,
        }

    if auth_enabled:

        async def require_user(
            authorization: str | None = Header(default=None),
        ) -> dict:
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing or invalid token")
            payload = verify_jwt(user_store.secret, authorization[len("Bearer ") :])
            if not payload or not payload.get("sub"):
                raise HTTPException(status_code=401, detail="Missing or invalid token")
            # A download capability is not a session. Without this check a link
            # copied out of a chat message would authenticate as its owner for
            # every authenticated endpoint until it expired.
            if is_download_token(payload):
                raise HTTPException(status_code=401, detail="Missing or invalid token")
            return {
                "id": payload["sub"],
                "email": payload.get("email", ""),
                "name": payload.get("name", ""),
                "category": payload.get("category", ""),
                "organisation_id": payload.get("organisation_id"),
                "workspaces": payload.get("workspaces") or [],
            }

    else:

        async def require_user() -> dict:
            return dev_identity()

    user_dep = Depends(require_user)

    def citation_token_for(user):
        """Mint a download capability for `user`, or None when auth is off.

        Without auth there is nothing to scope a link to and nothing to check it
        against, so the plain URL is emitted and documents stay as open as dev
        mode already is.
        """
        if not auth_enabled or not user:
            return None

        def mint(source):
            return mint_download_token(link_secret, user["id"], source)

        return mint

    def refresh_source_links(messages, user):
        """Re-sign the citation links stored with a session.

        Sessions outlive the hour a link is good for, and the URLs are persisted
        verbatim. Re-minting on read is what lets a week-old conversation still
        open its evidence; the source is unchanged, only the capability is new.
        """
        mint = citation_token_for(user)
        if mint is None:
            return messages
        refreshed = []
        for message in messages:
            sources = message.get("sources") if isinstance(message, dict) else None
            if not isinstance(sources, list) or not sources:
                refreshed.append(message)
                continue
            refs = []
            for ref in sources:
                if isinstance(ref, dict) and ref.get("source"):
                    ref = {
                        **ref,
                        "url": document_url(
                            ref["source"], mint(ref["source"]), ref.get("page_start")
                        ),
                    }
                refs.append(ref)
            refreshed.append({**message, "sources": refs})
        return refreshed

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

    def get_org_bot(request: Request, organisation_id: str, category: str):
        """A workspace bot bound to one organisation's own data dir and index.

        Per-organisation scoping (Section 3.4) is only real if the index is
        per-organisation too: two organisations both holding a "finance"
        workspace would otherwise share one vector store, and one could retrieve
        the other's material. Resolved through organisation_workspace_config so
        the bot's data_dir is that organisation's served copy.

        Explicitly injected registries (an app handed ``bot=`` or ``workspaces=``)
        keep winning, the same precedence get_bot() has always used: a caller that
        supplies its own bots is supplying them for every organisation.
        """
        app_ = request.app
        if app_.state.bot is not None or app_.state.workspace_registry:
            return get_bot(request, category)
        if category not in workspace_names():
            raise HTTPException(status_code=403, detail=f"Unknown workspace: {category}")
        key = (organisation_id, category)
        cached = app_.state.org_bots.get(key)
        if cached is None:
            cached = make_workspace_bot(
                category,
                organisation_workspace_config(organisation_id, category),
            )
            app_.state.org_bots[key] = cached
        return cached

    def get_bot(request: Request, category: str, organisation_id: str | None = None):
        if organisation_id:
            return get_org_bot(request, organisation_id, category)
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

    def sync_workspace_state(request: Request):
        """Re-derive app.state.workspace_config after the workspace set changes.

        workspace_config is a startup snapshot of workspace_names(). Creating a
        workspace through the admin API refreshes the module-level registry but
        not that snapshot, so get_bot() could not resolve a workspace created
        seconds earlier: upload wrote the file to disk and then failed the
        following ingest with 403 "Unknown workspace".
        """
        app_ = request.app
        if app_.state.bot is not None or app_.state.workspace_registry:
            # Single-bot or explicitly injected registry: nothing to derive.
            return
        conf = default_workspace_config()
        # Drop cached bots for workspaces that no longer exist, but keep the
        # rest so live vector-store handles survive unrelated changes.
        app_.state.category_bots = {
            cat: b for cat, b in app_.state.category_bots.items() if cat in conf
        }
        app_.state.org_bots = {
            key: b for key, b in app_.state.org_bots.items() if key[1] in conf
        }
        app_.state.workspace_config = conf

    def resolve_session(request: Request, session_id, user_id, store):
        if session_id:
            owner = store.owner_of(session_id)
            if owner != user_id:
                raise HTTPException(status_code=404, detail="Session not found")
        else:
            session_id = store.new_id()
        store.create(session_id, user_id=user_id)
        return session_id

    def workspace_summary(category, is_primary=True):
        return {
            "category": category,
            # Lets the switcher mark the active option without comparing against
            # a separate field on the response.
            "is_primary": is_primary,
            "categories": workspace_names(),
            **workspace_meta(category),
        }

    def workspaces_for(user):
        """Summaries of every workspace a user may reach, primary first.

        A user's primary workspace is `users.category`, which the JWT also
        carries. Tokens minted before the membership table have no `workspaces`
        claim, so they fall back to the single category they always had rather
        than rendering an empty switcher.
        """
        categories = user.get("workspaces") or [user.get("category")]
        primary = user.get("category")
        seen, ordered = set(), []
        for cat in categories:
            if cat and cat not in seen:
                seen.add(cat)
                ordered.append(workspace_summary(cat, is_primary=cat == primary))
        return ordered

    def user_workspaces(user):
        """Same list, restricted to workspaces the user can actually reach.

        For a live row this intersects the membership table with the configured
        workspace registry, so a membership pointing at a deleted or misnamed
        workspace cannot surface a dead switcher option.
        """
        summaries = workspaces_for(user)
        known = set(workspace_names())
        allowed = [
            s for s in summaries
            if s["category"] == user.get("category") or s["category"] in known
        ]
        return allowed or [workspace_summary(user.get("category"))]

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
                "POST /voice/stt",
                "POST /voice/tts",
                "POST /search",
                "POST /documents/link",
                "GET /documents/download",
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
            # Recursive, so this matches the count the delete guard reports and
            # the admin prompt can state it accurately.
            file_count = len([p for p in folder.rglob("*") if p.is_file()]) if folder.is_dir() else 0
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
        sync_workspace_state(request)
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
        category: str,
        request: Request,
        purge: bool = False,
        get_store: AdminStore = admin_store_dep,
    ):
        if category not in workspace_names():
            raise HTTPException(status_code=404, detail="Unknown workspace")
        if category in base_workspace_names():
            raise HTTPException(
                status_code=403,
                detail="Built-in workspaces cannot be deleted; exclude them via the "
                "WORKSPACES env variable instead.",
            )
        folder = Path(root_data_dir()) / category
        documents = sorted(
            str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()
        ) if folder.is_dir() else []
        # Deleting the row alone left data/<category>/ and its vector index
        # behind as an unreferenced orphan. Removing documents is irreversible,
        # so refuse until the caller explicitly confirms with ?purge=true.
        if documents and not purge:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Workspace '{category}' still holds {len(documents)} "
                    f"document(s). Repeat with purge=true to permanently delete "
                    f"the folder and its index: " + ", ".join(documents[:10])
                ),
            )
        removed = []
        # An empty workspace has nothing to lose, so its folder and index are
        # removed without demanding a second confirmation.
        if purge or not documents:
            # Drop shared-document links first so the shared registry does not
            # keep rows pointing at files that no longer exist.
            for row in get_store.all_shared_targets():
                if row["category"] != category:
                    continue
                link = shared_docs.safe_name(row["link_name"] or row["filename"])
                target = shared_docs.target_path(category, link)
                if target.exists() or target.is_symlink():
                    target.unlink()
                get_store.remove_shared_target(row["filename"], category)
            if folder.is_dir():
                shutil.rmtree(folder)
                removed.append(str(folder))
            store_path = Path(index_dir()) / f"vectors_{category}.sqlite3"
            for path in (store_path,
                         store_path.with_suffix(".sqlite3-wal"),
                         store_path.with_suffix(".sqlite3-shm")):
                if path.exists():
                    path.unlink()
                    removed.append(str(path))
        if not get_store.delete_workspace(category):
            raise HTTPException(status_code=404, detail="Workspace not in admin store")
        reload_extra_workspaces(get_store.db_path)
        sync_workspace_state(request)
        # Evict the cached bot so no live sqlite handle survives the workspace.
        app.state.category_bots.pop(category, None)
        log_audit(
            request, "workspace.delete", "workspace", category,
            f"purge={bool(purge)} documents={len(documents)}",
        )
        return {"deleted": category, "purged": removed}

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
        shared_map = _shared_targets_by_path(app.state.admin_store)
        for cat in cats:
            indexed = _indexed_sources(cat)
            folder = Path(root_data_dir()) / cat
            all_files = []
            if folder.is_dir():
                for p in sorted(folder.iterdir()):
                    if not p.is_file():
                        continue
                    entry = {
                        "name": p.name,
                        "size": p.stat().st_size,
                        "indexed": p.name in indexed,
                    }
                    # Only present when the file is a shared-document link, so
                    # private-document listings keep their existing shape and the
                    # console can offer "detach" instead of a refused Delete.
                    shared = shared_map.get((cat, p.name))
                    if shared:
                        entry["shared"] = shared
                    all_files.append(entry)
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

    def _shared_conflict(store, category, name):
        """Explain why writing `name` into `category` would damage a shared doc.

        A shared document is hardlinked into its workspaces, so a plain
        `write_bytes` on the same name would truncate the shared inode and
        silently rewrite the content of every workspace that subscribes to it.
        Private uploads and deletes must refuse instead.
        """
        if store is None:
            return None
        shared_map = _shared_targets_by_path(store)
        filename = shared_map.get((category, name))
        if filename is None:
            return None
        others = [
            c for c in store.shared_target_categories(filename) if c != category
        ]
        where = ", ".join(others) if others else "no other workspace"
        return (
            f"'{name}' in {category} is a shared document served to {where}. "
            f"Edit or detach it from the Shared documents panel instead — "
            f"uploading over it would rewrite the shared copy."
        )

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
        store = app.state.admin_store
        for f in files:
            name = os.path.basename((f.filename or "").replace("\\", "/"))
            if not name:
                continue
            conflict = _shared_conflict(store, category, name)
            if conflict:
                raise HTTPException(status_code=409, detail=conflict)
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
        conflict = _shared_conflict(app.state.admin_store, req.category, name)
        if conflict:
            raise HTTPException(status_code=409, detail=conflict)
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

    # ---- cross-workspace shared documents -----------------------------------

    def _shared_targets_by_path(store):
        """Map (category, link_name) -> shared filename, for collision checks."""
        return {
            (t["category"], t["link_name"]): t["filename"]
            for t in store.all_shared_targets()
        }

    async def _reindex(request, categories, organisation_id=None):
        """Re-index each workspace once; returns per-category ingest stats.

        Pass `organisation_id` after an assignment change so the index that moves
        is the one the customer's users actually query, rather than the
        category-global workspace index.
        """
        results = {}
        for cat in dict.fromkeys(categories):
            try:
                bot = get_bot(request, cat, organisation_id)
            except HTTPException:
                continue
            if bot is None:
                continue
            results[cat] = (await run_in_threadpool(bot.read_and_embed_data)).__dict__
        return results

    def _apply_links(store, filename, categories, link_names, actor, skip_conflict=False):
        """Create the on-disk link plus the registry row for each category.

        `link_names` maps category -> name inside that workspace folder. Any
        category omitted from it keeps the name already recorded in the
        registry, so re-linking a workspace never orphans its previous name.

        Returns (linked, skipped). `skipped` entries carry the reason so the
        admin console can explain a partial success instead of silently
        dropping a workspace.
        """
        canonical = shared_docs.canonical_path(filename)
        linked, skipped = [], []
        for cat in categories:
            existing = store.get_shared_target(filename, cat)
            link_name = (link_names or {}).get(cat) or (existing or {}).get(
                "link_name"
            ) or filename
            try:
                target = shared_docs.target_path(cat, link_name)
            except ValueError as exc:
                skipped.append({"category": cat, "reason": str(exc)})
                continue
            if skip_conflict and shared_docs.conflicting_target(canonical, target):
                skipped.append({
                    "category": cat,
                    "reason": f"'{link_name}' already exists in {cat} and is not "
                              f"this shared document.",
                })
                continue
            # A previously registered name in this workspace is being replaced;
            # drop it so the old file cannot linger and be ingested as though it
            # were a private document.
            if existing and existing["link_name"] != link_name:
                try:
                    stale = shared_docs.target_path(cat, existing["link_name"])
                except ValueError:
                    stale = None
                if stale is not None and (stale.exists() or stale.is_symlink()):
                    if shared_docs.same_file(canonical, stale):
                        stale.unlink()
            try:
                kind = shared_docs.create_link(canonical, target)
            except (OSError, ValueError) as exc:
                skipped.append({"category": cat, "reason": str(exc)})
                continue
            store.add_shared_target(filename, cat, link_name, kind)
            linked.append({"category": cat, "link_name": link_name, "link_kind": kind})
        if linked:
            store.log_event(
                actor, "shared.link", "shared_document", filename,
                ",".join(l["category"] for l in linked),
            )
        return linked, skipped

    @app.get("/admin/shared-documents", dependencies=admin_gate_deps)
    def admin_shared_documents():
        store = app.state.admin_store
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Admin store not configured (set API_KEY to enable admin "
                "console features).",
            )
        docs = store.list_shared_docs()

        def lookup(filename):
            try:
                path = shared_docs.canonical_path(filename)
            except ValueError:
                return None
            return path if path.exists() else None

        for doc in docs:
            doc["present"] = lookup(doc["filename"]) is not None
            doc["size"] = (
                shared_docs.canonical_path(doc["filename"]).stat().st_size
                if doc["present"] else 0
            )
            doc["targets"] = shared_docs.verify_links(doc["targets"], lookup)
            doc["workspace_count"] = len(doc["targets"])
        return {"documents": docs, "workspaces": workspace_names()}

    @app.post(
        "/admin/shared-documents",
        dependencies=admin_gate_deps,
        response_model=dict,
    )
    async def admin_shared_upload(
        request: Request,
        categories: str = Form(default=""),
        files: list[UploadFile] = File(default=[]),
    ):
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        cats = [c.strip() for c in categories.replace(",", " ").split() if c.strip()]
        cats = [c for c in cats if c in workspace_names()]
        if not cats:
            raise HTTPException(
                status_code=422, detail="Select at least one valid workspace."
            )

        shared_docs.shared_root().mkdir(parents=True, exist_ok=True)
        started = time.time()
        saved = []
        for f in files:
            name = shared_docs.safe_name(f.filename)
            if not name:
                continue
            canonical = shared_docs.canonical_path(name)
            previously = store.shared_target_categories(name)
            size = shared_docs.replace_canonical(canonical, await f.read())
            store.add_shared_doc(name, size, actor=actor_label(request))
            # Re-link every workspace that already consumed this document: the
            # content changed under their existing links, so their index must
            # be rebuilt or they will keep serving the previous version.
            _apply_links(store, name, cats + previously, None, actor_label(request))
            saved.append(name)
        if not saved:
            raise HTTPException(status_code=422, detail="No files were uploaded")

        affected = []
        for name in saved:
            for cat in store.shared_target_categories(name):
                if cat not in affected:
                    affected.append(cat)
        stats = await _reindex(request, affected)
        log_audit(request, "shared.upload", "shared_document", ",".join(saved),
                  ",".join(affected))
        return {
            "saved_files": saved,
            "reindexed": stats,
            "duration_seconds": round(time.time() - started, 1),
        }

    @app.post("/admin/shared-documents/link", dependencies=admin_gate_deps)
    async def admin_shared_link(request: Request, req: SharedLinkRequest):
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        filename = shared_docs.safe_name(req.filename)
        if not filename or not shared_docs.canonical_path(filename).exists():
            raise HTTPException(status_code=404, detail="Shared document not found")
        cats = [c for c in dict.fromkeys(req.categories) if c in workspace_names()]
        if not cats:
            raise HTTPException(
                status_code=422, detail="Select at least one valid workspace."
            )
        linked, skipped = _apply_links(
            store, filename, cats, req.link_names, actor_label(request), skip_conflict=True
        )
        stats = await _reindex(request, [l["category"] for l in linked])
        return {"filename": filename, "linked": linked, "skipped": skipped,
                "reindexed": stats}

    @app.post("/admin/shared-documents/unlink", dependencies=admin_gate_deps)
    async def admin_shared_unlink(request: Request, req: SharedUnlinkRequest):
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        filename = shared_docs.safe_name(req.filename)
        row = store.get_shared_target(filename, req.category) if filename else None
        if row is None:
            raise HTTPException(status_code=404, detail="That link does not exist")
        try:
            target = shared_docs.target_path(req.category, row["link_name"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if target.exists() or target.is_symlink():
            target.unlink()
        store.remove_shared_target(filename, req.category)
        # The workspace no longer has the file, so re-index to prune its chunks.
        stats = await _reindex(request, [req.category])
        log_audit(request, "shared.unlink", "shared_document", filename, req.category)
        return {"filename": filename, "detached": req.category, "reindexed": stats}

    @app.post("/admin/shared-documents/delete", dependencies=admin_gate_deps)
    async def admin_shared_delete(request: Request, req: SharedUnlinkRequest):
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        filename = shared_docs.safe_name(req.filename)
        doc = store.get_shared_doc(filename) if filename else None
        if doc is None:
            raise HTTPException(status_code=404, detail="Shared document not found")
        affected = store.shared_target_categories(filename)
        for cat in affected:
            row = store.get_shared_target(filename, cat)
            try:
                target = shared_docs.target_path(cat, row["link_name"])
            except ValueError:
                continue
            if target.exists() or target.is_symlink():
                target.unlink()
        try:
            shared_docs.canonical_path(filename).unlink()
        except FileNotFoundError:
            pass
        store.remove_shared_doc(filename)
        stats = await _reindex(request, affected)
        log_audit(request, "shared.delete", "shared_document", filename,
                  ",".join(affected))
        return {"deleted": filename, "detached_from": affected, "reindexed": stats}

    # ---- master library and per-organisation assignment (Section 3.4) ------

    @app.post("/admin/organisations", dependencies=admin_gate_deps)
    def admin_create_organisation(req: OrganisationCreateRequest, request: Request):
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        try:
            org = user_store.create_organisation(req.id, req.name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        log_audit(request, "org.create", "organisation", org["id"], req.name or "")
        return org

    @app.get("/admin/organisations", dependencies=admin_gate_deps)
    def admin_list_organisations():
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        store = app.state.admin_store
        out = []
        for org in user_store.list_organisations():
            entry = dict(org)
            entry["user_count"] = user_store.count_for_organisation(org["id"])
            if store is not None:
                entry["assignments"] = len(store.list_content_assignments(org["id"]))
            out.append(entry)
        return {"organisations": out}

    @app.post("/admin/organisations/delete", dependencies=admin_gate_deps)
    def admin_delete_organisation(req: OrganisationDeleteRequest, request: Request):
        """Q12: delete an organisation's content immediately, after confirmation.

        Two steps on purpose. `confirm` must be the organisation id, so a
        mis-click cannot destroy a customer's material: the caller has to echo
        back what they are deleting.
        """
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        if req.confirm != req.organisation_id:
            raise HTTPException(
                status_code=400,
                detail="Confirmation does not match the organisation id. "
                       "Nothing was deleted.",
            )
        org = user_store.get_organisation(req.organisation_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Organisation not found")
        if org["system_owned"]:
            raise HTTPException(
                status_code=403,
                detail=f"'{req.organisation_id}' is system-owned and cannot be deleted.",
            )
        removed_files = content_library.purge_organisation(req.organisation_id)
        store = app.state.admin_store
        if store is not None:
            for row in store.list_content_assignments(req.organisation_id):
                store.remove_content_assignment(
                    row["organisation_id"], row["category"], row["filename"]
                )
        members = user_store.count_for_organisation(req.organisation_id)
        user_store.delete_organisation(req.organisation_id)
        log_audit(
            request, "org.delete", "organisation", req.organisation_id,
            f"members={members} files_removed={removed_files}",
        )
        return {
            "deleted": req.organisation_id,
            "members_deleted": members,
            "content_purged": removed_files,
        }

    @app.post("/admin/library/upload", dependencies=admin_gate_deps, response_model=dict)
    async def admin_library_upload(
        request: Request,
        subject: str = Form(default=""),
        jurisdiction: str = Form(default=""),
        effective_date: str = Form(default=""),
        version: str = Form(default=""),
        files: list[UploadFile] = File(default=[]),
    ):
        """Upload to the curated master library. Platform Administrators only.

        Uploading here does not serve anyone. Content reaches an organisation
        only through an explicit assignment, which is the whole of the access
        control model (Section 3.4).
        """
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        root = content_library.library_root()
        root.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in files:
            name = shared_docs.safe_name(f.filename)
            if not name:
                continue
            target = root / name
            payload = await f.read()
            tmp = target.with_name(target.name + ".partial")
            try:
                tmp.write_bytes(payload)
                tmp.replace(target)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
            store.add_library_doc(
                name, size=len(payload), subject=subject or None,
                jurisdiction=jurisdiction or None,
                effective_date=effective_date or None,
                version=version or None, actor=actor_label(request),
            )
            saved.append(name)
        if not saved:
            raise HTTPException(status_code=422, detail="No files were uploaded")
        log_audit(request, "library.upload", "library_document", ",".join(saved),
                  subject or "")
        return {"saved_files": saved}

    @app.get("/admin/library", dependencies=admin_gate_deps)
    def admin_library(subject: str | None = None):
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        docs = store.list_library_docs(subject=subject or None)
        root = content_library.library_root()
        for doc in docs:
            doc["present"] = (root / doc["filename"]).exists()
            doc["assigned_to"] = [
                row["organisation_id"]
                for row in store.list_content_assignments()
                if row["filename"] == doc["filename"]
            ]
        return {"documents": docs, "subjects": sorted(
            {d["subject"] for d in docs if d["subject"]}
        )}

    @app.post("/admin/library/assign", dependencies=admin_gate_deps, response_model=dict)
    async def admin_assign_content(request: Request, req: ContentAssignRequest):
        """Assign library content to one organisation's workspaces.

        Materialises a served copy per workspace and records the act, so the
        audit log can answer which content an organisation was given (D12).
        """
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        if user_store is None:
            raise HTTPException(status_code=503, detail="User accounts are disabled")
        org = user_store.get_organisation(req.organisation_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Organisation not found")
        cats = [c for c in dict.fromkeys(req.categories) if c in workspace_names()]
        if not cats:
            raise HTTPException(
                status_code=422, detail="Select at least one valid workspace."
            )
        root = content_library.library_root()
        assigned, skipped = [], []
        for filename in dict.fromkeys(req.filenames):
            name = shared_docs.safe_name(filename)
            source = root / name if name else None
            if not name or not source.is_file():
                skipped.append({"filename": filename, "reason": "Not in the library."})
                continue
            for cat in cats:
                try:
                    dest = content_library.materialise(
                        req.organisation_id, cat, source, filename=name
                    )
                except ValueError as exc:
                    skipped.append({"filename": name, "category": cat, "reason": str(exc)})
                    continue
                store.add_content_assignment(
                    req.organisation_id, cat, name, actor=actor_label(request)
                )
                assigned.append(
                    {"category": cat, "filename": name, "path": str(dest)}
                )
        if not assigned:
            raise HTTPException(
                status_code=422, detail="Nothing was assigned. " + str(skipped)
            )
        stats = await _reindex(request, cats, req.organisation_id)
        log_audit(
            request, "content.assign", "organisation", req.organisation_id,
            ",".join(sorted({a["filename"] for a in assigned})),
        )
        return {
            "organisation_id": req.organisation_id,
            "assigned": assigned,
            "skipped": skipped,
            "reindexed": stats,
        }

    @app.post("/admin/library/unassign", dependencies=admin_gate_deps, response_model=dict)
    async def admin_unassign_content(request: Request, req: ContentUnassignRequest):
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        cats = [c for c in dict.fromkeys(req.categories) if c in workspace_names()]
        if not cats:
            raise HTTPException(
                status_code=422, detail="Select at least one valid workspace."
            )
        removed = []
        for filename in dict.fromkeys(req.filenames):
            name = shared_docs.safe_name(filename)
            if not name:
                continue
            for cat in cats:
                try:
                    existed = content_library.remove(
                        req.organisation_id, cat, name
                    )
                except ValueError:
                    existed = False
                if store.remove_content_assignment(req.organisation_id, cat, name) or existed:
                    removed.append({"category": cat, "filename": name})
        stats = await _reindex(request, cats, req.organisation_id)
        log_audit(request, "content.unassign", "organisation",
                  req.organisation_id, ",".join(r["filename"] for r in removed))
        return {"organisation_id": req.organisation_id, "removed": removed,
                "reindexed": stats}

    @app.get("/admin/organisations/{organisation_id}/content",
             dependencies=admin_gate_deps)
    def admin_organisation_content(organisation_id: str):
        """What one organisation is currently served, and what it could be given."""
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=503, detail="Admin store not configured.")
        if user_store is not None:
            org = user_store.get_organisation(organisation_id)
            if org is None:
                raise HTTPException(status_code=404, detail="Organisation not found")
        rows = store.list_content_assignments(organisation_id)
        by_category = {}
        for row in rows:
            entry = by_category.setdefault(row["category"], [])
            served = content_library.list_documents(organisation_id, row["category"])
            entry.append({
                "filename": row["filename"],
                "assigned_at": row["assigned_at"],
                "actor": row["actor"],
                # Recorded assignment vs what is actually on disk. A gap means
                # somebody deleted a served copy outside the console, and the
                # assignment would otherwise look intact.
                "present": row["filename"] in served,
            })
        return {
            "organisation_id": organisation_id,
            "assignments": by_category,
            "library": store.list_library_docs(),
            "workspaces": workspace_names(),
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
                req.email, req.password, req.name, req.category,
                organisation_id=req.organisation_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        user["session_count"] = app.state.session_store.count(user_id=user["id"])
        log_audit(
            request, "user.create", "user", user.get("id"),
            f"{req.email} org={user.get('organisation_id')}",
        )
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

    @app.get("/admin/usage", dependencies=admin_gate_deps)
    def admin_usage(range: str = "7d", category: str | None = None,
                    store: AdminStore = admin_store_dep):
        range_days = {"24h": 1, "7d": 7, "30d": 30}.get(range)
        if range != "all" and range_days is None:
            raise HTTPException(status_code=422, detail="range must be 24h, 7d, 30d or all")
        return store.usage_stats(range_days=range_days, category=category or None)

    @app.get("/admin/settings/cost", dependencies=admin_gate_deps)
    def admin_cost_rates(store: AdminStore = admin_store_dep):
        return store.get_rates()

    @app.put("/admin/settings/cost", dependencies=admin_gate_deps)
    def admin_set_cost_rates(req: CostRatesRequest, request: Request,
                             store: AdminStore = admin_store_dep):
        rates = store.set_rates(req.price_input_per_m, req.price_output_per_m)
        log_audit(
            request, "cost.update", "settings", None,
            f"input=${req.price_input_per_m:.6f}/1M output=${req.price_output_per_m:.6f}/1M",
        )
        return rates

    @app.get("/admin/usage/export.csv", dependencies=admin_gate_deps)
    def admin_usage_csv(range: str = "7d", category: str | None = None,
                        store: AdminStore = admin_store_dep):
        range_days = {"24h": 1, "7d": 7, "30d": 30}.get(range)
        if range != "all" and range_days is None:
            raise HTTPException(status_code=422, detail="range must be 24h, 7d, 30d or all")
        rows = store.usage_export_rows(range_days=range_days, category=category or None)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["at", "kind", "model", "prompt_tokens", "completion_tokens",
             "duration_s", "units", "category", "user_id", "session_id", "request_id"]
        )
        for r in rows:
            writer.writerow(
                [
                    r["at"], r["kind"], r["model"] or "", r["prompt_tokens"],
                    r["completion_tokens"], r["duration_s"] or "", r["units"] or "",
                    r["category"] or "", r["user_id"] or "", r["session_id"] or "",
                    r["request_id"] or "",
                ]
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="usage.csv"',
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
        return AuthResponse(
            token=token,
            user=user,
            workspace=workspace_summary(user["category"]),
            workspaces=user_workspaces(user),
        )

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
        return AuthResponse(
            token=token,
            user=user,
            workspace=workspace_summary(user["category"]),
            workspaces=user_workspaces(user),
        )

    @app.get("/me", response_model=MeResponse)
    def me(user: dict = user_dep):
        profile = user
        reachable = None
        if user_store is not None:
            # Re-read from the store rather than trusting the token, so a
            # membership granted after the token was issued takes effect without
            # waiting for the token to expire. This also means a revoked
            # workspace disappears from the switcher on the next /me.
            full = user_store.get(user["id"])
            if full:
                profile = full
                reachable = user_store.workspace_categories(user["id"])
        if not reachable:
            # No user store, or a user with no membership rows: the token's
            # claim list, else the single category.
            reachable = user.get("workspaces") or [profile["category"]]
        # The store row carries no memberships, so the reachable list is spliced
        # in before validation. Returning the bare row here is what made every
        # /me report an empty workspace list.
        scoped = {**profile, "workspaces": reachable}
        return MeResponse(
            user=scoped,
            workspace=workspace_summary(profile["category"]),
            workspaces=user_workspaces(scoped),
        )

    @app.post("/chat", response_model=ChatResponse, dependencies=rate_deps)
    async def chat(
        req: ChatRequest,
        request: Request,
        user: dict = user_dep,
        store=store_dep,
    ):
        session_id = resolve_session(request, req.session_id, user["id"], store)
        history = store.history(session_id, limit=req.history_limit)
        bot = get_bot(request, user["category"], user.get("organisation_id"))
        tz = tz_offset_minutes(request)
        reply = smalltalk.handle(req.question, user["name"], user["category"], tz)
        sources = []
        if reply is None:
            usage_events = []
            try:
                reply = await run_in_threadpool(
                    bot.ask,
                    question=req.question,
                    k=req.k,
                    source=req.source,
                    history=history,
                    usage_sink=usage_events,
                )
            except RuntimeError as e:
                raise HTTPException(status_code=503, detail=str(e))
            record_usage_events(
                request, usage_events,
                category=user["category"], user_id=user["id"], session_id=session_id,
            )
            sources = citation_refs(
                getattr(bot, "last_results", []), citation_token_for(user)
            )
        store.append(session_id, "user", req.question)
        store.append(
            session_id, "assistant", reply,
            sources=[ref.model_dump() for ref in sources],
        )
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
        bot = get_bot(request, user["category"], user.get("organisation_id"))
        tz = tz_offset_minutes(request)
        reply = smalltalk.handle(req.question, user["name"], user["category"], tz)

        def event_stream():
            chunks = []
            usage_events = []
            sources = []
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
                        usage_sink=usage_events,
                    ):
                        chunks.append(piece)
                        yield f"data: {json.dumps({'delta': piece})}\n\n"
                    # Resolved before the persistence step below so the stored
                    # assistant turn keeps the citations it was answered with.
                    sources = citation_refs(
                        getattr(bot, "last_results", []), citation_token_for(user)
                    )
            except RuntimeError as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            finally:
                record_usage_events(
                    request, usage_events,
                    category=user["category"], user_id=user["id"], session_id=session_id,
                )
                store.append(session_id, "user", req.question)
                store.append(
                    session_id,
                    "assistant",
                    "".join(chunks),
                    sources=[ref.model_dump() for ref in sources],
                )
            yield f"data: {json.dumps({'sources': [s.model_dump() for s in sources]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"X-Session-ID": session_id},
        )

    def record_voice_usage(*, kind, model, duration_s=None, units, user):
        """Meter one voice call into the usage ledger (best-effort)."""
        store = app.state.admin_store
        if store is None:
            return
        store.record_usage(
            kind=kind,
            model=model,
            duration_s=duration_s,
            units=units,
            category=user["category"],
            user_id=user["id"],
            request_id=telemetry.get_request_id(),
        )

    @app.post("/voice/stt", dependencies=rate_deps)
    async def voice_stt(
        request: Request,
        file: UploadFile = File(...),
        user: dict = user_dep,
    ):
        """Multipart audio in (webm/opus, mp4/aac, wav, ogg, mpeg) -> transcript.

        Gated and rate-limited exactly like /chat. The transcript is returned
        for the caller to review and send through the normal chat pipeline —
        voice is an adapter, not a pipeline of its own. Audio is metered as
        kind "stt" in units of seconds.
        """
        max_mb = int_env("VOICE_MAX_UPLOAD_MB", 5)
        max_bytes = max_mb * 1024 * 1024
        data = await file.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Audio exceeds the {max_mb} MB upload limit",
            )
        if not data:
            raise HTTPException(status_code=422, detail="Empty audio upload")
        # Some mobile browsers send an empty/generic content type for blob
        # uploads, so a missing type is accepted only when the filename
        # extension is on the allowlist.
        if file.content_type and not voice.mime_allowed(file.content_type):
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported audio content type: {file.content_type}",
            )
        if not file.content_type and not voice.filename_allowed(file.filename):
            raise HTTPException(
                status_code=422,
                detail="Unsupported audio file type (no content type given)",
            )
        try:
            provider = voice.get_stt_provider()
            text, duration = await run_in_threadpool(
                provider.transcribe, data, file.filename or "audio"
            )
        except voice.UndecodableAudio as e:
            # Allowed extension, unreadable bytes: the recording itself is bad.
            raise HTTPException(status_code=422, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        record_voice_usage(
            kind="stt", model=voice.stt_model_name(),
            duration_s=duration, units=duration, user=user,
        )
        return {"text": text}

    @app.post("/voice/tts", dependencies=rate_deps)
    async def voice_tts(req: TTSRequest, user: dict = user_dep):
        """Text in -> audio bytes out. Metered as kind "tts" in characters.

        The media type comes from the provider: local Kokoro returns WAV,
        the Azure OpenAI speech endpoint returns mp3. Both play in every
        browser, so the client just takes whatever arrives.
        """
        max_chars = int_env("VOICE_TTS_MAX_CHARS", 4000)
        if len(req.text) > max_chars:
            raise HTTPException(
                status_code=422,
                detail=f"Text exceeds the {max_chars}-character limit",
            )
        try:
            provider = voice.get_tts_provider()
            audio = await run_in_threadpool(provider.synthesize, req.text)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        record_voice_usage(
            kind="tts", model=voice.tts_model_name(),
            units=len(req.text), user=user,
        )
        return Response(
            content=audio,
            media_type=getattr(provider, "media_type", "audio/mpeg"),
        )

    @app.post("/search", response_model=SearchResponse, dependencies=rate_deps)
    async def search(
        req: SearchRequest,
        request: Request,
        user: dict = user_dep,
    ):
        bot = get_bot(request, user["category"], user.get("organisation_id"))
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
                page_start=r.page_start,
                page_end=r.page_end,
            )
            for r in results
        ]
        return SearchResponse(question=req.question, results=items)

    @app.post(DOCUMENT_LINK_PATH, response_model=DocumentLinkResponse, dependencies=rate_deps)
    def document_link(req: DocumentLinkRequest, user: dict = user_dep):
        """Mint a fresh download link for a document the caller may already open.

        Links in a chat response are signed when the answer is produced, which is
        right for the common case but goes stale in a long-lived session. This
        re-issues one without reloading the conversation.
        """
        try:
            resolve_document(
                user["category"], req.source, user.get("organisation_id")
            )
        except DocumentNotFound:
            raise HTTPException(status_code=404, detail="Document not found")
        mint = citation_token_for(user)
        token = mint(req.source) if mint else None
        return DocumentLinkResponse(
            source=req.source,
            url=document_url(req.source, token, req.page_start),
            expires_in=download_ttl_seconds(),
        )

    @app.get(DOCUMENT_DOWNLOAD_PATH, dependencies=rate_deps)
    def download_document(
        request: Request,
        source: str | None = Query(default=None, max_length=512),
        token: str | None = Query(default=None, max_length=4096),
        authorization: str | None = Header(default=None),
    ):
        """Serve a cited source document to a user entitled to their workspace.

        Two ways in, because the two ways a citation gets opened are different:

        * `?source=` plus the caller's bearer token, which is what the app's own
          fetch calls send.
        * `?source=` plus a signed download token, which is what a native browser
          navigation sends, since navigating never attaches an Authorization
          header. That is what makes cmd-click and "open in new tab" work.

        With a download token the source comes from the token, never from the
        query string, so a link cannot be edited to name a different document in
        the same workspace. The workspace comes from the live user record, so
        deleting or moving a user immediately invalidates links already in their
        browser history.

        The `source` value is re-derived server-side by documents.resolve_document,
        which confines the result to the caller's workspace or the shared store.
        Every failure mode after authentication is a 404, so the response never
        reveals whether a document outside the caller's scope exists.

        Delivery follows download_links.delivery_for: PDF, text and raster images
        render inline so the `#page=` fragment lands on the cited page, while
        anything the browser might treat as a document the caller wrote (HTML,
        SVG, XML, office formats) is served as an opaque attachment. Inline HTML on
        this origin would be stored XSS against the very session that serves it.
        """
        via_token = False
        if not auth_enabled:
            identity = dev_identity()
            effective = source
        else:
            # Both credential families resolve to a (user id, document) pair; the
            # download token carries the document with it, the session token does
            # not and trusts the query string because it is scoped to the user.
            capability = verify_download_token(link_secret, token) if token else None
            via_token = capability is not None
            if via_token:
                user_id, token_source = capability["user_id"], capability["source"]
                if source and source != token_source:
                    raise HTTPException(status_code=404, detail="Document not found")
                effective = token_source
            else:
                payload = None
                if authorization and authorization.startswith("Bearer "):
                    payload = verify_jwt(
                        user_store.secret, authorization[len("Bearer ") :]
                    )
                    if payload and is_download_token(payload):
                        payload = None
                if not payload or not payload.get("sub"):
                    raise HTTPException(status_code=401, detail="Missing or invalid token")
                user_id, effective = payload["sub"], source
            identity = user_store.get(user_id)
            if identity is None:
                # Correctly signed, but the account behind it is gone.
                raise HTTPException(status_code=401, detail="Missing or invalid token")
        if not effective:
            raise HTTPException(status_code=404, detail="Document not found")
        try:
            # The organisation comes from the live user record, not the token, so
            # moving a user between organisations immediately invalidates links
            # already in their browser history.
            path = resolve_document(
                identity["category"], effective, identity.get("organisation_id")
            )
        except DocumentNotFound:
            raise HTTPException(status_code=404, detail="Document not found")
        log_audit(
            request,
            "document.download",
            "document",
            f"{identity['category']}/{effective}",
            # Which credential opened the file is the first question asked of this
            # row, so both paths are labelled. A capability URL that turns up in a
            # shared document is a different incident from an in-app fetch.
            detail=f"user={identity['id']} via={'token' if via_token else 'bearer'}",
            actor=f"user:{identity.get('email') or identity['id']}",
        )
        media_type, disposition, filename = delivery_for(path)
        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "Content-Disposition": content_disposition(disposition, filename),
                "X-Content-Type-Options": "nosniff",
                # A citation URL carries a capability. If a reader clicks a link
                # inside the served PDF, this is what stops the token from
                # travelling to that third-party site as a Referer.
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "private, no-store",
            },
        )

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
        return SessionMessages(
            session_id=session_id,
            messages=refresh_source_links(store.messages(session_id), user),
        )

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
    # TLS is terminated by a reverse proxy in every real deployment (Azure App
    # Service front end in prod), so without proxy headers request.client.host is
    # the proxy for every caller and per-IP rate limiting silently degrades into
    # one global bucket. FORWARDED_ALLOW_IPS names the peers whose headers we
    # trust; on App Service only the platform front end can reach the container,
    # hence "*" in deploy.yml.
    forwarded = os.environ.get("FORWARDED_ALLOW_IPS", "")
    uvicorn.run(
        "chatbot.api:app",
        host=host,
        port=port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips=forwarded or "127.0.0.1",
    )