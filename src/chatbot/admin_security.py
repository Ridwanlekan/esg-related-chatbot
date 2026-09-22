"""Optional admin-console authentication layers.

These are strict *additions* on top of the API-key gate and are only active
when the matching environment variables are set (see `doc/env_example.txt`):

- ``ADMIN_BASIC_USER`` + ``ADMIN_BASIC_PASS``  -> HTTP Basic auth
- ``ADMIN_TOTP_SECRET``                        -> RFC 6238 TOTP (30s, 6 digits)

Both are implemented with the standard library only.
"""

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time

from fastapi import HTTPException


def _b32decode(secret):
    s = "".join((secret or "").upper().split())
    s += "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s)


def totp_code(secret_b32, at=None, step=30, digits=6):
    """Compute an RFC 6238 TOTP code for ``secret_b32`` at time ``at``."""
    if at is None:
        at = int(time.time())
    counter = int(at // step)
    key = _b32decode(secret_b32)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def verify_totp(secret_b32, code, window=1):
    """Return True if ``code`` matches within ``window`` steps either side of now."""
    if not code or not secret_b32:
        return False
    code = (code or "").strip()
    if not code.isdigit() or not secret_b32.strip():
        return False
    try:
        key = _b32decode(secret_b32)
    except (ValueError, TypeError):
        return False
    now = int(time.time())
    for w in range(-window, window + 1):
        counter = (now + w * 30) // 30
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        if str(binary % 1000000).zfill(6) == code:
            return True
    return False


def make_basic_auth_check(username, password):
    """Return a checker callable, or None when the layer is not configured.

    The callable takes a Request and returns True when the HTTP Basic header
    matches the expected credentials, else raises 401.
    """
    expected_user = username or ""
    expected_pass = password or ""
    if not expected_user or not expected_pass:
        return None

    # Constant-time comparison that tolerates length mismatches.
    def const_eq(a, b):
        return secrets.compare_digest(
            hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest()
        )

    def check(request):
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("basic "):
            raise HTTPException(
                status_code=401,
                detail="Admin Basic credentials required (ADMIN_BASIC_USER/PASS)",
            )
        try:
            decoded = base64.b64decode(authorization[6:]).decode("utf-8", "strict")
            user, sep, password = decoded.partition(":")
            if not sep:
                raise ValueError("malformed")
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="Invalid Basic credentials")
        if not const_eq(user, expected_user) or not const_eq(password, expected_pass):
            raise HTTPException(status_code=401, detail="Invalid Basic credentials")
        return True

    return check


def make_totp_dep(secret_b32):
    """Return a TOTP checker callable, or None when not configured.

    The callable takes a Request and verifies the X-Admin-TOTP header, raising
    401 when missing, stale, or incorrect.
    """
    if not (secret_b32 or "").strip():
        return None

    def check(request):
        code = request.headers.get("x-admin-totp", "")
        if not verify_totp(secret_b32, code):
            raise HTTPException(
                status_code=401,
                detail="Authenticator code required/expired - re-enter your "
                "authenticator code for this admin console",
            )
        return True

    return check


def admin_gate_config():
    """Public, secret-free knobs the admin UI needs to render its auth card."""
    return {
        "basic": bool(os.environ.get("ADMIN_BASIC_USER") and os.environ.get("ADMIN_BASIC_PASS")),
        "totp": bool(os.environ.get("ADMIN_TOTP_SECRET")),
    }