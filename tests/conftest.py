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
    "TRUSTED_PROXY_IPS",
    "FORWARDED_ALLOW_IPS",
    # D18 knobs: a developer's real SMTP relay or cap must not change test
    # behaviour, and EMAIL_VERIFICATION_REQUIRED is scrubbed so the gate is off
    # unless a test asks for it (accounts are otherwise unverified at signup).
    "MAX_FREE_ACCOUNTS",
    "EMAIL_VERIFICATION_REQUIRED",
    "VERIFICATION_TOKEN_TTL_HOURS",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    # Billing (D3): without scrubbing, a developer's real Stripe keys could make
    # tests sign webhooks they never set up, or silently flip into "configured".
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRICE_TEAM_MONTHLY",
    "STRIPE_PRICE_TEAM_ANNUAL",
    "STRIPE_PRICE_BUSINESS_MONTHLY",
    "STRIPE_PRICE_BUSINESS_ANNUAL",
    "STRIPE_PRICE_ENTERPRISE_ANNUAL",
    "STRIPE_PRICE_EXTRA_SEAT_TEAM",
    "STRIPE_PRICE_EXTRA_SEAT_BUSINESS",
    "STRIPE_PRICE_QUESTION_OVERAGE",
    "STRIPE_METER_EXTRA_SEATS",
    "STRIPE_METER_QUESTIONS",
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


@pytest.fixture
def verified_signup():
    """Sign up and confirm the address, returning the auth payload.

    Signup now leaves a free account unconfirmed (D18), which blocks the
    content endpoints. Suites that use the public signup path only to obtain a
    working token should use this rather than reaching past the gate, so each
    one keeps testing what it means to test.
    """
    def _signup(client, email="u@corp.com", category="finance", name="User",
                password="password123", invite=None, **extra):
        payload = {
            "email": email,
            "password": password,
            "name": name,
            "category": category,
            **extra,
        }
        if invite:
            payload["invite"] = invite
        res = client.post("/auth/signup", json=payload)
        assert res.status_code == 200, res.text
        user_id = res.json()["user"]["id"]
        client.app.state.user_store.set_verified(user_id)
        return res.json()

    return _signup