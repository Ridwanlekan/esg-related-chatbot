#!/usr/bin/env bash
#
# Throwaway Azure test deployment for the ESG Workspace Chatbot.
#
# Provisions a resource group with a Linux B2 App Service running the published
# container image, applies the app settings, and waits for /health. B2 is billed
# hourly (~US$0.03/hr), so bring it up for a few hours and then remove
# everything with the companion script:
#
#     scripts/azure-test-down.sh
#
# Prerequisite: the Azure CLI, signed in (`az login`).
#
# Required environment (the app calls Azure OpenAI to answer questions):
#     AZURE_OPENAI_API_KEY
#     AZURE_OPENAI_ENDPOINT      e.g. https://my-resource.openai.azure.com
#
# Optional environment:
#     MODEL_NAME                 default: gpt-4.1-mini
#     OPENAI_API_VERSION         default: 2025-04-14
#     RG APP LOC PLAN SKU IMAGE  defaults below
#     GHCR_USER GHCR_TOKEN       only if the GHCR package is private
#     ADMIN_BASIC_USER           default: admin
#
set -euo pipefail

RG="${RG:-esg-chatbot-test-rg}"
LOC="${LOC:-southeastasia}"
PLAN="${PLAN:-esg-chatbot-test-plan}"
SKU="${SKU:-B2}"
APP="${APP:-esg-chatbot-test-$RANDOM$RANDOM}"
IMAGE="${IMAGE:-ghcr.io/ridwanlekan/esg-related-chatbot:latest}"
MODEL_NAME="${MODEL_NAME:-gpt-4.1-mini}"
OPENAI_API_VERSION="${OPENAI_API_VERSION:-2025-04-14}"

say() { printf '\n==> %s\n' "$*"; }

command -v az >/dev/null 2>&1 || {
  echo "Azure CLI (az) not found. Install it, then run 'az login'." >&2
  exit 1
}
az account show >/dev/null 2>&1 || {
  echo "Not signed in to Azure. Run 'az login' first." >&2
  exit 1
}

: "${AZURE_OPENAI_API_KEY:?Set AZURE_OPENAI_API_KEY before running this script.}"
: "${AZURE_OPENAI_ENDPOINT:?Set AZURE_OPENAI_ENDPOINT before running this script.}"

AUTH_SECRET="${AUTH_SECRET:-$(openssl rand -base64 32)}"
API_KEY="${API_KEY:-$(openssl rand -hex 24)}"

say "Resource group: $RG ($LOC)"
say "App:            $APP"
say "Plan:           $PLAN ($SKU, Linux)"
say "Image:          $IMAGE"

say "Creating resource group (idempotent)"
az group create -n "$RG" -l "$LOC" -o none

say "Creating App Service plan"
az appservice plan create -g "$RG" -n "$PLAN" --is-linux --sku "$SKU" -o none

say "Creating Web App for Containers"
az webapp create -g "$RG" -p "$PLAN" -n "$APP" \
  --deployment-container-image-name "$IMAGE" -o none

if [[ -n "${GHCR_USER:-}" && -n "${GHCR_TOKEN:-}" ]]; then
  say "Setting private GHCR registry credentials"
  az webapp config container set -g "$RG" -n "$APP" \
    --docker-custom-image-name "$IMAGE" \
    --docker-registry-server-url https://ghcr.io \
    --docker-registry-server-user "$GHCR_USER" \
    --docker-registry-server-password "$GHCR_TOKEN" -o none
fi

say "Applying app settings"
az webapp config appsettings set -g "$RG" -n "$APP" -o none --settings \
  WEBSITES_PORT=8000 \
  WEBSITES_ENABLE_APP_SERVICE_STORAGE=true \
  INDEX_DIR=/home/esg-index \
  DATA_DIR=/home/esg-data \
  DOCLING_VET_DIR=/home/esg-docling-vet \
  FORWARDED_ALLOW_IPS='*' \
  TRUSTED_PROXY_IPS='*' \
  TOKENIZERS_PARALLELISM=False \
  ADMIN_SESSION_SECURE=1 \
  AZURE_OPENAI_API_KEY="$AZURE_OPENAI_API_KEY" \
  AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
  OPENAI_API_VERSION="$OPENAI_API_VERSION" \
  MODEL_NAME="$MODEL_NAME" \
  AUTH_SECRET="$AUTH_SECRET" \
  API_KEY="$API_KEY" \
  ADMIN_BASIC_USER="${ADMIN_BASIC_USER:-admin}"

say "Enforcing HTTPS-only, TLS 1.2 and the /health check"
az webapp update -g "$RG" -n "$APP" --set httpsOnly=true -o none
az webapp config set -g "$RG" -n "$APP" \
  --min-tls-version 1.2 \
  --generic-configurations '{"healthCheckPath":"/health"}' -o none

# Keep the models warm between requests (Basic tier and above).
az webapp config set -g "$RG" -n "$APP" --always-on true -o none 2>/dev/null \
  || echo "   (Always On not available on this tier - fine for a test)"

URL="https://$APP.azurewebsites.net"

say "Waiting for $URL/health (first boot loads the models; up to 10 min)"
up=false
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$URL/health" 2>/dev/null || true)"
  if [[ "$code" == "200" ]]; then up=true; break; fi
  printf '.'
  sleep 10
done
echo

if [[ "$up" == "true" ]]; then
  echo " /health is up."
else
  echo " /health did not return 200 yet. Check progress with:"
  echo "   az webapp log tail -g $RG -n $APP"
fi

cat <<EOF

Test deployment ready
---------------------
  Health:  $URL/health
  Admin:   $URL/admin
  App:     $URL
  API key: $API_KEY         (sign in at /admin; keep TOTP unset for the test)

The index is empty on first boot, so /chat returns 503 until you seed documents
through /admin (Workspaces -> Documents -> Re-index).

Tear everything down when done (removes the whole resource group):

  RG=$RG scripts/azure-test-down.sh
EOF
