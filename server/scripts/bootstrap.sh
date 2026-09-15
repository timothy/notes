#!/usr/bin/env bash
# Never replace an existing identity. The lock and same-directory rename protect concurrent bootstraps.
set -euo pipefail
cd "$(dirname "$0")/../.."
umask 077
lock=.env.bootstrap.lock
mkdir "$lock" 2>/dev/null || { echo 'Another bootstrap holds .env.bootstrap.lock' >&2; exit 1; }
temporary=
trap '[ -z "$temporary" ] || rm -f "$temporary"; rmdir "$lock"' EXIT
temporary=$(mktemp .env.bootstrap.XXXXXX)
image=${NOTES_API_IMAGE:-notes-api:dev}
if [ ! -e .env ]; then
  docker run --rm "$image" python -m notes_api.dev_issuer env > "$temporary"
  mv "$temporary" .env
elif ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?CURSOR_SIGNING_KEY[[:space:]]*=' .env; then
  cat .env > "$temporary"
  printf '\n' >> "$temporary"
  docker run --rm "$image" python -c 'import secrets; print("CURSOR_SIGNING_KEY=" + secrets.token_hex(32))' >> "$temporary"
  mv "$temporary" .env
fi
echo 'Development configuration ready (.env preserved).'
