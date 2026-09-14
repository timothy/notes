# Notes API server

Reference implementation of the Notes API contract in [`../openapi.yaml`](../openapi.yaml), version 2.0.0. The contract is the source of truth: request bodies are validated by the spec's own JSON Schemas, typed models are generated from the document, and every response in the test suite is checked against it. Work in progress; the plan and task list are in [`../tasks/`](../tasks/).

## Development

```sh
cd server
uv sync --locked
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

The suite runs on SQLite by default. To run it against PostgreSQL, start the compose database from the repository root and create the test database once:

```sh
docker compose up -d db
docker compose exec -T db createdb -U notes notes_test
cd server && NOTES_API_TEST_DATABASE_URL=postgresql+psycopg://notes:notes@127.0.0.1:5432/notes_test uv run pytest
```

## Container

The image is the delivery unit: one non-root, read-only image built from the repository root (the server needs `openapi.yaml`), running a single uvicorn process per container and scaled with replicas. The same image runs migrations as a separate command. Base images are pinned by digest in the [`Dockerfile`](../Dockerfile) and Dependabot proposes updates weekly; because Docker Hub rebuilds the base only occasionally, the runtime stage also applies Debian's security updates at build time, and CI fails the image on any fixable CRITICAL or HIGH finding.

### Prerequisite on macOS with Homebrew Docker

`docker build` needs BuildKit for the Dockerfile's cache and bind mounts. Homebrew's Docker CLI does not ship the buildx plugin, so install it once and link it where the CLI looks, the same way the compose plugin is linked:

```sh
brew install docker-buildx
mkdir -p ~/.docker/cli-plugins && ln -sfn "$(brew --prefix)/opt/docker-buildx/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx
docker buildx version
```

`docker compose build` works without it, because Compose bundles BuildKit.

### Build and run

From the repository root:

```sh
docker compose build
docker compose run --rm --no-deps -T api python -m notes_api.dev_issuer env > .env
docker compose up --wait
TOKEN=$(docker compose run --rm --no-deps -T api python -m notes_api.dev_issuer token --sub ada --name 'Ada Okafor')
curl -si -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/me
docker compose down -v
```

[`compose.yaml`](../compose.yaml) starts PostgreSQL 17 (`db`), applies the migration in a one-shot container (`migrate`), and starts the API (`api`) once the migration has completed. The `api` and `migrate` services run with a read-only root filesystem, no capabilities, `no-new-privileges`, and a tmpfs at `/tmp`, the same constraints a deployment should use. If port 8000 or 5432 is taken on your machine, set `NOTES_API_PORT` or `NOTES_API_DB_PORT`.

The server refuses to start without an identity provider, and the stack has none, so the image carries a development one: `python -m notes_api.dev_issuer env` generates a key pair and prints the `.env` lines the stack reads (`OIDC_ISSUER`, `OIDC_AUDIENCE`, the public key as an inline `OIDC_JWKS`, and the private key as `NOTES_DEV_ISSUER_KEY`), and `token --sub <subject> --name <display name>` mints an RS256 access token with it; every distinct `--sub` is a distinct user. The file is gitignored. It is a development convenience only: a deployment sets `OIDC_JWKS_URL` to a real provider and never sets `NOTES_DEV_ISSUER_KEY`.

### Smoke test

```sh
NOTES_API_IMAGE=notes-api:dev server/scripts/smoke_image.sh
```

The script drives `compose.yaml` and asserts the container contract: the image refuses to start without `DATABASE_URL` or without the OIDC settings, naming the variable and never the database password; the stack comes up with keys from the image's own dev issuer; the api runs as uid 10001 with no capabilities, `no-new-privileges`, uvicorn as PID 1, a read-only root filesystem, a writable `/tmp`, and no uv or test tooling; the probes answer and every unknown route or undeclared method is the contract's `404` Problem; `GET /v1/me` is `401` with the bearer challenge without a token, `401` with `error="invalid_token"` with a bad one, and `200` with a minted one; readiness follows the database down and back up; the schema is at head and migrating again is a no-op; `notes-api purge-expired` runs from the image against the stack's database; SIGTERM stops the api cleanly; the source label is set. CI runs the same script in [`.github/workflows/image.yml`](../.github/workflows/image.yml) after hadolint and a Trivy scan for fixable CRITICAL and HIGH vulnerabilities.

### Configuration

Settings are read from the environment by pydantic-settings with no prefix (`notes_api.config.Settings`).

| Variable | Required | Notes |
| --- | --- | --- |
| `DATABASE_URL` | yes | SQLAlchemy URL; in a deployment `postgresql+psycopg://user:password@host:5432/db`. It carries a password: inject it from a secret store, never bake it into an image or commit it. A process without it refuses to start. SQLite URLs are for tests only. |
| `CONTRACT_PATH` | preset in the image | `/app/openapi.yaml`. Outside a container it defaults to the checkout's `openapi.yaml`. |
| `OIDC_ISSUER`, `OIDC_AUDIENCE` | yes | The `iss` and `aud` every bearer token must carry. A process without them refuses to start. |
| `OIDC_JWKS_URL` or `OIDC_JWKS` | exactly one | Where the issuer's signing keys come from: the JWKS URL of a real identity provider (fetched on demand, cached in memory for five minutes, never at start-up), or an inline JWKS document (JSON) as the compose stack and the tests use. Both or neither refuses to start; an empty value counts as unset. |
| `NOTES_DEV_ISSUER_KEY` | never in a deployment | The dev issuer's private key (base64 PKCS#8), written to `.env` by `python -m notes_api.dev_issuer env` and read only by `... token`. The server ignores it. |
| `FORWARDED_ALLOW_IPS` | behind a proxy | uvicorn trusts `X-Forwarded-*` headers from loopback only. Set the ingress CIDR, never `*`. |
| `WEB_CONCURRENCY` | leave unset | One uvicorn worker per container; scale with replicas. |

