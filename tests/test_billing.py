"""D3: the Stripe webhook is how payment truth reaches the application.

Stripe is the source of truth for a subscription; our job is to mirror the
relevant fields onto the organisation and to do it idempotently, because
Stripe retries deliveries and our own handlers may run more than once.
These tests pin the lifecycle the pricing proposal describes: a settled
payment activates an organisation, a failed payment moves it to past_due
(usable, not deleted), and cancellation happens at the period boundary.
"""

import json

import pytest
import stripe
from fastapi.testclient import TestClient

from chatbot import billing
from chatbot.api import create_app
from chatbot.admin_store import AdminStore
from chatbot.session_store import SessionStore
from chatbot.users import UserStore
from chatbot.vector_store import SearchResult

KEY = "secret-admin-key"
SECRET = "whsec_test_0123456789abcdef"

PRICE_TEAM_M = "price_team_monthly"
PRICE_TEAM_Y = "price_team_annual"
PRICE_BIZ_M = "price_business_monthly"
PRICE_BIZ_Y = "price_business_annual"
PRICE_ENT_Y = "price_enterprise_annual"


def _wire_stripe_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_checkout")
    monkeypatch.setenv("STRIPE_PRICE_TEAM_MONTHLY", PRICE_TEAM_M)
    monkeypatch.setenv("STRIPE_PRICE_TEAM_ANNUAL", PRICE_TEAM_Y)
    monkeypatch.setenv("STRIPE_PRICE_BUSINESS_MONTHLY", PRICE_BIZ_M)
    monkeypatch.setenv("STRIPE_PRICE_BUSINESS_ANNUAL", PRICE_BIZ_Y)
    monkeypatch.setenv("STRIPE_PRICE_ENTERPRISE_ANNUAL", PRICE_ENT_Y)
    monkeypatch.setenv("STRIPE_PRICE_EXTRA_SEAT_TEAM", "price_extra_seat_team")
    monkeypatch.setenv("STRIPE_PRICE_EXTRA_SEAT_BUSINESS", "price_extra_seat_business")
    monkeypatch.setenv("STRIPE_PRICE_EXTRA_SEAT_TEAM_ANNUAL", "price_extra_seat_team_year")
    monkeypatch.setenv("STRIPE_PRICE_EXTRA_SEAT_BUSINESS_ANNUAL", "price_extra_seat_business_year")
    monkeypatch.setenv("STRIPE_PRICE_QUESTION_OVERAGE", "price_overage")
    monkeypatch.setenv("STRIPE_PRICE_QUESTION_OVERAGE_ANNUAL", "price_overage_year")


@pytest.fixture
def env(tmp_path, monkeypatch):
    _wire_stripe_env(monkeypatch, tmp_path)
    app = create_app(
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="s"),
        admin_store=AdminStore(str(tmp_path / "a.sqlite3")),
        api_key=KEY,
        rate_limit=0,
    )
    store = app.state.user_store
    store.create_organisation("acme", "Acme Ltd")
    return TestClient(app), store


def post_event(c, event_type, obj, event_id):
    """Send a signed webhook event, the way Stripe would deliver it."""
    payload = json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}})
    signature = stripe.WebhookSignature.generate_signature_header(payload, SECRET)
    return c.post(
        "/billing/webhook",
        content=payload,
        headers={"stripe-signature": signature},
    )


def subscription(customer="cus_1", status="active", plan=PRICE_TEAM_M, org="acme",
                 period_end=1780000000, sub_id="sub_1"):
    return {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "current_period_end": period_end,
        "metadata": {"organisation_id": org},
        "items": {"data": [{"price": {"id": plan}}]},
    }


class TestWebhookDelivery:
    def test_subscription_update_activates_org(self, env):
        c, store = env
        r = post_event(c, "customer.subscription.updated",
                       subscription(), "evt_1")
        assert r.status_code == 200 and r.json()["received"] is True
        org = store.get_organisation("acme")
        assert org["stripe_subscription_id"] == "sub_1"
        assert org["plan"] == "team"
        assert org["billing_status"] == "active"
        assert org["current_period_end"] == "2026-05-28T20:26:40+00:00"

    def test_business_and_enterprise_prices_resolve_plan(self, env):
        c, store = env
        for price, plan in ((PRICE_BIZ_M, "business"), (PRICE_ENT_Y, "enterprise")):
            store.set_organisation_billing("acme", plan=None, billing_status=None)
            r = post_event(c, "customer.subscription.created",
                           subscription(plan=price), f"evt_{price}")
            assert r.status_code == 200
            assert store.get_organisation("acme")["plan"] == plan

    def test_addon_price_does_not_change_plan(self, env, monkeypatch):
        c, store = env
        post_event(c, "customer.subscription.updated", subscription(), "evt_1")
        addon = "price_extra_seat"
        monkeypatch.setenv("STRIPE_PRICE_EXTRA_SEAT_TEAM", addon)
        # A subscription whose items only carry the add-on price must not erase
        # the plan: the base line decides the tier, so an add-on-only payload is
        # treated as "no plan signal", keeping the existing one.
        obj = subscription(plan=PRICE_TEAM_M)
        obj["items"] = {"data": [{"price": {"id": addon}}]}
        post_event(c, "customer.subscription.updated", obj, "evt_2")
        assert store.get_organisation("acme")["plan"] == "team"

    def test_checkout_links_customer_and_activates(self, env):
        c, store = env
        r = post_event(c, "checkout.session.completed", {
            "customer": "cus_1", "subscription": "sub_9",
            "metadata": {"organisation_id": "acme"},
        }, "evt_c")
        assert r.status_code == 200
        org = store.get_organisation("acme")
        assert org["stripe_customer_id"] == "cus_1"
        assert org["stripe_subscription_id"] == "sub_9"
        assert org["billing_status"] == "active"

    def test_event_resolves_org_by_customer_when_metadata_missing(self, env):
        c, store = env
        store.set_organisation_billing("acme", stripe_customer_id="cus_1")
        obj = subscription(org=None)  # no organisation metadata
        obj.pop("metadata", None)
        obj["customer"] = "cus_1"
        r = post_event(c, "customer.subscription.updated", obj, "evt_1")
        assert r.status_code == 200
        assert store.get_organisation("acme")["plan"] == "team"


