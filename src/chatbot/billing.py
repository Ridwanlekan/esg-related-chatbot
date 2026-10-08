"""Stripe billing: the receiving and sending halves of the payment loop.

The Stripe account owns the truth about a subscription; the webhook at
POST /billing/webhook is how that truth gets into the application. Every event
arrives signed and is applied to the organisation that owns the Stripe customer.
Lifecycle per the pricing proposal: a settled payment moves an organisation to
active, a failed payment to past_due (usable but overlaid with a clear notice),
and cancellation to canceled on the next period boundary.

The sending half is POST /billing/checkout, which builds the Checkout Session
the owner is redirected to. It carries the price/plan mapping this module
already defines so the checkout line items and the webhook handlers always
agree on what a plan looks like. POST /billing/portal is the third piece: the
destination every "your payment failed" or "your subscription ended" message
points at, because that is where a card is replaced and a subscription is
resumed — never in our own database.
"""

import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("esg.billing")

# The base (per-workspace) price ids, keyed by the tier and billing interval.
# The webhook resolves a plan from the subscription's base line, the checkout
# builds that same line. Enterprise is annual-commitment only (proposal D1).
_BASE_PRICE_ENV = {
    ("team", "month"): "STRIPE_PRICE_TEAM_MONTHLY",
    ("team", "year"): "STRIPE_PRICE_TEAM_ANNUAL",
    ("business", "month"): "STRIPE_PRICE_BUSINESS_MONTHLY",
    ("business", "year"): "STRIPE_PRICE_BUSINESS_ANNUAL",
    ("enterprise", "year"): "STRIPE_PRICE_ENTERPRISE_ANNUAL",
}

# Extra-seat prices are metered per tier and ride on the base subscription.
# Checkout rejects line items with mixed billing intervals, so each tier needs
# an interval-matching price; the annual rates are the 12x monthly ones.
_EXTRA_SEAT_PRICE_ENV = {
    ("team", "month"): "STRIPE_PRICE_EXTRA_SEAT_TEAM",
    ("team", "year"): "STRIPE_PRICE_EXTRA_SEAT_TEAM_ANNUAL",
    ("business", "month"): "STRIPE_PRICE_EXTRA_SEAT_BUSINESS",
    ("business", "year"): "STRIPE_PRICE_EXTRA_SEAT_BUSINESS_ANNUAL",
}

# Question-pool overage rides on any plan (enterprise pool size is agreed, so
# enterprise checkout omits it). Same per-question rate at either interval.
_QUESTION_OVERAGE_ENV = {
    "month": "STRIPE_PRICE_QUESTION_OVERAGE",
    "year": "STRIPE_PRICE_QUESTION_OVERAGE_ANNUAL",
}

_PLANS = frozenset({"team", "business", "enterprise"})
_INTERVALS = frozenset({"month", "year"})

# What each tier includes, from the proposal's tier table (5.4). Enterprise's
# question pool is agreed per customer and its seats are unlimited, so both
# resolve to None meaning "not metered" - enterprise checkout deliberately
# carries no overage line either. Free-tier questions are never billed: there
# is no subscription to meter them against.
_QUESTION_POOL = {"team": 500, "business": 2000, "enterprise": None}
_INCLUDED_SEATS = {"team": 5, "business": 15, "enterprise": None}

# Event names of the two meters created in Stripe. The overage prices carry
# `recurring.meter`, so an event under these names is what ends up invoiced -
# they are contract, not configuration, and are named after the usage they
# measure rather than after a price.
METER_QUESTION = "esg.question"
METER_EXTRA_SEATS = "esg.extra_seats"

# Invoice reasons that mark the opening of a billing period. A seat snapshot
# belongs at exactly those points: Stripe resets the meter's sum for the new
# period, so the count at the start of the period is the value that period
# should bill.
_PERIOD_OPENING_REASONS = frozenset({"subscription_create", "subscription_cycle"})

# Stripe subscription.status -> the status we store on the organisation.
_SUBSCRIPTION_STATUS = {
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "trialing": "trialing",
    "unpaid": "unpaid",
    "incomplete": "incomplete",
    "incomplete_expired": "canceled",
}


def _env(var):
    value = os.environ.get(var, "").strip()
    return value or None


