#!/usr/bin/env bash
#
# End-to-end acceptance suite for a *deployed* ESG Workspace Chatbot.
#
# It exercises the live HTTP API (not the source tree). It is safe to re-run:
# every run uses fresh emails/ids and removes what it made (custom workspace,
# organisation, users, uploaded document) unless you pass KEEP=1.
#
# The retrieval tests use a throwaway *customer organisation* created through
# the admin API and seeded with one document, then log in as that org's owner.
# That is the real multi-tenant path. (Self-registered free users live in the
# system "sample" organisation, which only ever contains the launch pack and
# cannot be uploaded into -- see doc/deployment.md.)
#
# Prerequisites: curl, jq and xargs on PATH, and a reachable deployment.
#
# Usage:
#     BASE=https://your-app.azurewebsites.net \
#     API_KEY=<the admin API key> \
#     bash scripts/azure-test-suite.sh
#
# Optional environment:
#     CAT=finance     workspace for signup/ingest (default: first public one)
#     SKIP_VOICE=1    skip the voice tests (no python3, or you don't want them)
#     RUN_LOAD=1      add the 5-way concurrent /chat check (costs LLM tokens)
#     RUN_RESTART=1   restart the app with az and re-check persistence
#     RG=... APP=...  required only when RUN_RESTART=1
#     KEEP=1          do not delete the QA users/workspace/document afterwards
#
# Exit status is 0 when nothing FAILed (SKIP/INFO do not fail the run).
#
set -u

BASE="${BASE:-}"
API_KEY="${API_KEY:-}"
CAT="${CAT:-}"
SKIP_VOICE="${SKIP_VOICE:-0}"
RUN_LOAD="${RUN_LOAD:-0}"
RUN_RESTART="${RUN_RESTART:-0}"
KEEP="${KEEP:-0}"
RG="${RG:-}"
APP="${APP:-}"

if [ -z "$BASE" ]; then
    echo "error: BASE is required, e.g. BASE=https://app.azurewebsites.net $0" >&2
    exit 2
fi
BASE="${BASE%/}"
for bin in curl jq xargs; do
    command -v "$bin" >/dev/null 2>&1 || { echo "error: $bin is required" >&2; exit 2; }
done

if [ -t 1 ]; then
    G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; C=$'\033[36m'; B=$'\033[1m'; N=$'\033[0m'
else
    G=; R=; Y=; C=; B=; N=
fi

# ---- result bookkeeping ----------------------------------------------------

declare -a RES_IDS RES_NAMES RES_STATES RES_DETAILS
PASSES=0; FAILS=0; SKIPS=0; INFOS=0

record() { # id name state detail
    local id="$1" name="$2" state="$3" detail="${4:-}" col
    RES_IDS+=("$id"); RES_NAMES+=("$name"); RES_STATES+=("$state"); RES_DETAILS+=("$detail")
    case "$state" in
        PASS) PASSES=$((PASSES + 1)); col=$G ;;
        FAIL) FAILS=$((FAILS + 1)); col=$R ;;
        SKIP) SKIPS=$((SKIPS + 1)); col=$Y ;;
        *)    INFOS=$((INFOS + 1)); col=$C ;;
    esac
    printf '  %s%-5s%s %-46s %s%-4s%s %s\n' "$B" "$id" "$N" "$name" "$col" "$state" "$N" "$detail"
}
pass() { record "$1" "$2" PASS "${3:-}"; }
fail() { record "$1" "$2" FAIL "${3:-}"; }
skip() { record "$1" "$2" SKIP "${3:-}"; }
info() { record "$1" "$2" INFO "${3:-}"; }
section() { printf '\n%s== %s ==%s\n' "$B" "$1" "$N"; }

# ---- HTTP helpers ----------------------------------------------------------

