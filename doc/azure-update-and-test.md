# Update & test runbook — Azure (repeatable)

Use this **every time you ship a code change** and want to verify it on a real
Azure deployment. It is the short, repeatable loop:

> pull `main` → point the throwaway B2 app at the freshly built image →
> run the acceptance suite → tear the app down.

First-time / production deployment (OIDC, least-privilege identity, persistent
settings) lives in `doc/deployment.md`. This file is only the day-to-day
"deploy an update to the test app and test it" path.

> **Order matters:** `scripts/azure-test-up.sh` is the **deploy** step — run it
> *first*. `scripts/azure-test-suite.sh` is a **test runner** — it is not
> deployed anywhere; you run it *after* the app is up, against the app's URL.

---

## 0. One-time prerequisites

```bash
az login                       # Azure CLI, signed in
command -v curl jq xargs       # required by the suite
```

You do **not** need Docker locally — GitHub builds the image for you.

---

## 1. Get the update

`main` is the source of truth. Both helper scripts live there (the suite script
is only on `main`).

```bash
cd /path/to/esg-related-chatbot
git checkout main
git pull --ff-only
```

Merging to `main` triggers **`.github/workflows/container.yml`**, which builds
and pushes:

| Tag | Meaning |
| --- | --- |
| `ghcr.io/ridwanlekan/esg-related-chatbot:latest` | newest `main` build |
| `ghcr.io/ridwanlekan/esg-related-chatbot:sha-<7>` | immutable, pinned to a commit (7-char short sha) |

Wait for the **Container** workflow to finish before pulling the image, or you
will deploy the previous build. Check **Actions → Container** on GitHub, or:

```bash
gh run list --workflow container.yml -L 3   # only if you have the gh CLI
```

Tip: `latest` now points at the new commit only once the build succeeds. For a
reproducible test, use the `sha-<7>` tag of the merge commit instead.

---

## 2. Load credentials

The app calls Azure OpenAI, so `azure-test-up.sh` needs `AZURE_OPENAI_API_KEY`
and `AZURE_OPENAI_ENDPOINT`. They live in `.env`. Your `.env` may use
`KEY = value` (spaces around `=`), which bash cannot source — normalise once:

```bash
sed -i.bak -E 's/^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]+=/\1=/' .env
set -a; . ./.env; set +a
```

---

## 3. Deploy the update to the test app

Pick **one** of the two paths.

### 3a. Fast update — reuse the running app (recommended for updates)

If the throwaway app already exists, just repoint it at the new image and
restart. `/home` data is preserved, so this is a true "update", not a reset.

```bash
RG=esg-chatbot-test-rg
APP=esg-chatbot-test-113074444
IMAGE=ghcr.io/ridwanlekan/esg-related-chatbot:latest   # or :sha-<7> to pin

az webapp config container set -g "$RG" -n "$APP" \
  --docker-custom-image-name "$IMAGE"
az webapp restart -g "$RG" -n "$APP"

# wait for the new container to answer
until [ "$(curl -s -o /dev/null -w '%{http_code}' "https://$APP.azurewebsites.net/health")" = 200 ]; do
  sleep 5; echo -n .
done; echo
```

Retrieve the admin API key you will need for the suite (it's a plain app setting):

```bash
API_KEY=$(az webapp config appsettings list -g "$RG" -n "$APP" \
  --query "[?name=='API_KEY'].value | [0]" -o tsv)
echo "$API_KEY"
```

### 3b. Clean re-provision — fresh app, empty index

Use this when you want a from-scratch environment (empty index, fresh secrets).
`azure-test-up.sh` runs `az webapp create`, so the app name must not already
exist — delete the resource group first.

```bash
RG=esg-chatbot-test-rg bash scripts/azure-test-down.sh   # prompts y/N
# deletion is --no-wait; wait until the RG is gone:
az group show -n "$RG" -o none 2>/dev/null && echo "still deleting…" || echo "gone"

# Deploy (this is the deploy step). IMAGE defaults to :latest.
RG=esg-chatbot-test-rg APP=esg-chatbot-test-113074444 \
IMAGE=ghcr.io/ridwanlekan/esg-related-chatbot:latest \
API_KEY=<the app's admin key — see step 4 for how to read it> \
bash scripts/azure-test-up.sh
```

- up.sh prints `API key: ...`. Pass `API_KEY=...` yourself (as above) so you
  know it for step 4 without copying from the scroll-back.
- If you omit `API_KEY`, one is generated randomly and printed.
- The first boot is empty: `/chat` returns 503 until you seed documents via
  `/admin` (workspaces are normally created by the suite itself).

---

## 4. Run the acceptance suite

The suite is black-box — it only needs the app URL and the admin API key. It
creates then deletes its own throwaway org/owner/user/workspace.

```bash
BASE=https://esg-chatbot-test-113074444.azurewebsites.net \
API_KEY=<the key from step 3> \
bash scripts/azure-test-suite.sh
```

Useful switches (full table in `doc/deployment.md` §1d):

| Env var | Effect |
| --- | --- |
| `RUN_LOAD=1` | enable the concurrent-chat burst (section J) |
| `SKIP_VOICE=1` | skip the memory-heavy voice tests (section G) |
| `RUN_RESTART=1 RG=<rg> APP=<app>` | add an `az webapp restart` persistence check |
| `KEEP=1` | do not delete the throwaway test objects |
| `CAT=finance` | workspace/category to exercise |

It prints a PASS/FAIL/SKIP/INFO table and exits non-zero if anything fails. A
clean run is **0 FAIL** (a few `SKIP`/`INFO` rows are normal).

Handy: read the key straight from the app if you forgot it:

```bash
API_KEY=$(az webapp config appsettings list -g esg-chatbot-test-rg \
  -n esg-chatbot-test-113074444 --query "[?name=='API_KEY'].value" -o tsv)
```

---

## 5. Tear down (stop billing)

B2 is billed by the hour (~US$0.03/hr), so delete the whole resource group when
you are done:

```bash
RG=esg-chatbot-test-rg bash scripts/azure-test-down.sh   # prompts y/N
```

Confirm it is gone with `az group show -n esg-chatbot-test-rg -o none`.

---

## Cheat sheet

```bash
# --- every update ---
git checkout main && git pull --ff-only
sed -i.bak -E 's/^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]+=/\1=/' .env
set -a; . ./.env; set +a

# 1) deploy  (3a fast: repoint + restart, or 3b clean: down + up)
# 2) test
BASE=https://<app>.azurewebsites.net API_KEY=<key> \
  bash scripts/azure-test-suite.sh

# 3) teardown
RG=esg-chatbot-test-rg bash scripts/azure-test-down.sh
```

## Troubleshooting quick hits

| Symptom | Fix |
| --- | --- |
| `az webapp create ... already exists` | Reuse it (§3a) or delete the RG first (§3b). |
| Image unchanged after update | Container workflow not finished — wait, then `az webapp restart`. |
| `/chat` returns 503 | Index empty on a fresh app — seed via `/admin`, or let the suite populate it. |
| 500s under parallel load | Check the SQLite fix is in the deployed image (`sha-<7>` matches the merged commit). |
| Suite `A1 /health 504` on first row | Cold-start during container swap — re-run; it should pass. |
| Need server logs (SCM may 503) | `az webapp log tail -g esg-chatbot-test-rg -n esg-chatbot-test-113074444`. |
