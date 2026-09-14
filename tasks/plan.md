# Implementation Plan: Notes API server (contract-first, FastAPI)

## Context

The contract is frozen at 2.0.0 (PR #3 adds the license, changelog, versioning policy, breaking-change gate, Pages docs, and Prism mock). The next step of contract-driven development is a server that the contract validates and that the design guide's section 6 acceptance scenarios can run against. Today nothing executes: the checker is static, and the guide says authorization, atomicity, and the merge algorithm "require the following acceptance scenarios when a service is implemented."

Outcome: a FastAPI server under `server/` whose every response is validated against the spec's JSON Schemas in tests, that passes a Schemathesis run over all 47 operations, and whose test suite has one tagged module per row of the acceptance table. The contract stays the source of truth: request bodies are validated by the spec's own schemas at runtime, typed models are generated from `openapi.yaml`, and CI fails on drift.

Decisions Tim made on 2026-09-13 (not reopened here): Python with FastAPI, Pydantic models generated from the spec, Schemathesis; monorepo `server/`; uv with a lockfile; SQLAlchemy 2 + Alembic with SQLite for tests and PostgreSQL in production; PyJWT verifying JWKS from a configurable issuer, with a local RSA test issuer.

Local tooling present: uv 0.11.7, Python 3.12.11 (matches CI) and 3.14.0, Docker (no Postgres image yet). Current releases to pin: FastAPI 0.141, Pydantic 2.13, pydantic-settings 2.15, SQLAlchemy 2.0.52, Alembic 1.20, PyJWT 2.14, psycopg 3.3, datamodel-code-generator 0.80, Schemathesis 4.27, pytest 9.1, ruff 0.16, mypy 2.3.

## Architecture decisions

- **The contract is the only source of truth.** The server exposes the 47 operations under `/v1`, plus two operational probes outside it (`/healthz` and `/readyz`, not in the contract, blocked at the ingress; amended 2026-09-13), and nothing else (`openapi_url=None`); `openapi.yaml` changes only through the normal contract process, never to suit the server.
- **The spec validates requests at runtime.** Request bodies are validated against the spec's JSON Schemas (Draft 2020-12 through `referencing`, built exactly as the checker's `Contract` class does in `scripts/validate_contract.py:250-269`). This is the one refinement of the "generated Pydantic models" decision: datamodel-code-generator silently drops `if/then` (ReviewPolicy), `minProperties` (UpdateNote, ReviseEditRequest), the `PermissionSet` enum of arrays, and ordered `uniqueItems`, so a Pydantic validator would re-implement those rules by hand and drift. The generated models remain the typed layer that validated dicts are parsed into, with a drift check in CI.
- **Responses are dicts from one serializer module**, validated in tests against the spec. Response shapes carry per-status `if/then` and `unevaluatedProperties: false`; dict-building is shorter and the test fixture proves conformance on every request.
- **The acceptance table drives the test suite.** Each test is tagged with its section 6 row; an audit asserts all 17 rows are covered.
- **One check ladder for every mutation** (below), grounded in design guide section 2 ("Authenticate first. Resolve access ... before returning content, lifecycle details, ETags, or conflict data") and section 4 ("an old ETag returns `412`; using the current ETag with an invalid transition returns `409`").
- **Merge engine is a pure module built first.** No server or database dependency, byte-exact fixtures from the spec's examples, property tests.
- **Whole schema in one migration.** The data model is fully specified by the contract; slices add code, not tables.
- **The container image is the delivery unit** (added 2026-09-13). One digest-pinned, non-root, read-only image built from the repository root because it needs `openapi.yaml`; the same image runs `alembic upgrade head` as a separate step, never at process start. No shell entrypoint, no uv, tests, or dev dependencies at runtime. Tests stay uv-native in `server.yml`; `image.yml` proves the artifact.
- **Fail fast on configuration** (added 2026-09-13; extended 2026-09-14). `Settings()` raises when `DATABASE_URL`, `OIDC_ISSUER`, or `OIDC_AUDIENCE` is unset or when the key source is not exactly one of `OIDC_JWKS_URL`/`OIDC_JWKS`, and Alembic reads the same `Settings`, so a misconfigured container refuses to boot instead of quietly running on SQLite or accepting no token. `load_settings()` reports the variables, never their values, so the database password cannot reach the logs. Liveness has no dependencies and readiness pings the database, so a database incident never restarts the fleet but does take dead replicas out of rotation.
- **Routes are registered directly on the app** (added 2026-09-14). FastAPI 0.141 wraps `include_router` routes in a private object the test harness cannot resolve, so every operation goes through `routers.add_route` (`app.add_api_route` with the `/v1` prefix) and the `ContractClient` rejects any other kind of route.
- **The image carries a development identity provider** (added 2026-09-14). The compose stack and the smoke test have no issuer, so `python -m notes_api.dev_issuer` generates a key pair, prints the `.env` lines the stack reads, and mints tokens; the tests' local issuer is the same class. A deployment configures a real provider through `OIDC_JWKS_URL` and never sets `NOTES_DEV_ISSUER_KEY`.

## Defaults you can veto

