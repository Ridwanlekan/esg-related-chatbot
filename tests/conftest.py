import os

import pytest

# Config variables that chatbot reads from the environment. create_app() now
# loads .env on import, so scrub these so a developer's real .env cannot leak
# into tests (e.g. a real API_KEY making /ingest auth-gated).
_SCRUB = {
    "API_KEY",
    "AUTH_SECRET",
    "CORS_ORIGINS",
    "RATE_LIMIT_REQUESTS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "WORKSPACES",
    "DATA_DIR",
    "INDEX_DIR",
    "TOKEN_TTL_SECONDS",
    "LOG_LEVEL",
    "ADMIN_BASIC_USER",
    "ADMIN_BASIC_PASS",
    "ADMIN_TOTP_SECRET",
}


@pytest.fixture(autouse=True)
def _scrub_env_vars():
    saved = {k: os.environ.pop(k, None) for k in _SCRUB}
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _reset_extra_workspaces():
    import chatbot.workspaces as workspaces

    workspaces.clear_extra_workspaces()
    yield
    workspaces.clear_extra_workspaces()