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
docker build -t notes-api:dev .
docker compose up --build --wait
curl -si http://127.0.0.1:8000/healthz
docker compose down -v
```

[`compose.yaml`](../compose.yaml) starts PostgreSQL 17 (`db`), applies the migration in a one-shot container (`migrate`), and starts the API (`api`) once the migration has completed. The `api` and `migrate` services run with a read-only root filesystem, no capabilities, `no-new-privileges`, and a tmpfs at `/tmp`, the same constraints a deployment should use. If port 8000 or 5432 is taken on your machine, set `NOTES_API_PORT` or `NOTES_API_DB_PORT`. Optional settings such as `OIDC_*` go in a gitignored `.env` next to `compose.yaml`.

### Smoke test

```sh
NOTES_API_IMAGE=notes-api:dev server/scripts/smoke_image.sh
```

The script drives `compose.yaml` and asserts the container contract: the image refuses to start without `DATABASE_URL`; the stack comes up; the api runs as uid 10001 with no capabilities, `no-new-privileges`, uvicorn as PID 1, a read-only root filesystem, a writable `/tmp`, and no uv or test tooling; the probes answer and everything else is the contract's `404` Problem; readiness follows the database down and back up; the schema is at head and migrating again is a no-op; SIGTERM stops the api cleanly; the source label is set. CI runs the same script in [`.github/workflows/image.yml`](../.github/workflows/image.yml) after hadolint and a Trivy scan for fixable CRITICAL and HIGH vulnerabilities.

### Configuration

Settings are read from the environment by pydantic-settings with no prefix (`notes_api.config.Settings`).

| Variable | Required | Notes |
| --- | --- | --- |
| `DATABASE_URL` | yes | SQLAlchemy URL; in a deployment `postgresql+psycopg://user:password@host:5432/db`. It carries a password: inject it from a secret store, never bake it into an image or commit it. A process without it refuses to start. SQLite URLs are for tests only. |
| `CONTRACT_PATH` | preset in the image | `/app/openapi.yaml`. Outside a container it defaults to the checkout's `openapi.yaml`. |
| `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL`, `OIDC_JWKS` | from slice 2 | Token verification (task T2.1). For the compose stack, put them in `.env`. |
| `FORWARDED_ALLOW_IPS` | behind a proxy | uvicorn trusts `X-Forwarded-*` headers from loopback only. Set the ingress CIDR, never `*`. |
| `WEB_CONCURRENCY` | leave unset | One uvicorn worker per container; scale with replicas. |

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
