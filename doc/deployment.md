# Deployment runbook — Azure (App Service, container)

This is the operator's guide to running the ESG Workspace Chatbot in Azure.
It is written for a first Azure deployment and assumes you can copy/paste into
**Azure Cloud Shell** (`https://shell.azure.com`) or a terminal with the Azure
CLI (`az`) installed. Nothing here contains secrets — every secret is a
placeholder you replace.

> Companion docs: `README.md` (features, API), `doc/solution-workflow.md`
> (product workflow), `doc/env_example.txt` (every environment variable).

---

## 0. How the pieces fit

```
GitHub main push
  └─ CI (tests + eval)            .github/workflows/ci.yml
  └─ Container                    .github/workflows/container.yml
        builds the image, pushes ghcr.io/ridwanlekan/esg-related-chatbot:latest
          └─ Deploy               .github/workflows/deploy.yml
                OIDC login → sets app settings → pulls :latest into Azure Web App
                                                              │
                                    Azure App Service (Linux container)
                                      /home/esg-index   (vector + sessions + admin DB)
                                      /home/esg-data    (documents)
                                      /home/esg-docling-vet
```

- The image already contains the models (embeddings, reranker, whisper, Kokoro),
  so the container needs no runtime downloads.
- **Data is not in the image** — `/home/esg-data` and `/home/esg-index` are
  created empty on first boot. You seed them (§5) or ship your existing data and
  chat history wholesale (§5b).
- `Deploy` is inert until the repo variables in §1 are set.

---

## 1. GitHub setup (do this first)

### 1.1 Create the `production` environment
Repo → **Settings → Environments → New environment** → name it **`production`**.
(Optional: add required reviewers so a deploy waits for approval.)

- Add **Secrets** (Environment secrets on `production`):
  - `AZURE_CLIENT_ID`
  - `AZURE_TENANT_ID`
  - `AZURE_SUBSCRIPTION_ID`
- Add **Variables** (repository → Settings → Variables → Actions):
  | Variable | Required | Example |
  | --- | --- | --- |
  | `AZURE_WEBAPP_NAME` | yes | `esg-chatbot-demo` |
  - `AZURE_RESOURCE_GROUP` (optional but recommended; when set, the workflow
    also writes the storage/proxy settings) → e.g. `esg-rg`
  - Optional non-secret app settings the workflow will sync when set:
    `MODEL_NAME`, `OPENAI_API_VERSION`, `LOG_LEVEL`, `VOICE_TTS_VOICE`,
    `CORS_ORIGINS`.

> `deploy.yml` uses `environment: production`, so the OIDC token's subject is
> `repo:Ridwanlekan/esg-related-chatbot:environment:production`. The federated
> credential you create in §2.2 must match that subject (or use a branch
> subject such as `repo:Ridwanlekan/esg-related-chatbot:ref:refs/heads/main`).

---

## 1b. Choose the host

| | Azure App Service (this runbook) | Azure Container Apps |
| --- | --- | --- |
| Repo support | `deploy.yml` already wired | new workflow needed |
| Scale-to-zero | no | yes |
| Secrets | app settings / Key Vault | native secrets / Key Vault |
| Revisions/rollback | slot swap (needs extra setup) | built-in revisions |
| Best for | first deployment, simplest | scale + cheaper idle |

The rest of this runbook is the **App Service** path. Container Apps is a good
follow-up once you are comfortable; the app itself is unchanged.

**Keep it to ONE instance.** Sessions, the vector index and the admin/usage
store are SQLite files on `/home`. Two instances sharing them will corrupt them.
Scale **up** (bigger SKU), not **out**, until you move to Postgres.

---

## 1c. Quick throwaway test (B2, delete the same day)

For a smoke test you don't need the OIDC/GitHub wiring yet. Two helper scripts
provision a **B2** Linux Web App running the published image, apply the app
settings and wait for `/health`, then delete the whole resource group when you
are done. B2 is billed per hour (~US$0.03/hr), so a same-day test is well under
a dollar.