BODY=""; CODE=""
req() { # METHOD PATH [curl args...] -> sets BODY and CODE
    local method="$1" path="$2"; shift 2
    local out
    out="$(curl -sS -X "$method" "$BASE$path" -w $'\n%{http_code}' "$@")"
    CODE="${out##*$'\n'}"
    BODY="${out%$'\n'*}"
}
sc() { # url [args...] -> status code only
    curl -s -o /dev/null -w '%{http_code}' "$@"
}
jget() { printf '%s' "$1" | jq -r "$2" 2>/dev/null; }
jok()  { printf '%s' "$1" | jq -e "$2" >/dev/null 2>&1; }

AUTH_ADMIN=(-H "Authorization: Bearer $API_KEY")

# state carried between sections
TOKEN=""; USER_ID=""; ORG_ID=""; ORG_UID=""; ORG_TOKEN=""
FNAME=""; ORG_DOC=0; SESSION_ID=""
PASSWORD="Passw0rd!pass"

echo "${B}ESG Workspace Chatbot - acceptance suite${N}"
echo "  base   : $BASE"
echo "  api key: $([ -n "$API_KEY" ] && echo set || echo 'NOT SET (admin tests skip)')"

# ===========================================================================
section "A. Platform / liveness"
# ===========================================================================

req GET /health
if [ "$CODE" = 200 ] && [ "$(jget "$BODY" '.status // empty')" = ok ]; then
    pass A1 "/health returns ok" "$CODE"
else
    fail A1 "/health returns ok" "code=$CODE body=$(printf '%s' "$BODY" | head -c 100)"
fi

metrics="$(curl -s "$BASE/metrics")"
if printf '%s' "$metrics" | grep -q '^esg_'; then
    pass A2 "/metrics exposes esg_*" "$(printf '%s' "$metrics" | grep -c '^esg_') series"
else
    fail A2 "/metrics exposes esg_*" "no esg_ lines"
fi

[ "$(sc "$BASE/definitely-not-a-route")" = 404 ] \
    && pass A3 "unknown route is 404" "404" \
    || fail A3 "unknown route is 404" "not 404"

if curl -s -D - -o /dev/null "$BASE/health" | grep -qi '^x-content-type-options:'; then
    pass A4 "security headers present" "nosniff"
else
    fail A4 "security headers present" "header missing"
fi

# ===========================================================================
section "B. Admin console and admin API"
# ===========================================================================

if [ -z "$API_KEY" ]; then
    skip B1-B9 "admin tests" "API_KEY not set"
else
    admin_html="$(curl -s "$BASE/admin")"
    case "$admin_html" in
        *"Admin Console"*) pass B1 "/admin serves the console" "HTML" ;;
        *) fail B1 "/admin serves the console" "no 'Admin Console' marker" ;;
    esac

    req GET /admin/status "${AUTH_ADMIN[@]}"
    if [ "$CODE" = 200 ] && jok "$BODY" 'type=="object"'; then
        pass B2 "admin/status with key" "$CODE"
    else
        fail B2 "admin/status with key" "code=$CODE"
    fi

    [ "$(sc "$BASE/admin/status")" = 401 ] \
        && pass B3 "admin/status without key is 401" "401" \
        || fail B3 "admin/status without key is 401" "not 401"

    req GET /admin/workspaces "${AUTH_ADMIN[@]}"
    if [ "$CODE" = 200 ] && jok "$BODY" '.workspaces | length > 0'; then
        pass B4 "admin/workspaces lists built-ins" "$(jget "$BODY" '.workspaces|length') rows"
    else
        fail B4 "admin/workspaces lists built-ins" "code=$CODE"
    fi

    WS="qa-$RANDOM$RANDOM"
    req POST /admin/workspaces "${AUTH_ADMIN[@]}" -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg c "$WS" '{category:$c,label:"QA Test",blurb:"temporary"}')"
    [ "$CODE" = 200 ] && pass B5 "create custom workspace" "$WS" \
        || fail B5 "create custom workspace" "code=$CODE $(printf '%s' "$BODY" | head -c 100)"

    if curl -s "$BASE/workspaces" | grep -q "\"category\":\"$WS\""; then
        pass B6 "custom workspace is public" "$WS"
    else
        fail B6 "custom workspace is public" "not in /workspaces"
    fi

    req PATCH "/admin/workspaces/$WS" "${AUTH_ADMIN[@]}" -H 'Content-Type: application/json' \
        -d '{"label":"QA Test edited"}'
    [ "$CODE" = 200 ] && pass B7 "edit custom workspace" "$CODE" \
        || fail B7 "edit custom workspace" "code=$CODE"

    if [ "$KEEP" = 1 ]; then
        skip B8 "delete custom workspace" "KEEP=1"
    else
        req DELETE "/admin/workspaces/$WS" "${AUTH_ADMIN[@]}"
        [ "$CODE" = 200 ] && pass B8 "delete custom workspace" "$CODE" \
            || fail B8 "delete custom workspace" "code=$CODE"
    fi

    BUILTIN="$(curl -s "$BASE/admin/workspaces" "${AUTH_ADMIN[@]}" \
        | jq -r '.workspaces[] | select(.type=="built-in") | .category' 2>/dev/null | head -n1)"
    req DELETE "/admin/workspaces/${BUILTIN:-finance}" "${AUTH_ADMIN[@]}"
    [ "$CODE" = 403 ] && pass B9 "built-in workspace is undeletable" "${BUILTIN:-finance}" \
        || fail B9 "built-in workspace is undeletable" "code=$CODE"
