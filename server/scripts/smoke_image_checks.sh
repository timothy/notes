#!/usr/bin/env bash
# Proves the container contract against a built image, through compose.yaml, identically on a developer
# machine and in CI:
#   NOTES_API_IMAGE=notes-api:dev server/scripts/smoke_image.sh
# The outer runner allocates an isolated project, temporary credentials, and ephemeral localhost ports.
# Asserts: fail-fast boot without DATABASE_URL or without the OIDC settings, naming the variable and never the
# password; db -> migrate -> api come up with keys from the image's own dev issuer; the api runs as uid 10001
# with no capabilities, no-new-privileges, uvicorn as PID 1, a read-only root filesystem and a writable /tmp,
# and without uv or test tooling; the probes and the contract's 404 behave; GET /v1/me is 401 with the bearer
# challenge without a token and 200 with a minted one; readiness follows the database; the schema is at head
# and migrating again is a no-op; `notes-api purge-expired` runs from the image; SIGTERM stops the api
# cleanly; the source label is set.
set -euo pipefail

: "${NOTES_API_IMAGE:?set NOTES_API_IMAGE to the image under test, e.g. notes-api:dev}"
export NOTES_API_IMAGE
: "${NOTES_API_TEST_PROJECT:?run smoke_image.sh, not the internal checks directly}"
: "${NOTES_API_TEST_DIRECTORY:?missing isolated directory}"
cd "$(git rev-parse --show-toplevel)"

BASE=
TMP=$(mktemp -d)

compose() { docker compose --project-name "$NOTES_API_TEST_PROJECT" --project-directory "$NOTES_API_TEST_DIRECTORY" --env-file "$NOTES_API_TEST_DIRECTORY/.env" -f compose.yaml "$@"; }
fail() { echo "smoke FAILED: $*" >&2; exit 1; }
step() { echo "--- $*"; }

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then compose logs --no-color api migrate >&2 || true; fi
  rm -rf "$TMP"
  exit "$status"
}
trap cleanup EXIT