```bash
# One-time: sign in (skip if already signed in)
az login

# From the repo root, load the app credentials. Your .env may use "KEY = value"
# (spaces around '='), which bash cannot source — normalise it once first:
sed -i.bak -E 's/^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]+=/\1=/' .env
set -a; . ./.env; set +a

scripts/azure-test-up.sh      # B2 by default; provisions + deploys + waits for /health
# ... open the printed https://<app>.azurewebsites.net URL, seed data via /admin ...

RG=esg-chatbot-test-rg scripts/azure-test-down.sh   # deletes the whole resource group
```

> `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `OPENAI_API_VERSION` and
> `MODEL_NAME` all come from `.env`. The script defaults to **B2** and model
> `gpt-4.1-mini` / API version `2025-04-14` if those are not set.

Override any default with env vars: `RG`, `APP`, `LOC`, `PLAN`, `SKU`, `IMAGE`,
`MODEL_NAME`, `OPENAI_API_VERSION`; set `GHCR_USER`/`GHCR_TOKEN` if the package
is private. The scripts are the fast path — §2 onward is the production path
(OIDC, least-privilege identity, persistent settings).

> **Free (F1) is not enough for this image.** F1 gives 1 GB RAM / 60 CPU-min per
> day; the container loads torch + docling + the embedding/reranker models (plus
> ~840 MB of voice weights), so it OOMs. Use **B1** (1.75 GB, light corpora) or
> **B2** (3.5 GB, safe default): `SKU=B1 scripts/azure-test-up.sh`.

---

## 1d. Acceptance test suite

`scripts/azure-test-suite.sh` is a black-box acceptance run against a *running*
deployment. It needs no cloud credentials — just the app URL and the admin API
key — so it works equally on the throwaway app from §1c and on production.

```bash
BASE=https://<app>.azurewebsites.net \
API_KEY=<admin API key> \
bash scripts/azure-test-suite.sh
```

It prints a PASS/FAIL/SKIP/INFO table and exits non-zero if anything fails.
Requires `curl`, `jq`, `xargs` (and `python3` for the voice tests).

| Section | Covers |
| --- | --- |
| A | liveness, `/metrics`, 404s, security headers |
| B | admin console + admin API (workspace CRUD, built-in protection) |
| C | free-tier self-service: signup, `/me`, verify, login, password reset |
| D | tenant setup: create an org + its verified owner via the admin API, assign and ingest a document |
| E | retrieval & chat **as that organisation's owner** (chat, search, stream, sessions, multilingual, cross-tenant isolation) |
| F | document link minting, token download, tampered-token rejection |
| G | voice STT/TTS and non-audio rejection |
| H | organisations, usage and billing endpoints (informational) |
| I | security: unauthorized access, prompt-injection echo, rate limiting |
| J | latency, concurrency, restart persistence, OOM |

It creates a throwaway custom workspace, an organisation, an org owner, and a
free-tier user, then **deletes all of them** at the end (`KEEP=1` to leave them).

| Env var | Effect |
| --- | --- |
| `CAT` | workspace/category to exercise (default `finance`) |
| `SKIP_VOICE=1` | skip section G (memory-heavy) |
| `RUN_LOAD=1` | enable the concurrent-chat burst in J |
| `RUN_RESTART=1 RG=<rg> APP=<app>` | add an `az webapp restart` persistence check |
| `KEEP=1` | do not delete the throwaway test objects |

> **Rate limiting (I5) only trips when the request IP is stable.** The limiter
> keys on the client IP from the proxy header, so the test pins a fixed
> `X-Forwarded-For`. This works only because the app trusts forwarded headers
> (`TRUSTED_PROXY_IPS=*`, set by `azure-test-up.sh`); without that — or behind a
> proxy that does not set a stable client IP — per-IP limiting silently does
> nothing. Expect a few `INFO`/`SKIP` rows (options above); a clean run is
> **0 FAIL**.

---

## 2. Provision Azure

Run these in Cloud Shell. Replace `<...>` placeholders.

### 2.1 Resource group, plan, web app

```bash
RG=esg-rg
APP=<globally-unique-app-name>        # e.g. esg-workspace-chatbot
LOC=southeastasia                    # pick a region near your users
IMAGE=ghcr.io/ridwanlekan/esg-related-chatbot:latest

