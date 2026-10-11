# Solution workflow — ESG Workspace Chatbot

Status: current as of 2026-10-10. Branch `feat/organisation-scoping`.

This document is the end-to-end workflow of the shipped system: who the actors
are, what each one does, and how the pieces fit together. It reflects the
organisation, billing, free-tier and owner-console work, and supersedes the
billing/tenancy sketches in `doc/solution-workflow.txt` (the working decision
log, kept local). Where the two disagree, this file wins.

---

## 1. What the product is

A retrieval-augmented chat service over an organisation's own documents.
Each **workspace** (called a *category* internally — `finance`, `hr`, and any
customer-defined ones) has its own documents and its own vector index. A user
belongs to an **organisation**, is granted access to one or more workspaces, and
asks questions inside those workspaces. Every answer cites the source document,
and citations open the real file at the cited page.

The product is sold **per organisation**, not per individual: an organisation
buys a plan, that plan grants a number of workspaces, seats, and a monthly
question pool, and many employees consume it.

---

## 2. Actors and roles

| Actor | Identified by | Can do |
| --- | --- | --- |
| Platform admin | `API_KEY` bearer (`Authorization: Bearer …`) | Everything: create/delete organisations, move users, assign content, run migrations, see cross-customer usage and audit. |
| Organisation owner | `org_roles` row, role `owner` (exactly one) | The organisation console: analytics, members, seats, invites, billing (choose/change plan), transfer ownership. Can reach and chat in every workspace in the organisation. |
| Organisation admin | `org_roles` row, role `admin` | The organisation console except billing and ownership transfer. Can reach and chat in every workspace in the organisation. |
| Seat (member) | any user in the organisation | Chat, search, voice, download cited documents inside their granted workspaces. |
| Free-tier account | user in the system sample organisation `_sample` | The sample workspace, verified email, capped questions (see §5). |

An organisation is either **system-owned** (`_sample`, `_default` — no owner, not
a customer, never billed) or a **customer organisation**. Roles live only in
`org_roles`; the owner is singular and is enforced in code, not by a unique
index. The earliest member of a customer organisation becomes its owner via the
startup `backfill_org_roles` if no owner was set.

---

## 3. End-to-end journeys

### 3.1 Free signup → cap → upgrade

1. A visitor signs up (`POST /auth/signup`) with no invite and lands in `_sample`.
   Signup also records the email for confirmation; the composer stays shut until
   the address is confirmed (`EMAIL_VERIFICATION_REQUIRED`, default on).
2. While unverified, only `/me`, verification resend, billing, and password
   recovery endpoints are reachable. Verification unlocks content. Password
   recovery is available from the start: the login box offers **Forgot
   password?** (`POST /auth/forgot-password`), and the emailed link drives the
   `/reset-password` page (`POST /auth/reset-password`) to a fresh session.
   Pre-reset sessions are invalidated, and reset links are single-use and
   expire after 2 hours.
3. Once verified, the account asks questions against the sample workspace. Each
   answered question is counted against the **free-tier question cap** (§5).
4. `/me` reports `free_tier {cap, used, remaining, limited, reached}` so the UI
   shows remaining questions and warns before the wall.
5. At the cap, further questions are refused with HTTP **402**; the chat UI
   shows an **Upgrade now** button that jumps to the upgrade card, and the
   header badge also opens it. Small talk still works (no model).
6. **Self-serve upgrade:** the cappable free account can go straight to Stripe
   from the UI (`POST /billing/upgrade`) — no platform admin needed. The new
   organisation (`org-<user_id>`) and ownership are created only after
   checkout completes (webhook or `POST /billing/sync` on return), so an
   abandoned checkout leaves the account on the free tier. After payment, the
   UI re-confirms and reports the move to the new paid organisation.

The free tier is also bounded at the *account* level by `MAX_FREE_ACCOUNTS`
(default 500): self-registration closes when the sample organisation is full.

