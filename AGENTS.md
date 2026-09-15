# Working on Notes API

## Project priorities

This repository uses contract-based development and container-first delivery. Preserve both choices: make behaviour explicit in the contract, implement it faithfully, and verify the artifact that runs it. The rationale is at the start of [README.md](README.md#two-design-choices).

## Sources of truth

- `openapi.yaml` and `docs/design-guide.md` define the public API, permissions, HTTP semantics, and acceptance scenarios. Read the relevant operations and guide sections before changing behaviour.
- `server/README.md` explains implementation conventions, the order of request checks, transactions, tests, and operations.
- `Makefile`, `Dockerfile`, `compose.yaml`, and `.github/workflows/` define the supported workflows and delivery checks.
- `tasks/plan.md` and `tasks/todo.md` record the implementation plan and progress; consult them for context alongside the current code and documentation.

## Contract-based development

- For implementation fixes, preserve the published contract. Do not relax schemas, change fixtures, or weaken conformance checks just to make an implementation pass.
- When a task intentionally changes public behaviour, define it in the guide and OpenAPI document before implementing it. Update affected validator expectation tables, examples, `tests/negative_cases.yaml`, acceptance tests, documentation, and `CHANGELOG.md` together.
- Follow the versioning rules in `docs/design-guide.md`: `/v1` is the client compatibility line and `info.version` is the document's semantic version. Deliberate client-breaking changes require a new prefix and major version; the `breaking-change` label is reserved for that process.
- Validate request bodies through `notes_api.contract` using the document's JSON Schemas. Generated Pydantic models are typed conveniences and do not express every schema constraint.
- Do not hand-edit `server/src/notes_api/generated/schemas.py`. Regenerate from the repository root with `cd server && uv run scripts/gen_models.py`, then run the drift check.
- Use `server/tests/contract_client.py` for API tests so responses are checked against the contract. Tag tests covering design-guide acceptance rows with the existing `pytest.mark.acceptance` convention. Add regression coverage for changed behaviour, including relevant failure and concurrency cases.

## Implementation conventions

- The server is a Python 3.12 uv project using FastAPI, SQLAlchemy, and Alembic. Keep dependencies and `server/uv.lock` in sync; use `uv sync --locked` for normal setup.
- Follow the existing split under `server/src/notes_api/`: `routers/` for HTTP handlers, `services/` for business rules and writes, `serializers.py` for response rendering, and `merge/` for the pure merge engine. Handler names follow operation IDs in snake_case.
- Preserve the documented [request-check order](server/README.md#the-check-ladder), including body validation before transactions, visibility before permission errors, and version checks before lifecycle checks.
- Keep authorization, version checks, and writes together in `uow.transaction`. Preserve lock order: note, edit request, then child; team, then membership. Test database-sensitive behaviour on PostgreSQL as well as SQLite.
- Follow `server/pyproject.toml` for Ruff formatting/lint and strict mypy settings. Use the existing clock and identity fixtures for deterministic tests.

## Container-first delivery

- Build from the repository root. Keep `openapi.yaml`, runtime dependencies, and Alembic migrations in the application image; the contract is required at runtime.
- Use the same application image for the API and one-shot migration job. Run migrations before rollout, not at API process startup, and keep schema changes compatible with replicas still running the previous image.
- Preserve the multi-stage build, locked dependencies, digest-pinned bases, non-root uid/gid 10001, and minimal runtime. Keep development and test tooling out of the runtime image.
- Preserve Compose hardening: read-only root filesystem, dropped capabilities, no privilege escalation, and writable temporary storage at `/tmp`. Retain one uvicorn process with graceful SIGTERM handling.
- Read configuration from the environment and fail startup when required settings are missing. Keep liveness (`/healthz`) independent of the database and readiness (`/readyz`) dependent on it.
- Prefer the existing Make targets and isolated verification runners. Host-based uv tests are part of the supported fast development loop; image and PostgreSQL checks verify assumptions those tests cannot cover.

## Commands and verification

Run Make targets from the repository root. Starting the stack requires Docker with Compose 2.24+, Make, and Bash. Checks and SQLite tests need uv and Python 3.12; PostgreSQL/image checks also need Docker/Compose, and smoke/end-to-end checks need curl and Bash. Redocly uses Node.js/npm.

| Command | Purpose |
| --- | --- |
| `make help` | List supported targets and prerequisites. |
| `make up` / `make down` | Build, bootstrap, migrate, and start / stop while preserving development data. |
| `make check` | Locked setup, lint, format check, strict typing, generated-model drift, and static contract validation. |
| `make test` | Full SQLite suite with inherited PostgreSQL test URLs cleared. |
| `make test-postgres` | Full suite against an automatically created disposable PostgreSQL stack. |
| `make smoke` | Build and check the image's runtime and lifecycle in an isolated stack. |
| `make e2e` | Build and exercise the API through contract-checked curl workflows. |
| `make verify` | Run check, SQLite tests, PostgreSQL tests, smoke, and end-to-end checks in order. |
| `cd server && uv run scripts/gen_models.py --check` | Check committed generated models against the contract. |
| `npx --yes @redocly/cli@2.52.1 lint openapi.yaml` | Run the additional OpenAPI lint used by contract CI. |

- For server changes, run `make check` and `make test`; add `make test-postgres` for persistence, migrations, locking, or concurrency changes.
- For contract changes, run `make check`, the Redocly lint, and affected conformance/acceptance tests. Review compatibility; CI compares the contract with the pull request's base branch using oasdiff.
- For image, configuration, startup, or deployment changes, run `make smoke` and `make e2e`, plus `docker compose config -q` when Compose changes. CI also runs hadolint and Trivy.
- For documentation-only changes, check accuracy against the relevant files, links, and `git diff --check`; application test runs are unnecessary unless executable examples or behaviour changed.
- Report checks actually run, their results, and any unavailable prerequisites. Do not describe unrun checks as passing.

The PostgreSQL test fixture drops and recreates the target schema. Prefer `make test-postgres`; any manually supplied `NOTES_API_TEST_DATABASE_URL` must point to a disposable test database. Existing runners create and clean up their own stacks; preserve that isolation and development volumes.

## Code Review Rules

- Flag changes that bypass ownership or review policy: shares grant only `read`, `comment`, and `propose_edit`; protected notes reject direct content edits; peer approval requires the documented participation by distinct owners. Implement changes through the contract's review flow.
- Flag authorization or version checks moved outside the transaction, altered error precedence, or inconsistent lock ordering. Preserve the check ladder and atomic checks with writes.
- Flag request handling that bypasses contract validation, returns undeclared statuses or off-schema bodies, omits required headers, or uses incorrect media types. Keep the contract client and conformance coverage effective.
- Flag migrations moved into API startup, removal of runtime hardening, or omissions of the contract from the image. Keep delivery behaviour covered by image checks.
- Keep credentials, tokens, note bodies, and search text out of logs and committed files. Use the existing redacted request logging and gitignored development credentials.

This root-level guide follows the [OpenAI AGENTS.md guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md). Keep it concise and update it when project conventions change.