fi

# ===========================================================================
section "C. Accounts and auth (free-tier self-service)"
# ===========================================================================

if [ -z "$CAT" ]; then
    CAT="$(jget "$(curl -s "$BASE/workspaces")" '.[0].category // empty')"
fi
[ -n "$CAT" ] || { echo "error: could not determine a workspace" >&2; exit 2; }
echo "  workspace: $CAT"

EMAIL="qa-$RANDOM$RANDOM@example.com"
req POST /auth/signup -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg e "$EMAIL" --arg c "$CAT" --arg p "$PASSWORD" \
        '{email:$e,password:$p,name:"QA Bot",category:$c}')"
SIGNUP_BODY="$BODY"
TOKEN="$(jget "$SIGNUP_BODY" '.token // empty')"
USER_ID="$(jget "$SIGNUP_BODY" '.user.id // empty')"
if [ "$CODE" = 200 ] && [ -n "$TOKEN" ]; then
    pass C1 "signup returns a session" "$EMAIL"
else
    fail C1 "signup returns a session" "code=$CODE $(printf '%s' "$BODY" | head -c 140)"
fi

req GET /me -H "Authorization: Bearer $TOKEN"
if [ "$CODE" = 200 ] && [ "$(jget "$BODY" '.user.email // empty')" = "$EMAIL" ]; then
    pass C2 "/me returns the profile" "$CODE"
else
    fail C2 "/me returns the profile" "code=$CODE"
fi

VERIF_LINK="$(jget "$SIGNUP_BODY" '.verification.link // empty')"
if [ -n "$VERIF_LINK" ]; then
    req POST /chat -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        -d '{"question":"hello there"}'
    [ "$CODE" = 403 ] && pass C3 "unverified user is refused" "$CODE" \
        || fail C3 "unverified user is refused" "code=$CODE"

    req POST /auth/verify-email -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg t "${VERIF_LINK##*token=}" '{token:$t}')"
    [ "$CODE" = 200 ] && pass C4 "verify-email accepts token" "$CODE" \
        || fail C4 "verify-email accepts token" "code=$CODE $(printf '%s' "$BODY" | head -c 100)"
else
    info C3 "unverified user is refused" "verification not enforced here"
    skip C4 "verify-email accepts token" "no verification link"
fi

req POST /auth/login -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg e "$EMAIL" --arg p "$PASSWORD" '{email:$e,password:$p}')"
TOKEN="$(jget "$BODY" '.token // empty')"
[ "$CODE" = 200 ] && [ -n "$TOKEN" ] && pass C5 "login returns a session" "$CODE" \
    || fail C5 "login returns a session" "code=$CODE"