az group create -n "$RG" -l "$LOC"

# Linux plan. B2 is a fine start; move up if you see slow cold starts.
az appservice plan create -g "$RG" -n esg-plan --is-linux --sku B2

# Web App for Containers, from the public GHCR image
az webapp create -g "$RG" -p esg-plan -n "$APP" \
  --deployment-container-image-name "$IMAGE"
```

**If the GHCR package is private**, either make it public (GitHub → Packages →
the package → Package settings → Change visibility) or give the Web App
credentials:

```bash
az webapp config container set -g "$RG" -n "$APP" \
  --docker-custom-image-name "$IMAGE" \
  --docker-registry-server-url https://ghcr.io \
  --docker-registry-server-user <github-username> \
  --docker-registry-server-password <PAT-with-read:packages>
```

### 2.2 GitHub Actions OIDC identity (no stored cloud passwords)

```bash
SUB=$(az account show --query id -o tsv)

# App registration + service principal
az ad app create --display-name "esg-github-deploy"
CLIENT_ID=$(az ad app list --display-name esg-github-deploy --query "[0].appId" -o tsv)
az ad sp create --id "$SUB" 2>/dev/null || true

# Role: Contributor on just this resource group (least privilege)
az role assignment create --assignee "$CLIENT_ID" \
  --role Contributor \
  --scope "/subscriptions/$SUB/resourceGroups/$RG"

echo "AZURE_CLIENT_ID=$CLIENT_ID"
az account show --query tenantId -o tsv   # AZURE_TENANT_ID
```

Add the **federated credential** (this is what lets GitHub log in without a
password — update the repo/owner if you forked):

```bash
az ad app federated-credential create --id "$(az ad app list --display-name esg-deploy-identity --query '[0].appId' -o tsv 2>/dev/null || echo $CLIENT_ID)" \
  --parameters '{
    "name": "github-production",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:Ridwanlekan/esg-related-chatbot:environment:production",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

Put `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` into the
**production** environment secrets from §0.

### 2.2a Allow the web app to pull the image (private package only)

Deployment Center → Registry settings → `https://ghcr.io`, user = GitHub
username, password = PAT with `read:packages`. (Skip if the package is public.)

---

## 3. Application settings and secrets

`deploy.yml` sets the **platform** settings and any non-secret values you put in
repo variables. **Secrets are deliberately not in the repo** — set them once as
App Service app settings (or, better, Key Vault references; see §8).

```bash
az webapp config appsettings set -g "$RG" -n "$APP" --settings \
  AZURE_OPENAI_API_KEY='<key>' \
  AZURE_OPENAI_ENDPOINT='<https://<resource>.openai.azure.com>' \
  OPENAI_API_VERSION='<version>' \
  MODEL_NAME='<deployment name, e.g. gpt-4.1-mini>' \
  TOKENIZERS_PARALLELISM=False \
  AUTH_SECRET='<random 32+ chars>' \
  API_KEY='<random admin key>' \
  ADMIN_BASIC_USER='admin' \
  ADMIN_BASIC_PASS='<strong password>' \
  ADMIN_TOTP_SECRET='<base32 TOTP secret>' \
  ADMIN_SESSION_SECURE=1

# Only for the tiers you actually sell (Stripe):
az webapp config appsettings set -g "$RG" -n "$APP" --settings \
  STRIPE_SECRET_KEY='sk_...' \
  STRIPE_WEBHOOK_SECRET='whsec_...' \
  STRIPE_PRICE_TEAM_MONTHLY='price_...' \
  STRIPE_PRICE_TEAM_ANNUAL='price_...' \
  STRIPE_PRICE_BUSINESS_MONTHLY='price_...' \
  STRIPE_PRICE_BUSINESS_ANNUAL='price_...' \
  STRIPE_PRICE_ENTERPRISE_ANNUAL='price_...'

# Optional email (otherwise verification/reset mail lands in the admin outbox):
az webapp config appsettings set -g "$RG" -n "$APP" --settings \
  SMTP_HOST='smtp.yourprovider.com' SMTP_PORT=587 \
  SMTP_USER='<user>' SMTP_PASSWORD='<pass>' \
  SMTP_FROM='ESG Assistant <no-reply@yourdomain.com>'
```