def billing_enabled():
    return _env("STRIPE_SECRET_KEY") is not None and _env(
        "STRIPE_WEBHOOK_SECRET"
    ) is not None


def plan_from_price(price_id):
    """The plan an organisation is on, resolved from the subscription's base price.

    Returns None for add-on prices (extra seats, question overage) and for
    prices the environment has not been configured with. Unknown prices must not
    throw: a mismatched env is a configuration bug, not a reason to 500 webhooks.
    """
    if not price_id:
        return None
    for (_tier, _interval), env_var in _BASE_PRICE_ENV.items():
        if _env(env_var) == price_id:
            return _tier
    return None


def _iso_from_epoch(epoch_seconds):
    if not epoch_seconds:
        return None
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).isoformat()


def _object_data(obj):
    """Stripe's objects accept both attribute and subscript access; normalise."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        return obj.to_dict()
    except AttributeError:
        return {}


def _metadata_of(obj):
    data = _object_data(obj)
    return data.get("metadata") or {}


def _subscription_period_end(sub):
    """Renewal date of a subscription, as epoch seconds, or None.

    API version 2025-05-28.basil removed ``current_period_start`` /
    ``current_period_end`` from the subscription object and moved them onto
    each SubscriptionItem, so a current payload carries nothing usable at the
    top level (verified against a live subscription: the key is absent there
    and present on the item). The item list is expanded on every shape we see
    - retrieve, list and webhook bodies alike - so the source; the
    top-level key is still read first for payloads minted on older versions.
    All items on a subscription share the billing period, so the first one
    that carries a value answers.
    """
    return _subscription_period_field(sub, "current_period_end")


def _subscription_period_start(sub):
    """Opening date of a subscription's current period, or None.

    Same shape and same caveats as _subscription_period_end, and it matters
    for the same reason from the other end: the question pool is counted from
    here, so an organisation with no period start cannot be metered at all
    rather than being metered against a guessed window.
    """
    return _subscription_period_field(sub, "current_period_start")


def _subscription_period_field(sub, field):
    data = _object_data(sub)
    top_level = data.get(field)
    if top_level:
        return top_level
    items = data.get("items")
    if isinstance(items, dict):
        rows = items.get("data") or []
    elif isinstance(items, list):
        rows = items
    else:
        rows = []
    for row in rows:
        value = _object_data(row).get(field)
        if value:
            return value
    return None


def _org_for_event(app, event_data):
    return _org_for_data(app.state.user_store, event_data)


def _org_for_data(user_store, event_data):
    """Resolve which organisation a billing payload belongs to.

    Metadata wins (we set it at checkout), falling back to the customer id.
    Returns None when the organisation cannot be found: the event is acked and
    logged rather than 4xxed, so Stripe does not retry something we can never fix.
    """
    metadata = _metadata_of(event_data)
    org_id = metadata.get("organisation_id")
    customer_id = event_data.get("customer")
    if org_id:
        org = user_store.get_organisation(org_id)
    elif customer_id:
        org = user_store.get_organisation_by_stripe_customer(customer_id)
    else:
        org = None
    if org is None:
        logger.warning("billing event for unknown organisation (metadata=%s)",
                       org_id or customer_id)
        return None
    return org


def _subscription_billing_fields(sub, org):
    """The organisation columns one subscription payload implies.

    Shared by the webhook and by the retrieve path, which must never disagree
    about what a subscription says: a row repaired by hand that drifts from the
    next event to arrive would be repaired again, silently.
    """
    plan = None
    for item in sub.get("items", {}).get("data", []) or []:
        price_id = item.get("price", {}).get("id")
        plan = plan_from_price(price_id) or plan
        # quantity carried on the base line is the workspace count, which the
        # checkout sets. Not required to flip lifecycle, so it is read, not
        # forced.
    status = _SUBSCRIPTION_STATUS.get(sub.get("status"), sub.get("status"))
    billing_fields = dict(
        stripe_customer_id=sub.get("customer"),
        stripe_subscription_id=sub.get("id"),
        plan=plan or org.get("plan"),
        billing_status=status,
        current_period_end=_iso_from_epoch(_subscription_period_end(sub)),
    )
    # The pool is counted from the period opening, so it is stored on every
    # subscription event rather than only the first: a renewal moves the window
    # and an organisation must not carry last period's questions into this one.
    # Written only when the payload carries it, so a slimmed-down event cannot
    # blank a window we already know and silently stop the metering.
    period_start = _iso_from_epoch(_subscription_period_start(sub))
    if period_start:
        billing_fields["current_period_start"] = period_start
    return billing_fields


def _apply_subscription_event(app, event_type, sub, event_id):
    """Shared handling for subscription created / updated events."""
    org = _org_for_event(app, sub)
    if org is None or org.get("system_owned"):
        return None
    app.state.user_store.set_organisation_billing(
        org["id"], **_subscription_billing_fields(sub, org)
    )


def refresh_subscription_from_stripe(user_store, subscription_id):
    """Write an organisation's billing columns from Stripe's own record.

    The webhook is the writer this stands in for, and it is for the row that
    never saw one: an organisation seeded by hand, or restored from a copy,
    whose stored period does not match the account. Without the period opening
    the question meter has no window to count from, so usage is silently not
    reported rather than reported wrongly. Stripe is only read - nothing is
    created, cancelled or adjusted there. Returns the updated organisation.
    """
    if not billing_enabled():
        raise BillingUnconfigured("Billing is not configured in this environment")
    if not subscription_id:
        raise InvalidBillingRequest("No subscription recorded for this organisation")
    import stripe

    stripe.api_key = _env("STRIPE_SECRET_KEY")
    try:
        sub = _object_data(stripe.Subscription.retrieve(subscription_id))
    except Exception as exc:
        if _is_stripe_error(exc):
            raise BillingUpstreamError(
                f"Stripe refused the subscription read: {exc}"
            ) from exc
        raise
    org = _org_for_data(user_store, sub)
    if org is None or org.get("system_owned"):
        raise InvalidBillingRequest(
            "No organisation owns that subscription; check the customer id "
            "on the account."
        )
    return user_store.set_organisation_billing(
        org["id"], **_subscription_billing_fields(sub, org)
    )


def _apply_checkout_event(app, event_type, session, event_id):
    org = _org_for_event(app, session)
    if org is None or org.get("system_owned"):
        return None
    app.state.user_store.set_organisation_billing(
        org["id"],
        stripe_customer_id=session.get("customer"),
        stripe_subscription_id=session.get("subscription"),
        # A completed checkout means a payment was taken; activation is the
        # safe, immediate reading (per the proposal, seats activate at payment
        # confirmation). The subscription event refines the details.
        billing_status="active",
    )


def _apply_invoice_event(app, payment_succeeded, invoice, event_id):
    org = _org_for_event(app, invoice)
    if org is None or org.get("system_owned"):
        return None
    billing_fields = dict(billing_status="active" if payment_succeeded else "past_due")
    if payment_succeeded and invoice.get("billing_reason") in _PERIOD_OPENING_REASONS:
        # The invoice covers the period that is opening, so its window is the
        # window the pool is counted over. Written even when a subscription
        # event already said it: this is the event that means a period really
        # started and was paid for, and the two are read from the same source.
        period_start = _iso_from_epoch(invoice.get("period_start"))
        period_end = _iso_from_epoch(invoice.get("period_end"))
        if period_start:
            billing_fields["current_period_start"] = period_start
        if period_end:
            billing_fields["current_period_end"] = period_end
    updated = app.state.user_store.set_organisation_billing(org["id"], **billing_fields)
    if payment_succeeded and invoice.get("billing_reason") in _PERIOD_OPENING_REASONS:
        # Stripe resets the meter's sum when a period opens, so the seat count
        # at this moment is the one that period should bill: a snapshot here is
        # correct whatever changed during the last period, and it needs no
        # membership hook to be right at renewal.
        sync_extra_seats(
            app.state.user_store, updated or org, snapshot=True, hint=event_id
        )


def _apply_subscription_deleted(app, event_type, sub, event_id):
    org = _org_for_event(app, sub)
    if org is None or org.get("system_owned"):
        return None
    app.state.user_store.set_organisation_billing(
        org["id"], billing_status="canceled"
    )


_HANDLERS = {
    "checkout.session.completed": _apply_checkout_event,
    "customer.subscription.created": _apply_subscription_event,
    "customer.subscription.updated": _apply_subscription_event,
    "customer.subscription.deleted": _apply_subscription_deleted,
    "invoice.paid": lambda app, t, i, e: _apply_invoice_event(app, True, i, e),
    "invoice.payment_failed": lambda app, t, i, e: _apply_invoice_event(app, False, i, e),
}


def register_billing_routes(app, user_store, checkout_dep=None, usage_dep=None):
    """Mount the billing endpoints on the application.

    POST /billing/webhook deliberately carries no API-key or rate-limit
    dependencies: authenticity comes from the Stripe signature, and Stripe
    retries need a stable 2xx contract rather than a per-IP throttle.

    POST /billing/checkout is owner-only (plan purchase is an owner decision
    per the pricing proposal), so it receives a FastAPI dependency that yields
    (organisation, caller) for the authenticated owner. POST /billing/portal
    rides on the same dependency for the same reason.

    GET /billing/usage takes a second, looser dependency: allowances are
    something every member should be able to see, including in an organisation
    that has no owner at all, so it is authenticated but not role-gated.
    """

    try:
        import stripe
    except ImportError:  # not a declared dependency yet - degrade gracefully
        stripe = None

    @app.post("/billing/webhook")
    async def billing_webhook(request: Request):
        secret = _env("STRIPE_WEBHOOK_SECRET")
        if stripe is None or not secret:
            raise HTTPException(status_code=503, detail="Billing is not configured")

        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        try:
            event = stripe.Webhook.construct_event(payload, signature, secret)
        except ValueError:  # malformed json
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.SignatureVerificationError:
            raise HTTPException(status_code=400, detail="Invalid signature")

        event_type = event.type
        event_id = event.id
        store = request.app.state.user_store
        if store.has_billing_event(event_id):
            # Delivered more than once (Stripe retries, our own worker retries,
            # a replay test). Already applied, so ack without re-applying.
            return {"received": True, "duplicate": True}

        handler = _HANDLERS.get(event_type)
        if handler is not None:
            data = _object_data(event.data.object)
            handler(request.app, event_type, data, event_id)

        detail = json.dumps({"type": event_type, "id": event_id})[:2000]
        store.record_billing_event(event_id, event_type, None, detail)
        return {"received": True}

    if checkout_dep is not None:
        _register_checkout(app, user_store, checkout_dep)
        _register_portal(app, user_store, checkout_dep)
    if usage_dep is not None:
        _register_usage(app, user_store, usage_dep)


class BillingUnconfigured(Exception):
    """Billing is not set up in this environment (no secret key / prices)."""


class InvalidBillingRequest(Exception):
    """Valid HTTP request, but not something Stripe will build us a session for."""


class BillingUpstreamError(Exception):
    """Stripe itself refused the call: a deleted customer, a network failure.

    Distinct from InvalidBillingRequest because no change to the request would
    help, so the caller is told the platform is at fault rather than blamed.
    """


class CheckoutRequest(BaseModel):
    plan: str
    interval: str = "month"
    # The number of workspaces to bill. Defaults to the organisation's current
    # workspace count; a caller may override it when provisioning in the same
    # session as the workspaces.
    workspaces: int | None = None
    success_url: str | None = None
    cancel_url: str | None = None


class PortalRequest(BaseModel):
    return_url: str | None = None


def _base_price_for(plan, interval):
    env_var = _BASE_PRICE_ENV.get((plan, interval))
    return _env(env_var) if env_var else None


def build_checkout_session(user_store, org, caller, *, plan, interval,
                           workspaces=None, success_url=None, cancel_url=None):
    """Create a Stripe Checkout Session for a subscription and return its URL.

    The organisation is billed per workspace (proposal Q8/Q20): one base line at
    quantity = workspace count, plus the tier's metered extra-seat line and the
    question-overage line. Metadata carries the organisation id so the webhook
    can attribute the resulting subscription even though Checkout's customer
    object is what Stripe passes back on most events.
    """
    try:
        import stripe
    except ImportError:  # pragma: no cover - deps declared, guard kept for safety
        stripe = None
    secret_key = _env("STRIPE_SECRET_KEY")
    if stripe is None or not secret_key:
        raise BillingUnconfigured("Billing is not configured")
    stripe.api_key = secret_key

    plan = (plan or "").strip().lower()
    interval = (interval or "month").strip().lower()
    if plan not in _PLANS:
        raise InvalidBillingRequest("plan must be one of: team, business, enterprise")
    if interval not in _INTERVALS:
        raise InvalidBillingRequest("interval must be 'month' or 'year'")
    if plan == "enterprise" and interval != "year":
        raise InvalidBillingRequest("enterprise is an annual-commitment plan")

    base_price = _base_price_for(plan, interval)
    if base_price is None:
        raise BillingUnconfigured(f"no base price configured for {plan} {interval}")

    # Re-subscribing while a subscription (or half-dead one) is still attached
    # would stack invoices. Point the owner at reactivation instead.
    if org.get("stripe_subscription_id") and org.get("billing_status") not in (
        None, "canceled",
    ):
        raise InvalidBillingRequest(
            "This organisation already has a subscription. Use Stripe's "
            "customer portal to manage it."
        )

    computed = user_store.organisation_workspace_categories(org["id"])
    quantity = int(workspaces) if workspaces else (len(computed) or 1)
    if quantity < 1:
        raise InvalidBillingRequest("workspaces must be a positive number")

    customer_id = org.get("stripe_customer_id")
    metadata = {
        "organisation_id": org["id"],
        "plan": plan,
        "interval": interval,
        "workspaces": str(quantity),
    }
    if not customer_id:
        customer = stripe.Customer.create(
            email=caller.get("email"),
            metadata={"organisation_id": org["id"]},
        )
        customer_id = customer["id"]
        user_store.set_organisation_billing(org["id"], stripe_customer_id=customer_id)

    line_items = [{"price": base_price, "quantity": quantity}]
    extra_seat_var = _EXTRA_SEAT_PRICE_ENV.get((plan, interval))
    if extra_seat_var and _env(extra_seat_var):
        # Metered: no quantity. Each seat is reported through the meter.
        line_items.append({"price": _env(extra_seat_var)})
    overage_var = _QUESTION_OVERAGE_ENV.get(interval)
    if plan != "enterprise" and overage_var and _env(overage_var):
        line_items.append({"price": _env(overage_var)})

    base_url = _env("PUBLIC_BASE_URL") or "http://localhost:8000"
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=line_items,
        metadata=metadata,
        subscription_data={"metadata": metadata},
        success_url=success_url or f"{base_url}/ui",
        cancel_url=cancel_url or f"{base_url}/ui",
        allow_promotion_codes=False,
    )
    return session["url"], session["id"]


def _is_stripe_error(exc):
    """Whether an exception is Stripe's own, when the SDK is installed.

    Resolved lazily rather than at import so a missing SDK degrades to "not a
    Stripe error" instead of an ImportError at module load, matching how the
    rest of this file treats an undeclared dependency.
    """
    try:
        import stripe
    except ImportError:
        return False
    stripe_error = getattr(stripe, "StripeError", None)
    return isinstance(exc, stripe_error) if stripe_error else False


def build_portal_session(org, *, return_url=None):
    """Open Stripe's customer portal for an organisation and return its URL.

    This is where a payment method is replaced, an unpaid invoice is retried,
    and a subscription's card details are managed — all of it Stripe-side,
    which is the point: the subscription is theirs, so the controls for it are
    too. It needs the customer we created at checkout, so an organisation that
    has never paid has nowhere to go and is told so rather than sent to a
    portal that would show Stripe an error.
    """
    try:
        import stripe
    except ImportError:  # pragma: no cover - deps declared, guard kept for safety
        stripe = None
    secret_key = _env("STRIPE_SECRET_KEY")
    if stripe is None or not secret_key:
        raise BillingUnconfigured("Billing is not configured")
    stripe.api_key = secret_key

    customer_id = org.get("stripe_customer_id")
    if not customer_id:
        raise InvalidBillingRequest(
            "This organisation has no payment method on file yet. Choose a "
            "plan first, then manage it here."
        )

    base_url = _env("PUBLIC_BASE_URL") or "http://localhost:8000"
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url or f"{base_url}/ui",
        )
    except Exception as exc:
        if _is_stripe_error(exc):
            # The common case is a customer deleted from the dashboard, which
            # no retry of this request can fix.
            raise BillingUpstreamError(str(exc)) from exc
        raise
    return session["url"]


# ---- usage reporting -------------------------------------------------------
#
# What the application owes Stripe: the two things the proposal meters, asked
# questions past the pool and seats past the allowance, reported as they happen
# rather than reconstructed at invoice time. Stripe's meters reset their sum at
# each billing period, so a period-opening seat snapshot and one event per
# overage question together produce exactly the arrears the proposal describes.

def question_pool(plan):
    """Questions a plan includes per billing period, or None when unmetered."""
    return _QUESTION_POOL.get(plan) if plan else None


def included_seats(plan):
    """Users a plan includes per workspace, or None when seats are unlimited."""
    return _INCLUDED_SEATS.get(plan) if plan else None


def send_meter_event(event_name, customer_id, value, *, identifier=None):
    """Report one usage event to Stripe. Best-effort: False when it did not go.

    Billing must never break the request it is metering - a question already
    answered is not made unaskable by a Stripe timeout - so every failure is
    logged and reported as a quiet False. A caller that cares (the seat sync,
    which records what it managed to report) can then leave its own state
    unchanged and try again later.
    """
    if not customer_id or not value or int(value) <= 0:
        return False
    try:
        import stripe
    except ImportError:
        return False
    secret_key = _env("STRIPE_SECRET_KEY")
    if not secret_key:
        return False
    stripe.api_key = secret_key
    payload = {"stripe_customer_id": customer_id, "value": str(int(value))}
    try:
        stripe.billing.MeterEvent.create(
            event_name=event_name,
            payload=payload,
            identifier=identifier,
        )
    except Exception:
        logger.warning("meter event not reported (%s=%s)", event_name, int(value),
                       exc_info=True)
        return False
    return True


def report_question_usage(user_store, admin_store, organisation_id):
    """Report the question that took an organisation past its pool, if it did.

    Called after the question has been written to the ledger, so the count
    already includes it: the first question over the line reports 1 and every
    one after it reports 1, and the meter's sum is therefore the overage and
    nothing else. Questions inside the pool are never sent at all, which is
    what keeps an included question from being invoiced.
    """
    if not organisation_id or user_store is None or admin_store is None:
        return False
    org = user_store.get_organisation(organisation_id)
    if org is None or org.get("system_owned"):
        return False
    pool = question_pool(org.get("plan"))
    customer_id = org.get("stripe_customer_id")
    period_start = org.get("current_period_start")
    # Unmetered plans (enterprise, free, none), organisations that have never
    # paid, and organisations with no period window all answer "nothing to
    # report" - each for a different reason, none of them an error.
    if pool is None or not customer_id or not period_start:
        return False
    used = admin_store.questions_in_period(organisation_id, since=period_start)
    if used <= pool:
        return False
    return send_meter_event(
        METER_QUESTION,
        customer_id,
        1,
        identifier=f"q:{organisation_id}:{period_start}:{used}",
    )


def extra_seats_for(user_store, org):
    """Seats this organisation is over its per-workspace allowance by.

    The allowance is counted per workspace (Q8), so an organisation with two
    workspaces on Team gets ten included seats, and only a workspace that
    actually has people in it can be over the line. Enterprise includes
    everyone, so it answers 0 rather than asking the caller to special-case it.
    """
    included = included_seats(org.get("plan"))
    if included is None:
        return 0
    total = 0
    for row in user_store.organisation_workspace_seats(org["id"]):
        over = int(row.get("seats") or 0) - included
        if over > 0:
            total += over
    return total


def sync_extra_seats(user_store, org, *, snapshot=False, hint=None):
    """Report an organisation's extra seats to Stripe.

    Two shapes, because the meter sums and cannot be decremented:

    * at a period opening (`snapshot=True`) the sum is empty again, so the
      whole count is sent - this is what makes a seat dropped mid-period stop
      being billed, at the next renewal rather than immediately;
    * at any other time only the growth since the last report is sent, so a
      join is billed from when it happened and nothing is counted twice.

    `seats_reported` holds what has gone out for the current period; it is
    updated only on success so a failed call is retried rather than lost.
    Returns the value reported, or None when there was nothing to send.
    """
    if org is None or org.get("system_owned"):
        return None
    if not org.get("stripe_subscription_id") or not org.get("stripe_customer_id"):
        return None
    extra = extra_seats_for(user_store, org)
    reported = int(org.get("seats_reported") or 0)
    value = extra if snapshot else extra - reported
    if value <= 0:
        # Seats fell, or nothing changed. No event to send either way, but the
        # local figure still moves down so the next join is measured from the
        # count that is actually true rather than from last month's.
        if extra != reported:
            user_store.set_organisation_billing(org["id"], seats_reported=extra)
        return None
    identifier = "seats:{}:{}:{}".format(
        org["id"],
        org.get("current_period_end") or "open",
        hint or uuid4().hex[:12],
    )
    if not send_meter_event(
        METER_EXTRA_SEATS, org["stripe_customer_id"], value, identifier=identifier
    ):
        return None
    user_store.set_organisation_billing(org["id"], seats_reported=extra)
    return value


def usage_summary(user_store, admin_store, org):
    """What the owner needs to see about this organisation's allowances."""
    plan = org.get("plan")
    period_start = org.get("current_period_start")
    pool = question_pool(plan)
    used = (
        admin_store.questions_in_period(org["id"], since=period_start)
        if admin_store is not None and period_start
        else 0
    )
    by_workspace = user_store.organisation_workspace_seats(org["id"])
    included = included_seats(plan)
    return {
        "organisation_id": org["id"],
        "plan": plan,
        "billing_status": org.get("billing_status"),
        "period_start": period_start,
        "period_end": org.get("current_period_end"),
        "questions": {
            "included": pool,
            "used": used,
            "overage": max(0, used - pool) if pool is not None else 0,
            "metered": pool is not None and bool(org.get("stripe_customer_id")),
        },
        "seats": {
            "included_per_workspace": included,
            "total": sum(int(row.get("seats") or 0) for row in by_workspace),
            "extra": extra_seats_for(user_store, org),
            "reported": int(org.get("seats_reported") or 0),
            "by_workspace": by_workspace,
        },
        "reporting": {
            "configured": billing_enabled() and bool(org.get("stripe_customer_id")),
            "period_start_set": bool(period_start),
        },
    }