OLD="$TOKEN"
req POST /me/password -H "Authorization: Bearer $OLD" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg p "$PASSWORD" '{current_password:$p,new_password:"Passw0rd!new"}')"
NEWTOK="$(jget "$BODY" '.token // empty')"
[ "$CODE" = 200 ] && [ -n "$NEWTOK" ] && pass C6 "change password" "$CODE" \
    || fail C6 "change password" "code=$CODE $(printf '%s' "$BODY" | head -c 100)"
[ "$(sc "$BASE/me" -H "Authorization: Bearer $OLD")" = 401 ] \
    && pass C7 "old token revoked" "401" \
    || fail C7 "old token revoked" "not 401"
TOKEN="$NEWTOK"

req POST /auth/forgot-password -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg e "$EMAIL" '{email:$e}')"
RST_LINK="$(jget "$BODY" '.link // empty')"
[ "$CODE" = 200 ] && pass C8 "forgot-password (generic + link)" "$CODE" \
    || fail C8 "forgot-password (generic + link)" "code=$CODE"

if [ -n "$RST_LINK" ]; then
    req POST /auth/reset-password -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg t "${RST_LINK##*token=}" '{token:$t,password:"Passw0rd!reset"}')"
    [ "$CODE" = 200 ] && pass C9 "reset-password works" "$CODE" \
        || fail C9 "reset-password works" "code=$CODE $(printf '%s' "$BODY" | head -c 100)"
    TOKEN="$(jget "$BODY" '.token // empty')"
else
    skip C9 "reset-password works" "no reset link"
fi

req POST /auth/signup -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg e "weak-$RANDOM@example.com" '{email:$e,password:"short",name:"x",category:"finance"}')"
[ "$CODE" = 422 ] && pass C10 "short password is 422" "$CODE" \
    || fail C10 "short password is 422" "code=$CODE"

req POST /auth/signup -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg e "$EMAIL" --arg p "$PASSWORD" '{email:$e,password:$p,name:"dup",category:"finance"}')"
case "$CODE" in
    409|422) pass C11 "duplicate email is refused" "$CODE" ;;
    *) fail C11 "duplicate email is refused" "code=$CODE" ;;
esac

# Diagnostic: does the free tier answer at all? It can only if the sample pack
# was seeded into the system sample organisation (see doc/deployment.md).
if [ -n "$TOKEN" ]; then
    c="$(sc -X POST "$BASE/chat" -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' -d '{"question":"hello there"}')"
    case "$c" in
        200) info C12 "free-tier chat readiness" "200 (sample pack present)" ;;
        503) info C12 "free-tier chat readiness" "503 - sample pack missing in this deployment" ;;
        *)   info C12 "free-tier chat readiness" "code=$c" ;;
    esac
fi

# ===========================================================================
section "D. Tenant setup and content assignment"
# ===========================================================================

if [ -z "$API_KEY" ]; then
    skip D1-D4 "tenant setup" "API_KEY not set (retrieval tests skip)"