Runtime and layout
1. Layout: `server/pyproject.toml`, `server/src/notes_api/` with `main.py`, `config.py`, `clock.py`, `db.py`, `models.py`, `uow.py`, `contract.py`, `etags.py`, `cursors.py`, `serializers.py`, `cli.py`, `generated/schemas.py` (never edited by hand), `http/` (problems, middleware, bodies, deps), `auth/`, `merge/`, `services/`, `routers/`; tests in `server/tests/` with `merge/`, `conformance/`, and one acceptance module per row.
2. Sync SQLAlchemy 2.0 with plain `def` endpoints in FastAPI's threadpool. No async database layer.
3. Configuration via pydantic-settings: `DATABASE_URL` (required, no default; SQLite is tests-only), `OIDC_ISSUER` and `OIDC_AUDIENCE` (required, non-blank; amended 2026-09-14), exactly one of `OIDC_JWKS_URL` or an inline `OIDC_JWKS` (the compose stack and the tests use the inline form), `CONTRACT_PATH` (defaults to the checkout's `openapi.yaml`; the image sets `/app/openapi.yaml`). An empty variable counts as unset (`env_ignore_empty`), so compose can pass `${OIDC_JWKS_URL:-}`.
4. Probes are `GET /healthz` (liveness, no dependencies) and `GET /readyz` (readiness, `SELECT 1`, `503` when the database is unreachable, no connection details in the body), outside `/v1` and outside the contract, blocked at the ingress. Superseded on 2026-09-13 the earlier "unauthenticated `GET /v1/me` returning the 401 Problem": Kubernetes `httpGet` probes fail on any status of 400 or above, so that signal could never serve as a probe.
5. Delivery: pull requests per group of slices (0-1, then 2 and 3 separately as PR 2a and PR 2b since 2026-09-14, then 4, 5, and 6 separately as PR 3a, 3b, and 3c, then 7-9, 10-12, 13-14), each merged green before the next branch is cut from `main`, never stacked. `tasks/plan.md` and `tasks/todo.md` are committed in the first PR.

Contract plumbing
6. Bodies are read from the raw request, not declared as FastAPI parameters, so the order is `415` (not `application/json`), `400 malformed_request` (unparseable or empty when required), then `422 validation_failed` from the spec schema with `errors[]` (location `body`, RFC 6901 pointer; unknown fields reported per key with detail `unknown field`). Query, header, and path errors are `422` with the parameter name; a missing `If-Match` is `428`; a weak, list, or `*` value is `400` with a `header` error named `If-Match`. Body-less actions ignore `Content-Type`.
7. `Cache-Control: no-store` on every response through pure-ASGI middleware, including framework 404s and the last-resort 500 (which leaks nothing). Problem `type` is literally `https://notes-api.example.com/problems/{code}`. `Location` values are hand-built `/v1/...` paths.
8. An unknown route or an undeclared method on a known path is `404 not_found`, so the server never emits a status the contract does not declare. The two probe paths (default 4) are the only routes outside the contract.

Data
9. IDs are uuid4 (SQLAlchemy `Uuid`: CHAR(32) on SQLite, `uuid` on PostgreSQL). Timestamps go through a `UTCDateTime` type that accepts only aware UTC, stores microseconds on both backends, and renders `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
10. ETags are `secrets.token_hex(16)` per resource version in a `version` column, returned quoted; comparison is equality; no-op PATCH, repeated approve, and repeated revoke return the existing token. Because the request ETag is a stored column, the live `noteTitle` and `requiredApprovals` stay outside it as the contract requires.
11. Cursors are base64url JSON `{k: [created_at_us, id], f: fingerprint}` where the fingerprint is `sha256(caller|collection|canonical filters|limit)[:16]`; any decode or fingerprint failure is `400 invalid_cursor`. No HMAC: authorization is re-applied on every page, so a forged cursor can only reposition the caller's own view.
12. Search folds at write time: `title_fold` and `body_fold` columns hold Python `casefold()` text, the query is folded the same way, and `contains(autoescape=True)` makes `%` and `_` literal. Identical behavior on SQLite and PostgreSQL; no NFC normalization (documented).
13. Tags live in a `note_tags` table with a position column so order is retained; the `tag` filter is one `EXISTS` per value.
14. Diffs are computed on read from stored base and proposed text, never stored.
15. Trash expiry is enforced at read time (`now >= expires_at` is `404`). Physical purge is `uv run notes-api purge-expired` for an external scheduler.
16. SQLite is configured for real write serialization: `isolation_level=None`, `BEGIN IMMEDIATE` emitted on every transaction begin, WAL, `busy_timeout=5000`, `foreign_keys=ON`. Tests use a file database in `tmp_path`, never `:memory:`, so multiple connections work. SQLite is tests-only: there is no default `DATABASE_URL`, and containers run PostgreSQL, whose engine gets a five-second `connect_timeout`.

Auth
17. PyJWT with `PyJWKClient` (or inline JWKS), algorithms `RS256` and `ES256`, verifying signature, `iss`, `aud`, `exp`, `nbf` (60 s leeway, against system time: PyJWT has no injectable clock, so the `Clock` governs timestamps only; amended 2026-09-14), requiring `exp` and `sub`, and accepting a `typ` header only when it is `at+jwt` or `JWT`. `(iss, sub)` maps to one local user; provisioning is a lookup transaction and then, on a miss, a second transaction with a `SAVEPOINT` insert and an `IntegrityError` re-select, so concurrent first access cannot duplicate and `hooks.before_begin("provision_user")` fires exactly between the miss and the insert (with `BEGIN IMMEDIATE` a single transaction could not be interleaved on SQLite; amended 2026-09-14). `displayName` comes from `name`, else `preferred_username`, else `user-` plus the first 8 characters of `sub`, truncated to 200 code points, never refreshed. The 401 challenge is `Bearer realm="notes-api"`, adding `error="invalid_token"` when a token was present. Opaque tokens and introspection are deferred.

Testing and CI
18. The unit-of-work wrapper `transaction(session, op)` calls `hooks.before_begin(op)` (a no-op in production) before `BEGIN`. Race tests run a competitor inside that hook in its own session, which proves every check lives inside the transaction, deterministically on SQLite. Thread-and-barrier tests assert the outcome set (`{200, 412}`) and run unchanged on PostgreSQL.
19. A `ContractClient` test wrapper maps each response back to its path template and fails the test on an undeclared status, a body that violates the `$ref` schema, a missing or malformed required header, or a wrong media type. Schemathesis runs from slice 4 on an include list of implemented operation ids that grows per slice; slice 13 removes the filter. Configuration for Schemathesis 4.27 (2026-09-14): a lazy schema from a fixture that seeds a note, a team, and a membership and pins their ids as path parameters; the persona's bearer header with `with_security_parameters=False`; `positive_data_acceptance` and `ignored_auth` excluded (the first rejects the ladder's own `400`/`412`/`422`/`428`, the second triples every 2xx); `428` added to `missing_required_header`; the coverage phase positive-only without undeclared-method probes; 25 examples per operation, derandomized, both generation modes.
20. `.github/workflows/server.yml`: `uv sync --locked` (fails on lockfile drift instead of installing the stale lock), ruff, `mypy --strict`, pytest on SQLite, pytest on a PostgreSQL 17.11 service container, and the model drift check, on every push to `main` and every pull request. `.github/workflows/image.yml` on the same triggers: hadolint, a linux/amd64 build with the GitHub Actions layer cache, a Trivy gate on fixable CRITICAL and HIGH findings, and `server/scripts/smoke_image.sh` through `compose.yaml`.
21. Merge engine: lines split on `\n` only with ends kept (never `str.splitlines`, which also splits on `\x0b` and ` `), `SequenceMatcher(autojunk=False)` for both diff pairs, a custom unified formatter (difflib gets hunk headers right but cannot emit `\ No newline at end of file`), three-way walk of stable and unstable chunks with conflicts in one-based half-open base coordinates, title as one line with a virtual newline. No GPL dependencies.
22. Observability: structured JSON request logs with request id, route template, status, and Problem `code`; no metrics or tracing in this plan. Delivered 2026-09-14 (T4.0): one JSON line per request on stdout with time, request id, method, route, path, status, code, user id, and duration, never the query string, headers, or bodies; `X-Request-Id` is kept when it matches `^[A-Za-z0-9._-]{1,128}$`, generated otherwise, and echoed on every response; probes are not logged.
23. The server has no version of its own; `pyproject.toml` records the contract version it implements (2.0.0), and its arrival is a repository line under Unreleased in `CHANGELOG.md`.

Container (added 2026-09-13; the full rationale is in the container-first plan)
24. `Dockerfile` and an allow-list `.dockerignore` at the repository root; builder and runtime are both `python:3.12.14-slim-trixie` pinned by digest (the venv records the interpreter path, so the stages must match), uv 0.11.7 pinned by digest through a named `FROM` stage so Dependabot can see it. Two `uv sync --locked` steps (dependencies from the lockfile alone, then the project non-editable) with `UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 UV_NO_DEV=1`. The runtime stage runs `apt-get upgrade` once to apply the Debian security updates published after the pinned base was built (the first Trivy run caught 12 fixable CVEs in the 2026-09-02 build while Docker Hub had not rebuilt the tag); it installs nothing else. No alpine, distroless, uv-managed interpreter, `HEALTHCHECK`, `ENTRYPOINT`, tini, or `uv run`.
25. Runtime: uid and gid 10001 (numeric `USER`), `/app/.venv`, `/app/openapi.yaml`, `/app/alembic.ini`, `/app/alembic/` root-owned and read-only; `ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1 CONTRACT_PATH=/app/openapi.yaml`; exec-form `CMD uvicorn --factory notes_api.main:create_app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 20`; one worker per container, scaled with replicas; proxy headers trusted from loopback only unless `FORWARDED_ALLOW_IPS` names the ingress; OCI `source` and `licenses` labels.
26. `compose.yaml`: `db` (`postgres:17.11` by digest, `pg_isready` over TCP), `migrate` (one-shot `alembic -c /app/alembic.ini upgrade head`, `restart: "no"`), `api` (`depends_on` the healthy database and the completed migration; readiness healthcheck through the image's Python). `api` and `migrate` run `read_only` with `cap_drop: [ALL]`, `no-new-privileges`, and a tmpfs `/tmp`. Ports bind to `127.0.0.1` and move with `NOTES_API_PORT` and `NOTES_API_DB_PORT`; `NOTES_API_IMAGE` selects the image; an optional gitignored `.env` carries `OIDC_*`. No override file, watch mode, profiles, toolchain services, or devcontainer.
27. `server/scripts/smoke_image.sh` is the executable container contract (fail-fast boot, uid, capabilities, PID 1, read-only rootfs, no uv or test tooling, probe and 404 behaviour, readiness following the database, schema at head, clean SIGTERM, labels), run identically on a developer machine and in `image.yml`.
28. Not published to a registry yet. When it is: `ghcr.io`, tags `main` and `sha-<commit>`, provenance and SBOM attestations from `docker/build-push-action`, a manual visibility flip; multi-arch only when an arm64 consumer exists. Dependabot covers the `docker` and `docker-compose` ecosystems weekly, holding uv on 0.11.x, Python on 3.12, and PostgreSQL on 17.

## Data model

| Table | Columns | Constraints and indexes |
|---|---|---|
| `users` | `id`, `issuer`, `subject`, `display_name`, `created_at` | `UNIQUE(issuer, subject)`; `INDEX(created_at, id)` |
| `teams` | `id`, `name`, `created_at`, `updated_at` | `INDEX(created_at, id)` |
| `memberships` | `team_id` FK cascade, `user_id` FK, `role`, `joined_at`, `updated_at` | `PK(team_id, user_id)`; `INDEX(user_id)`; `INDEX(team_id, joined_at, user_id)` |
| `notes` | `id`, `author_id` FK, `title`, `body`, `title_fold`, `body_fold`, `review_mode`, `review_required_approvals` NULL, `created_at`, `updated_at`, `deleted_at` NULL, `expires_at` NULL, `version` | `CHECK ((deleted_at IS NULL) = (expires_at IS NULL))`; `CHECK ((review_mode = 'self_merge') = (review_required_approvals IS NULL))`; `INDEX(created_at, id)`; `INDEX(author_id)`; `INDEX(expires_at)` |
| `note_owners` | `note_id` FK cascade, `user_id` FK, `position`, `added_at` | `PK(note_id, user_id)`; `UNIQUE(note_id, position)` (author is 0); `INDEX(user_id)` |
| `note_tags` | `note_id` FK cascade, `position`, `tag` | `PK(note_id, position)`; `UNIQUE(note_id, tag)`; `INDEX(tag)` |
| `shares` | `id`, `note_id` FK cascade, `recipient_type`, `recipient_id`, `can_comment`, `can_propose`, `created_at`, `updated_at` | `UNIQUE(note_id, recipient_type, recipient_id)` maps to `409 duplicate_share`; `INDEX(recipient_type, recipient_id)`; `INDEX(note_id, created_at, id)`. `read` is implied, so it is not a column |
| `comments` | `id`, `note_id` FK cascade, `author_id` FK, `body`, `created_at`, `updated_at`, `version` | `INDEX(note_id, created_at, id)` |
| `edit_requests` | `id`, `note_id` FK cascade, `proposer_id` FK, `status`, `base_title`, `base_body`, `proposed_title`, `proposed_body`, `explanation` NULL, `created_at`, `updated_at`, `closed_at` NULL, `rejected_by` NULL FK, `rejection_reason` NULL, `merged_title`, `merged_body`, `merged_note_etag`, `merged_by` FK, `merged_at` (NULL unless merged), `required_approvals_at_close` NULL, `version` | `CHECK ((status = 'open') = (closed_at IS NULL))`; `INDEX(note_id, status, created_at, id)`; `INDEX(proposer_id, status, created_at, id)` |
| `approvals` | `request_id` FK cascade, `user_id` FK, `approved_at` | `PK(request_id, user_id)`; rows change only while the request is open, so freezing needs no copy; `requiredApprovals` is otherwise live, hence `required_approvals_at_close` |
| `request_comments` | `id`, `request_id` FK cascade, `author_id` FK, `body`, `created_at`, `updated_at`, `version` | `INDEX(request_id, created_at, id)` |

Effective `requiredApprovals`: `0` when the note has one owner or `self_merge`; otherwise `min(N, owners - 1)` for an owner's proposal and `max(2, min(N, owners))` for anyone else's; closed requests return `required_approvals_at_close`.

## Locking and the check ladder

Lock order is always note, then edit request, then child row; and team, then membership. Team mutations never lock a note and note-scoped mutations never lock a team, so no cycle exists. Every note-scoped mutation locks the note row `FOR UPDATE` even when it does not write the note (one primitive; the cost is serialized comment inserts per note). Child mutations whose authorization came from shares or memberships also lock those rows `FOR SHARE`, because the guide requires that "if revocation wins the race, an unauthorized mutation must not commit" and a membership removal locks the team, not the note. `removeOwner` additionally locks the note's open requests to delete the leaver's approvals and bump those request versions. `mergeEditRequest` locks note then request and rechecks both versions, both states, the owner set, and the approval count before writing. Preview locks nothing: one joined `SELECT` is the "coherent pair". Provisioning locks nothing: `SAVEPOINT` insert, `IntegrityError`, re-select. On PostgreSQL `with_for_update()` must be a bare `SELECT ... WHERE id = ?` first, since `FOR UPDATE` rejects the nullable side of outer joins; SQLite drops the clause and relies on `BEGIN IMMEDIATE`.

Check ladder, in order: `401`; request shape (`415`, `400 malformed_request`, `422` for body, query, header, or path); `404` visibility (hidden, expired, or mis-nested resource; request not inspectable); `403`; `If-Match` presence and shape (`428`, `400`); `412` version; `409` lifecycle (`note_not_active`, `request_not_open`, `note_already_active`); semantic `422` that needs resource state (`/finalContent` under peer approval, identical-to-base `/proposedContent`, unknown `/userId`, `/requiredApprovals` above the owner count, `/recipient/id` is an owner); business `409` (`approval_required`, then `merge_conflict`, `duplicate_*`, `last_admin`, `author_cannot_be_removed`, `direct_edit_not_allowed`); write. Bodies are parsed before the transaction, so no lock is held while they are read; the team mutations were aligned to this order on 2026-09-14.

## Interpretations pinned by tests and listed in the server README

- `403` is evaluated before `428`/`400` for `If-Match` (both orders are defensible; this one never reveals precondition details to a caller who may not act).
- Unknown route or undeclared method: `404 not_found`.
- An owner who proposed on their own note sees that request under `view=outgoing&state=trashed`; the contract's "empty" statement is about non-owner proposers.
- `approval_required` is checked before `merge_conflict`.
- Display-name claim precedence: `name`, `preferred_username`, `user-<sub prefix>`; never refreshed after provisioning.
- A missing `typ` header is accepted; a present one must be `at+jwt` or `JWT` (case-insensitive).
- Cursors are bound to the caller as well as the collection, filters, and limit.
- Membership mutations check that the caller may act (`403`) before whether the target membership exists (`404`), so a nonmember cannot probe who belongs to a team.
- Request strings may not contain U+0000 (`422` at the field's pointer); PostgreSQL text cannot store it. Added 2026-09-14 after the Schemathesis run produced a 500 on PostgreSQL.
- Search folds with `casefold()` and does not NFC-normalize.

## Task list

Every task also meets this definition of done: ruff and `mypy --strict` clean, pytest green on SQLite, the `ContractClient` validating every response, acceptance tests tagged with their section 6 row, and `openapi.yaml` untouched. Sizes: S is up to half a day, M about a day. A checkpoint is ruff, mypy, pytest on SQLite, the drift check, and the Schemathesis include-list run, all green, committed on the branch.

### Slice 0: scaffold and contract plumbing (PR 1 with slice 1)

**T0.1 Project scaffold and CI (S).** uv project with runtime deps (fastapi, uvicorn, sqlalchemy, alembic, pydantic, pydantic-settings, pyjwt[crypto], jsonschema, referencing, pyyaml, rfc3339-validator, psycopg[binary]) and dev deps (pytest, httpx, hypothesis, schemathesis, datamodel-code-generator, ruff, mypy); `server.yml` with lint, types, SQLite tests, drift check, and a PostgreSQL service job; `tasks/plan.md` and `tasks/todo.md`.
AC: `uv sync --frozen` works from a clean checkout; an empty suite runs green; the workflow passes on the PR without touching the contract workflows.
Verify: run the CI steps locally. Deps: none. Files: `server/pyproject.toml`, `server/uv.lock`, `.github/workflows/server.yml`, `server/README.md` stub, `tasks/`.

**T0.2 Contract loader, generated models, drift check (M).** `contract.py` loads `openapi.yaml`, builds the `referencing` registry like the checker, exposes `validate_body(schema_name, instance)` returning `FieldError` dicts (unknown keys reported individually as `unknown field`; `required` reported at the missing key's pointer; otherwise `absolute_path` as a JSON Pointer) with a cached `Draft202012Validator` per schema and the format checker on. Generate `generated/schemas.py` (pydantic v2, annotated, field constraints, standard collections, strict nullable, no timestamp, never unique-items-as-set); a script regenerates to a temp file and diffs.
AC: every `tests/negative_cases.yaml` must_fail and must_pass payload gets the expected verdict through `validate_body`; the `ProblemValidationFailed` example's two errors (`/title`, `/authorId` unknown field) are reproduced from `{"title": "   ", "authorId": "..."}` against `CreateNote`; the drift check fails when the generated file is edited.
Verify: `tests/test_contract_loader.py`. Deps: T0.1. Files: `contract.py`, `generated/schemas.py`, `server/scripts/gen_models.sh`.

**T0.3 Problems, middleware, body parser, If-Match parser (M).** `http/problems.py` exception hierarchy and handlers (Problem, `RequestValidationError` split into `400 malformed_request` for `json_invalid` versus `422` with location from `loc[0]`, `HTTPException` 404/405 to `404 not_found`, last-resort 500); ASGI middleware for `Cache-Control`; `http/bodies.py::parse_body(request, schema_name, required)`; `etags.py::parse_if_match`, `new_version`, `quote`.
AC: malformed JSON `400`, wrong media type `415`, unknown field `422` with pointer, missing `If-Match` `428`, `W/"x"`, `*`, and `"a", "b"` `400` with `errors[0].pointer == "If-Match"`; every response including 401, 404, 405, and 500 carries `Cache-Control: no-store` and errors are `application/problem+json`; 401 carries the bearer challenge, with `error="invalid_token"` when a token was present.
Verify: `tests/test_problems.py` against a throwaway route. Deps: T0.2. Files: `http/problems.py`, `http/middleware.py`, `http/bodies.py`, `etags.py`.

**T0.4a ORM models and initial migration (M).** `models.py` with every table above, the `UTCDateTime` type, CHECK constraints, indexes, and FK cascades; `alembic/versions/0001_initial.py`; a test that upgrades a fresh database and asserts autogenerate finds no difference.
AC: the migration applies on SQLite and PostgreSQL; metadata and migration agree; deleting a note cascades to its children on SQLite with `foreign_keys=ON`.
Verify: `tests/test_schema.py`. Deps: T0.1. Files: `models.py`, `db.py` (type only), `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial.py`.

**T0.4b Engine, session, unit of work, clock (M).** `db.py::make_engine(url)` with the SQLite connect hooks and the `BEGIN IMMEDIATE` begin event, PostgreSQL `QueuePool` with `pool_pre_ping`; `uow.py::transaction(session, op)` calling `hooks.before_begin(op)` then `session.begin()`, `expire_on_commit=False`; `clock.py` with a `Clock` protocol and `SystemClock`; `config.py`.
AC: two threads on a file SQLite database doing read-then-write inside `transaction` serialize with no lost update and no `database is locked` surfacing as 500; `hooks.before_begin` runs before the transaction starts and can run a competitor in its own session; `UTCDateTime` round-trips microseconds and returns aware UTC on both backends.
Verify: `tests/test_db.py`. Deps: T0.4a. Files: `db.py`, `uow.py`, `clock.py`, `config.py`.

**T0.5 Test harness and ContractClient (M).** `conftest.py` with a per-test file SQLite database in `tmp_path` (or PostgreSQL with truncation), `FakeClock`, the local RSA issuer fixture (keypair, inline JWKS through settings, `token_for(sub, name, aud, exp)`), personas `ada`, `ben`, `cara`, `dan`, and `ContractClient` over httpx's ASGI transport that resolves the matched route template and validates status, body, headers, and media type against the spec. `tests/test_ladder.py` skeleton.
AC: a deliberately wrong response body fails a test through the client; an undeclared status fails; personas get distinct provisioned ids.
Verify: `tests/test_harness.py`. Deps: T0.3, T0.4b. Files: `tests/conftest.py`, `tests/contract_client.py`, `tests/test_ladder.py`.

**Checkpoint A.**

### Slice 1: merge engine, pure

**T1.1 Line splitting and unified diff (M).** `merge/lines.py::split_lines` (split on `\n` only, ends kept, `join(split(s)) == s`); `merge/unified.py::unified_diff` using `SequenceMatcher(autojunk=False).get_grouped_opcodes(3)` and difflib's range formatting, emitting `\ No newline at end of file` after any line lacking `\n`; `field_diff` returns `""` when equal; `title_diff` wraps each title as one line with a virtual newline.
AC: byte-exact equality with every `proposalDiff` and `mergeDiff` string in the spec's examples, loaded programmatically (EditRequestOpen, EditRequestConflicting, PreviewClean, PreviewWithFinalContent, PreviewConflict, PreviewConflictResolved); missing-final-newline and CRLF fixtures match committed `git diff --no-index` output; Hypothesis round-trip of `split_lines` over random Unicode including `\r`, `\x0b`, ` `.
Verify: `tests/merge/test_unified.py`. Deps: T0.1. Files: `merge/lines.py`, `merge/unified.py`.

**T1.2 Three-way merge with conflicts (M).** `merge/three_way.py::merge_title` per the guide's title rule; `merge_body` implementing diff3 over matched blocks from both `SequenceMatcher` pairs, walking stable and unstable chunks: one side unchanged takes the other, both equal takes once, otherwise a `Conflict(field="body", base_range, base, current, proposed)` with one-based half-open coordinates and `start == end`, `base == ""` for insertions; `preview(base, current, proposed, final=None)` producing candidate, conflicts, `proposalDiff`, `mergeDiff` with `current`/`candidate` labels, `canMerge`, `usedFinalContent`.
AC: the `PreviewConflict` example's two conflicts (title with `baseRange: null`; body `{start: 5, end: 6}` with its exact segments) are reproduced from its texts; separate-region edits combine and identical edits appear once; competing insertions at the same base line, overlapping replace versus delete, and incompatible titles are conflicts with no silent preference.
Verify: `tests/merge/test_three_way.py`. Deps: T1.1. Files: `merge/three_way.py`.

**T1.3 Text-merge acceptance suite (S).** Table-driven cases for the "Text merge" row: empty base, empty proposed, Unicode (combining marks, emoji, RTL), mixed CRLF and LF, missing final newline on one or both sides, insertion at start and end, deletion versus edit of the same line, candidate equal to current, deterministic under a seeded property test, a 5,000-line body under one second.
AC: every item the guide lists for "Text merge" has a named test; results are deterministic; the performance bound holds.
Verify: `tests/merge/test_acceptance.py` tagged `acceptance("Text merge")`. Deps: T1.2. Files: tests only.

**Checkpoint B.**

### Slice 2: auth, /me, /users (PR 2 with slice 3)

**T2.1 JWT verification and 401 (M).** `auth/jwt.py` with `PyJWKClient` or inline JWKS, algorithm allowlist, `iss`/`aud`/`exp`/`nbf` checks, rejection of a `typ` that is neither `at+jwt` nor `JWT`, `Identity(issuer, subject, display_name)`; `http/deps.py::current_user` raising the 401 Problem with the challenge.
AC: missing, expired, wrong-audience, wrong-issuer, and unsigned tokens are `401` with `WWW-Authenticate`; a valid token passes; a JWKS fetch failure is `401`, not `500`; `docker compose up --wait` still boots the api, with any newly required `OIDC_*` setting documented as a `.env` key in `server/README.md`.
Verify: `tests/test_auth.py`. Deps: T0.5. Files: `auth/jwt.py`, `http/deps.py`, `config.py`.

**T2.2 Provisioning and GET /me (S).** `auth/provisioning.py::get_or_create_user` (a lookup transaction; on a miss a second transaction with a `SAVEPOINT` insert, `IntegrityError`, re-select; amended 2026-09-14, see default 17); router for `/v1/me`; `main.py` app factory wiring.
AC: `GET /me` twice returns the same id; eight threads with one fresh identity produce one row; a hook-based test that inserts the identity in `before_begin("provision_user")`, between the miss and the insert, still succeeds with that row.
Verify: `tests/test_users.py` tagged `acceptance("Directory and teams")`. Deps: T2.1. Files: `auth/provisioning.py`, `routers/users.py`, `services/users.py`, `main.py`.

**T2.3 Cursor codec, pagination helper, GET /users and /users/{userId} (M).** `cursors.py::encode/decode`; `paginate(query, order, limit, cursor)` fetching `limit + 1` with a `tuple_` row-value comparison on `(created_at, id)`; `GET /users` sorted `createdAt DESC, id DESC`; `GET /users/{userId}` with `404`.
AC: `limit=0`, `101`, and `abc` are `422` with `location: query, pointer: limit`; a cursor from `limit=2` reused with `limit=3`, garbage, or empty is `400 invalid_cursor`; two users with identical `created_at` under `FakeClock` paginate deterministically by id at `limit=1`.
Verify: `tests/test_users.py`, `tests/test_cursors.py`. Deps: T2.2. Files: `cursors.py`, `services/users.py`, `routers/users.py`, `serializers.py`.

**Checkpoint C.**

### Slice 3: teams and memberships

**T3.1 Teams CRUD (M).** `POST /teams` with the creator as admin in the same transaction, `GET /teams?scope=all|mine`, `GET /teams/{teamId}`, admin-only unconditional `PATCH`, admin-only `DELETE` cascading memberships and deleting team-addressed shares; `Location: /v1/teams/{id}`.
AC: non-admin member and nonmember get `403` on PATCH and DELETE and an unknown team is `404`; `scope=mine` lists only the caller's teams; deleting a team leaves notes, comments, and requests intact and removes its shares (asserted at the table level until slice 6).
Verify: `tests/test_teams.py`. Deps: T2.3. Files: `services/teams.py`, `routers/teams.py`, `serializers.py`.

**T3.2 Memberships with last-admin atomicity (M).** `GET /teams/{teamId}/members` (members only, nonmember `403`, `joinedAt DESC, userId DESC`), `POST` (admin; unknown user `422 /userId`; duplicate `409 duplicate_membership`; default role `member`; `Location`), `PATCH` (admin; last-admin demotion `409 last_admin`; non-member `404`), `DELETE` (admin removes anyone, member removes only themselves, else `403`; last admin `409 last_admin`); every mutation locks the team row first.
AC: nonmember listing is `403`, not `404`; two threads demoting or removing a team's two admins yield exactly one success; a member leaving is `204` while a lone admin leaving is `409`.
Verify: `tests/test_memberships.py` tagged `acceptance("Directory and teams")`. Deps: T3.1. Files: `services/teams.py`, `routers/teams.py`.

**Checkpoint D.** "Directory and teams" row complete.

### Slice 4: notes create, read, update (PR 3 with slices 5 and 6)

**T4.1 Permission resolver and note serializer (M).** `services/permissions.py::resolve(session, note, user_id, lock=False)` returning `Access(is_owner, read, comment, propose)` as owner-all or the union of direct and team shares joined with current memberships (`lock=True` adds `with_for_update()` to those selects); `visible(note, access, now)`; `serializers.note` and `note_summary` with `ownerIds` by position, `reviewPolicy`, canonical `effectivePermissions`, `isOwner`.
AC: a table-driven test over all share combinations returns canonical order; an owner holding an extra share still shows all three permissions and `isOwner: true`; serializer output validates against `Note` and `NoteSummary`.
Verify: `tests/test_permissions.py`. Deps: T0.5. Files: `services/permissions.py`, `serializers.py`.

**T4.2 POST /notes and GET /notes/{noteId} (M).** Create with defaults (`body=""`, `tags=[]`, author at position 0, `self_merge`, fold columns, version), `201` with `Location` and `ETag`; `GET` with `404` for missing, hidden, or expired notes and the `ETag` header.
AC: create and get round-trip validates with matching ETags; a stranger gets `404`, not `403`; `id` or `authorId` in the body is `422 unknown field`.
Verify: `tests/test_notes.py`. Deps: T4.1. Files: `services/notes.py`, `routers/notes.py`.

**T4.3 PATCH /notes/{noteId} (M).** (Amended 2026-09-14: hook seams use snake_case operation names such as `update_note`; the request-log task T4.0 and the team ladder alignment T4.0b precede this slice.) The ladder end to end: note locked `FOR UPDATE`, `If-Match` parsed after `403`, `412` on mismatch, `409 note_not_active` when trashed, owner count read under the lock so `title` or `body` on a protected note is `409 direct_edit_not_allowed`, tags replaced wholesale, no-op returns the existing version. Start the Schemathesis include list with the user, team, and note operations so far.
AC: title, body, and tag updates advance ETag and `updatedAt` while an identical PATCH returns the same ETag; a reader is `403`, missing `If-Match` is `428`, `W/"x"` is `400`, stale is `412`; a white-box test that inserts a second owner row through `hooks.before_begin` makes a body PATCH `409 direct_edit_not_allowed` while a tags-only PATCH succeeds.
Verify: `tests/test_notes.py`, `tests/test_ladder.py`, tagged `acceptance("Atomicity/errors")`. Deps: T4.2. Files: `services/notes.py`, `routers/notes.py`, `tests/conformance/test_schemathesis.py`.

**Checkpoint E.**

### Slice 5: trash, restore, list, search

**T5.1 Trash, restore, expiry, purge CLI (M).** `DELETE /notes/{noteId}` (lock, owner, `If-Match`; a repeat with the trash version is `204` with the same ETag and no change; otherwise set `deleted_at`, `expires_at = now + 720h`, delete shares, bump version; `204` with `ETag`); `POST /restore` (`409 note_already_active`; expired `404`; clear timestamps, bump version, shares stay absent); `now >= expires_at` is `404` everywhere; `cli.py purge-expired`.
AC: trash removes shares and the `204` ETag restores, and a repeated DELETE with the trash ETag does not move `expiresAt`; with `FakeClock` at exactly `expiresAt` both GET and restore are `404` while one microsecond earlier both work; restore advances the ETag with unchanged content.
Verify: `tests/test_trash.py` tagged `acceptance("Trash")`, `tests/test_cli.py`. Deps: T4.3. Files: `services/notes.py`, `routers/notes.py`, `cli.py`.

**T5.2a GET /notes: scope, state, keyset, dedupe (M).** Access as `EXISTS` subqueries (owner row, direct share, team share via membership) so each note appears once; `scope=mine` is owner, `shared` is readable and not owner; `state=trashed` is owner with an unexpired `deleted_at`; sort `created_at DESC, id DESC`; the cursor fingerprint covers all filters.
AC: a note shared directly and through two teams appears once; `scope=shared&state=trashed` is empty; revoking access between page one and page two hides the note on page two and the cursor still works.
Verify: `tests/test_list_notes.py` tagged `acceptance("Lists")`. Deps: T5.1, T2.3. Files: `services/notes.py`.

**T5.2b GET /notes: q, tag, teamId (M).** `q` folded and matched against both fold columns with `autoescape`; `tag` repeatable up to 10, unique, each valid (else `422 location: query, pointer: tag`), one `EXISTS` per tag; `teamId` as an `EXISTS` on team shares that grants nothing.
AC: `q=STRASSE` finds "Straße" and `q=%` and `q=_` are literal; `tag=Release` does not match `release`, two tags require both, eleven tags are `422`; `teamId` with `state=trashed` is empty, a direct recipient who is not a member still matches `teamId`, an unknown team is an empty `200`.
Verify: `tests/test_list_notes.py`. Deps: T5.2a. Files: `services/notes.py`, `routers/notes.py`.

**Checkpoint F.**

### Slice 6: shares and visibility

**T6.1 Shares CRUD (M).** Owner-only list, get, create, update, delete under the note lock; trashed note: create `409 note_not_active`, others `404`; recipient user must exist and not be an owner, team must exist, else `422 /recipient/id`; duplicate `409 duplicate_share` from the unique constraint; canonical permissions; `Location`; a child from another note is `404`.
AC: `["propose_edit"]` returns `["read", "propose_edit"]` and PATCH `["comment", "propose_edit"]` returns all three; sharing to the author, a co-owner, an unknown user, or an unknown team is `422` with pointer `/recipient/id`; `/notes/A/shares/{share of B}` is `404`.
Verify: `tests/test_shares.py` tagged `acceptance("Permission combinations")`. Deps: T5.2b. Files: `services/shares.py`, `routers/shares.py`.

**T6.2 Overlapping grants, revocation, admins gain nothing (M).** Scenario tests for direct plus team grants, removing each path, `effectivePermissions`, visibility, list membership; a recipient team's admin cannot PATCH, share, or trash; deleting a team removes its share path; the `lock=True` path exercised (on PostgreSQL a membership removal blocked behind an in-flight comment commits after it).
AC: removing the direct grant keeps team capabilities and vice versa; losing the last read path makes GET `404`; a team admin without a share gets `404` and with a read-only share gets `403` on owner operations.
Verify: `tests/test_access_paths.py` tagged `acceptance("Overlapping grants")` and `acceptance("Isolation")`; the section 5 "Create and share" flow end to end. Deps: T6.1. Files: tests, small changes in `services/permissions.py`.

**Checkpoint G.**

### Slice 7: comments (PR 4 with slices 8 and 9)

**T7.1 List, get, create comments (S).** Readers list and get (`createdAt ASC, id ASC`); create needs `comment` (owners always) else `403`; trashed: owners read, create `409 note_not_active`; `201` with `Location` and `ETag`.
AC: a proposal-only recipient lists `200` and creates `403`; a comment reached through another note's path is `404`; creating a comment leaves the note ETag unchanged.
Verify: `tests/test_comments.py` tagged `acceptance("Comments")`. Deps: T6.2. Files: `services/comments.py`, `routers/comments.py`.

**T7.2 Update and delete comments (M).** PATCH by the author with current `comment` permission and the comment ETag, owners editing another's comment `403`; DELETE by any owner or by the author with current permission, permanent; trashed `409 note_not_active`.
AC: owner PATCH of another's comment is `403` and owner DELETE is `204`; an author downgraded to read gets `200` on GET and `403` on PATCH and DELETE; a stale comment ETag is `412` and a missing one `428`.
Verify: `tests/test_comments.py`. Deps: T7.1. Files: same.

**Checkpoint H.**

### Slice 8: edit requests: submit, inspect, list, revise, withdraw, reject

**T8.1 POST /notes/{noteId}/edit-requests (M).** Lock note and permission rows; `propose_edit` else `403`; `baseNoteETag` versus version is `412` creating nothing; trashed `409`; proposed equal to the base is `422 /proposedContent`; base captured from the row; `201` with the flat `Location` and the request ETag; note untouched.
AC: note ETag and `updatedAt` unchanged after submission; a stale base is `412` with zero rows written; an owner may submit on their own note, and `baseContent` or `tags` in the body is `422 unknown field`.
Verify: `tests/test_edit_requests.py` tagged `acceptance("Submission")`. Deps: T7.2, T1.1. Files: `services/edit_requests.py`, `routers/edit_requests.py`.

**T8.2 GET /edit-requests/{requestId} and serializer (M).** Inspect rule (owner, or proposer with current read; trashed owners only; expired nobody) else `404`; serializer computes `proposalDiff`, live `noteTitle`, effective or frozen `requiredApprovals`, sorted `approvals`, status-dependent nulls.
AC: a read-only third party gets `404`; output validates for open, merged, rejected, and withdrawn fixtures against `EditRequest`; the ETag equals the stored version and does not change when the note title changes.
Verify: `tests/test_edit_requests.py` tagged `acceptance("Isolation")`. Deps: T8.1. Files: `serializers.py`, `services/edit_requests.py`.

**T8.3 Note-scoped list and inbox (M).** `GET /notes/{noteId}/edit-requests` (owner all, proposer own, other readers an empty page, non-readers `404`; `status` defaults to `open`); `GET /edit-requests` with `view`, `status`, `state`; sort `createdAt DESC, id DESC`; summaries carry live `noteTitle`, `requiredApprovals`, `approvals`.
AC: an owner's own proposal appears in both views; a non-owner proposer with `state=trashed` gets an empty page; `noteTitle` reflects a later PATCH of the note.
Verify: `tests/test_inbox.py` tagged `acceptance("Lists")`. Deps: T8.2. Files: `services/edit_requests.py`, `routers/edit_requests.py`.

**T8.4 Revise, withdraw, reject (M).** PATCH by the proposer with current `propose_edit` else `403`, request `If-Match`, closed `409 request_not_open`, trashed `409 note_not_active`, identical-to-base `422 /proposedContent`, no effective change returns the existing ETag, content change deletes all approvals, explanation-only keeps them; withdraw by the proposer with read (else `404`), owners `403`; reject by owners (`403` for a non-owner proposer) with `rejectedBy` and optional `reason`; closing sets `closed_at` and `required_approvals_at_close` and bumps the version.
AC: a revision of only `explanation: null` keeps seeded approvals and clears the explanation, and an identical resubmission returns the same ETag; a read-only proposer withdraws `200` and revises `403`, a no-read proposer withdraws `404`; an old ETag on a closed request is `412` and the current one is `409 request_not_open`.
Verify: `tests/test_edit_requests.py` tagged `acceptance("Lifecycle")` and `acceptance("Revoked proposers")`. Deps: T8.3. Files: same.

**Checkpoint I.**

### Slice 9: preview and merge under self_merge

**T9.1 POST /edit-requests/{requestId}/preview (M).** Owner else `403` or `404`; one joined `SELECT`; closed `409 request_not_open`; trashed `409 note_not_active`; body optional; `finalContent` under `peer_approval` on a protected note is `422 /finalContent` (wired now, exercised in slice 11); returns `requestETag`, `currentNoteETag`, and the engine's preview result; no writes.
AC: the `PreviewClean`, `PreviewConflict`, and `PreviewConflictResolved` shapes are reproduced from seeded rows, including retained conflicts with `usedFinalContent: true`; two consecutive previews change no version or `updatedAt`; after a note PATCH, `currentNoteETag` reflects the new version and the preview compares against the new body.
Verify: `tests/test_preview.py` tagged `acceptance("Owner adjustments")`. Deps: T8.4, T1.3. Files: `services/edit_requests.py`, `routers/edit_requests.py`.

**T9.2 POST /edit-requests/{requestId}/merge (M).** The ladder: owner; `If-Match` `428`/`400`; `412` if the request or `expectedNoteETag` mismatches; `409 request_not_open` or `note_not_active`; `422 /finalContent` under peer approval; `409 approval_required` (count per the guide, `0` under `self_merge`); candidate computed or `finalContent` taken; `409 merge_conflict`; write note title, body, folds, version, and `updated_at` only, plus the request's merged columns, `closed_at`, `required_approvals_at_close`, version; `MergeResult` with the request's ETag header.
AC: a merge whose candidate equals live content still advances the note version and records `mergeRecord`; a `finalContent` merge stores merged content apart from `proposedContent` with tags and owners unchanged; any failing check (stale, conflict, trashed) leaves both rows byte-identical by snapshot comparison.
Verify: `tests/test_merge.py` tagged `acceptance("Atomicity/errors")`. Deps: T9.1. Files: same.

**T9.3 Review race tests (M).** Hook-based deterministic tests with the competitor in `hooks.before_begin`: two merges of different requests on one note, revise versus merge, withdraw versus merge, reject versus merge, note PATCH after preview; thread-and-barrier tests on file SQLite asserting outcome sets, reused unchanged by the PostgreSQL job.
AC: the second merge is `412` with the first merge's content intact; revise-then-merge with the pre-revision ETag is `412`; withdraw-then-merge is `412` with the old ETag and `409 request_not_open` with the new one.
Verify: `tests/test_races.py` tagged `acceptance("Review races")`. Deps: T9.2. Files: tests, possibly hook naming in `uow.py`.

**Checkpoint J.** The section 5 "Submit" and "Preview and merge" flows pass end to end.

### Slice 10: owners, review policy, protected notes (PR 5 with slices 11 and 12)

**T10.1 Add and remove owners (M).** `POST /notes/{noteId}/owners`: author only (`403` for co-owners and readers, `404` for non-readers), lock, `If-Match`, trashed `409`, unknown user `422 /userId`, existing owner `409 duplicate_owner`, twenty-first `422 /userId`, next position, existing share kept. `DELETE /notes/{noteId}/owners/{userId}`: author removes any co-owner, a co-owner removes only themselves, else `403`; non-owner target `404`; author target `409 author_cannot_be_removed`; open requests locked, the leaver's approvals deleted, those request versions bumped.
AC: a share held by the new owner persists and is effective again after removal; removal deletes the leaver's approvals on open requests and changes only those requests' ETags while closed requests are untouched; the note ETag advances on both, so an earlier `baseNoteETag` or `expectedNoteETag` fails with `412`.
Verify: `tests/test_owners.py` tagged `acceptance("Owner control")`. Deps: T9.3. Files: `services/ownership.py`, `routers/ownership.py`.

**T10.2 Review policy and protected-note behavior (M).** `PATCH /notes/{noteId}/review-policy`: author only, full `ReviewPolicy` body validated by the spec schema, `requiredApprovals` above the owner count `422 /requiredApprovals`, a single-owner note accepts only `1`, stored, ETag advanced. End-to-end protection tests: body PATCH on a two-owner note `409 direct_edit_not_allowed`, tags-only `200`, removing the second owner restores direct edits and leaves open requests mergeable.
AC: the guide's section 5 "Protect a note" walkthrough is reproduced through `runbook-v3`; `requiredApprovals: 2` on a single-owner note is `422`; a stored `peer_approval` of 3 with two owners yields an effective `requiredApprovals` of 1 for an owner's proposal.
Verify: `tests/test_review_policy.py` tagged `acceptance("Protected notes")`. Deps: T10.1. Files: same.

**Checkpoint K.**

### Slice 11: approvals and peer approval

**T11.1 Approve and revoke-approval (M).** Both: inspect right else `404`, owner else `403`, proposer `403` with the self-approval detail, request `If-Match`, closed `409 request_not_open`, trashed `409 note_not_active`, note lock then request lock. Approve inserts or no-ops; revoke deletes or no-ops; effective changes bump version and `updatedAt`.
AC: approving twice returns an identical ETag and one approval, and revoking a missing approval is a no-op; approvals are recorded on a `self_merge` note and on a single-owner note; approve versus revise racing on the same request ETag has exactly one winner (hook and thread tests).
Verify: `tests/test_approvals.py` tagged `acceptance("Approvals")`. Deps: T10.2. Files: `services/approvals.py`, `routers/edit_requests.py`.

**T11.2 Peer-approval merge counting and freezing (M).** Count is approvals by current owners other than the proposer plus one when the merger is neither an approver nor the proposer; fewer than `requiredApprovals` is `409 approval_required` with nothing written; `finalContent` is `422 /finalContent` on protected peer-approval notes in preview and merge and allowed otherwise; closing freezes `approvals` and `requiredApprovals`.
AC: a non-owner's proposal with `requiredApprovals: 1` and two owners needs two owners (approve plus merge by different owners; a lone owner's merge is `409 approval_required`); an owner removed after approving makes the merge `412` with the old request ETag and `409 approval_required` with the new one; after a merge, adding an owner and raising the policy leaves the closed request's `approvals` and `requiredApprovals` unchanged, and the `MergeResult` example shape validates.
Verify: `tests/test_peer_approval.py` tagged `acceptance("Approvals")` and `acceptance("Review races")`; the section 5 "Protect a note and merge with peer approval" flow end to end. Deps: T11.1. Files: `services/edit_requests.py`, `services/approvals.py`.

**Checkpoint L.**

### Slice 12: request comments

**T12.1 Request comments CRUD (M).** List and get for inspectors (`404` otherwise); create for inspectors without note `comment` permission, allowed on closed requests, trashed `409`; PATCH by the author only (owners `403`) with the comment ETag; DELETE by any owner or the author; `edit_requests.version` never touched; `Location`.
AC: a propose-only proposer comments `201` and another reader gets `404`; owner PATCH of the proposer's comment is `403` and owner DELETE is `204`; the request ETag is identical before and after create, update, and delete.
Verify: `tests/test_request_comments.py` tagged `acceptance("Request comments")`. Deps: T11.2. Files: `services/request_comments.py`, `routers/request_comments.py`.

**Checkpoint M.** All three section 5 flows pass.

### Slice 13: conformance sweep (PR 6 with slice 14)

**T13.1 Full Schemathesis and negative-case replay (M).** Remove the include filter; load `openapi.yaml` with `app=`, override the base URL to end in `/v1`, inject the bearer header, run status, schema, header, and content-type conformance plus negative-data rejection with bounded examples and a fixed seed. Replay every `tests/negative_cases.yaml` request-schema payload against a real endpoint (must fail is `422`, must pass is not `422`).
AC: zero Schemathesis failures across 47 operations; every request-schema fixture maps to an endpoint and behaves; the sweep fits the CI time budget.
Verify: `tests/conformance/`. Deps: T12.1. Files: `tests/conformance/test_schemathesis.py`, `tests/conformance/test_negative_replay.py`.

**T13.2 Acceptance audit and PostgreSQL job (M).** A test collects the `acceptance` markers and fails if any of the 17 rows has no test; the PostgreSQL job runs the whole suite including thread-based race tests; dialect issues fixed (bare `FOR UPDATE` select before joins); `alembic upgrade head` on PostgreSQL in CI.
AC: all 17 rows covered; the PostgreSQL suite is green; the migration applies in CI.
Verify: CI. Deps: T13.1. Files: `tests/conftest.py`, `.github/workflows/server.yml`.

**Checkpoint N.**

### Slice 14: documentation

**T14.1 Server README and repository docs (S).** `server/README.md` with setup, environment variables, running, tests on both databases, regenerating models, the purge command, the interpretations list, the locking rules, and the hook seam; the root README layout row and pointer; the changelog line under Unreleased.
AC: a new developer can follow the README from clone to a green test run without other help.
Verify: follow the README on a clean checkout. Deps: T13.2. Files: `server/README.md`, `README.md`, `CHANGELOG.md`.

**Checkpoint O.** Every acceptance row covered, all CI jobs green, documentation complete.

## Risks and mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Generated models drop `if/then`, `minProperties`, enum-of-arrays, ordered `uniqueItems` | High: hand-written rules drift from the contract | Requests validated by the spec's JSON Schema before any model; responses built as dicts and validated on every test request; generated models are typed containers with a drift check, never the validator |
| pysqlite's lazy `BEGIN` means `SELECT` then `UPDATE` holds no lock | High: "SQLite serializes writers" is false by default | `isolation_level=None` plus `BEGIN IMMEDIATE` on begin, WAL, `busy_timeout`; file database in `tmp_path`; a two-thread lost-update test in T0.4b |
| Race rules only truly exercised on PostgreSQL | High | `hooks.before_begin` runs a competitor before the primary transaction, proving checks live inside it on SQLite; thread tests assert outcome sets and run unchanged on PostgreSQL |
| Team-path revocation racing a child mutation (membership removal locks the team, not the note) | Medium | `resolve(lock=True)` locks the share and membership rows the authorization used; PostgreSQL-only effect, tested in that job |
| Unicode search differs across databases; `LIKE` wildcards | Medium | Fold in Python at write time into `title_fold` and `body_fold`, fold the query, `contains(autoescape=True)`; one Unicode test in both CI jobs |
| Timestamp ties and `createdAt DESC, id DESC` | Medium | Microsecond `UTCDateTime` on both backends (SQLite strings always carry six fractional digits, so lexical order is chronological), integer microseconds in the cursor, `tuple_` row-value keyset, an injectable clock to force ties |
| Diff fidelity: CRLF, missing final newline, virtual title newline, `\x0b`/` ` | Medium | Split on `\n` only with ends kept; custom unified formatter; golden strings loaded from the spec's examples plus `git diff --no-index` fixtures; round-trip property test |
| `SequenceMatcher` quadratic worst case with `autojunk=False` | Low | Pure module boundary so a Myers implementation can replace it; 5,000-line performance test |
| Concurrent first-access provisioning | Medium | `UNIQUE(issuer, subject)`, `SAVEPOINT` insert with `IntegrityError` re-select, thread barrier and hook tests |
| FastAPI's own 415/422 handling breaks the ladder | Medium | No declared body parameters; `parse_body` after auth; handlers own every error path; `ContractClient` fails any test that bypasses them |
| Schemathesis friction (placeholder server URL, auth, runtime) | Low | Pinned version, base URL override, injected header, include list per slice, bounded examples with a fixed seed |

## Verification (end to end)

1. `cd server && uv sync --frozen && uv run pytest` runs the full suite on SQLite with the `ContractClient` validating every response.
2. `DATABASE_URL=postgresql://... uv run pytest` runs the same suite against PostgreSQL; CI does this with a service container.
3. `uv run pytest tests/conformance` runs Schemathesis over all 47 operations against the ASGI app and replays every schema fixture through its endpoint.
4. The three section 5 flows (create and share; submit, preview, merge; protect and merge with peer approval) run as end-to-end tests using the spec's example payloads, and the acceptance audit shows all 17 rows covered.
5. `server/scripts/gen_models.sh --check` proves the committed models match the contract.
6. The contract checks in `.github/workflows/contract.yml` still pass, proving `openapi.yaml` was not bent to fit the server.
7. `docker build -t notes-api:dev . && NOTES_API_IMAGE=notes-api:dev server/scripts/smoke_image.sh` prints `smoke OK`, and the `Container image` workflow is green on the pull request.

## Open questions

None blocking. Every remaining choice is listed under "Defaults you can veto"; the one that touches an earlier decision is default 6 together with the second architecture decision (the spec's JSON Schema, not Pydantic, rejects invalid requests at runtime).