### 3.2 Organisation owner — buy a plan

1. The owner opens the organisation console at **`/owner`**.
2. On **Billing**, the plan catalog (`GET /org/plans`) shows Team, Business and
   Enterprise with what each includes, and which are purchasable in this
   deployment.
3. Choosing a plan calls `POST /billing/checkout`, which creates a Stripe
   Checkout Session (one base line at quantity = workspace count, plus metered
   extra-seat and question-overage lines) and redirects to Stripe.
4. Stripe's webhook (`POST /billing/webhook`) writes the subscription onto the
   organisation: `plan`, `billing_status`, `stripe_customer_id`,
   `stripe_subscription_id`, period window.
5. While the subscription is live (`active`/`trialing`, or `past_due` with a
   notice), the workspaces are usable. `canceled`/`unpaid` stop service with 402.

If the webhook has not been delivered (no forwarder running locally), a platform
admin can reconcile from Stripe with
`POST /admin/organisations/{id}/billing/refresh`.

### 3.3 Organisation owner — change or cancel

The **Manage / change plan** button calls `POST /billing/portal`, which opens
Stripe's customer portal: change plan, replace the card, retry an invoice, or
cancel. Plan switching is enabled by a portal configuration (named
`esg-plan-switcher` or set via `STRIPE_PORTAL_CONFIGURATION_ID`) so a subscriber
can move between Team and Business without leaving Stripe. Enterprise remains a
direct purchase or quote path. A never-paid organisation has no customer to
send, so the console hides the button and points at choosing a plan instead.

### 3.4 Organisation manager — people and seats

On **People**, an owner or admin can:

- add a seat (`POST /org/members`) — creates a verified account in the
  organisation;
- change a member's organisation role (`POST /org/members/{id}/role`);
- remove a seat (`DELETE /org/members/{id}`);
- adjust workspace access (`POST /org/members/{id}/workspaces`);
- mint a shareable invite (`POST /org/invites`) scoped to the organisation and a
  workspace, and list existing ones (`GET /org/invites`);
- transfer ownership (`POST /org/transfer-ownership`, owner only);
- reset a member's password (`POST /org/members/{id}/reset-password`),
  which also signs that member's other sessions out.

Every add/remove/access change re-reports extra seats to Stripe
(`refresh_seat_usage`), so billing tracks the live seat count.

### 3.5 Seat — daily use

Sign in, switch workspace, ask a question (`POST /chat` / `POST /chat/stream`),
read the answer with citations, click a citation to download the source, or use
retrieval-only search and voice. Sessions, transcripts, and citations persist.

### 3.6 Workspace reach and switching

The chat answers from the account's **active** workspace. What an account may
reach depends on its role:

- a **seat** reaches the workspaces they are explicitly granted (`workspace_memberships`). An org member can belong to **multiple workspaces** simultaneously, and may switch between them from the chat header. Their active workspace determines which documents the bot retrieves. 
- an **owner or admin** reaches every workspace that belongs to the
  organisation — the workspaces any member holds a seat in, plus the ones the
  organisation has content indexed under. Managing a workspace and entering it
  are one permission, so a manager is never locked out of what they administer.
  This reach is computed, not stored: switching to a workspace the manager does
  not hold a seat in deliberately does **not** create a membership, or every
  workspace an owner visited would be billed as an extra seat.

`GET /me` reports the reachable set. When there is more than one, the chat
header shows a **workspace switcher**; changing it calls `POST /me/workspace`,
which re-points the active workspace after checking the target against the
caller's reachable set (a crafted request cannot point an account at another
organisation's content). Reachable workspaces are read live on every request, so
a grant or revocation takes effect without a new sign-in.

---

## 4. Organisation console (`/owner`)

The owner/admin console is the org-scoped twin of the platform admin console.
It is a single page with four tabs; **Chat is a link back to `/ui`**, not a tab
of its own, so the console answers "how is the organisation doing" and hands the
reader back to the chat product.