else
    ORG_ID="qa-org-$RANDOM$RANDOM"
    req POST /admin/organisations "${AUTH_ADMIN[@]}" -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg c "$ORG_ID" '{id:$c,name:"QA Org"}')"
    [ "$CODE" = 200 ] && pass D1 "create organisation" "$ORG_ID" \
        || fail D1 "create organisation" "code=$CODE $(printf '%s' "$BODY" | head -c 120)"

    ORG_EMAIL="qa-org-$RANDOM$RANDOM@example.com"
    req POST /admin/users "${AUTH_ADMIN[@]}" -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg e "$ORG_EMAIL" --arg p "$PASSWORD" --arg c "$CAT" --arg o "$ORG_ID" \
            '{email:$e,password:$p,name:"Org Owner",category:$c,organisation_id:$o}')"
    ORG_UID="$(jget "$BODY" '.id // empty')"
    [ "$CODE" = 200 ] && [ -n "$ORG_UID" ] && pass D2 "create verified org owner" "$ORG_EMAIL" \
        || fail D2 "create verified org owner" "code=$CODE $(printf '%s' "$BODY" | head -c 120)"

    req POST /auth/login -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg e "$ORG_EMAIL" --arg p "$PASSWORD" '{email:$e,password:$p}')"
    ORG_TOKEN="$(jget "$BODY" '.token // empty')"
    [ "$CODE" = 200 ] && [ -n "$ORG_TOKEN" ] && pass D3 "org owner logs in" "$CODE" \
        || fail D3 "org owner logs in" "code=$CODE"

    FNAME="qa-esg-$RANDOM.txt"
    SAMPLE="$(mktemp -t esg-qa-XXXXXX)"
    printf 'Acme ESG policy: the company pledged to cut scope 3 emissions by 40 percent by 2030 and reach net zero by 2040.\n' > "$SAMPLE"
    resp="$(curl -s -w $'\n%{http_code}' -X POST "$BASE/admin/upload" \
        "${AUTH_ADMIN[@]}" -F "category=$CAT" -F "organisation_id=$ORG_ID" \
        -F "files=@$SAMPLE;filename=$FNAME")"
    CODE="${resp##*$'\n'}"; BODY="${resp%$'\n'*}"
    if [ "$CODE" = 200 ]; then
        pass D3b "assign content to the org" "$FNAME"; ORG_DOC=1
    else
        fail D3b "assign content to the org" "code=$CODE $(printf '%s' "$BODY" | head -c 140)"
    fi

    req GET /ingest/status "${AUTH_ADMIN[@]}"
    [ "$CODE" = 200 ] && pass D4 "ingest/status" "$CODE" || fail D4 "ingest/status" "code=$CODE"
fi

# ===========================================================================
section "E. Retrieval and chat (as the org owner)"
# ===========================================================================

if [ -z "$ORG_TOKEN" ]; then
    skip E1-E10 "retrieval and chat" "no tenant session (API_KEY required)"
else
    req POST /chat -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
        -d '{"question":"hello there"}'
    [ "$CODE" = 200 ] && pass E1 "verified user can chat" "$CODE" \
        || fail E1 "verified user can chat" "code=$CODE $(printf '%s' "$BODY" | head -c 100)"

    req POST /search -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
        -d '{"question":"What did Acme pledge about scope 3 emissions?"}'
    if [ "$CODE" = 200 ] && jok "$BODY" '.results | length > 0'; then
        pass E2 "/search returns hits" "$(jget "$BODY" '.results|length') hits"
    else
        fail E2 "/search returns hits" "code=$CODE $(printf '%s' "$BODY" | head -c 100)"
    fi

    req POST /chat -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
        -d '{"question":"What did Acme pledge about scope 3 emissions?"}'
    SESSION_ID="$(jget "$BODY" '.session_id // empty')"
    if [ "$CODE" = 200 ] && [ -n "$SESSION_ID" ]; then
        pass E3 "/chat answers with a session" "$(jget "$BODY" '.answer // empty' | head -c 34)..."
    else
        fail E3 "/chat answers with a session" "code=$CODE $(printf '%s' "$BODY" | head -c 100)"
    fi

    req POST /chat -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
        -d '{"question":"What is the weather in Paris tomorrow?"}'
    [ "$CODE" = 200 ] && pass E4 "out-of-scope handled" "$CODE" \
        || fail E4 "out-of-scope handled" "code=$CODE"

    req POST /chat -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
        -H 'X-Timezone-Offset: -330' -d '{"question":"hi there"}'
    [ "$CODE" = 200 ] && pass E5 "small talk with timezone" "$CODE" \
        || fail E5 "small talk with timezone" "code=$CODE"

    stream="$(curl -s -N -X POST "$BASE/chat/stream" \
        -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
        --max-time 90 -d '{"question":"Summarise the emissions pledge."}')"
    if printf '%s' "$stream" | grep -q 'data:'; then
        pass E6 "/chat/stream emits SSE" "$(printf '%s' "$stream" | grep -c 'data:') frames"
    else
        fail E6 "/chat/stream emits SSE" "no data: frames"
    fi

    req GET /sessions -H "Authorization: Bearer $ORG_TOKEN"
    [ "$CODE" = 200 ] && pass E7 "/sessions lists history" "$(jget "$BODY" '.total // "?"')" \
        || fail E7 "/sessions lists history" "code=$CODE"
    if [ -n "$SESSION_ID" ]; then
        req GET "/sessions/$SESSION_ID" -H "Authorization: Bearer $ORG_TOKEN"
        [ "$CODE" = 200 ] && pass E8 "/sessions/{id} returns messages" "$CODE" \
            || fail E8 "/sessions/{id} returns messages" "code=$CODE"
    else
        skip E8 "/sessions/{id} returns messages" "no session id"
    fi

    req POST /chat -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
        -d '{"question":"What is the pledge?","language":"hi"}'
    [ "$CODE" = 200 ] && pass E9 "multilingual chat" "$CODE" \
        || fail E9 "multilingual chat" "code=$CODE"

    # A free-tier user (sample org) must not see this org's document.
    if [ -n "$TOKEN" ]; then
        cross="$(curl -s -X POST "$BASE/search" -H "Authorization: Bearer $TOKEN" \
            -H 'Content-Type: application/json' -d '{"question":"Acme scope 3 emissions pledge"}')"
        printf '%s' "$cross" | grep -q "$FNAME" \
            && fail E10 "cross-tenant isolation" "leaked $FNAME" \
            || pass E10 "cross-tenant isolation" "no leak"
    else
        skip E10 "cross-tenant isolation" "no free-tier session"
    fi
