# Notes API server: task list

Checklist for `tasks/plan.md` (approved 2026-09-13). Each task's full description, acceptance criteria, verification, and files are in the plan; this file tracks progress. Definition of done for every task: ruff and `mypy --strict` clean, pytest green on SQLite, the `ContractClient` validating every response, acceptance tests tagged with their section 6 row, `openapi.yaml` untouched.

## PR 1: slices 0 and 1

- [x] T0.1 Project scaffold and CI (S) — deps: none. AC: `uv sync --frozen` from clean checkout; empty suite green; `server.yml` passes without touching the contract workflows.
- [x] T0.2 Contract loader, generated models, drift check (M) — deps: T0.1. AC: every `negative_cases.yaml` verdict reproduced by `validate_body`; `ProblemValidationFailed` errors reproduced; drift check fails on an edited generated file.
- [x] T0.3 Problems, middleware, body parser, If-Match parser (M) — deps: T0.2. AC: 400/415/422/428 mapping and `If-Match` shapes; `Cache-Control: no-store` on every response; 401 challenge variants.
- [x] T0.4a ORM models and initial migration (M) — deps: T0.1. AC: migration applies on SQLite and PostgreSQL; metadata and migration agree; cascades work with `foreign_keys=ON`.
- [x] T0.4b Engine, session, unit of work, clock (M) — deps: T0.4a. AC: two-thread read-then-write serializes; `hooks.before_begin` runs before `BEGIN`; `UTCDateTime` round-trips microseconds.
- [x] T0.5 Test harness and ContractClient (M) — deps: T0.3, T0.4b. AC: wrong body fails through the client; undeclared status fails; personas get distinct ids (asserted through `GET /me` in T2.2, once provisioning exists).
- [x] Checkpoint A
- [x] T1.1 Line splitting and unified diff (M) — deps: T0.1. AC: byte-exact spec diffs; missing-final-newline and CRLF fixtures match git; split/join round-trip property.
- [x] T1.2 Three-way merge with conflicts (M) — deps: T1.1. AC: `PreviewConflict` conflicts reproduced; separate regions combine and identical edits appear once; competing insertions, overlaps, and incompatible titles conflict.
- [x] T1.3 Text-merge acceptance suite (S) — deps: T1.2. AC: every "Text merge" item named; deterministic; 5,000 lines under one second.
- [x] Checkpoint B

## PR C1: fail-fast configuration and probes (container-first, 2026-09-13)

- [x] C1.1 Fail-fast configuration (S) — deps: none. AC: `Settings()` without `DATABASE_URL` raises naming `database_url`; Alembic reads `Settings`; suite green on SQLite and PostgreSQL.
- [x] C1.2 Probe routes (M) — deps: C1.1. AC: `/healthz` 200; `/readyz` 200, or 503 without connection details; other methods and `/v1` variants are the 404 Problem; `ContractClient` rejects the probes as undeclared.
- [x] Checkpoint C1

## PR C2: container image, local stack, CI gate, docs

- [x] C2.1 BuildKit prerequisite (S, docs) — deps: none. AC: `docker buildx version` works locally; both base-image digests reproduce; the step is in `server/README.md`.
- [x] C2.2 Dockerfile and .dockerignore (M) — deps: C2.1, PR C1. AC: builds on arm64; uid 10001; refuses to start without `DATABASE_URL`; contract loads through `CONTRACT_PATH` on a read-only rootfs; `alembic heads` prints 0001; no uv or pytest in the image; hadolint clean; source label set.
- [x] C2.3 compose.yaml (S) — deps: C2.2. AC: `up --wait` brings up db, migrate, api; probes answer; `.env` reaches the api; `stop -t 25 api` exits 0; a second `up` is a no-op migration.
- [x] C2.4 Smoke script (M) — deps: C2.3. AC: `smoke OK` locally; a foreign image fails; shellcheck clean.
- [x] C2.5 image.yml and server.yml alignment (M) — deps: C2.4. AC: `Container image` lint and build jobs green on the PR with `smoke OK`; `server.yml` green with `--locked` and `postgres:17.11`; contract workflows untouched.
- [x] C2.6 Docs and plan bookkeeping (S) — deps: C2.5. AC: `server/README.md` Container section; root README rows and Running the server; CHANGELOG Unreleased line; plan amendments (architecture, defaults 3/4/8/16/20, 24-28, verification 7).
- [x] Checkpoint C2: all four workflows green on the PR

## PR 2a: slice 2 (auth and directory; split from slice 3 on 2026-09-14)