A misconfigured process prints which variables are missing or invalid and exits; the message never contains a value, so the database password cannot reach the logs that way.

### Authentication

Requests to `/v1` carry `Authorization: Bearer <access token>`. The token must be a JWT signed with `RS256` or `ES256` by a key the configured source holds (selected by `kid`), with `iss` and `aud` equal to the settings, `exp` and `sub` present, and `exp` and `nbf` valid within a 60-second leeway; a `typ` header, when present, must be `at+jwt` or `JWT`. Anything else is the contract's `401` with `WWW-Authenticate: Bearer realm="notes-api"`, plus `error="invalid_token"` when a token was present; the response never says why. Time claims are checked against system time.

The validated `(iss, sub)` maps to one local user, created on first contact with `displayName` taken from the `name` claim, else `preferred_username`, else `user-` and the first eight characters of `sub`, cut to 200 code points. Concurrent first requests create one row: the insert runs under a savepoint and a constraint violation re-selects the winner.

### Request log

Every request except the probes writes one JSON line to stdout (`docker compose logs api` shows them): `time`, `request_id`, `method`, `route` (the matched template, `null` for an unknown route), `path`, `status`, `code` (the Problem code, `null` on success), `user` (the caller's id once authenticated), and `duration_ms`. Never the query string, headers, or bodies, so search text, tokens, and note content cannot reach the logs. An incoming `X-Request-Id` is kept when it matches `^[A-Za-z0-9._-]{1,128}$` and replaced otherwise; every response echoes the id in `X-Request-Id`.

### Conformance run

[`tests/conformance/test_schemathesis.py`](tests/conformance/test_schemathesis.py) runs Schemathesis over every implemented operation: requests are generated from the contract, valid and deliberately invalid, and each response must be a declared status with the declared headers, media type, and body schema. The `OPERATIONS` list grows with each slice, and an id that matches nothing fails the run. Two checks are excluded on purpose: `positive_data_acceptance`, because it rejects the ladder's own `400`, `412`, `422`, and `428` answers to schema-valid but semantically wrong input, and `ignored_auth`, because `tests/test_auth.py` already pins the `401` and the check would triple every successful request.

### Interpretations pinned by tests

Where the contract leaves a choice, the server's choice is fixed by a test and listed here.

- An unknown route or an undeclared method on a known path is `404 not_found`; the server never emits a status the contract does not declare.
- A missing `typ` header is accepted; a present one must name an access token (`at+jwt` or `JWT`, case-insensitive).
- `displayName` precedence is `name`, `preferred_username`, `user-<sub prefix>`, and it is never refreshed after provisioning.
- Cursors are bound to the caller as well as the collection, filters, and limit; another user cannot continue your page.
- Membership mutations check that the caller may act (`403`) before whether the target membership exists (`404`), so a nonmember cannot probe who belongs to a team. A nonmember deleting their own absent membership is `404`.
- A rename to the same name and a role change to the same role are `200` no-ops that leave `updatedAt` unchanged.
- Request bodies are validated before visibility and authorization (the ladder is `401`, request shape, then `404` and `403`), so a malformed body is `422` for anyone, and no lock is held while a body is read.
- `GET /notes/{noteId}` never returns `403`: a note the caller cannot read is `404`, a trashed note is visible to its owners only, and an expired note to nobody.
- Trash and restore: a repeated `DELETE` with the trash ETag is `204` with the same ETag and never extends the recovery period; the version check (`412`) precedes both the idempotent repeat and the lifecycle check, so a stale ETag is `412` even on a trashed note. `POST /notes/{noteId}/restore` ignores any request body and content type. At `expiresAt` exactly, reads, restore, and repeated trash are `404`.
- `GET /notes`: a repeated `q` parameter and duplicate `tag` values are `422` naming the parameter; the cursor is bound to every filter, so continuing with a changed filter is `400 invalid_cursor`; `scope=shared&state=trashed` and `teamId` with `state=trashed` are empty pages from the same query, not special cases.
- Shares on a trashed note: creating one is `409 note_not_active`; because trashing deleted every share, the owner's list is an empty page and `GET`, `PATCH`, and `DELETE` of a former share are `404`. A share reached through another note's path is `404`. A `PATCH` that leaves the permission set unchanged keeps `updatedAt`.
- No string in a request body or in the `q` and `tag` query parameters may contain U+0000: it is `422` at the field's pointer with detail `must not contain NUL characters`, the one rule the server adds beyond the schemas, because PostgreSQL text cannot store it (found by the conformance run); for query parameters the detail is the same and the location is `query`.
- A `PATCH /notes/{noteId}` that changes nothing returns the existing representation and ETag with `updatedAt` unchanged. The version check (`412`) precedes the lifecycle check (`409`), so a stale ETag on a trashed note is `412`.
- A team's mutations lock the team row first, so two admins demoting or removing each other, or the last admin leaving twice, are decided one at a time: the second attempt is `409 last_admin` when it would leave no admin, or `403` when the first attempt already took the caller's admin role.
- Search folds with `casefold()` and does not NFC-normalize (from slice 5).

### Notes, permissions, and shares

Only owners (the author and any co-owners) may change a note, manage its shares, trash and restore it. Anyone else holds the union of their direct share and the shares addressed to teams they currently belong to: `read` is implied by any share, `comment` and `propose_edit` come from the share. Team roles grant nothing beyond the share. Every note representation reports the caller's own `effectivePermissions` in canonical order and an `isOwner` flag. A share's recipient must exist and must not already own the note (`422` at `/recipient/id`); a second share for the same recipient is `409 duplicate_share`; shares carry no ETag, change nothing about the note, and vanish when the note is trashed.

### Purging expired notes

Trashed notes expire exactly 30 × 24 hours after `deletedAt`; from that instant they are `404` for everyone, purge or no purge. Storage is reclaimed by `notes-api purge-expired`, a console script in the image's virtualenv that deletes every note at or past its expiry together with everything that hangs off it (owners, tags, shares, comments, edit requests, approvals, request comments) and prints `purged N expired notes`. Run it from an external scheduler (a cron job or a Kubernetes CronJob) with the same image and the same environment as the server; it reads the same settings and refuses to run without them. Locally: `docker compose run --rm -T api notes-api purge-expired`.

### Migrations

Run `alembic upgrade head` as a one-shot job with the same image before rolling out the image that needs it, never at process start, where replicas would race:

```sh
docker run --rm -e DATABASE_URL=postgresql+psycopg://... notes-api:dev alembic -c /app/alembic.ini upgrade head
```

A migration must stay compatible with the image currently running, because old and new replicas share the schema during a rollout.

### Probes

- `GET /healthz`: liveness. `200 {"status":"ok"}` with no dependencies, so a database incident never restarts the fleet.
- `GET /readyz`: readiness. `SELECT 1` through the pool. `200` with `checks.database: ok`, or `503` with `checks.database: unavailable` and no connection details.

Both paths are outside `/v1`, are not part of the contract, and are unauthenticated, so the ingress must not route them. Kubernetes uses `httpGet` probes on the two paths; compose's own healthcheck calls `/readyz` with the image's Python, because the slim image has no curl.

### Deployment constraints

The image assumes what the smoke test asserts. A Kubernetes `securityContext` should state the same: `runAsNonRoot: true`, `runAsUser` and `runAsGroup` 10001, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, capabilities `drop: [ALL]`, `seccompProfile: RuntimeDefault`, an `emptyDir` (memory) at `/tmp`. uvicorn drains in-flight requests for up to 20 seconds on SIGTERM, so `terminationGracePeriodSeconds` should be at least 30, with a short `preStop` sleep so the load balancer stops routing before the signal arrives.

### What the image contains

`/app/.venv` (the locked dependencies and `notes_api`, bytecode precompiled), `/app/openapi.yaml`, `/app/alembic.ini`, and `/app/alembic/`, all root-owned and read-only to the app user. Debian security updates are applied in the runtime stage; nothing else is installed. It does not contain uv, the tests, dev dependencies, a shell entrypoint, or curl. The uncompressed image is about 85 MB.