fi

# ===========================================================================
section "F. Document links and download"
# ===========================================================================

if [ -z "$ORG_TOKEN" ] || [ "$ORG_DOC" != 1 ]; then
    skip F1-F4 "document links" "no tenant document"
else
    req POST /documents/link -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg s "$FNAME" '{source:$s}')"
    url="$(jget "$BODY" '.url // empty')"   # relative: /documents/download?...
    if [ "$CODE" = 200 ] && [ -n "$url" ]; then
        pass F1 "mint a signed link" "$(jget "$BODY" '.expires_in // "?"')s"
    else
        fail F1 "mint a signed link" "code=$CODE $(printf '%s' "$BODY" | head -c 100)"
    fi

    if [ -n "$url" ]; then
        [ "$(sc "$BASE$url")" = 200 ] && pass F2 "download with token" "200" \
            || fail F2 "download with token" "not 200"
        # Flip a middle character of the token: a real byte change, unlike the
        # last base64url char whose low bits are padding and may not decode
        # differently.
        tok="${url##*token=}"
        mid=$(( ${#tok} / 2 ))
        flip=A
        [ "${tok:$mid:1}" = A ] && flip=B
        bad="${url%token=*}token=${tok:0:$mid}${flip}${tok:$((mid + 1))}"
        c="$(sc "$BASE$bad")"
        case "$c" in
            401|403|404) pass F3 "tampered token rejected" "$c" ;;
            *) fail F3 "tampered token rejected" "code=$c" ;;
        esac
    else
        skip F2-F3 "download with token" "no link"
    fi

    req GET "/documents/download?source=$FNAME" -H "Authorization: Bearer $ORG_TOKEN"
    [ "$CODE" = 200 ] && pass F4 "download with bearer" "$CODE" \
        || fail F4 "download with bearer" "code=$CODE"
fi

# ===========================================================================
section "G. Voice (memory-heavy)"
# ===========================================================================

if [ "$SKIP_VOICE" = 1 ]; then
    skip G1-G3 "voice" "SKIP_VOICE=1"
elif [ -z "$ORG_TOKEN" ]; then
    skip G1-G3 "voice" "no tenant session"
elif ! command -v python3 >/dev/null 2>&1; then
    skip G1-G3 "voice" "python3 unavailable for a sample"
else
    WAV="$(mktemp -t esg-voice-XXXXXX).wav"
    python3 - "$WAV" <<'PY'
import sys, wave, struct, math
p = sys.argv[1]
w = wave.open(p, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * t / 16000))) for t in range(16000)))
w.close()
PY
    resp="$(curl -s -w $'\n%{http_code}' -X POST "$BASE/voice/stt" \
        -H "Authorization: Bearer $ORG_TOKEN" -F "file=@$WAV;type=audio/wav")"
    CODE="${resp##*$'\n'}"; BODY="${resp%$'\n'*}"
    if [ "$CODE" = 200 ] && jok "$BODY" 'has("text")'; then
        pass G1 "/voice/stt transcribes" "$CODE"
    else
        fail G1 "/voice/stt transcribes" "code=$CODE $(printf '%s' "$BODY" | head -c 100)"
    fi

    ct="$(curl -s -o /dev/null -w '%{http_code} %{content_type}' -X POST "$BASE/voice/tts" \
        -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
        -d '{"text":"The emissions target is net zero by 2040."}')"
    case "$ct" in
        "200 audio/"*) pass G2 "/voice/tts returns audio" "${ct#200 }" ;;
        *) fail G2 "/voice/tts returns audio" "$ct" ;;
    esac

    resp="$(curl -s -w $'\n%{http_code}' -X POST "$BASE/voice/stt" \
        -H "Authorization: Bearer $ORG_TOKEN" -F "file=@$SAMPLE;type=text/plain")"
    CODE="${resp##*$'\n'}"
    case "$CODE" in
        4*) pass G3 "non-audio upload refused" "$CODE" ;;
        *)  fail G3 "non-audio upload refused" "code=$CODE" ;;
    esac
    rm -f "$WAV"