- [x] T2.0 Harness guard and route helper (S) — deps: none. AC: the ContractClient rejects `include_router` routes with an explicit message; `routers.add_route` registers `/v1` routes it can see; `serializers` render the contract's timestamps, users, and pages.
- [x] T2.1 Required OIDC configuration and safe errors (S) — deps: none. AC: `Settings()` without `OIDC_ISSUER`, `OIDC_AUDIENCE`, or exactly one of `OIDC_JWKS_URL`/`OIDC_JWKS` raises naming the variable; blanks count as unset; `load_settings()` names variables but never values.
- [x] T2.2 Dev issuer module (S) — deps: none. AC: `python -m notes_api.dev_issuer env` prints four single-quoted `.env` lines that round-trip into an issuer whose JWKS verifies its tokens; `token --sub` mints from `NOTES_DEV_ISSUER_KEY`; runs without `DATABASE_URL`.
- [x] T2.3 JWT verification and 401 (M) — deps: T2.1. AC: every rejection is `401` with the challenge (`error="invalid_token"` exactly when a token was present); `at+jwt`, `JWT`, and no `typ` pass; a JWKS fetch failure is `401`, not `500`; `401` precedes `422`.
- [x] T2.4 Provisioning and GET /me (M) — deps: T2.3. AC: `GET /me` twice returns one id; four personas get four ids; a competitor inserting the identity in `before_begin("provision_user")` is reused; eight threads produce one row; the display name is never refreshed.
- [x] T2.5 Cursors, pagination, GET /users and /users/{userId} (M) — deps: T2.4. AC: `limit=0`, `101`, `abc` are `422 query/limit`; a cursor reused with another limit, by another caller, or tampered is `400 invalid_cursor`; ties page by id; `/users/{userId}` is `404` or `422 path/userId`.
- [x] T2.6 Compose, smoke, docs, bookkeeping (M) — deps: T2.5. AC: `smoke OK` with the fail-fast split and the authenticated `GET /v1/me`; `docker compose config -q` passes with no `.env`; READMEs, CHANGELOG, and the plan are current.
- [x] Checkpoint C: all four workflows green on PR #12 (2026-09-14)

## PR 2b: slice 3 (teams and memberships)

- [x] T3.1 Teams CRUD (M) — deps: T2.5. AC: creator is admin; `scope=mine`; non-admins `403` on PATCH and DELETE; body errors after authorization; unchanged rename is a no-op; deletion removes memberships and team shares while notes, comments, and edit requests stay.
- [x] T3.2 Memberships with last-admin atomicity (M) — deps: T3.1. AC: nonmember listing `403`; default role `member`; unknown user `422 /userId`; duplicate `409`; last admin cannot be demoted or leave; `403` before the target's `404`; hook and thread races leave exactly one admin.
- [x] Checkpoint D: "Directory and teams" row complete; all four workflows green on PR #13 (2026-09-14)

## PR 3a: slice 4 (notes core; slices 4, 5, and 6 split into PR 3a, 3b, and 3c on 2026-09-14)

- [x] T4.0 Request log and request id (S) — deps: none. AC: one JSON line per request with time, request id, method, route, path, status, code, user, duration; probes unlogged; `X-Request-Id` kept when simple, replaced otherwise, echoed on every response; no token, query string, or body text in the output.
- [x] T4.0b Ladder alignment for teams (S) — deps: none. AC: team bodies are parsed before the lock; a non-admin's malformed body is `422`.
- [x] T4.1 Permission resolver and note serializers (M) — deps: T0.5. AC: owner or union of direct and team shares; team roles grant nothing; `visible` over active, trashed, and expired at the boundary; `Note` and `NoteSummary` validate.
- [x] T4.2 POST /notes and GET /notes/{noteId} (M) — deps: T4.1. AC: the contract's `CreateNoteRequest` round-trips with equal ETags; defaults; every body rule at its pointer; a stranger gets `404`, a reader `200`.
- [x] T4.3 PATCH /notes/{noteId} and the Schemathesis start (M) — deps: T4.2. AC: effective changes advance the ETag, no-ops do not; `428`/`400`/`412`/`409` in order with nothing written; a competitor makes the primary `412`; an owner added between read and write makes a body PATCH `409 direct_edit_not_allowed`; fifteen operations conform.
- [x] T4.4 Docs and bookkeeping (S) — deps: T4.3.
- [x] Checkpoint E: all four workflows green on PR #14 (2026-09-14)

## PR 3b: slice 5 (trash, restore, purge, list and search)

