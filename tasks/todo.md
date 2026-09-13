# Notes API server: task list

Checklist for `tasks/plan.md` (approved 2026-09-13). Each task's full description, acceptance criteria, verification, and files are in the plan; this file tracks progress. Definition of done for every task: ruff and `mypy --strict` clean, pytest green on SQLite, the `ContractClient` validating every response, acceptance tests tagged with their section 6 row, `openapi.yaml` untouched.

## PR 1: slices 0 and 1

- [x] T0.1 Project scaffold and CI (S) — deps: none. AC: `uv sync --frozen` from clean checkout; empty suite green; `server.yml` passes without touching the contract workflows.
- [x] T0.2 Contract loader, generated models, drift check (M) — deps: T0.1. AC: every `negative_cases.yaml` verdict reproduced by `validate_body`; `ProblemValidationFailed` errors reproduced; drift check fails on an edited generated file.
- [x] T0.3 Problems, middleware, body parser, If-Match parser (M) — deps: T0.2. AC: 400/415/422/428 mapping and `If-Match` shapes; `Cache-Control: no-store` on every response; 401 challenge variants.
- [x] T0.4a ORM models and initial migration (M) — deps: T0.1. AC: migration applies on SQLite and PostgreSQL; metadata and migration agree; cascades work with `foreign_keys=ON`.
- [x] T0.4b Engine, session, unit of work, clock (M) — deps: T0.4a. AC: two-thread read-then-write serializes; `hooks.before_begin` runs before `BEGIN`; `UTCDateTime` round-trips microseconds.
- [ ] T0.5 Test harness and ContractClient (M) — deps: T0.3, T0.4b. AC: wrong body fails through the client; undeclared status fails; personas get distinct ids.
- [ ] Checkpoint A
- [ ] T1.1 Line splitting and unified diff (M) — deps: T0.1. AC: byte-exact spec diffs; missing-final-newline and CRLF fixtures match git; split/join round-trip property.
- [ ] T1.2 Three-way merge with conflicts (M) — deps: T1.1. AC: `PreviewConflict` conflicts reproduced; separate regions combine and identical edits appear once; competing insertions, overlaps, and incompatible titles conflict.
- [ ] T1.3 Text-merge acceptance suite (S) — deps: T1.2. AC: every "Text merge" item named; deterministic; 5,000 lines under one second.
- [ ] Checkpoint B

## PR 2: slices 2 and 3

- [ ] T2.1 JWT verification and 401 (M) — deps: T0.5.
- [ ] T2.2 Provisioning and GET /me (S) — deps: T2.1.
- [ ] T2.3 Cursor codec, pagination helper, GET /users and /users/{userId} (M) — deps: T2.2.
- [ ] Checkpoint C
- [ ] T3.1 Teams CRUD (M) — deps: T2.3.
- [ ] T3.2 Memberships with last-admin atomicity (M) — deps: T3.1.
- [ ] Checkpoint D: "Directory and teams" row complete

## PR 3: slices 4, 5, and 6

- [ ] T4.1 Permission resolver and note serializer (M) — deps: T0.5.
- [ ] T4.2 POST /notes and GET /notes/{noteId} (M) — deps: T4.1.
- [ ] T4.3 PATCH /notes/{noteId} (M) — deps: T4.2. Starts the Schemathesis include list.
- [ ] Checkpoint E
- [ ] T5.1 Trash, restore, expiry, purge CLI (M) — deps: T4.3.
- [ ] T5.2a GET /notes: scope, state, keyset, dedupe (M) — deps: T5.1, T2.3.
- [ ] T5.2b GET /notes: q, tag, teamId (M) — deps: T5.2a.
- [ ] Checkpoint F
- [ ] T6.1 Shares CRUD (M) — deps: T5.2b.
- [ ] T6.2 Overlapping grants, revocation, admins gain nothing (M) — deps: T6.1. Section 5 "Create and share" end to end.
- [ ] Checkpoint G

## PR 4: slices 7, 8, and 9

- [ ] T7.1 List, get, create comments (S) — deps: T6.2.
- [ ] T7.2 Update and delete comments (M) — deps: T7.1.
- [ ] Checkpoint H
- [ ] T8.1 POST /notes/{noteId}/edit-requests (M) — deps: T7.2, T1.1.
- [ ] T8.2 GET /edit-requests/{requestId} and serializer (M) — deps: T8.1.
- [ ] T8.3 Note-scoped list and inbox (M) — deps: T8.2.
- [ ] T8.4 Revise, withdraw, reject (M) — deps: T8.3.
- [ ] Checkpoint I
- [ ] T9.1 POST /edit-requests/{requestId}/preview (M) — deps: T8.4, T1.3.
- [ ] T9.2 POST /edit-requests/{requestId}/merge (M) — deps: T9.1.
- [ ] T9.3 Review race tests (M) — deps: T9.2.
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