| Tab | Source | Shows |
| --- | --- | --- |
| **Overview** | `GET /org/overview` | Totals (workspaces, members, indexed chunks, questions used vs pool); per-workspace content and index status; subscription summary with a usage meter. |
| **Analytics** | `GET /org/usage?range=` | Token/cost analytics scoped to the organisation: LLL calls, tokens in/out, estimated cost, per-period chart, by workspace, by call type. |
| **People** | `GET /org/members`, `/org/invites` | Members with roles and workspace access; add seat; invites. |
| **Billing** | `/billing/usage`, `/org/plans` | Current plan, status, period; question pool meter; seats per workspace; plan catalog with choose/manage actions. |

Access is manager-gated on every call (`owner`/`admin` only) and the
organisation is always taken from the caller's own account — no route accepts an
`organisation_id`, so one organisation cannot address another's data.

---

## 5. Free-tier question cap

The free tier has no owner and no billing period, so its ceiling is **per
account**, the twin of the account-count cap:

- Variable: `FREE_TIER_QUESTION_CAP` (default **50**). `0` or negative means no
  limit; a non-numeric value falls back to the default.
- Measured in **answered questions** (`usage` rows of kind `question`, keyed by
  `user_id`), not raw LLM tokens: small talk and search do not count. A single
  answer that rewrites and then answers is still one question.
- Enforced in `/chat` and `/chat/stream` only once a question is known to need
  the model, and answered with **402** and an upgrade message.
- Only accounts in `_sample` are subject to it; any seat in a customer
  organisation is governed by that organisation's plan instead.

---

## 6. Adoption and entitlements