def _register_usage(app, user_store, usage_dep):
    """GET /billing/usage: this organisation's allowances and what it has used.

    Answers for any member rather than only the owner, because the question it
    answers - "how much of the pool is left" - is the one a team asks before it
    decides whether to keep going, and because an organisation that has fallen
    behind on payment still needs to see where it stands.
    """

    @app.get("/billing/usage")
    def billing_usage(ctx=Depends(usage_dep)):
        org, _caller = ctx
        return usage_summary(user_store, app.state.admin_store, org)


def _register_checkout(app, user_store, checkout_dep):
    """POST /billing/checkout: owner creates the subscription, gets a redirect URL."""

    @app.post("/billing/checkout")
    def billing_checkout(body: CheckoutRequest, ctx=Depends(checkout_dep)):
        org, caller = ctx
        try:
            url, session_id = build_checkout_session(
                user_store, org, caller,
                plan=body.plan, interval=body.interval,
                workspaces=body.workspaces,
                success_url=body.success_url, cancel_url=body.cancel_url,
            )
        except BillingUnconfigured:
            raise HTTPException(status_code=503, detail="Billing is not configured")
        except InvalidBillingRequest as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"session_id": session_id, "url": url}


def _register_portal(app, user_store, checkout_dep):
    """POST /billing/portal: the owner opens Stripe's customer portal.

    Owner-only for the same reason checkout is — it is a billing decision —
    and deliberately reachable whether the subscription is active, late or
    ended, because those are exactly the states a stuck payer needs it in.
    """

    @app.post("/billing/portal")
    def billing_portal(body: PortalRequest, ctx=Depends(checkout_dep)):
        org, caller = ctx
        try:
            url = build_portal_session(org, return_url=body.return_url)
        except BillingUnconfigured:
            raise HTTPException(status_code=503, detail="Billing is not configured")
        except InvalidBillingRequest as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except BillingUpstreamError:
            raise HTTPException(
                status_code=502,
                detail="Stripe could not open the billing portal. Try again shortly.",
            )
        return {"url": url}