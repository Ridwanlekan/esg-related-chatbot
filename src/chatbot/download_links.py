"""Short-lived capability tokens for the citation links the UI hands out.

A citation is only verifiable if the reader can actually open the document, and
the obvious way to be handed a link is a browser navigation: cmd-click, middle
click, "open in new tab", "copy link address". None of those send an
`Authorization` header, so a link gated on the caller's session token can only
ever be followed by JavaScript running inside our own page. That is why the
first version of this feature intercepted every click, and why it broke the
browser affordances people actually use.

The fix is a second, much weaker credential in the query string: a token signed
with the same AUTH_SECRET that resolves to exactly one document for exactly one
user and stops being valid after an hour. It is deliberately not the session
token. If a citation link leaks through browser history, a clipboard, a shared
chat message or the Referer of a link inside a served PDF, the damage is bounded
to "someone can read this one ESG document until the token expires" instead of
"someone has the user's session". Every such link is also re-minted whenever the
owning session is reloaded, and `AUTH_SECRET` rotation revokes all of them.

Two rules keep the two token families from being interchangeable. A download
token carries `purpose="document_download"` and is rejected anywhere a session
token is expected; a session token carries `purpose="session"` and is rejected by
the download endpoint. Without that symmetry a download token would be a session
token with a smaller TTL, which is precisely what this module exists to avoid.

This module also decides *how* a document is delivered. Serving everything as an
octet-stream attachment is the safe default, but an attachment has no page, so a
`#page=7` citation fragment would silently do nothing. PDF, plain text and raster
images are therefore served inline so the browser's viewer lands on the cited
page, while anything the browser might execute or embed as a document (HTML, SVG,
XML, office formats) keeps the attachment disposition. See `delivery_for`.
"""

import os
import urllib.parse

from chatbot.docling_loader import extension_of
from chatbot.users import sign_jwt, verify_jwt

# Long enough to survive a reading session, short enough that a leaked link is a
# nuisance rather than an incident.
DEFAULT_DOWNLOAD_TTL_SECONDS = 3600
MIN_DOWNLOAD_TTL_SECONDS = 60
MAX_DOWNLOAD_TTL_SECONDS = 86400

# The session counterpart lives in chatbot.users, next to the tokens it names.
DOWNLOAD_PURPOSE = "document_download"

TOKEN_QUERY_PARAM = "token"

# Types the browser will render without ever giving the file script access to
# this origin. SVG is deliberately absent: an inline SVG is a document, and a
# document can script.
INLINE_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "log": "text/plain; charset=utf-8",
    "md": "text/plain; charset=utf-8",
    "markdown": "text/plain; charset=utf-8",
    "csv": "text/plain; charset=utf-8",
    "vtt": "text/plain; charset=utf-8",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}

OCTET_STREAM = "application/octet-stream"


def download_ttl_seconds():
    raw = os.environ.get("DOWNLOAD_TOKEN_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_DOWNLOAD_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DOWNLOAD_TTL_SECONDS
    return max(MIN_DOWNLOAD_TTL_SECONDS, min(MAX_DOWNLOAD_TTL_SECONDS, value))


def mint_download_token(secret, user_id, source, ttl_seconds=None):
    """Sign a capability for one user to open one document.

    The category is deliberately absent. Entitlement is re-derived from the live
    user row at redemption time, so moving a user between workspaces takes effect
    immediately and a link minted before the move cannot outlive it.
    """
    if not user_id or not source:
        return None
    ttl = ttl_seconds if ttl_seconds is not None else download_ttl_seconds()
    return sign_jwt(
        secret,
        {
            "sub": user_id,
            "purpose": DOWNLOAD_PURPOSE,
            "src": source,
        },
        ttl_seconds=ttl,
    )


def verify_download_token(secret, token):
    """Return {user_id, source} for a valid download token, else None.

    Any failure — bad signature, expired, wrong purpose, a session token
    presented as a download token — collapses to None so the caller cannot leak
    which of those it was through timing or message differences.
    """
    if not token or not secret:
        return None
    payload = verify_jwt(secret, token)
    if not payload or payload.get("purpose") != DOWNLOAD_PURPOSE:
        return None
    user_id = payload.get("sub")
    source = payload.get("src")
    if not user_id or not source:
        return None
    return {"user_id": user_id, "source": source}


def is_download_token(payload):
    """True when an already-verified payload is a download capability.

    Used by the session authenticator so a download token cannot be presented as
    a bearer token and inherit the user's whole session.
    """
    return bool(payload) and payload.get("purpose") == DOWNLOAD_PURPOSE


def delivery_for(path):
    """Return (media_type, disposition, filename) for a served document.

    Safe types render inline so a `#page=` fragment means something; everything
    else is an opaque attachment, which is what stops a corporate HTML or SVG
    file from becoming stored XSS on this origin.
    """
    filename = os.path.basename(str(path)) or "document"
    media_type = INLINE_MEDIA_TYPES.get(extension_of(path))
    if media_type is None:
        return OCTET_STREAM, "attachment", filename
    return media_type, "inline", filename


def content_disposition(disposition, filename):
    """Build a Content-Disposition header safe for non-ASCII filenames.

    The quoted `filename` keeps old clients working; `filename*` is RFC 5987 and
    is what browsers actually read for names like "FY24 – Impact Report.pdf".
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    quoted = urllib.parse.quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def page_fragment(page_start):
    """Build the `#page=N` fragment that opens a PDF at the cited page.

    Only the first page of a range is used. Chrome and Firefox both read a bare
    page number here; the `page=first-last` open-parameter form is honoured
    inconsistently, and a fragment a viewer silently ignores is worse than one it
    always understands. The full range is shown as text on the citation chip.
    """
    start = _as_page(page_start)
    return f"#page={start}" if start is not None else ""


def _as_page(value):
    """Coerce a stored page number to a positive int, or None if it has none."""
    if value is None or isinstance(value, bool):
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None
