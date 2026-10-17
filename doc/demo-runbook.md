# Demo runbook — run the app and restore your data + chat history

One page to follow before a demo: stand up a throwaway Azure app, push **your**
documents, accounts, organisations and chat history onto it, verify, then remove
everything afterwards.

This is the fast path. The full operator guide (production, OIDC, go-live) is
[`doc/deployment.md`](deployment.md); the mechanics of the data copy are
**§5b** there (doc/deployment.md:333).

- Bring the app up: [§1c of the deployment guide](deployment.md) (doc/deployment.md:85)
- Push your state: [§5b](deployment.md) (doc/deployment.md:333)

---

## 0. Prerequisites (once)

- Azure CLI installed and signed in: `az login`.
- Your local state is what you want to show: `data/` (documents) and `.index/`
  (**database of users, sessions/chat history, workspaces and vector indexes**).
- A `.env` in the repo root with your Azure OpenAI + app credentials.

```bash
cd /Users/raheemridwan/Workspace/esg-related-chatbot
az login
set -a; . ./.env; set +a          # exports AZURE_OPENAI_*, API_KEY, AUTH_SECRET, ...
```

> If your `.env` uses `KEY = value` (spaces around `=`), bash cannot source it.
> Normalise once: `sed -i.bak -E 's/^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]+=/\1=/' .env`.

## 1. Bring up the app

A B2 app (~US$0.03/hr) running the published `:latest` image. Pick a fixed name
so the next command can reuse it:

```bash
export RG=esg-chatbot-test-rg
export APP=esg-demo-$(date +%m%d)
scripts/azure-test-up.sh          # provisions, deploys, waits for /health, prints the URL
```

It prints `https://<APP>.azurewebsites.net`, the admin URL `/admin` and an API
key. (If you omit `APP`, the script generates one — copy the printed name.)

## 2. Bundle and push your state

```bash
./venv/bin/python scripts/pack-state.py            # -> dist/esg-state-<UTC>.tar.gz
RG="$RG" APP="$APP" scripts/azure-push-state.sh --sync-env --yes
```

- `--sync-env` copies `.env` into the app settings **keeping `AUTH_SECRET`
  identical**, so existing logins and citation links keep working. After this,
  the admin API key is the `API_KEY` from your `.env`.
- The script stops the app, replaces `/home/esg-data` and `/home/esg-index`,
  starts it again and waits for `/health`. Re-run it any time; it is safe.
- `dist/` is git-ignored — the bundle never gets committed.

## 3. Verify

```bash
BASE="https://$APP.azurewebsites.net"
curl -s "$BASE/health"                                    # {"status":"ok", ...}
curl -s -H "Authorization: Bearer $API_KEY" "$BASE/admin/status"
```

`/admin/status` should show the counts you expect (workspaces, chunks, users,
sessions) — the same numbers as the machine you bundled from.

Then open:

| Page | What to check |
| --- | --- |
| `$BASE/ui` | sign in with an **existing** account; your sessions/history are in the sidebar |
| `$BASE/admin` | sign in with `API_KEY`; **Content** shows the master library |

> Keep TOTP unset for a demo (`ADMIN_TOTP_SECRET` empty) so `/admin` sign-in is
> just the API key.

## 4. After the demo — tear down

```bash
RG="$RG" scripts/azure-test-down.sh     # deletes the whole resource group; billing stops
```

Deletes the resource group (plan + app + everything). Your local `data/` and
`.index/` are untouched, and the `dist/esg-state-*.tar.gz` bundle stays.

---

## Keep it for the next demo

- **Same state next time:** just repeat steps 0–3 on a fresh app. The bundle is
  reproducible — run `pack-state.py` again to snapshot the latest data.
- **Leave it running** instead of tearing down if you demo again within a day or
  two; ~US$0.03/hr. Restart it later with `az webapp restart -g "$RG" -n "$APP"`.
- **New image available?** A freshly created app always pulls `:latest`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `az webapp` 401 / Kudu `412` | handled by the scripts; make sure you are on the latest `scripts/azure-push-state.sh` |
| `/chat` works but `/admin` asks for TOTP | clear `ADMIN_TOTP_SECRET` and re-run step 2 |
| Citations 404 after a push | `AUTH_SECRET` changed — always pass `--sync-env` |
| `/health` not 200 yet | first boot loads the models (up to ~10 min): `az webapp log tail -g "$RG" -n "$APP"` |
| Nothing to restore | check `DATA_DIR`/`INDEX_DIR`; `pack-state.py` bundles those trees |