- [x] T5.1 Trash, restore, expiry, purge command, smoke purge check (M) — deps: T4.3. AC: trash removes shares and the `204` carries the trash ETag; a repeat is idempotent; restore is private and advances the ETag; `404` at the expiry instant, `200` a microsecond earlier; `notes-api purge-expired` deletes exactly the expired notes and their children and runs from the image in the smoke test.
- [x] T5.2a GET /notes: scope, state, keyset, dedupe (M) — deps: T5.1, T2.5. AC: overlapping grants list once; `scope` partitions; `state=trashed` is the owner's unexpired trash; ties page by id; revocation between pages hides the note; a page costs a bounded number of statements.
- [x] T5.2b GET /notes: q, tag, teamId (M) — deps: T5.2a. AC: folded literal search over title or body with literal wildcards; exact AND tags; every invalid parameter is `422` naming it; `teamId` selects team-shared notes for anyone who can read them and grants nothing.
- [x] T5.3 Docs and bookkeeping (S) — deps: T5.2b.
- [x] Checkpoint F: all four workflows green on PR #15 (2026-09-14)

## PR 3c: slice 6 (shares and access paths)

- [x] T6.1 Shares CRUD (M) — deps: T5.2b. AC: implied `read` and canonical order; `422 /recipient/id` for owners and unknown recipients; `409 duplicate_share`; trashed note `409` on create and `404` otherwise; wrong nesting `404`; readers `403`, strangers `404`; the note's ETag untouched.
- [x] T6.2 Overlapping grants, revocation, isolation, and the section 5 "Create and share" flow (M) — deps: T6.1. AC: direct and team paths survive each other's removal; the last read path hides the note; team admins gain nothing; lists and nested ids reveal only authorized entries; the flow runs with the contract's example payloads.
- [x] T6.3 Docs and bookkeeping (S) — deps: T6.2.
- [x] Checkpoint G: all four workflows green on PR #16 (2026-09-14); the "Create and share" flow passes end to end

## PR 4a: slice 7 (comments; slices 7, 8, and 9 split into PR 4a, 4b, and 4c on 2026-09-14)

- [x] T7.1 List, get, create comments (S) — deps: T6.2. AC: readers list and get `200` oldest first (`createdAt ASC, id ASC`, ties by id); a read-only or proposal-only recipient creates `403`; a comment reached through another note's path is `404`; creating a comment leaves the note ETag unchanged; owners of a trashed note read its comments and create `409 note_not_active`; the Create-and-share flow's comment step passes.
- [x] T7.2 Update and delete comments (M) — deps: T7.1. AC: owner PATCH of another's comment is `403` and owner DELETE is `204`; an author downgraded to read gets `200` on GET and `403` on PATCH and DELETE; an identical body is a no-op keeping the ETag; a stale comment ETag is `412`, a missing one `428`, a malformed one `400`; a trashed note is `409 note_not_active` after the version check; a competing edit makes the second `412`; the conformance run covers the five comment operations.
- [x] T7.3 Docs and bookkeeping (S) — deps: T7.2.
- [ ] Checkpoint H: all four workflows green on the PR

## PR 4b: slice 8 (edit requests)

- [ ] T8.1 POST /notes/{noteId}/edit-requests (M) — deps: T7.2, T1.1.
- [ ] T8.2 GET /edit-requests/{requestId} and serializer (M) — deps: T8.1.
- [ ] T8.3 Note-scoped list and inbox (M) — deps: T8.2.
- [ ] T8.4 Revise, withdraw, reject (M) — deps: T8.3.
- [ ] T8.5 Docs, the "Submit and discover" flow, and bookkeeping (S) — deps: T8.4.
- [ ] Checkpoint I

## PR 4c: slice 9 (preview and merge)

- [ ] T9.1 POST /edit-requests/{requestId}/preview (M) — deps: T8.4, T1.3.
- [ ] T9.2 POST /edit-requests/{requestId}/merge (M) — deps: T9.1.
- [ ] T9.3 Review race tests (M) — deps: T9.2.
- [ ] T9.4 Docs, the "Preview and merge" flow, and bookkeeping (S) — deps: T9.3.
- [ ] Checkpoint J: section 5 "Submit" and "Preview and merge" end to end

## PR 5: slices 10, 11, and 12

- [ ] T10.1 Add and remove owners (M) — deps: T9.3.
- [ ] T10.2 Review policy and protected-note behavior (M) — deps: T10.1.
- [ ] Checkpoint K
- [ ] T11.1 Approve and revoke-approval (M) — deps: T10.2.
- [ ] T11.2 Peer-approval merge counting and freezing (M) — deps: T11.1. Section 5 "Protect a note" end to end.
- [ ] Checkpoint L
- [ ] T12.1 Request comments CRUD (M) — deps: T11.2.
- [ ] Checkpoint M: all three section 5 flows pass

## PR 6: slices 13 and 14

- [ ] T13.1 Full Schemathesis and negative-case replay (M) — deps: T12.1.
- [ ] T13.2 Acceptance audit and PostgreSQL job (M) — deps: T13.1.
- [ ] Checkpoint N
- [ ] T14.1 Server README and repository docs (S) — deps: T13.2.
- [ ] Checkpoint O: every acceptance row covered, all CI jobs green, documentation complete