class TestSubscriptionPeriodEnd:
    """API version 2025-05-28.basil moved the period off the subscription.

    Verified against a live subscription on this account: the top-level
    ``current_period_end`` is absent and each SubscriptionItem carries it, so
    reading only the subscription stored null and every renewal date rendered
    as a dash. Both payload shapes have to keep working, because Stripe has
    not migrated every account's event payloads at the same moment.
    """

    def test_period_end_comes_from_the_subscription_item(self, env):
        c, store = env
        sub = subscription()
        sub.pop("current_period_end")  # current API version: not on the sub
        sub["items"] = {
            "data": [
                {"price": {"id": PRICE_TEAM_M}, "current_period_end": 1780000000},
                {"price": {"id": "price_extra_seat"}, "current_period_end": 1780000000},
            ]
        }
        r = post_event(c, "customer.subscription.updated", sub, "evt_1")
        assert r.status_code == 200
        org = store.get_organisation("acme")
        assert org["current_period_end"] == "2026-05-28T20:26:40+00:00"
        assert org["billing_status"] == "active"

    def test_period_end_still_reads_the_top_level_key(self, env):
        c, store = env
        post_event(c, "customer.subscription.updated", subscription(), "evt_1")
        assert store.get_organisation("acme")[
            "current_period_end"
        ] == "2026-05-28T20:26:40+00:00"

    def test_no_period_anywhere_stores_null_rather_than_500ing(self, env):
        c, store = env
        sub = subscription()
        sub.pop("current_period_end")
        sub["items"] = {"data": [{"price": {"id": PRICE_TEAM_M}}]}
        r = post_event(c, "customer.subscription.updated", sub, "evt_1")
        assert r.status_code == 200
        org = store.get_organisation("acme")
        assert org["current_period_end"] is None
        assert org["billing_status"] == "active"  # the rest still applied


class TestLifecycle:
    def test_payment_failed_moves_to_past_due_not_deletion(self, env):
        c, store = env
        post_event(c, "customer.subscription.updated", subscription(), "evt_1")
        r = post_event(c, "invoice.payment_failed", {"customer": "cus_1"}, "evt_2")
        assert r.status_code == 200
        assert store.get_organisation("acme")["billing_status"] == "past_due"

    def test_invoice_paid_restores_active(self, env):
        c, store = env
        post_event(c, "customer.subscription.updated", subscription(), "evt_1")
        post_event(c, "invoice.payment_failed", {"customer": "cus_1"}, "evt_2")
        post_event(c, "invoice.paid", {"customer": "cus_1"}, "evt_3")
        assert store.get_organisation("acme")["billing_status"] == "active"

    def test_subscription_deleted_cancels_at_period_end(self, env):
        c, store = env
        # a real subscription first links this customer to the organisation
        post_event(c, "customer.subscription.created", subscription(), "evt_0")
        assert store.get_organisation("acme")["stripe_customer_id"] == "cus_1"
        r = post_event(c, "customer.subscription.deleted",
                       {"customer": "cus_1"}, "evt_1")
        assert r.status_code == 200
        assert store.get_organisation("acme")["billing_status"] == "canceled"


class TestIdempotency:
    def test_replayed_event_is_acknowledged_but_not_reapplied(self, env):
        c, store = env
        post_event(c, "customer.subscription.updated", subscription(), "evt_1")
        # advance to past_due with a distinct event
        post_event(c, "invoice.payment_failed", {"customer": "cus_1"}, "evt_2")
        assert store.get_organisation("acme")["billing_status"] == "past_due"
        # Stripe redelivers the original "active" event -> duplicate, no reapply
        r = post_event(c, "customer.subscription.updated", subscription(), "evt_1")
        assert r.status_code == 200 and r.json()["duplicate"] is True
        assert store.get_organisation("acme")["billing_status"] == "past_due"

    def test_processed_event_ids_are_recorded(self, env):
        c, store = env
        post_event(c, "invoice.paid", {"customer": "cus_1"}, "evt_9")
        assert store.has_billing_event("evt_9") is True
        assert store.has_billing_event("evt_nope") is False


class TestDeliveryProtections:
    def test_bad_signature_rejected(self, env):
        c, _ = env
        r = c.post("/billing/webhook", content='{"id":"x"}',
                   headers={"stripe-signature": "t=1,v1=bogus"})
        assert r.status_code == 400
        assert "signature" in r.json()["detail"].lower()

    def test_malformed_body_rejected(self, env):
        c, _ = env
        payload = "this is not json"
        signature = stripe.WebhookSignature.generate_signature_header(payload, SECRET)
        r = c.post("/billing/webhook", content=payload,
                   headers={"stripe-signature": signature})
        assert r.status_code == 400

    def test_unconfigured_billing_returns_503(self, env, monkeypatch):
        c, _ = env
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
        r = c.post("/billing/webhook", content="{}", headers={"stripe-signature": "x"})
        assert r.status_code == 503

    def test_unknown_org_event_is_acked_without_crash(self, env):
        c, store = env
        r = post_event(c, "invoice.paid", {"customer": "cus_ghost"}, "evt_ghost")
        assert r.status_code == 200 and r.json()["received"] is True
        # nothing to mutate; the id is recorded so a retry is not re-processed
        assert store.has_billing_event("evt_ghost") is True

    def test_system_owned_org_never_touched(self, env):
        c, store = env
        obj = subscription()
        obj["metadata"] = {"organisation_id": "_sample"}
        r = post_event(c, "customer.subscription.updated", obj, "evt_sample")
        assert r.status_code == 200
        sample = store.get_organisation("_sample")
        assert sample["billing_status"] is None
        assert sample["plan"] is None


class TestBillingStore:
    def test_lazy_columns_leave_older_orgs_intact(self):
        store = UserStore(db_path=":memory:", secret="s")
        # rows that predate the billing columns must read as None, not as a
        # made-up state, until a webhook actually writes to them
        store.create_organisation("legacy", "Legacy Co")
        store.set_organisation_billing(
            "legacy", billing_status="past_due", plan="team"
        )
        org = store.get_organisation("legacy")
        assert org["billing_status"] == "past_due"
        assert org["plan"] == "team"
        assert org["stripe_customer_id"] is None

    def test_set_billing_whitelists_fields(self):
        store = UserStore(db_path=":memory:", secret="s")
        store.create_organisation("acme", "Acme")
        store.set_organisation_billing("acme", name="HACKED", billing_status="active")
        org = store.get_organisation("acme")
        assert org["name"] == "Acme"  # name is not a billing field
        assert org["billing_status"] == "active"

    def test_record_and_check_billing_events(self):
        store = UserStore(db_path=":memory:", secret="s")
        assert store.has_billing_event("evt_1") is False
        store.record_billing_event("evt_1", "invoice.paid", None, "x")
        assert store.has_billing_event("evt_1") is True


def _paid_org(store, category="finance"):
    """An organisation with an owner in one workspace, ready to pay."""
    owner = store.create_user(
        "owner@acme.com", "password123", "Owner", category,
        organisation_id="acme", verified=True,
    )
    store.set_org_role("acme", owner["id"], "owner")
    return owner


def _checkout_ctx(store, owner=None, extra_workspace=False):
    """Auth headers for the org owner, plus a second workspace when asked."""
    if owner is None:
        owner = _paid_org(store)
    if extra_workspace:
        store.add_workspace(owner["id"], "hr")
    token = store.token_for(owner)
    return {"authorization": f"Bearer {token}"}


