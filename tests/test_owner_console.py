"""The organisation console and the free-tier question cap.

Two features that share a home: the owner's dashboard reads organisation-scoped
analytics, and the free tier gets a per-account ceiling before it has to
upgrade. Both hang off the same organisation model, so the access rules are
pinned here together - a manager sees their own organisation and nothing else,
and only the ownerless sample workspace is capped.
"""

import pytest
from fastapi.testclient import TestClient

from chatbot.admin_store import AdminStore
from chatbot.api import create_app
from chatbot.session_store import SessionStore
from chatbot.users import (
    DEFAULT_FREE_TIER_QUESTION_CAP,
    SAMPLE_ORGANISATION_ID,
    UserStore,
    max_free_questions,
)

KEY = "secret-admin-key"


class FakeBot:
    """A model-free bot so no test reaches OpenAI."""

    last_results = []

    def __init__(self):
        self.store = type("S", (), {"count": staticmethod(lambda: 0)})()

    def ask(self, question, k=3, source=None, history=None, usage_sink=None, language=None):
        return f"answer to: {question}"

    def ask_stream(self, question, k=3, source=None, history=None, usage_sink=None, language=None):
        yield "answer"

    def read_and_embed_data(self):
        return type("R", (), {
            "documents_seen": 0, "documents_reindexed": 0,
            "chunks_upserted": 0, "stale_chunks_removed": 0,
        })()


def make_app(tmp_path):
    return create_app(
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="s"),
        admin_store=AdminStore(str(tmp_path / "a.sqlite3")),
        bot=FakeBot(),
        api_key=KEY,
        rate_limit=0,
    )


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def client(app):
    return TestClient(app)


def add_manager(app, org_id="acme", *, role="owner", email=None):
    """Create a customer organisation with one manager and return (user, token)."""
    store = app.state.user_store
    store.create_organisation(org_id, org_id.title())
    email = email or f"{role}@{org_id}.com"
    user = store.create_user(
        email, "password123", role.title(), "finance",
        organisation_id=org_id, verified=True,
    )
    if role == "owner":
        store.ensure_initial_owner(org_id, user["id"])
    else:
        store.set_org_role(org_id, user["id"], "admin")
    return user, store.token_for(user)


def auth(token):
    return {"authorization": "Bearer " + token}


# ---- free-tier question cap ----------------------------------------------


class TestFreeTierCapConfig:
    def test_default_is_used_when_unset(self, monkeypatch):
        monkeypatch.delenv("FREE_TIER_QUESTION_CAP", raising=False)
        assert max_free_questions() == DEFAULT_FREE_TIER_QUESTION_CAP

    def test_env_overrides_the_default(self, monkeypatch):
        monkeypatch.setenv("FREE_TIER_QUESTION_CAP", "3")
        assert max_free_questions() == 3

    def test_zero_and_negative_mean_no_limit(self, monkeypatch):
        for raw in ("0", "-5"):
            monkeypatch.setenv("FREE_TIER_QUESTION_CAP", raw)
            assert max_free_questions() == 0

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("FREE_TIER_QUESTION_CAP", "lots")
        assert max_free_questions() == DEFAULT_FREE_TIER_QUESTION_CAP


class TestFreeTierCapEnforcement:
    def _free_user(self, client):
        res = client.post("/auth/signup", json={
            "email": "free@corp.com", "password": "password123",
            "name": "Free", "category": "finance",
        })
        assert res.status_code == 200, res.text
        payload = res.json()
        client.app.state.user_store.set_verified(payload["user"]["id"])
        return payload

    def test_me_reports_free_tier_runway(self, client, monkeypatch):
        monkeypatch.setenv("FREE_TIER_QUESTION_CAP", "10")
        payload = self._free_user(client)
        me = client.get("/me", headers=auth(payload["token"])).json()
        assert me["free_tier"]["cap"] == 10
        assert me["free_tier"]["used"] == 0
        assert me["free_tier"]["remaining"] == 10
        assert me["free_tier"]["reached"] is False

    def test_question_is_blocked_once_the_cap_is_reached(self, client, app, monkeypatch):
        monkeypatch.setenv("FREE_TIER_QUESTION_CAP", "2")
        payload = self._free_user(client)
        uid = payload["user"]["id"]
        admin = app.state.admin_store
        for _ in range(2):
            admin.record_usage(kind="question", user_id=uid,
                               organisation_id=SAMPLE_ORGANISATION_ID)

        res = client.post(
            "/chat",
            json={"question": "What does our risk policy say about flooding?"},
            headers=auth(payload["token"]),
        )
        assert res.status_code == 402
        assert "free plan" in res.json()["detail"].lower()

    def test_greetings_do_not_count_or_get_blocked(self, client, monkeypatch):
        monkeypatch.setenv("FREE_TIER_QUESTION_CAP", "1")
        payload = self._free_user(client)
        # A greeting never reaches the model, so it is neither capped nor
        # counted: it must still answer even at the limit.
        res = client.post("/chat", json={"question": "hello"},
                          headers=auth(payload["token"]))
        assert res.status_code == 200
        assert res.json()["answer"]

    def test_a_paid_org_is_not_capped(self, client, app, monkeypatch):
        monkeypatch.setenv("FREE_TIER_QUESTION_CAP", "1")
        add_manager(app, "acme", role="owner")
        member = app.state.user_store.create_user(
            "seat@acme.com", "password123", "Seat", "finance",
            organisation_id="acme", verified=True,
        )
        token = app.state.user_store.token_for(member)
        # A seat in a customer organisation is not on the free tier, so the cap
        # does not apply and the request proceeds past the quota gate.
        res = client.post(
            "/chat",
            json={"question": "What does our risk policy say about flooding?"},
            headers=auth(token),
        )
        assert res.status_code != 402