Generate secrets with `openssl rand -base64 32`. **`AUTH_SECRET` must stay
stable**: changing it invalidates every session and every open citation link.

> `OPENAI_API_VERSION` in the local `.env` is `2025-04-14` — re-verify against
> what your Azure OpenAI / Foundry resource actually accepts before go-live.

---

## 4. First deployment

1. Ensure `main` is green (CI + Container).
2. GitHub → **Actions → Deploy → Run workflow** (or wait for the Container
   workflow on `main` to succeed; Deploy fires automatically after it).
3. Watch the run. On success, open:
   - `https://<app>.azurewebsites.net/health` → expect `{"status":"ok", ...}`
   - `https://<app>.azurewebsites.net/admin` → sign in with `API_KEY` (+ TOTP)

If the container is still starting, App Service shows a "warming up" page for a
minute or two while the models load.

---

## 5. First-boot: seed data

The index starts empty, so `/chat` returns **503** until documents are indexed.
Two ways to seed `/home/esg-data`:

**A. Through the admin console (small corpora)**
1. `/admin` → **Workspaces** → create/confirm your workspaces (finance, hr, …).
2. **Documents** → drag-and-drop the real files into each workspace.
3. **Re-index** (or `POST /ingest`), watch the progress bar, confirm chunks > 0.

**B. Copy files in, then index (larger corpora)**
```bash
# list files and open a shell in the running container
az webapp ssh -g "$RG" -n "$APP"
# inside: copy into /home/esg-data/<workspace>/ then re-index from /admin
```

Recommended first-boot checks:
1. Create an organisation + owner (`/admin` → Organisations), add a member.
2. Sign in at `/ui`, ask a workspace question, click a citation, open the source.
3. `GET /admin/status` confirms per-workspace chunk counts.
4. Run the acceptance suite (§1d) against the live URL — a clean run is 0 FAIL.

---

## 5b. Ship existing data and chat history

Instead of seeding a fresh deployment by hand, you can ship **everything** from
your machine — documents, the master library, accounts, organisations, the admin
store and the chat history — in one bundle. Two scripts do it:

> For a demo, see the one-page [`doc/demo-runbook.md`](demo-runbook.md) that
> chains this section with the throwaway-app setup in §1c.

| Script | Does |
| --- | --- |
| `scripts/pack-state.py` | Bundles `DATA_DIR` + `INDEX_DIR` into `dist/esg-state-<stamp>.tar.gz` |
| `scripts/azure-push-state.sh` | Uploads the bundle over the Kudu API and unpacks it onto `/home` |

The bundle carries exactly the two trees the app persists, laid out as
`esg-data/` (→ `/home/esg-data`) and `esg-index/` (→ `/home/esg-index`):

- **`esg-data/`** — `library/`, `organisations/<org>/`, `_shared/`, workspace trees.
- **`esg-index/`** — `users.sqlite3` (accounts, orgs, roles), `sessions.sqlite3`
  (**the chat history**), `workspaces.sqlite3` (workspaces, assignments, settings,
  outbox, usage) and `vectors_*.sqlite3` (+ `organisations/<org>/…`).

SQLite files are captured with the `sqlite3` **backup API**, not a plain copy, so
a database with an un-checkpointed write-ahead log travels whole and restores as
a single consistent file (no `-wal`/`-shm` sidecars). Local junk that must never
travel — `data/.index`, `data/admin.db`, `.DS_Store` — is excluded automatically.

### Quick path (throwaway test app)

```bash
# 1. Bring up a throwaway B2 app. Reuse your local secrets so logins and
#    citation links keep working: export them from .env first.
set -a; . <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env); set +a
scripts/azure-test-up.sh                       # prints the app URL + API key
RG=esg-chatbot-test-rg APP=<app-from-output> \
  scripts/azure-push-state.sh --sync-env --yes

# 2. Open https://<app>.azurewebsites.net/ui and sign in with an existing account.
#    Your sessions/history are in the sidebar; the master library is under
#    /admin → Content.
```

