#!/usr/bin/env bash
#
# Ship a state bundle (data + chat history) into a running Azure App Service.
#
# Reads the bundle written by scripts/pack-state.py, uploads it through the Kudu
# VFS, and unpacks it onto /home so that /home/esg-data and /home/esg-index are
# replaced with this machine's data. The app is restarted afterwards so it opens
# the new files. No azcopy, no docker and no interactive SSH are needed — only
# the Azure CLI and curl.
#
#     ./venv/bin/python scripts/pack-state.py
#     RG=esg-chatbot-test-rg ./scripts/azure-push-state.sh --sync-env
#
# Options:
#   -g RG              resource group (default: esg-chatbot-test-rg)
#   -n APP             web app name (default: the only app in the group)
#   -b BUNDLE          bundle to ship (default: newest dist/esg-state*.tar.gz)
#   --sync-env         also copy every key in .env to app settings, so the
#                      deployment keeps the same AUTH_SECRET (logins, citation
#                      links and history stay valid) plus Stripe/SMTP config
#   --no-restart       start the app but do not wait for /health afterwards
#   -y, --yes          skip the confirmation prompt
#
# What is replaced: /home/esg-data (documents) and /home/esg-index (users,
# sessions/chat history, workspaces and vector indexes). Everything else in
# /home is left alone.
#
# The app is stopped for the swap (otherwise it recreates files under
# /home/esg-index mid-clear) and started again at the end, even on failure.
#
set -euo pipefail

RG="${RG:-esg-chatbot-test-rg}"
APP="${APP:-}"
BUNDLE="${BUNDLE:-}"
SYNC_ENV=0
NO_RESTART=0
ASSUME_YES=0

# Settings the platform or the deployment scripts own; never overwrite them
# with whatever happens to be in a local .env.
PLATFORM_KEYS='WEBSITES_PORT WEBSITES_ENABLE_APP_SERVICE_STORAGE WEBSITES_CONTAINER_START_TIME_LIMIT INDEX_DIR DATA_DIR DOCLING_VET_DIR FORWARDED_ALLOW_IPS TRUSTED_PROXY_IPS ADMIN_SESSION_SECURE ADMIN_SESSION_TTL_HOURS TOKENIZERS_PARALLELISM'

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

APP_STOPPED=0
start_app() {
  if [ "$APP_STOPPED" = "1" ]; then
    printf '\n==> Starting the app\n'
    az webapp start -g "$RG" -n "$APP" -o none >/dev/null 2>&1 || true
    APP_STOPPED=0
  fi
}
trap start_app EXIT

while [ $# -gt 0 ]; do
  case "$1" in
    -g) RG="${2:?}"; shift 2 ;;
    -n) APP="${2:?}"; shift 2 ;;
    -b|--bundle) BUNDLE="${2:?}"; shift 2 ;;
    --sync-env) SYNC_ENV=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
WAIT_HEALTH=$(( NO_RESTART ? 0 : 1 ))

command -v az >/dev/null 2>&1 || die "Azure CLI (az) not found."
command -v curl >/dev/null 2>&1 || die "curl not found."
az account show >/dev/null 2>&1 || die "Not signed in to Azure. Run 'az login'."

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 not found (needed to build the Kudu request)."

if [ -z "$APP" ]; then
  APPLIST="$(az webapp list -g "$RG" --query "[].name" -o tsv 2>/dev/null || true)"
  COUNT="$(printf '%s\n' "$APPLIST" | grep -c . || true)"
  [ "$COUNT" = "1" ] || die "Found $COUNT apps in $RG; pass -n APP."
  APP="$APPLIST"
fi

az webapp show -g "$RG" -n "$APP" --query name -o tsv >/dev/null \
  || die "Web app '$APP' not found in '$RG'."

if [ -z "$BUNDLE" ]; then
  shopt -s nullglob
  BUNDLES=(dist/esg-state*.tar.gz)
  shopt -u nullglob
  [ "${#BUNDLES[@]}" -gt 0 ] || die "No bundle found. Run scripts/pack-state.py first."
  BUNDLE="$(printf '%s\n' "${BUNDLES[@]}" | tail -n1)"
fi
[ -f "$BUNDLE" ] || die "Bundle not found: $BUNDLE"
BUNDLE="$(cd "$(dirname "$BUNDLE")" && pwd)/$(basename "$BUNDLE")"

SIZE_MB="$(du -m "$BUNDLE" | cut -f1)"
say "Target"
printf '  resource group: %s\n  web app:        %s\n  bundle:         %s (%s MiB)\n' \
  "$RG" "$APP" "$BUNDLE" "$SIZE_MB"
cat <<'NOTE'