# ---- organisation console ------------------------------------------------


class TestOrgConsoleAccess:
    def test_owner_sees_their_own_overview(self, client, app):
        user, token = add_manager(app, "acme", role="owner")
        res = client.get("/org/overview", headers=auth(token))
        assert res.status_code == 200
        body = res.json()
        assert body["organisation"]["id"] == "acme"
        assert body["your_role"] == "owner"
        assert "totals" in body and "workspaces" in body and "billing" in body

    def test_admin_can_reach_the_analytics(self, client, app):
        user, token = add_manager(app, "acme", role="admin")
        for path in ("/org/overview", "/org/usage", "/org/content",
                     "/org/invites", "/org/plans"):
            res = client.get(path, headers=auth(token))
            assert res.status_code == 200, path

    def test_a_member_is_refused_the_console(self, client, app):
        # The earliest account becomes the owner on the startup backfill, so the
        # member must be someone added after an owner already exists.
        add_manager(app, "acme", role="owner")
        member = app.state.user_store.create_user(
            "member@acme.com", "password123", "Member", "finance",
            organisation_id="acme", verified=True,
        )
        token = app.state.user_store.token_for(member)
        assert client.get("/org/overview", headers=auth(token)).status_code == 403

    def test_a_free_tier_account_is_refused(self, client):
        res = client.post("/auth/signup", json={
            "email": "free@corp.com", "password": "password123",
            "name": "Free", "category": "finance",
        })
        token = res.json()["token"]
        assert client.get("/org/overview", headers=auth(token)).status_code == 403

    def test_the_console_is_scoped_to_the_callers_org(self, client, app):
        # Two organisations; each owner only ever sees their own.
        _, token_a = add_manager(app, "acme", role="owner")
        _, token_b = add_manager(app, "beta", role="owner", email="owner@beta.com")
        a = client.get("/org/overview", headers=auth(token_a)).json()
        b = client.get("/org/overview", headers=auth(token_b)).json()
        assert a["organisation"]["id"] == "acme"
        assert b["organisation"]["id"] == "beta"


class TestOrgInvites:
    def test_manager_can_mint_and_list_an_invite(self, client, app):
        user, token = add_manager(app, "acme", role="owner")
        res = client.post(
            "/org/invites",
            json={"category": "finance", "max_uses": 2, "ttl_hours": 24},
            headers=auth(token),
        )
        assert res.status_code == 200, res.text
        assert res.json()["token"]

        listed = client.get("/org/invites", headers=auth(token)).json()
        assert listed["organisation_id"] == "acme"
        assert all(i["organisation_id"] == "acme" for i in listed["invites"])

    def test_an_unknown_workspace_is_refused(self, client, app):
        user, token = add_manager(app, "acme", role="owner")
        res = client.post(
            "/org/invites",
            json={"category": "does-not-exist"},
            headers=auth(token),
        )
        assert res.status_code == 422