`--sync-env` copies every key from `.env` into the app's settings (skipping
platform-managed ones). That is what keeps **`AUTH_SECRET` identical** to your
machine: it is what signs session cookies and citation links, so a different
value logs everyone out and 404s old citation links (the history itself is not
lost). It also pushes the Stripe/SMTP values so billing and mail work.

### What the push does, and its one caveat

`azure-push-state.sh` reads the app's **publishing credentials**, `PUT`s the
bundle to the Kudu VFS, clears `/home/esg-data` and `/home/esg-index`, unpacks
`tar -xzf` through the Kudu command API, restarts the app and waits for
`/health`. It **replaces** both trees on the target, so **anything written on
Azure during the swap is lost**.

It **stops the app first** and starts it again at the end (a shell `trap` starts
it even if the script fails midway). This is not optional: a running app
recreates `users.sqlite3`/`sessions.sqlite3`-style files under
`/home/esg-index` while the clear is happening, so `rm -rf` can fail with
*"Directory not empty"*. Kudu (the `*.scm.azurewebsites.net` sidecar) stays
reachable while the site is stopped, so the upload and unpack still work. For
production, quiesce traffic first; the throwaway app is fine as-is.

Two Kudu quirks the script handles for you:

- **Writes need `If-Match`.** A bare `PUT`/`DELETE` to the VFS returns **412**;
  both are sent with `If-Match: *` ("no concurrency check").
- **The command API is not a shell.** It execs the first token with the rest as
  `argv`, so a `cmd1 && cmd2` string is not run by a shell — `rm` receives
  `&&`/`tar`/`-xzf` as literal arguments (`rm: invalid option -- 'x'`). Each
  step is therefore dispatched as its own single command.

**Embedding model must match.** The `vectors_*.sqlite3` files are only reusable
if the deployment's image uses the same embedding model that built them (the
image bakes the models, so `:latest` from the same build does). If you changed
models, ship the documents and re-index instead:

```bash
RG=… APP=… scripts/azure-push-state.sh --bundle dist/esg-state-*.tar.gz --no-restart
# then restart with an empty index dir and re-index from /admin, or:
az webapp ssh -g "$RG" -n "$APP"   # inside: rm -f /home/esg-index/vectors_*.sqlite3, then /admin → Re-index
```

### Manual fallback

If the Kudu VFS path differs on your host (or you prefer to see it happen), the
shell inside the container shares the same `/home`:

```bash
az webapp ssh -g "$RG" -n "$APP"
# inside the container:
#   (upload the bundle with `az webapp ssh` + a second terminal, or upload it
#    yourself via the Kudu UI at https://<app>.scm.azurewebsites.net)
rm -rf /home/esg-data /home/esg-index && tar -xzf /home/esg-state.tar.gz -C /home
exit
az webapp restart -g "$RG" -n "$APP"
```

Confirm the shipment afterwards: `/admin` → **Organisations** shows your orgs,
`/admin/status` shows non-zero chunk counts, `/admin` → **Users** lists the
accounts, and `/ui` shows the prior sessions in the sidebar.

---

## 6. Go-live checklist

- [ ] **HTTPS only + TLS 1.2** — set by `deploy.yml`; verify
      `az webapp show -g "$RG" -n "$APP" --query httpsOnly`.
- [ ] **Custom domain + managed certificate** (voice and citation links need HTTPS):
      `az webapp config hostname add` → `az webapp config ssl create`.
- [ ] **Health check** path `/health` configured (set by `deploy.yml`).
- [ ] **Restrict `/admin`.** It is protected only by `API_KEY` (+ Basic/TOTP).
      Options: App Service Access Restrictions (IP allow-list for the whole app),
      Entra ID Easy Auth in front of `/admin`, or Azure Front Door/WAF with a
      path rule. (Tracked as backlog item 11.)
- [ ] **Stripe webhook** → `https://<domain>/billing/webhook` with the matching
      `STRIPE_WEBHOOK_SECRET`; then confirm a checkout flips the org to paid.
- [ ] **SMTP** configured (else verification/reset mail sits in `/admin/emails`;
      see backlog item 15 for the outbox-drain worker). Verify SPF/DKIM/DMARC.