fi

# ===========================================================================
section "H. Organisations and billing (informational)"
# ===========================================================================

if [ -z "$ORG_TOKEN" ]; then
    info H1 "org/billing" "no tenant session"
else
    for p in /org/overview /org/usage /org/plans /billing/usage; do
        info "H:$p" "$p" "$(sc "$BASE$p" -H "Authorization: Bearer $ORG_TOKEN")"
    done
    [ -n "$API_KEY" ] && info "H:/admin/usage" "/admin/usage" "$(sc "$BASE/admin/usage" "${AUTH_ADMIN[@]}")"
fi

# ===========================================================================
section "I. Security"
# ===========================================================================

[ "$(sc "$BASE/admin/status")" = 401 ] && pass I1 "admin without creds is 401" "401" \
    || fail I1 "admin without creds is 401" "not 401"
[ "$(sc "$BASE/me")" = 401 ] && pass I2 "/me without token is 401" "401" \
    || fail I2 "/me without token is 401" "not 401"

if [ -n "$ORG_TOKEN" ]; then
    resp="$(curl -s -w $'\n%{http_code}' -X POST "$BASE/chat" -H "Authorization: Bearer $ORG_TOKEN" \
        -H 'Content-Type: application/json' \
        -d '{"question":"'\''; DROP TABLE users;-- <script>alert(1)</script>"}')"
    CODE="${resp##*$'\n'}"; BODY="${resp%$'\n'*}"
    case "$CODE" in
        2*|4*) pass I3 "injection string handled" "$CODE" ;;
        *)     fail I3 "injection string handled" "code=$CODE" ;;
    esac
    printf '%s' "$BODY" | grep -q '<script>alert' \
        && fail I4 "no reflected script" "echoed in response" \
        || pass I4 "no reflected script" "clean"
else
    skip I3-I4 "injection" "no tenant session"
fi

# Rate limiting. The bucket key is the caller IP; behind App Service that comes
# from X-Forwarded-For, which the app trusts (TRUSTED_PROXY_IPS=*). Pin one so
# the burst lands in a single bucket and the limiter is actually exercised.
XFF="203.0.113.$((RANDOM % 200 + 10))"
codes="$(seq 1 90 | xargs -P 30 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
    -X POST "$BASE/auth/forgot-password" -H 'Content-Type: application/json' \
    -H "X-Forwarded-For: $XFF" -d '{"email":"nobody@example.com"}')"
n429="$(printf '%s\n' "$codes" | grep -c '^429$')"
[ "$n429" -gt 0 ] && pass I5 "rate limiting kicks in" "$n429 x 429" \
    || fail I5 "rate limiting kicks in" "no 429 in 90 tries"

# ===========================================================================
section "J. Performance and persistence"
# ===========================================================================