This REPLACES /home/esg-data and /home/esg-index on the target. Its users,
accounts, chat history and indexes on Azure will be overwritten by this
machine's. Do it while no one is using the app.
NOTE

if [ "$ASSUME_YES" != "1" ]; then
  printf '\nType "yes" to continue: '
  read -r reply
  [ "$reply" = "yes" ] || die "aborted."
fi

say "Reading publishing credentials"
SCM_USER="$(az webapp deployment list-publishing-credentials -g "$RG" -n "$APP" \
  --query publishingUserName -o tsv)"
SCM_PASS="$(az webapp deployment list-publishing-credentials -g "$RG" -n "$APP" \
  --query publishingPassword -o tsv)"
SCM="https://$APP.scm.azurewebsites.net"

if [ "$SYNC_ENV" = "1" ]; then
  say "Syncing .env into app settings (skipping platform-managed keys)"
  [ -f .env ] || die ".env not found; run from the repo root or drop --sync-env."
  SETTINGS=()
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|\#*) continue ;; esac
    case "$line" in *=*) : ;; *) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    [ -n "$key" ] || continue
    skip=0
    case "$key" in WEBSITES_*) skip=1 ;; esac
    for pk in $PLATFORM_KEYS; do
      if [ "$key" = "$pk" ]; then skip=1; break; fi
    done
    if [ "$skip" = "1" ]; then
      printf '  skip %s (platform-managed)\n' "$key"
      continue
    fi
    SETTINGS+=("$key=$val")
  done < .env
  [ "${#SETTINGS[@]}" -gt 0 ] || die "No settings found in .env."
  az webapp config appsettings set -g "$RG" -n "$APP" -o none \
    --settings "${SETTINGS[@]}"
  printf '  applied %s app setting(s)\n' "${#SETTINGS[@]}"
fi

say "Uploading bundle to /home/esg-state.tar.gz"
# Kudu's VFS requires If-Match for writes (a bare PUT is a 412); the wildcard
# means "no concurrency check". Clear any leftover from an earlier run first.
curl -fsS -u "$SCM_USER:$SCM_PASS" -H "If-Match: *" -X DELETE \
  "$SCM/api/vfs/esg-state.tar.gz" >/dev/null 2>&1 || true
curl -fsS -u "$SCM_USER:$SCM_PASS" \
  -H "If-Match: *" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @"$BUNDLE" -X PUT "$SCM/api/vfs/esg-state.tar.gz" \
  >/dev/null || die "Upload failed."

kudu_run() {
  local cmd="$1" payload result
  payload="$("$PYTHON" -c 'import json,sys; print(json.dumps({"command": sys.argv[1], "dir": "/home"}))' "$cmd")"
  result="$(curl -fsS -u "$SCM_USER:$SCM_PASS" \
    -H "Content-Type: application/json" \
    --data-binary "$payload" -X POST "$SCM/api/command")" \
    || die "Kudu command request failed: $cmd"
  "$PYTHON" - "$result" <<'PY'
import json, sys
raw = sys.argv[1]
try:
    data = json.loads(raw)
except ValueError:
    print(raw)
    sys.exit(0)
out = (data.get("Output") or "").strip()
err = (data.get("Error") or "").strip()
if out:
    print(out)
if err:
    print(err, file=sys.stderr)
sys.exit(0 if data.get("ExitCode", 0) == 0 else 1)
PY
}

say "Stopping the app for a clean swap (Kudu stays reachable)"
az webapp stop -g "$RG" -n "$APP" -o none
APP_STOPPED=1
sleep 10

say "Extracting onto /home"
# The Kudu command API does not run a shell — it execs the first token with the
# rest as argv — so every step is one simple command, no && or pipes.
kudu_run "rm -rf /home/esg-data /home/esg-index" || die "could not clear old dirs."
kudu_run "tar -xzf /home/esg-state.tar.gz -C /home" || die "tar failed on the server."
kudu_run "rm -f /home/esg-state.tar.gz" || true
kudu_run "ls -la /home/esg-index"
kudu_run "ls -la /home/esg-data"

start_app
if [ "$WAIT_HEALTH" = "1" ]; then
  URL="https://$APP.azurewebsites.net"
  say "Waiting for $URL/health (first boot loads the models)"
  up=false
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$URL/health" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then up=true; break; fi
    printf '.'
    sleep 10
  done
  echo
  [ "$up" = "true" ] && echo " /health is up." \
    || echo " /health not 200 yet; check: az webapp log tail -g $RG -n $APP"
fi

say "Done"
printf '  Admin: %s/admin\n  App:   %s\n' \
  "https://$APP.azurewebsites.net" "https://$APP.azurewebsites.net"
echo "  Sign in with the API_KEY from your .env (or from --sync-env)."