- [ ] **Backups** of `/home/esg-index` and `/home/esg-data` (App Service backup).
- [ ] **Logs** → Log Analytics / App Insights; keep `/metrics` for Prometheus.
- [ ] One instance only (SQLite). Scale up, not out.
- [ ] `ADMIN_SESSION_SECURE=1` (already required for the admin cookie over HTTPS).

---

## 7. Day-2 operations

**Redeploy** — push to `main`; Container rebuilds `:latest`, Deploy pulls it.
App settings and `/home` persist across restarts.

**Rollback** — the image is tagged `sha-<sha>` and by version (`v*`). Pin the
Web App to a known-good tag:
```bash
az webapp config container set -g "$RG" -n "$APP" \
  --docker-custom-image-name ghcr.io/ridwanlekan/esg-related-chatbot:sha-<sha> \
  --docker-custom-image-url "" 2>/dev/null || true
az webapp restart -g "$RG" -n "$APP"
```
(For repeatable rollbacks, adopt deployment slots or Container Apps revisions.)

**Scale up** — `az appservice plan update -g "$RG" -n esg-plan --sku P0v3`.

**Inspect the running box**
```bash
az webapp log tail -g "$RG" -n "$APP"          # live logs
az webapp ssh      -g "$RG" -n "$APP"          # shell into the container
```

**Reset local state** (destructive): inside the box,
`rm -f /home/esg-index/vectors_*.sqlite3` then re-index.

---

## 8. Security (production standard)

- **Secrets**: move from app settings to **Azure Key Vault** and reference them
  with `@Microsoft.KeyVault(SecretUri=...)`, using the Web App's **managed
  identity**. App settings are visible to anyone with Reader on the resource.
- **Admin**: Entra ID in front of `/admin`; keep TOTP on.
- **Network**: Access Restrictions or Front Door/WAF; do not leave the app open
  to the whole Internet if it is a pilot.
- **Least privilege**: the GitHub OIDC identity only gets Contributor on the
  resource group, nothing subscription-wide.
- **Scanning**: Dependabot, CodeQL, secret scanning and dependency review are
  already enabled on the repo.
- **`.dockerignore`/`.gitignore`** keep `.env`, `data/`, `models/`, proposals
  and the local workflow log out of the image and the repo. Never commit secrets.

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Deploy job skipped | `vars.AZURE_WEBAPP_NAME` empty | set the repo variable |
| `azure/login` fails | OIDC subject mismatch | federated credential must match the environment subject |
| App shows "Application Error" / 503 on start | missing `AZURE_OPENAI_*` (fails fast) | set the app settings in §3 |
| `/health` ok but `/chat` 503 | index empty | seed + re-index (§5) |
| Free-tier `/chat` 503 but a customer org works | free users read the pooled `_sample` organisation, not the global tree | assign content to the user's organisation, or seed the sample pack (§5) |
| Rate limiting never returns 429 | limiter keys on the client IP from the proxy header; an absent/unstable `X-Forwarded-For` makes every request its own bucket | ensure the proxy sets a stable `X-Forwarded-For` and the app trusts it (`TRUSTED_PROXY_IPS`), or rate-limit at the front end |
| Container pull fails | private GHCR without creds | add registry credentials (§2.2a) |
| `/admin` or `/ui` shows "not found" | `static/*.html` missing from the installed wheel | declare `package-data` in `pyproject.toml` (`chatbot = ["static/*.html"]`) and rebuild |
| Admin cookie not set | HTTPS required | use the https URL; `ADMIN_SESSION_SECURE=1` |
| Slow first response | cold start + model load | scale up the plan |
| Data lost after redeploy | app-service storage disabled | ensure `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` |

## 10. Cost notes (rough)

- App Service B2 ≈ US$70/mo; P0v3 ≈ US$80/mo (region-dependent).
- Azure OpenAI is usage-based (the app calls it for answers/rewrites only;
  retrieval and voice run locally, so cost tracks questions).
- Voice weights (~840 MB) and the models are baked into the image — no extra
  storage cost, but allow time on the first pull.