class TestMeBillingVisibility:
    def test_owner_sees_plan_and_status_no_stripe_ids(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing(
            "acme", stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
            plan="business", billing_status="active",
            current_period_end="2026-05-28T20:26:40+00:00",
        )
        r = c.get("/me", headers=headers)
        assert r.status_code == 200
        org = r.json()["organisation"]
        assert org["id"] == "acme"
        assert org["plan"] == "business"
        assert org["billing_status"] == "active"
        assert org["role"] == "owner"
        assert "stripe_customer_id" not in org
        assert "stripe_subscription_id" not in org

    def test_unsubscribed_org_reports_empty_plan(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        org = c.get("/me", headers=headers).json()["organisation"]
        assert org["plan"] is None
        assert org["billing_status"] is None

    def test_sample_user_gets_no_billing_status(self, env):
        c, store = env
        user = store.create_user(
            "free@sample.com", "password123", "Free", "finance",
            organisation_id="_sample", verified=True,
        )
        headers = {"authorization": f"Bearer {store.token_for(user)}"}
        org = c.get("/me", headers=headers).json()["organisation"]
        assert org["system_owned"] is True
        assert org["plan"] is None


def _create_recorded(monkeypatch):
    """Replace the Stripe API calls, recording their kwargs for assertions."""
    calls = {}

    def make_customer(**kwargs):
        calls["customer"] = kwargs
        return {"id": "cus_1"}

    def make_session(**kwargs):
        calls["session"] = kwargs
        return {"id": "cs_1", "url": "https://pay.stripe.test/cs_1"}

    monkeypatch.setattr("stripe.Customer.create", make_customer)
    monkeypatch.setattr("stripe.checkout.Session.create", make_session)
    return calls


class TestCheckoutSession:
    def test_owner_creates_subscription_session(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        calls = _create_recorded(monkeypatch)
        r = c.post("/billing/checkout", json={"plan": "team"}, headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["url"] == "https://pay.stripe.test/cs_1"
        session = calls["session"]
        assert session["mode"] == "subscription"
        assert session["line_items"] == [
            {"price": PRICE_TEAM_M, "quantity": 1},
            {"price": "price_extra_seat_team"},
            {"price": "price_overage"},
        ]
        assert session["subscription_data"]["metadata"]["plan"] == "team"
        assert session["metadata"]["organisation_id"] == "acme"
        # a brand-new customer is created and remembered on the organisation
        assert calls["customer"]["email"] == "owner@acme.com"
        assert store.get_organisation("acme")["stripe_customer_id"] == "cus_1"

    def test_quantity_defaults_to_workspace_count(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store, extra_workspace=True)
        calls = _create_recorded(monkeypatch)
        r = c.post("/billing/checkout", json={"plan": "team"}, headers=headers)
        assert r.status_code == 200
        base = calls["session"]["line_items"][0]
        assert base == {"price": PRICE_TEAM_M, "quantity": 2}

    def test_workspace_override_and_interval(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        calls = _create_recorded(monkeypatch)
        r = c.post("/billing/checkout",
                   json={"plan": "business", "interval": "year", "workspaces": 5},
                   headers=headers)
        assert r.status_code == 200
        assert calls["session"]["line_items"][0] == {
            "price": PRICE_BIZ_Y, "quantity": 5,
        }
        assert calls["session"]["metadata"]["interval"] == "year"

    def test_annual_uses_interval_matching_addons(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        calls = _create_recorded(monkeypatch)
        r = c.post("/billing/checkout",
                   json={"plan": "team", "interval": "year"}, headers=headers)
        assert r.status_code == 200
        # all three lines share the annual interval: Checkout rejects mixed ones
        assert calls["session"]["line_items"] == [
            {"price": PRICE_TEAM_Y, "quantity": 1},
            {"price": "price_extra_seat_team_year"},
            {"price": "price_overage_year"},
        ]
        r = c.post("/billing/checkout",
                   json={"plan": "business", "interval": "month"}, headers=headers)
        assert calls["session"]["line_items"] == [
            {"price": PRICE_BIZ_M, "quantity": 1},
            {"price": "price_extra_seat_business"},
            {"price": "price_overage"},
        ]

    def test_existing_customer_is_reused(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing("acme", stripe_customer_id="cus_existing")
        calls = _create_recorded(monkeypatch)
        r = c.post("/billing/checkout", json={"plan": "team"}, headers=headers)
        assert r.status_code == 200
        assert "customer" not in calls  # no new Stripe customer created
        assert calls["session"]["customer"] == "cus_existing"

    def test_enterprise_is_annual_and_bare_bones(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        calls = _create_recorded(monkeypatch)
        r = c.post("/billing/checkout",
                   json={"plan": "enterprise", "interval": "year"}, headers=headers)
        assert r.status_code == 200
        # no extra-seat or question meters ride on enterprise
        assert calls["session"]["line_items"] == [
            {"price": PRICE_ENT_Y, "quantity": 1},
        ]

    def test_rejects_active_subscription(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing(
            "acme", stripe_subscription_id="sub_1", billing_status="active"
        )
        _create_recorded(monkeypatch)
        r = c.post("/billing/checkout", json={"plan": "team"}, headers=headers)
        assert r.status_code == 409
        assert "already has a subscription" in r.json()["detail"]

    def test_canceled_org_can_resubscribe(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing(
            "acme", stripe_subscription_id="sub_old", billing_status="canceled"
        )
        calls = _create_recorded(monkeypatch)
        r = c.post("/billing/checkout", json={"plan": "team"}, headers=headers)
        assert r.status_code == 200
        assert calls["session"] is not None


class TestCheckoutProtections:
    def test_requires_owner(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        admin = store.create_user(
            "admin@acme.com", "password123", "Admin", "finance",
            organisation_id="acme", verified=True,
        )
        store.add_workspace(admin["id"], "finance")
        store.set_org_role("acme", admin["id"], "admin")
        admin_headers = {"authorization": f"Bearer {store.token_for(admin)}"}
        _create_recorded(monkeypatch)
        r = c.post("/billing/checkout", json={"plan": "team"}, headers=admin_headers)
        assert r.status_code == 403
        assert "owner" in r.json()["detail"].lower()
        # the owner's own call works (guards the direction of the test)
        r = c.post("/billing/checkout", json={"plan": "team"}, headers=headers)
        assert r.status_code == 200

    def test_requires_authentication(self, env, monkeypatch):
        c, _ = env
        _create_recorded(monkeypatch)
        r = c.post("/billing/checkout", json={"plan": "team"})
        assert r.status_code == 401

    def test_unknown_plan_and_interval(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        _create_recorded(monkeypatch)
        for bad in ({"plan": "mega"}, {"plan": "team", "interval": "decade"}):
            r = c.post("/billing/checkout", json=bad, headers=headers)
            assert r.status_code == 409

    def test_enterprise_rejects_monthly(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        _create_recorded(monkeypatch)
        r = c.post("/billing/checkout", json={"plan": "enterprise"},
                   headers=headers)
        assert r.status_code == 409

    def test_unconfigured_billing_returns_503(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        monkeypatch.delenv("STRIPE_SECRET_KEY")
        r = c.post("/billing/checkout", json={"plan": "team"}, headers=headers)
        assert r.status_code == 503

    def test_system_owned_org_has_no_checkout(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        # create_user in _sample: the free tier has no owner, so checkout must
        # refuse the only kind of caller the endpoint accepts
        user = store.create_user(
            "someone@free.com", "password123", "Alone", "finance",
            organisation_id="_sample", verified=True,
        )
        headers = {"authorization": f"Bearer {store.token_for(user)}"}
        _create_recorded(monkeypatch)
        r = c.post("/billing/checkout", json={"plan": "team"}, headers=headers)
        assert r.status_code == 403

def _portal_recorded(monkeypatch, url="https://billing.stripe.test/bps_1"):
    """Replace the portal call, recording its kwargs for assertions."""
    calls = {}

    def make_session(**kwargs):
        calls["portal"] = kwargs
        return {"id": "bps_1", "url": url}

    monkeypatch.setattr("stripe.billing_portal.Session.create", make_session)
    return calls


class TestCustomerPortal:
    """Where every "your payment failed" message points (D3).

    Owner-only, like checkout, but reachable in every billing state: late,
    unpaid and ended are exactly the states a stuck payer needs it in.
    """

    def test_owner_opens_the_portal(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing("acme", stripe_customer_id="cus_1")
        calls = _portal_recorded(monkeypatch)
        r = c.post("/billing/portal", json={}, headers=headers)
        assert r.status_code == 200
        assert r.json()["url"] == "https://billing.stripe.test/bps_1"
        assert calls["portal"]["customer"] == "cus_1"
        assert calls["portal"]["return_url"].endswith("/ui")

    def test_return_url_is_honoured(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing("acme", stripe_customer_id="cus_1")
        calls = _portal_recorded(monkeypatch)
        r = c.post("/billing/portal",
                   json={"return_url": "https://example.test/back"},
                   headers=headers)
        assert r.status_code == 200
        assert calls["portal"]["return_url"] == "https://example.test/back"

    def test_organisation_without_a_customer_is_told_to_choose_a_plan(
        self, env, monkeypatch
    ):
        c, store = env
        headers = _checkout_ctx(store)  # acme has never been given a customer
        calls = _portal_recorded(monkeypatch)
        r = c.post("/billing/portal", json={}, headers=headers)
        assert r.status_code == 409
        assert "plan" in r.json()["detail"].lower()
        assert "portal" not in calls  # nothing was built for Stripe to open

    def test_requires_owner(self, env, monkeypatch):
        c, store = env
        _checkout_ctx(store)
        admin = store.create_user(
            "admin@acme.com", "password123", "Admin", "finance",
            organisation_id="acme", verified=True,
        )
        store.add_workspace(admin["id"], "finance")
        store.set_org_role("acme", admin["id"], "admin")
        headers = {"authorization": f"Bearer {store.token_for(admin)}"}
        _portal_recorded(monkeypatch)
        r = c.post("/billing/portal", json={}, headers=headers)
        assert r.status_code == 403
        assert "owner" in r.json()["detail"].lower()

    def test_requires_authentication(self, env, monkeypatch):
        c, _ = env
        _portal_recorded(monkeypatch)
        assert c.post("/billing/portal", json={}).status_code == 401

    def test_unconfigured_billing_returns_503(self, env, monkeypatch):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing("acme", stripe_customer_id="cus_1")
        _portal_recorded(monkeypatch)
        monkeypatch.delenv("STRIPE_SECRET_KEY")
        assert c.post("/billing/portal", json={}, headers=headers).status_code == 503

    def test_stripe_refusal_is_reported_as_502(self, env, monkeypatch):
        """A customer deleted from the dashboard is not the caller's fault."""
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing("acme", stripe_customer_id="cus_gone")

        def boom(**kwargs):
            raise stripe.InvalidRequestError(
                "No such customer: cus_gone", param="customer", http_status=404
            )

        monkeypatch.setattr("stripe.billing_portal.Session.create", boom)
        r = c.post("/billing/portal", json={}, headers=headers)
        assert r.status_code == 502
        assert "Stripe" in r.json()["detail"]

    def test_system_owned_org_has_no_portal(self, env, monkeypatch):
        c, store = env
        user = store.create_user(
            "someone@free.com", "password123", "Alone", "finance",
            organisation_id="_sample", verified=True,
        )
        headers = {"authorization": f"Bearer {store.token_for(user)}"}
        _portal_recorded(monkeypatch)
        assert c.post("/billing/portal", json={}, headers=headers).status_code == 403


class TestLifecycleEnforcement:
    """`canceled` and `unpaid` stop content; late and never-subscribed do not.

    The gate sits behind `require_verified` on every endpoint that spends
    model time or hands back a document, and nowhere else, so a locked-out
    owner can still read `/me` and reach the portal from inside the state the
    server just refused.
    """

    # Every endpoint the gate protects, with a payload that gets past input
    # validation so the only thing that can answer 402 is the gate itself.
    SERVED = (
        ("/chat", {"question": "hello"}),
        ("/chat/stream", {"question": "hello"}),
        ("/search", {"question": "hello"}),
        ("/documents/link", {"source": "absent.pdf"}),
        ("/voice/tts", {"text": "hello"}),
    )

    def test_ended_subscription_is_refused_on_every_content_endpoint(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing("acme", billing_status="canceled")
        for path, payload in self.SERVED:
            r = c.post(path, json=payload, headers=headers)
            assert r.status_code == 402, (path, r.status_code, r.text[:200])
            assert "choose a plan" in r.json()["detail"]

    def test_giving_up_after_retries_is_refused_too(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing("acme", billing_status="unpaid")
        r = c.post("/chat", json={"question": "hello"}, headers=headers)
        assert r.status_code == 402
        assert "payment method" in r.json()["detail"]

    def test_a_late_payer_keeps_their_workspace(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing("acme", billing_status="past_due")
        r = c.post("/documents/link", json={"source": "absent.pdf"}, headers=headers)
        # A 404 is a pass: the document lookup is what answered, so billing
        # did not stop the request. Only 402 would mean it did.
        assert r.status_code == 404

    def test_an_org_that_never_subscribed_is_not_governed(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        assert store.get_organisation("acme")["billing_status"] is None
        r = c.post("/documents/link", json={"source": "absent.pdf"}, headers=headers)
        assert r.status_code == 404

    def test_a_live_subscription_is_untouched(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing(
            "acme", stripe_customer_id="cus_1",
            stripe_subscription_id="sub_1", plan="team",
            billing_status="active",
            current_period_end="2026-11-07T11:56:18+00:00",
        )
        r = c.post("/documents/link", json={"source": "absent.pdf"}, headers=headers)
        assert r.status_code == 404

    def test_a_webhook_cancellation_takes_effect_immediately(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        post_event(c, "customer.subscription.created", subscription(), "evt_0")
        r = c.post("/documents/link", json={"source": "absent.pdf"}, headers=headers)
        assert r.status_code == 404
        post_event(c, "customer.subscription.deleted", {"customer": "cus_1"}, "evt_1")
        r = c.post("/documents/link", json={"source": "absent.pdf"}, headers=headers)
        assert r.status_code == 402

    def test_the_free_tier_is_not_governed_by_billing(self, env):
        c, store = env
        user = store.create_user(
            "free@sample.com", "password123", "Free", "finance",
            organisation_id="_sample", verified=True,
        )
        headers = {"authorization": f"Bearer {store.token_for(user)}"}
        store.set_organisation_billing("_sample", billing_status="canceled")
        r = c.post("/documents/link", json={"source": "absent.pdf"}, headers=headers)
        assert r.status_code != 402

    def test_the_locked_out_owner_can_still_read_me(self, env):
        """The banner and the portal button both depend on `/me` answering."""
        c, store = env
        headers = _checkout_ctx(store)
        store.set_organisation_billing(
            "acme", stripe_customer_id="cus_1", billing_status="canceled"
        )
        r = c.get("/me", headers=headers)
        assert r.status_code == 200
        org = r.json()["organisation"]
        assert org["billing_status"] == "canceled"
        assert org["role"] == "owner"


class TestBillingDoorInTheUI:
    """The console has to show the door the 402 message points at."""

    @staticmethod
    def _ui():
        return open("src/chatbot/static/ui.html").read()

    def test_the_notice_and_its_payment_button_exist(self):
        html = self._ui()
        assert 'id="billing-notice"' in html
        assert 'id="billing-manage"' in html

    def test_the_button_opens_the_portal_endpoint(self):
        assert '"/billing/portal"' in self._ui()

    def test_a_stopped_subscription_closes_the_composer_but_a_late_one_does_not(
        self
    ):
        html = self._ui()
        # Past_due must keep working (proposal 4.6); canceled and unpaid are
        # the two states the server refuses with 402, and the two it gates.
        assert (
            'composerBlocked.billing = status === "canceled" || status === "unpaid"'
            in html
        )

    def test_only_the_owner_is_offered_the_payment_button(self):
        html = self._ui()
        assert 'org.role === "owner"' in html
        assert 'status === "canceled")' in html  # the portal cannot restart it

class TestUsagePanelInTheUI:
    """The allowance readout has to be reachable, and stay the owner's."""

    @staticmethod
    def _ui():
        return open("src/chatbot/static/ui.html").read()

    def test_the_panel_and_its_refresh_button_exist(self):
        html = self._ui()
        assert 'id="usage-section"' in html
        assert 'id="usage-panel"' in html
        assert 'id="usage-refresh"' in html

    def test_it_reads_the_usage_endpoint(self):
        assert '"/billing/usage"' in self._ui()

    def test_only_the_owner_is_shown_the_panel(self):
        html = self._ui()
        # usageVisible() is the single gate: members keep the header badge.
        gate = html.split("function usageVisible()")[1].split("}")[0]
        assert 'org.role === "owner"' in gate
        assert "!org.system_owned" in gate

    def test_an_idle_period_is_explained_rather_than_left_blank(self):
        """A period_start the server has never received must read as a reason."""
        assert "Reporting idle:" in self._ui()



# ---- Metered usage reporting ----------------------------------------------

PERIOD_START = "2026-10-01T00:00:00+00:00"
PERIOD_END = "2026-11-01T00:00:00+00:00"


def _meters(monkeypatch, *, fail=False):
    """Replace Stripe's meter endpoint, recording every event sent."""
    calls = []

    def record(**kwargs):
        if fail:
            raise stripe.APIConnectionError("meter unreachable")
        calls.append(kwargs)
        return {"id": "me_1"}

    monkeypatch.setattr("stripe.billing.MeterEvent.create", record)
    return calls


def _metered_org(store, *, plan="team", customer="cus_1", subscription="sub_1",
                 seats_reported=0, status="active", period_start=PERIOD_START):
    """A paid organisation sitting inside a known billing period."""
    store.set_organisation_billing(
        "acme",
        stripe_customer_id=customer,
        stripe_subscription_id=subscription,
        plan=plan,
        billing_status=status,
        current_period_start=period_start,
        current_period_end=PERIOD_END,
        seats_reported=seats_reported,
    )
    return store.get_organisation("acme")


def _seat(store, index, category="finance", organisation_id="acme"):
    return store.create_user(
        f"s{index}@acme.com", "password123", f"Seat {index}", category,
        organisation_id=organisation_id, verified=True,
    )


def _seed_questions(admin, count, organisation_id="acme"):
    for i in range(count):
        admin.record_usage(
            kind="question", organisation_id=organisation_id,
            category="finance", request_id=f"r{i}",
        )


def _seed_question_at(admin, at, organisation_id="acme", request_id="old"):
    """A question asked at a known time, for period boundaries."""
    admin.conn.execute(
        "INSERT INTO usage (at, kind, category, user_id, request_id, "
        "organisation_id) VALUES (?, 'question', 'finance', 'u1', ?, ?)",
        (at, request_id, organisation_id),
    )
    admin.conn.commit()


class TestQuestionMetering:
    """Only the questions past the pool ever reach Stripe."""

    def test_questions_inside_the_pool_are_never_reported(self, env, monkeypatch):
        c, store = env
        calls = _meters(monkeypatch)
        _metered_org(store)
        admin = c.app.state.admin_store
        _seed_questions(admin, billing.question_pool("team"))
        assert billing.report_question_usage(store, admin, "acme") is False
        assert calls == []

    def test_the_first_question_past_the_pool_is_reported(self, env, monkeypatch):
        c, store = env
        calls = _meters(monkeypatch)
        _metered_org(store)
        admin = c.app.state.admin_store
        _seed_questions(admin, billing.question_pool("team") + 1)

        assert billing.report_question_usage(store, admin, "acme") is True
        assert len(calls) == 1
        event = calls[0]
        assert event["event_name"] == "esg.question"
        assert event["payload"] == {"stripe_customer_id": "cus_1", "value": "1"}
        assert str(billing.question_pool("team") + 1) in event["identifier"]

    def test_a_repeat_report_carries_the_same_identifier(self, env, monkeypatch):
        """Stripe drops a repeated identifier, so a retry cannot bill twice."""
        c, store = env
        calls = _meters(monkeypatch)
        _metered_org(store)
        admin = c.app.state.admin_store
        _seed_questions(admin, billing.question_pool("team") + 1)
        billing.report_question_usage(store, admin, "acme")
        billing.report_question_usage(store, admin, "acme")
        assert len(calls) == 2
        assert calls[0]["identifier"] == calls[1]["identifier"]

    def test_questions_asked_before_the_period_are_not_counted(self, env, monkeypatch):
        c, store = env
        calls = _meters(monkeypatch)
        _metered_org(store)
        admin = c.app.state.admin_store
        _seed_question_at(admin, "2026-09-01T00:00:00+00:00", request_id="pre")
        _seed_question_at(admin, "2026-09-30T23:59:59+00:00", request_id="pre2")
        assert billing.report_question_usage(store, admin, "acme") is False
        assert calls == []

    def test_enterprise_questions_are_not_metered(self, env, monkeypatch):
        c, store = env
        calls = _meters(monkeypatch)
        _metered_org(store, plan="enterprise")
        admin = c.app.state.admin_store
        _seed_questions(admin, 5000)
        assert billing.report_question_usage(store, admin, "acme") is False
        assert calls == []

    def test_an_organisation_that_never_paid_is_not_metered(self, env, monkeypatch):
        c, store = env
        calls = _meters(monkeypatch)
        store.set_organisation_billing(
            "acme", plan="team", current_period_start=PERIOD_START,
        )
        admin = c.app.state.admin_store
        _seed_questions(admin, billing.question_pool("team") + 1)
        assert billing.report_question_usage(store, admin, "acme") is False
        assert calls == []

    def test_an_organisation_with_no_period_window_is_not_metered(self, env, monkeypatch):
        """A guessed window would bill the wrong questions; none is better."""
        c, store = env
        calls = _meters(monkeypatch)
        store.set_organisation_billing(
            "acme", plan="team", stripe_customer_id="cus_1",
            stripe_subscription_id="sub_1",
        )
        admin = c.app.state.admin_store
        _seed_questions(admin, billing.question_pool("team") + 1)
        assert billing.report_question_usage(store, admin, "acme") is False
        assert calls == []

    def test_a_system_organisation_is_never_metered(self, env, monkeypatch):
        c, store = env
        calls = _meters(monkeypatch)
        admin = c.app.state.admin_store
        _seed_questions(admin, 10, organisation_id="_sample")
        assert billing.report_question_usage(store, admin, "_sample") is False
        assert calls == []

    def test_a_stripe_failure_is_swallowed(self, env, monkeypatch):
        c, store = env
        _meters(monkeypatch, fail=True)
        _metered_org(store)
        admin = c.app.state.admin_store
        _seed_questions(admin, billing.question_pool("team") + 1)
        assert billing.report_question_usage(store, admin, "acme") is False


class _MeteredBot:
    """Answers with one LLM usage event, the way the real bot reports usage."""

    def __init__(self):
        self.last_results = [SearchResult(
            chunk_id="c1", source="10. IFRS S1.pdf", chunk_index=0,
            content="chunk body", distance=0.25,
        )]

    def retrieve(self, question, k=3, source=None, history=None, usage_sink=None):
        return self.last_results

    def ask(self, question, k=3, source=None, history=None, usage_sink=None):
        if usage_sink is not None:
            usage_sink.append({
                "kind": "answer", "model": "m",
                "prompt_tokens": 10, "completion_tokens": 5, "duration_s": 0.1,
            })
        return "answer"

    def ask_stream(self, question, k=3, source=None, history=None, usage_sink=None):
        if usage_sink is not None:
            usage_sink.append({
                "kind": "answer", "model": "m",
                "prompt_tokens": 10, "completion_tokens": 5, "duration_s": 0.1,
            })
        yield "answer"


@pytest.fixture
def meter_env(tmp_path, monkeypatch):
    """The chat path wired to a stub bot, for end-to-end metering."""
    _wire_stripe_env(monkeypatch, tmp_path)
    app = create_app(
        bot=_MeteredBot(),
        session_store=SessionStore(db_path=str(tmp_path / "s.sqlite3")),
        user_store=UserStore(db_path=str(tmp_path / "u.sqlite3"), secret="s"),
        admin_store=AdminStore(str(tmp_path / "a.sqlite3")),
        api_key=KEY,
        rate_limit=0,
    )
    store = app.state.user_store
    store.create_organisation("acme", "Acme Ltd")
    return TestClient(app), store


class TestQuestionMeteringEndToEnd:
    """A question asked in chat is recorded and reported without help."""

    @staticmethod
    def _asked(c, store, monkeypatch, *, plan="team"):
        calls = _meters(monkeypatch)
        owner = _paid_org(store)
        _metered_org(store, plan=plan)
        headers = {"authorization": f"Bearer {store.token_for(owner)}"}
        res = c.post("/chat", json={"question": "targets?"}, headers=headers)
        return res, calls

    def test_a_question_past_the_pool_reaches_stripe(self, meter_env, monkeypatch):
        c, store = meter_env
        monkeypatch.setitem(billing._QUESTION_POOL, "team", 0)
        res, calls = self._asked(c, store, monkeypatch)
        assert res.status_code == 200, res.text
        admin = c.app.state.admin_store
        assert admin.questions_in_period("acme", since=PERIOD_START) == 1
        assert [e["event_name"] for e in calls] == ["esg.question"]
        assert calls[0]["payload"]["value"] == "1"

    def test_a_question_inside_the_pool_sends_nothing(self, meter_env, monkeypatch):
        c, store = meter_env
        res, calls = self._asked(c, store, monkeypatch)
        assert res.status_code == 200, res.text
        assert c.app.state.admin_store.questions_in_period("acme", since=PERIOD_START) == 1
        assert calls == []

    def test_a_stripe_outage_never_fails_the_answer(self, meter_env, monkeypatch):
        c, store = meter_env
        monkeypatch.setitem(billing._QUESTION_POOL, "team", 0)
        calls = _meters(monkeypatch, fail=True)
        res, _ = self._asked(c, store, monkeypatch)
        assert res.status_code == 200, res.text
        assert calls == []

    def test_a_ended_subscription_is_refused_before_it_is_metered(self, meter_env, monkeypatch):
        c, store = meter_env
        monkeypatch.setitem(billing._QUESTION_POOL, "team", 0)
        calls = _meters(monkeypatch)
        owner = _paid_org(store)
        _metered_org(store, status="canceled")
        headers = {"authorization": f"Bearer {store.token_for(owner)}"}
        res = c.post("/chat", json={"question": "targets?"}, headers=headers)
        assert res.status_code == 402
        assert c.app.state.admin_store.questions_in_period("acme", since=PERIOD_START) == 0
        assert calls == []


class TestSeatMetering:
    """Seats past the per-workspace allowance, reported as they change."""

    @staticmethod
    def _staff(store, count, category="finance"):
        for i in range(count):
            _seat(store, f"{category}{i}", category=category)

    def test_seats_are_counted_per_workspace(self, env, monkeypatch):
        _c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 1)
        _metered_org(store)
        self._staff(store, 3, "finance")
        self._staff(store, 2, "hr")
        # 3 - 1 in finance, 2 - 1 in hr.
        assert billing.extra_seats_for(store, store.get_organisation("acme")) == 3

    def test_enterprise_includes_every_seat(self, env, monkeypatch):
        _c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 1)
        _metered_org(store, plan="enterprise")
        self._staff(store, 4, "finance")
        assert billing.extra_seats_for(store, store.get_organisation("acme")) == 0

    def test_a_seat_inside_the_allowance_is_not_an_extra(self, env, monkeypatch):
        _c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 5)
        _metered_org(store)
        self._staff(store, 5, "finance")
        assert billing.extra_seats_for(store, store.get_organisation("acme")) == 0

    def test_a_period_opening_snapshots_the_whole_count(self, env, monkeypatch):
        _c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 1)
        calls = _meters(monkeypatch)
        _metered_org(store, seats_reported=0)
        self._staff(store, 3, "finance")

        value = billing.sync_extra_seats(
            store, store.get_organisation("acme"), snapshot=True, hint="evt_1"
        )
        assert value == 2
        assert calls[0]["event_name"] == "esg.extra_seats"
        assert calls[0]["payload"]["value"] == "2"
        assert store.get_organisation("acme")["seats_reported"] == 2

    def test_a_mid_period_join_reports_only_the_growth(self, env, monkeypatch):
        _c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 1)
        calls = _meters(monkeypatch)
        _metered_org(store, seats_reported=1)
        self._staff(store, 3, "finance")

        billing.sync_extra_seats(store, store.get_organisation("acme"))
        assert [e["payload"]["value"] for e in calls] == ["1"]
        assert store.get_organisation("acme")["seats_reported"] == 2

    def test_unchanged_seats_are_reported_once(self, env, monkeypatch):
        _c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 1)
        calls = _meters(monkeypatch)
        _metered_org(store, seats_reported=1)
        self._staff(store, 3, "finance")

        billing.sync_extra_seats(store, store.get_organisation("acme"))
        # The first sync moves the local figure to the true count; the second
        # has nothing left to send.
        billing.sync_extra_seats(store, store.get_organisation("acme"))
        assert len(calls) == 1

    def test_a_leaving_seat_is_not_refunded_mid_period(self, env, monkeypatch):
        """The meter sums, so it cannot be decremented before the next opening."""
        _c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 5)
        calls = _meters(monkeypatch)
        _metered_org(store, seats_reported=3)
        self._staff(store, 6, "finance")  # one extra seat, was three

        value = billing.sync_extra_seats(store, store.get_organisation("acme"))
        assert value is None
        assert calls == []
        assert store.get_organisation("acme")["seats_reported"] == 1

    def test_an_organisation_without_a_subscription_is_not_metered(self, env, monkeypatch):
        _c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 1)
        calls = _meters(monkeypatch)
        store.set_organisation_billing("acme", plan="team", stripe_customer_id="cus_1")
        self._staff(store, 3, "finance")
        assert billing.sync_extra_seats(store, store.get_organisation("acme")) is None
        assert calls == []


class TestSeatMeteringOnTheInvoice:
    """The invoice that opens a period is where the seat count is taken."""

    @staticmethod
    def _invoice(billing_reason="subscription_cycle"):
        return {
            "customer": "cus_1",
            "billing_reason": billing_reason,
            "period_start": 1790000000,
            "period_end": 1792592000,
            "metadata": {"organisation_id": "acme"},
        }

    @staticmethod
    def _staff(store, count, category="finance"):
        for i in range(count):
            _seat(store, f"{category}{i}", category=category)

    def test_the_period_opening_snapshots_seats(self, env, monkeypatch):
        c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 1)
        calls = _meters(monkeypatch)
        _metered_org(store, seats_reported=0)
        self._staff(store, 3, "finance")

        res = post_event(c, "invoice.paid", self._invoice(), "evt_seat_1")
        assert res.status_code == 200
        assert [e["payload"]["value"] for e in calls] == ["2"]
        assert store.get_organisation("acme")["seats_reported"] == 2

    def test_the_period_opening_window_comes_from_the_invoice(self, env, monkeypatch):
        c, store = env
        _meters(monkeypatch)
        _metered_org(store, period_start=None)
        post_event(c, "invoice.paid", self._invoice(), "evt_seat_2")
        org = store.get_organisation("acme")
        assert org["current_period_start"] == billing._iso_from_epoch(1790000000)
        assert org["current_period_end"] == billing._iso_from_epoch(1792592000)

    def test_an_invoice_that_does_not_open_a_period_does_not_snapshot(self, env, monkeypatch):
        c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 1)
        calls = _meters(monkeypatch)
        _metered_org(store, seats_reported=0)
        self._staff(store, 3, "finance")

        res = post_event(
            c, "invoice.paid", self._invoice("subscription_update"), "evt_seat_3"
        )
        assert res.status_code == 200
        assert calls == []
        assert store.get_organisation("acme")["seats_reported"] == 0

    def test_a_failed_payment_does_not_snapshot(self, env, monkeypatch):
        c, store = env
        monkeypatch.setitem(billing._INCLUDED_SEATS, "team", 1)
        calls = _meters(monkeypatch)
        _metered_org(store, seats_reported=0, status="past_due")
        self._staff(store, 3, "finance")

        res = post_event(c, "invoice.payment_failed", self._invoice(), "evt_seat_4")
        assert res.status_code == 200
        assert calls == []
        assert store.get_organisation("acme")["billing_status"] == "past_due"


class TestPeriodWindow:
    """The pool is counted from a window the subscription itself declares."""

    @staticmethod
    def _with_start(start_epoch):
        obj = subscription()
        obj["items"]["data"][0]["current_period_start"] = start_epoch
        return obj

    def test_a_subscription_event_records_the_opening(self, env):
        c, store = env
        post_event(c, "customer.subscription.updated", self._with_start(1790000000),
                   "evt_window_1")
        org = store.get_organisation("acme")
        assert org["current_period_start"] == billing._iso_from_epoch(1790000000)

    def test_a_slim_payload_cannot_blank_the_window(self, env):
        c, store = env
        post_event(c, "customer.subscription.updated", self._with_start(1790000000),
                   "evt_window_2")
        post_event(c, "customer.subscription.updated", subscription(), "evt_window_3")
        org = store.get_organisation("acme")
        assert org["current_period_start"] == billing._iso_from_epoch(1790000000)


class TestUsageEndpoint:
    """GET /billing/usage: what the allowance looks like from the inside."""

    def test_requires_authentication(self, env):
        c, _store = env
        assert c.get("/billing/usage").status_code == 401

    def test_reports_the_pool_and_the_seats(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        _metered_org(store)
        res = c.get("/billing/usage", headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["organisation_id"] == "acme"
        assert body["plan"] == "team"
        assert body["period_start"] == PERIOD_START
        assert body["questions"] == {
            "included": 500, "used": 0, "overage": 0, "metered": True,
        }
        assert body["seats"]["included_per_workspace"] == 5
        assert body["seats"]["total"] == 1
        assert body["seats"]["extra"] == 0
        assert body["reporting"]["configured"] is True

    def test_counts_questions_and_reports_the_overage(self, env):
        c, store = env
        headers = _checkout_ctx(store)
        _metered_org(store)
        admin = c.app.state.admin_store
        _seed_questions(admin, 503)
        body = c.get("/billing/usage", headers=headers).json()
        assert body["questions"]["used"] == 503
        assert body["questions"]["overage"] == 3

    def test_answers_for_a_cancelled_organisation(self, env):
        """The banner telling someone to choose a plan must be answerable."""
        c, store = env
        headers = _checkout_ctx(store)
        _metered_org(store, status="canceled")
        assert c.get("/billing/usage", headers=headers).status_code == 200

    def test_answers_for_a_member_with_no_role(self, env):
        """Seats and pools belong to everyone in the organisation (P1 gap)."""
        c, store = env
        _metered_org(store)
        member = store.create_user(
            "member@acme.com", "password123", "Member", "finance",
            organisation_id="acme", verified=True,
        )
        assert store.org_role(member["id"]) is None
        headers = {"authorization": f"Bearer {store.token_for(member)}"}
        res = c.get("/billing/usage", headers=headers)
        assert res.status_code == 200, res.text
        assert res.json()["organisation_id"] == "acme"

    def test_a_free_tier_account_sees_an_unmetered_pool(self, env):
        c, store = env
        user = store.create_user(
            "free@sample.com", "password123", "Free", "finance",
            organisation_id="_sample", verified=True,
        )
        headers = {"authorization": f"Bearer {store.token_for(user)}"}
        body = c.get("/billing/usage", headers=headers).json()
        assert body["questions"]["metered"] is False
        assert body["questions"]["included"] is None


# ---- The row that never saw a webhook -------------------------------------

LIVE_START = 1793542578   # 2026-11-07T11:56:18+00:00
LIVE_END = 1796134578     # 2026-12-07T11:56:18+00:00


def live_subscription(customer="cus_live_1", org="acme", plan=PRICE_TEAM_M,
                      sub_id="sub_live_1", start=LIVE_START, end=LIVE_END):
    """A retrieve-shaped subscription.

    API version 2025-05-28.basil carries the period on each SubscriptionItem
    rather than at the top level, which is the shape the repair path reads.
    """
    return {
        "id": sub_id,
        "customer": customer,
        "status": "active",
        "metadata": {"organisation_id": org},
        "items": {"data": [{
            "price": {"id": plan},
            "current_period_start": start,
            "current_period_end": end,
        }]},
    }


def _stub_retrieve(monkeypatch, payload):
    """Make Subscription.retrieve answer, and record what it was asked for."""
    calls = []

    def retrieve(subscription_id, **kw):
        calls.append(subscription_id)
        return payload

    monkeypatch.setattr(stripe.Subscription, "retrieve", retrieve)
    return calls


def _hand_seeded(store):
    """An organisation as a smoke test leaves it: no period window at all."""
    store.set_organisation_billing(
        "acme",
        stripe_customer_id="cus_live_1",
        stripe_subscription_id="sub_smoke_active_1",
        plan="team",
        billing_status="active",
    )
    return store.get_organisation("acme")


class TestRefreshFromStripe:
    """A period the webhook never delivered still has to be learnable."""

    def test_the_window_and_the_subscription_are_written(self, env, monkeypatch):
        c, store = env
        _hand_seeded(store)
        calls = _stub_retrieve(monkeypatch, live_subscription())

        updated = billing.refresh_subscription_from_stripe(store, "sub_smoke_active_1")

        assert calls == ["sub_smoke_active_1"]  # the id we hold, not one we invent
        assert updated["stripe_subscription_id"] == "sub_live_1"
        assert updated["stripe_customer_id"] == "cus_live_1"
        assert updated["plan"] == "team"
        assert updated["billing_status"] == "active"
        assert updated["current_period_start"] == billing._iso_from_epoch(LIVE_START)
        assert updated["current_period_end"] == billing._iso_from_epoch(LIVE_END)

    def test_metering_has_a_window_to_count_from_again(self, env, monkeypatch):
        c, store = env
        _hand_seeded(store)
        # A window that opened before today, so the seeded questions fall in it.
        _stub_retrieve(monkeypatch, live_subscription(start=1790000000))
        admin = c.app.state.admin_store
        _seed_questions(admin, 503)
        calls = _meters(monkeypatch)

        billing.refresh_subscription_from_stripe(store, "sub_smoke_active_1")
        reported = billing.report_question_usage(store, admin, "acme")

        assert reported is True
        # One event per new overage question; the identifier carries the count
        # so a retry of the same total is deduplicated by Stripe.
        assert calls[0]["payload"]["value"] == "1"
        assert calls[0]["identifier"].endswith(":503")

    def test_reading_the_account_writes_nothing_back(self, env, monkeypatch):
        """The repair must never create, cancel or meter anything at Stripe."""
        c, store = env
        _hand_seeded(store)
        calls = _stub_retrieve(monkeypatch, live_subscription())

        def explode(**kw):
            raise AssertionError("a read must not send a meter event")

        monkeypatch.setattr("stripe.billing.MeterEvent.create", explode)
        billing.refresh_subscription_from_stripe(store, "sub_smoke_active_1")
        assert len(calls) == 1

    def test_a_subscription_nobody_owns_is_refused(self, env, monkeypatch):
        _c, store = env
        _stub_retrieve(monkeypatch, live_subscription(org="ghost"))
        with pytest.raises(billing.InvalidBillingRequest):
            billing.refresh_subscription_from_stripe(store, "sub_live_1")

    def test_stripe_refusal_is_reported_as_upstream(self, env, monkeypatch):
        _c, store = env

        def refuse(subscription_id, **kw):
            raise stripe.StripeError("no such subscription")

        monkeypatch.setattr(stripe.Subscription, "retrieve", refuse)
        with pytest.raises(billing.BillingUpstreamError):
            billing.refresh_subscription_from_stripe(store, "sub_live_1")

    def test_without_a_secret_key_it_is_not_configured(self, env, monkeypatch):
        _c, store = env
        monkeypatch.delenv("STRIPE_SECRET_KEY")
        with pytest.raises(billing.BillingUnconfigured):
            billing.refresh_subscription_from_stripe(store, "sub_live_1")

    def test_the_admin_endpoint_repairs_the_row(self, env, monkeypatch):
        c, store = env
        _hand_seeded(store)
        _stub_retrieve(monkeypatch, live_subscription())
        key = {"authorization": f"Bearer {KEY}"}

        res = c.post("/admin/organisations/acme/billing/refresh", headers=key)

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["billing"]["stripe_subscription_id"] == "sub_live_1"
        assert body["billing"]["current_period_start"] == billing._iso_from_epoch(LIVE_START)
        assert store.get_organisation("acme")["current_period_start"] == (
            billing._iso_from_epoch(LIVE_START)
        )

    def test_the_admin_endpoint_is_gated(self, env, monkeypatch):
        c, store = env
        _hand_seeded(store)
        _stub_retrieve(monkeypatch, live_subscription())
        assert c.post("/admin/organisations/acme/billing/refresh").status_code == 401

    def test_an_organisation_with_no_subscription_is_told_so(self, env):
        c, _store = env
        key = {"authorization": f"Bearer {KEY}"}
        res = c.post("/admin/organisations/acme/billing/refresh", headers=key)
        assert res.status_code == 422
        assert "No subscription recorded" in res.json()["detail"]

    def test_a_system_organisation_is_refused(self, env, monkeypatch):
        c, _store = env
        _stub_retrieve(monkeypatch, live_subscription(org="_sample"))
        key = {"authorization": f"Bearer {KEY}"}
        res = c.post("/admin/organisations/_sample/billing/refresh", headers=key)
        assert res.status_code == 422
        assert "not billed" in res.json()["detail"]