TIMEFORMAT='%R'
t="$( { time curl -s -o /dev/null "$BASE/health"; } 2>&1 )"
info J1 "idle /health latency" "${t:-?}s"

if [ "$RUN_LOAD" = 1 ] && [ -n "$ORG_TOKEN" ]; then
    for i in 1 2 3 4 5; do
        curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/chat" \
            -H "Authorization: Bearer $ORG_TOKEN" -H 'Content-Type: application/json' \
            -d '{"question":"What is the emissions pledge?"}' > "/tmp/esg-load-$i.$$" &
    done
    wait
    ok="$(cat /tmp/esg-load-*.$$ 2>/dev/null | grep -c '^200$')"
    rm -f /tmp/esg-load-*.$$
    [ "$ok" = 5 ] && pass J2 "5 concurrent chats all 200" "$ok/5" \
        || fail J2 "5 concurrent chats all 200" "$ok/5"
else
    skip J2 "5 concurrent chats" "RUN_LOAD not set"
fi

if [ "$RUN_RESTART" = 1 ] && command -v az >/dev/null 2>&1 && [ -n "$RG" ] && [ -n "$APP" ]; then
    az webapp restart -g "$RG" -n "$APP" >/dev/null 2>&1
    for i in $(seq 1 40); do
        [ "$(sc "$BASE/health")" = 200 ] && break
        sleep 5
    done
    if [ -n "$ORG_TOKEN" ]; then
        c="$(sc "$BASE/me" -H "Authorization: Bearer $ORG_TOKEN")"
        [ "$c" = 200 ] && pass J3 "session survives restart" "$c" \
            || fail J3 "session survives restart" "code=$c"
    else
        skip J3 "session survives restart" "no tenant session"
    fi
else
    skip J3 "restart persistence" "RUN_RESTART=1 + RG + APP needed"
fi
info J4 "memory/OOM check" "az webapp log tail -g ${RG:-RG} -n ${APP:-APP} (watch for 137)"

# ===========================================================================
# Cleanup
# ===========================================================================
if [ "$KEEP" != 1 ] && [ -n "$API_KEY" ]; then
    if [ "$ORG_DOC" = 1 ]; then
        curl -s -o /dev/null -X POST "$BASE/admin/documents/delete" "${AUTH_ADMIN[@]}" \
            -H 'Content-Type: application/json' \
            -d "$(jq -nc --arg c "$CAT" --arg f "$FNAME" --arg o "$ORG_ID" \
                '{category:$c,filename:$f,organisation_id:$o}')"
    fi
    [ -n "$ORG_UID" ] && curl -s -o /dev/null -X DELETE "$BASE/admin/users/$ORG_UID" "${AUTH_ADMIN[@]}"
    [ -n "$USER_ID" ] && curl -s -o /dev/null -X DELETE "$BASE/admin/users/$USER_ID" "${AUTH_ADMIN[@]}"
    [ -n "$ORG_ID" ] && curl -s -o /dev/null -X POST "$BASE/admin/organisations/delete" "${AUTH_ADMIN[@]}" \
        -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg o "$ORG_ID" '{organisation_id:$o,confirm:$o}')"
fi
rm -f "${SAMPLE:-}"

# ===========================================================================
# Summary
# ===========================================================================
printf '\n%s== Summary ==%s\n' "$B" "$N"
printf '  %sPASS %d%s   %sFAIL %d%s   %sSKIP %d%s   %sINFO %d%s\n' \
    "$G" "$PASSES" "$N" "$R" "$FAILS" "$N" "$Y" "$SKIPS" "$N" "$C" "$INFOS" "$N"

if [ "$FAILS" -gt 0 ]; then
    printf '\n%sFailed:%s\n' "$R" "$N"
    i=0
    while [ "$i" -lt "${#RES_IDS[@]}" ]; do
        [ "${RES_STATES[$i]}" = FAIL ] && \
            printf '  %s %s - %s\n' "${RES_IDS[$i]}" "${RES_NAMES[$i]}" "${RES_DETAILS[$i]}"
        i=$((i + 1))
    done
    exit 1
fi
echo "All good."