# request METHOD PATH [curl options]: prints the status; leaves headers in $TMP/headers and the body in $TMP/body.
request() {
  local method=$1 path=$2
  shift 2
  curl -sS -X "$method" "$@" -D "$TMP/headers" -o "$TMP/body" -w '%{http_code}' "$BASE$path"
}
header() { tr -d '\r' <"$TMP/headers" | awk -v name="$1" 'BEGIN{IGNORECASE=1} tolower($1)==tolower(name":"){sub(/^[^:]*: */,""); print; exit}'; }
json() { python3 -c 'import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    value = value[key]
print(value)' "$TMP/body" "$1"; }
in_api() { compose exec -T api "$@"; }

expect_problem_404() {
  local status
  status=$(request "$1" "$2")
  [ "$status" = 404 ] || fail "$1 $2: expected 404, got $status"
  [[ "$(header content-type)" == application/problem+json* ]] || fail "$1 $2: not application/problem+json"
  [ "$(header cache-control)" = "no-store" ] || fail "$1 $2: missing Cache-Control: no-store"
  [ "$(json code)" = "not_found" ] || fail "$1 $2: code is not not_found"
}

wait_for_status() {  # wait_for_status PATH EXPECTED SECONDS
  local i status
  for ((i = 0; i < $3; i++)); do
    status=$(request GET "$1" || true)
    [ "$status" = "$2" ] && return 0
    sleep 1
  done
  fail "$1 did not return $2 within $3 s (last: $status)"
}

step "1 fail-fast: the image requires database, identity, and cursor configuration"
oidc=(-e OIDC_ISSUER=https://issuer.example -e OIDC_AUDIENCE=notes-api -e 'OIDC_JWKS={"keys":[]}')
if output=$(docker run --rm "${oidc[@]}" "$NOTES_API_IMAGE" 2>&1); then fail "container started without DATABASE_URL"; fi
grep -q DATABASE_URL <<<"$output" || fail "startup failure does not name DATABASE_URL"
if output=$(docker run --rm -e DATABASE_URL=postgresql+psycopg://notes:smoke-secret@db:5432/notes "$NOTES_API_IMAGE" 2>&1); then
  fail "container started without the OIDC settings"
fi
grep -q OIDC_ISSUER <<<"$output" || fail "startup failure does not name OIDC_ISSUER"
grep -q smoke-secret <<<"$output" && fail "startup failure leaks the database password"

if output=$(docker run --rm "${oidc[@]}" -e DATABASE_URL=sqlite:// "$NOTES_API_IMAGE" 2>&1); then
  fail "container started without CURSOR_SIGNING_KEY"
fi
grep -q CURSOR_SIGNING_KEY <<<"$output" || fail "startup failure does not name CURSOR_SIGNING_KEY"

step "2 the stack comes up with keys from the image's dev issuer: db healthy, migrate completed, api healthy"
compose up --wait --wait-timeout 180 --no-build
BASE="http://$(compose port api 8000)"
[ "$(docker inspect -f '{{.State.ExitCode}}' "$(compose ps -aq migrate)")" = 0 ] || fail "migrate did not exit 0"
[ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q api)")" = healthy ] || fail "api is not healthy"

[ "$(docker inspect -f '{{.Image}}' "$(compose ps -q api)")" = "$(docker inspect -f '{{.Image}}' "$(compose ps -aq migrate)")" ] || fail "API and migration use different images"

step "3 hardening inside the api container"
[ "$(in_api id -u)" = 10001 ] || fail "api does not run as uid 10001"
in_api grep -qE '^CapEff:\s*0+$' /proc/1/status || fail "PID 1 holds capabilities"
in_api grep -qE '^NoNewPrivs:\s*1$' /proc/1/status || fail "no-new-privileges is not set"
cmdline=$(in_api sh -c "tr '\\0' ' ' </proc/1/cmdline")
[[ "$cmdline" == *uvicorn* && "$cmdline" != sh* ]] || fail "PID 1 is not uvicorn: $cmdline"
in_api sh -c 'touch /app/probe 2>/dev/null' && fail "/app is writable"
in_api sh -c 'touch /tmp/probe' || fail "/tmp is not writable"
in_api sh -c 'command -v uv >/dev/null 2>&1' && fail "uv is present in the runtime image"
in_api python -c 'import pytest' 2>/dev/null && fail "test dependencies are present in the runtime image"

step "4 HTTP surface: the probes and nothing but contract routes"
status=$(request GET /healthz)
[ "$status" = 200 ] || fail "GET /healthz: expected 200, got $status"
[[ "$(header content-type)" == application/json* ]] || fail "GET /healthz: not application/json"
[ "$(header cache-control)" = "no-store" ] || fail "GET /healthz: missing Cache-Control: no-store"
[ "$(json status)" = ok ] || fail "GET /healthz: status is not ok"
status=$(request GET /readyz)
[ "$status" = 200 ] || fail "GET /readyz: expected 200, got $status"
[ "$(json checks.database)" = ok ] || fail "GET /readyz: database check is not ok"
for path in /v1/nope /docs /openapi.json /redoc /health /v1/healthz /v1/notes/ /healthz/; do expect_problem_404 GET "$path"; done
expect_problem_404 POST /healthz
expect_problem_404 DELETE /v1/me

step "5 authentication: GET /v1/me is 401 without a token and 200 with one from the dev issuer"
status=$(request GET /v1/me)
[ "$status" = 401 ] || fail "GET /v1/me without a token: expected 401, got $status"
[[ "$(header content-type)" == application/problem+json* ]] || fail "GET /v1/me 401: not application/problem+json"
[ "$(header cache-control)" = "no-store" ] || fail "GET /v1/me 401: missing Cache-Control: no-store"
[ "$(header www-authenticate)" = 'Bearer realm="notes-api"' ] || fail "GET /v1/me 401: challenge is '$(header www-authenticate)'"
[ "$(json code)" = unauthenticated ] || fail "GET /v1/me 401: code is not unauthenticated"
status=$(request GET /v1/me -H 'Authorization: Bearer not-a-token')
[ "$status" = 401 ] || fail "GET /v1/me with garbage: expected 401, got $status"
[ "$(header www-authenticate)" = 'Bearer realm="notes-api", error="invalid_token"' ] || fail "GET /v1/me 401: challenge lacks error=invalid_token"
token=$(compose run --rm --no-deps -T api python -m notes_api.dev_issuer token --sub smoke --name 'Smoke Test')
status=$(request GET /v1/me -H "Authorization: Bearer $token")
[ "$status" = 200 ] || fail "GET /v1/me with a minted token: expected 200, got $status"
[ "$(header cache-control)" = "no-store" ] || fail "GET /v1/me 200: missing Cache-Control: no-store"
[ "$(json displayName)" = "Smoke Test" ] || fail "GET /v1/me: displayName is '$(json displayName)'"
user_id=$(json id)
status=$(request GET "/v1/users/$user_id" -H "Authorization: Bearer $token")
[ "$status" = 200 ] || fail "GET /v1/users/{id}: expected 200, got $status"
[ "$(json id)" = "$user_id" ] || fail "GET /v1/users/{id} returned another user"

step "6 image boundary regressions and log privacy"
in_api python < server/scripts/check_image_boundaries.py
status=$(request GET '/v1/notes?q=SMOKE-PRIVATE-SEARCH' -H "Authorization: Bearer $token" -H 'X-Request-Id: smoke-log-check')
[ "$status" = 200 ] || fail "search failed"
logs=$(compose logs --no-color api)
[[ "$logs" != *SMOKE-PRIVATE-SEARCH* && "$logs" != *"$token"* ]] || fail "sensitive text reached container logs"
[[ "$logs" != *'HTTP/1.1'* ]] || fail "uvicorn access log is enabled"
[[ "$logs" == *smoke-log-check* ]] || fail "structured request log missing"

step "7 readiness follows the database"
compose stop db >/dev/null
wait_for_status /readyz 503 15
[ "$(json checks.database)" = unavailable ] || fail "GET /readyz 503 body does not say unavailable"
grep -q 'notes:notes' "$TMP/body" && fail "GET /readyz leaks the connection string"
[ "$(request GET /healthz)" = 200 ] || fail "GET /healthz is not 200 while the database is down"
compose start db >/dev/null
wait_for_status /readyz 200 30

step "8 schema at head; migrating again is a no-op"
compose run --rm migrate alembic -c /app/alembic.ini current 2>/dev/null | grep -q '(head)' || fail "schema is not at head"
compose run --rm migrate >/dev/null || fail "re-running the migration failed"

step "9 the purge command runs from the image against the stack's database"
purged=$(compose run --rm --no-deps -T migrate notes-api purge-expired) || fail "notes-api purge-expired failed"
[ "$purged" = "purged 0 expired notes" ] || fail "unexpected purge output: '$purged'"

step "10 SIGTERM stops the api cleanly"
started=$(date +%s)
compose stop -t 25 api >/dev/null
elapsed=$(( $(date +%s) - started ))
[ "$(docker inspect -f '{{.State.ExitCode}}' "$(compose ps -aq api)")" = 0 ] || fail "api exit code is not 0 after SIGTERM"
[ "$elapsed" -lt 25 ] || fail "api took ${elapsed}s to stop (SIGKILL suspected)"

step "11 image metadata"
label=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.source"}}' "$NOTES_API_IMAGE")
[ "$label" = https://github.com/timothy/notes ] || fail "source label is '$label'"

echo "smoke OK"