class TestPlanCatalog:
    def test_plans_are_listed_and_not_purchasable_without_prices(self, client, app):
        user, token = add_manager(app, "acme", role="owner")
        plans = client.get("/org/plans", headers=auth(token)).json()["plans"]
        assert {p["plan"] for p in plans} == {"team", "business", "enterprise"}
        assert all(p["purchasable"] is False for p in plans)

    def test_a_configured_price_makes_a_plan_purchasable(self, client, app, monkeypatch):
        user, token = add_manager(app, "acme", role="owner")
        monkeypatch.setenv("STRIPE_PRICE_TEAM_MONTHLY", "price_team_m")
        plans = client.get("/org/plans", headers=auth(token)).json()["plans"]
        team = next(p for p in plans if p["plan"] == "team")
        assert team["purchasable"] is True
        assert "month" in team["intervals"]

    def test_each_plan_carries_its_token_cap(self, client, app):
        user, token = add_manager(app, "acme", role="owner")
        plans = client.get("/org/plans", headers=auth(token)).json()["plans"]
        caps = {p["plan"]: p["token_cap"] for p in plans}
        assert caps == {"team": 5_000_000, "business": 20_000_000, "enterprise": None}


# ---- per-plan token ceiling ----------------------------------------------


class TestTokenCap:
    def test_cap_table_matches_the_agreed_allowances(self):
        from chatbot import billing

        assert billing.token_cap("free") == 50_000
        assert billing.token_cap("team") == 5_000_000
        assert billing.token_cap("business") == 20_000_000
        # Enterprise is agreed per customer and an unplanned org is unmetered.
        assert billing.token_cap("enterprise") is None
        assert billing.token_cap(None) is None

    def test_free_account_is_blocked_once_its_tokens_are_spent(self, client, app):
        res = client.post("/auth/signup", json={
            "email": "free@corp.com", "password": "password123",
            "name": "Free", "category": "finance",
        })
        payload = res.json()
        uid = payload["user"]["id"]
        client.app.state.user_store.set_verified(uid)
        app.state.admin_store.record_usage(
            kind="llm", user_id=uid, organisation_id=SAMPLE_ORGANISATION_ID,
            prompt_tokens=50_000, completion_tokens=0,
        )
        r = client.post(
            "/chat",
            json={"question": "What does our risk policy say about flooding?"},
            headers=auth(payload["token"]),
        )
        assert r.status_code == 402
        assert "token allowance" in r.json()["detail"].lower()

    def test_free_account_under_the_cap_is_not_blocked(self, client, app):
        res = client.post("/auth/signup", json={
            "email": "free@corp.com", "password": "password123",
            "name": "Free", "category": "finance",
        })
        payload = res.json()
        uid = payload["user"]["id"]
        client.app.state.user_store.set_verified(uid)
        app.state.admin_store.record_usage(
            kind="llm", user_id=uid, organisation_id=SAMPLE_ORGANISATION_ID,
            prompt_tokens=49_999, completion_tokens=0,
        )
        r = client.post(
            "/chat",
            json={"question": "What does our risk policy say about flooding?"},
            headers=auth(payload["token"]),
        )
        assert r.status_code != 402

    def test_paid_org_is_blocked_once_its_period_tokens_are_spent(self, client, app):
        _, _token = add_manager(app, "acme", role="owner")
        store = app.state.user_store
        store.set_organisation_billing(
            "acme", plan="team", current_period_start="2020-01-01T00:00:00+00:00"
        )
        member = app.state.user_store.create_user(
            "seat@acme.com", "password123", "Seat", "finance",
            organisation_id="acme", verified=True,
        )
        token = app.state.user_store.token_for(member)
        app.state.admin_store.record_usage(
            kind="llm", user_id=member["id"], organisation_id="acme",
            prompt_tokens=5_000_000, completion_tokens=0,
        )
        r = client.post(
            "/chat",
            json={"question": "What does our risk policy say about flooding?"},
            headers=auth(token),
        )
        assert r.status_code == 402
        assert "token allowance" in r.json()["detail"].lower()

    def test_enterprise_is_never_capped(self, client, app):
        add_manager(app, "acme", role="owner")
        member = app.state.user_store.create_user(
            "seat@acme.com", "password123", "Seat", "finance",
            organisation_id="acme", verified=True,
        )
        token = app.state.user_store.token_for(member)
        app.state.user_store.set_organisation_billing(
            "acme", plan="enterprise", current_period_start="2020-01-01T00:00:00+00:00",
        )
        app.state.admin_store.record_usage(
            kind="llm", user_id=member["id"], organisation_id="acme",
            prompt_tokens=999_000_000, completion_tokens=0,
        )
        r = client.post(
            "/chat",
            json={"question": "What does our risk policy say about flooding?"},
            headers=auth(token),
        )
        assert r.status_code != 402
