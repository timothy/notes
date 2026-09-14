#!/usr/bin/env bash
# Proves the container contract against a built image, through compose.yaml, identically on a developer
# machine and in CI:
#   NOTES_API_IMAGE=notes-api:dev server/scripts/smoke_image.sh
# NOTES_API_PORT and NOTES_API_DB_PORT (compose.yaml's host ports) are honoured when 8000 or 5432 are taken.
# Asserts: fail-fast boot without DATABASE_URL; db -> migrate -> api come up; the api runs as uid 10001 with
# no capabilities, no-new-privileges, uvicorn as PID 1, a read-only root filesystem and a writable /tmp, and
# without uv or test tooling; the probes and the contract's 404 behave; readiness follows the database;
# the schema is at head and migrating again is a no-op; SIGTERM stops the api cleanly; the source label is set.
set -euo pipefail

: "${NOTES_API_IMAGE:?set NOTES_API_IMAGE to the image under test, e.g. notes-api:dev}"
export NOTES_API_IMAGE NOTES_API_PORT="${NOTES_API_PORT:-8000}" NOTES_API_DB_PORT="${NOTES_API_DB_PORT:-5432}"
cd "$(git rev-parse --show-toplevel)"

BASE="http://127.0.0.1:$NOTES_API_PORT"
TMP=$(mktemp -d)

compose() { docker compose -f compose.yaml "$@"; }
fail() { echo "smoke FAILED: $*" >&2; exit 1; }
step() { echo "--- $*"; }

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then compose logs --no-color api migrate >&2 || true; fi
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$TMP"
  exit "$status"
}
trap cleanup EXIT

# request METHOD PATH: prints the status; leaves headers in $TMP/headers and the body in $TMP/body.
request() { curl -sS -X "$1" -D "$TMP/headers" -o "$TMP/body" -w '%{http_code}' "$BASE$2"; }
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

step "1 fail-fast: the image refuses to start without DATABASE_URL"
if output=$(docker run --rm "$NOTES_API_IMAGE" 2>&1); then fail "container started without DATABASE_URL"; fi
grep -q database_url <<<"$output" || fail "startup failure does not name database_url"

step "2 the stack comes up: db healthy, migrate completed, api healthy"
compose up --wait --wait-timeout 180 --no-build
[ "$(docker inspect -f '{{.State.ExitCode}}' "$(compose ps -aq migrate)")" = 0 ] || fail "migrate did not exit 0"
[ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q api)")" = healthy ] || fail "api is not healthy"

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
for path in /v1/me /v1/nope /docs /openapi.json /redoc /health /v1/healthz; do expect_problem_404 GET "$path"; done
expect_problem_404 POST /healthz

step "5 readiness follows the database"
compose stop db >/dev/null
wait_for_status /readyz 503 15
[ "$(json checks.database)" = unavailable ] || fail "GET /readyz 503 body does not say unavailable"
grep -q 'notes:notes' "$TMP/body" && fail "GET /readyz leaks the connection string"
[ "$(request GET /healthz)" = 200 ] || fail "GET /healthz is not 200 while the database is down"
compose start db >/dev/null
wait_for_status /readyz 200 30

step "6 schema at head; migrating again is a no-op"
compose run --rm migrate alembic -c /app/alembic.ini current 2>/dev/null | grep -q '(head)' || fail "schema is not at head"
compose run --rm migrate >/dev/null || fail "re-running the migration failed"

step "7 SIGTERM stops the api cleanly"
started=$(date +%s)
compose stop -t 25 api >/dev/null
elapsed=$(( $(date +%s) - started ))
[ "$(docker inspect -f '{{.State.ExitCode}}' "$(compose ps -aq api)")" = 0 ] || fail "api exit code is not 0 after SIGTERM"
[ "$elapsed" -lt 25 ] || fail "api took ${elapsed}s to stop (SIGKILL suspected)"

step "8 image metadata"
label=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.source"}}' "$NOTES_API_IMAGE")
[ "$label" = https://github.com/timothy/notes ] || fail "source label is '$label'"

echo "smoke OK"