Plan tiers (from the pricing proposal's table):

| Plan | Question pool / period | Seats / workspace | Notes |
| --- | --- | --- | --- |
| Team | 500 | 5 | monthly or annual |
| Business | 2,000 | 15 | monthly or annual |
| Enterprise | agreed | unlimited | annual commitment only |

- **Per workspace billing.** The base fee is charged at quantity = the number of
  workspaces the organisation has provisioned. A workspace with no seats and no
  content is not provisioned and is not charged.
- **Extra seats**, past the per-workspace allowance, are reported through the
  `esg.extra_seats` meter as they are added; a period-opening snapshot catches
  anything missed.
- **Question overage**, past the pool, is reported one event per question
  through `esg.question` as it crosses the pool.

---

## 7. Content, indexing and tenancy

- Content is **per organisation** and **per workspace**. Served copies live
  under `data/organisations/<org>/<workspace>/`; indexes live under
  `data/.index/organisations/<org>/vectors_<workspace>.sqlite3`.
- Customers do not upload: a platform admin assigns library documents to
  organisations/workspaces, which materialises the served copies. There is no
  customer-side upload path (a deliberate trust boundary).
- Indexing is incremental and hash-keyed; re-indexing is idempotent. The legacy
  single-tenant global tree (`data/<workspace>/`, `.index/vectors_*.sqlite3`)
  is retired for org deployments and only reconstructed when it actually holds
  files.
- Workspaces are physically isolated per organisation, so a retrieval mistake
  cannot surface one customer's documents to another.

---

## 8. Citations, search, voice

- Answers carry structured citations; each opens the real document through
  `GET /documents/download` with a short-lived signed token, scoped to the
  caller's workspaces and audited.
- Page citations deep-link into PDFs at the cited page.
- Search (`POST /search`) is retrieval-only, no LLM.
- Voice: browser recording → local faster-whisper STT → transcript review →
  send; TTS reads answers aloud. Speech is bundled, not separately metered.

---

## 9. Platform admin console (`/admin`)

API-key gated (plus optional Basic/TOTP layers). Tabs: Overview, Organisations,
Documents, Workspaces, Users, Audit, and **Usage & Cost** (cross-customer
tokens, estimated spend, per-workspace/kind/user breakdowns, CSV export). This
is where organisations are created, users moved between them, content assigned,
and legacy migrations run.

---

## 10. API surface added for organisations and billing

Public/account:

- `POST /auth/signup`, `/auth/login`, `/auth/verify-email`,
  `/auth/resend-verification`
- `POST /auth/forgot-password` — emails a reset link (generic answer, no
  account probing)
- `POST /auth/reset-password` — resolves a reset link, signs in with a fresh
  session; `/reset-password` serves the reset page
- `POST /me/password` — change own password, rotates the session token
- `GET /me` — now includes `organisation` and `free_tier`
- `POST /me/workspace` — switch the account's active workspace
- `POST /chat`, `/chat/stream`, `/search`, `/voice/stt`, `/voice/tts`
- `GET /sessions`, `/sessions/{id}`, `DELETE /sessions/{id}`

Organisation console:

- `GET /org/overview`, `GET /org/usage`, `GET /org/content`, `GET /org/plans`
- `GET/POST /org/members`, `POST /org/members/{id}/role`,
  `DELETE /org/members/{id}`, `POST /org/members/{id}/workspaces`,
  `POST /org/members/{id}/reset-password` (manager resets a member's password)
- `GET/POST /org/invites`, `POST /org/transfer-ownership`

Billing:

- `POST /billing/checkout` (owner), `POST /billing/portal` (owner),
  `POST /billing/upgrade` (free account, self-serve),
  `POST /billing/sync` (re-confirm checkout on return),
  `GET /billing/usage` (any member), `POST /billing/webhook` (Stripe-signed)

Owner/admin web pages: `/ui` (chat), `/owner` (organisation console),
`/admin` (platform console).

A Postman collection generated from the live OpenAPI schema lives in
`postman/` (see `postman/README.md`).

---

## 11. Environment variables (billing, free tier, tenancy)

| Variable | Purpose |
| --- | --- |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe API and webhook verification. |
| `STRIPE_PORTAL_CONFIGURATION_ID` | Optional pinned portal config id that enables plan switching; if unset and the account's default config already switches, that is used, otherwise one named `esg-plan-switcher` is created. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Real mail relay for signup verification and password-reset emails. When unset, messages are written to the admin outbox (viewed under **Admin → Emails**) and reset/verification links are echoed back to the browser for local development. |
| `STRIPE_PRICE_TEAM_MONTHLY` / `..._ANNUAL`, `STRIPE_PRICE_BUSINESS_*`, `STRIPE_PRICE_ENTERPRISE_ANNUAL` | Base per-workspace prices; presence makes a tier purchasable. |
| `STRIPE_PRICE_EXTRA_SEAT_*`, `STRIPE_PRICE_QUESTION_OVERAGE*` | Metered add-on prices. |
| `MAX_FREE_ACCOUNTS` | Cap on self-service free accounts. |
| `FREE_TIER_QUESTION_CAP` | Per-account free question cap (this release). |
| `EMAIL_VERIFICATION_REQUIRED` | Whether the free path requires a confirmed address. |

---

## 12. Deployment note

Locally, Stripe webhooks do not arrive unless a forwarder is running. Start one
with:

```bash
stripe listen --forward-to localhost:8000/billing/webhook
```

Without it, subscriptions do not activate automatically; reconcile from Stripe
with `POST /admin/organisations/{id}/billing/refresh`.

---

## 13. Open items

1. Confirm with Stripe whether India's 18% IGST is inside Managed Payments scope
   (advisory, not blocking — the buyer is the client's HR/compliance team, a
   business, so B2B reverse charge applies).
2. Settlement vs. subscription for larger B2B accounts (quote/invoice path).
3. Trial handling: `trialing` is currently treated as usable; a trial end date
   and reminder are not yet surfaced in the console.
4. Email delivery: without `SMTP_*` configured, messages only land in the admin
   outbox and nothing drains it on a schedule (manual view/resend via Admin).
   A worker or crontab consumer is a natural next step.
