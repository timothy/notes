# Changelog

All notable changes to the Notes API contract are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers are the document's `info.version`
under the policy in the design guide ([section 4, Contract versioning](docs/design-guide.md#contract-versioning)).
Each release is an annotated tag `v<version>` on `main`, so a frozen copy of any release is available at
`https://raw.githubusercontent.com/timothy/notes/<tag>/openapi.yaml`.

## [Unreleased]

### Fixed

- Signing-key removal now takes effect after the five-minute JWKS document cache expires; failed refreshes reject authentication.
- Container and local startup disable Uvicorn access logs, preserving structured request tracing without query text, note bodies, or tokens.
- JSON lone surrogates return `422 validation_failed` before persistence or schema-error rendering, including malformed property names. Unexpected 500 responses retain the request ID and `Cache-Control`.
- Every conditional operation rejects repeated `If-Match` headers at the precondition stage. Trailing slashes return the documented 404 Problem without redirects.
- Pagination cursors are signed and expire 24 hours after issuance. API startup requires `CURSOR_SIGNING_KEY` (64 hex characters), shared across replicas. Rotation and the transition from unsigned cursors invalidate existing cursors; clients restart pagination. No schema migration is required.
- Compose builds the shared API/migration image once. Migrations and purge require only database settings. The README and API introduction now describe the implemented server and complete startup sequence.

### Added

- A Makefile for safe bootstrap/startup, checks, both database suites, smoke, and end-to-end verification. Bootstrap preserves identity credentials and adds a missing cursor key atomically with private permissions.
- Disposable verification stacks with unique projects, temporary credentials, and dynamic localhost ports. The reusable curl harness checks all 47 operations and audit regressions, including preservation of a control stack after successful and failed smoke cleanup; logs redact tokens and stay outside commits.
- Repository: a reference server under `server/` (FastAPI), shipped as a container image (`Dockerfile`: non-root,
  read-only, one uvicorn process). `compose.yaml` runs PostgreSQL 17, the migration, and the API locally;
  `.github/workflows/image.yml` lints, scans, and smoke-tests the image on every pull request. The server requires
  `DATABASE_URL`, `CURSOR_SIGNING_KEY`, and the `OIDC_*` settings, verifies bearer tokens (RS256 and ES256, inline JWKS or JWKS URL), and
  implements every operation of the contract: `GET /me`, `GET /users`, and `GET /users/{userId}` with keyset
  cursors; the team operations (`/teams`, `/teams/{teamId}`) and the membership operations
  (`/teams/{teamId}/members`, `/teams/{teamId}/members/{userId}`) with last-admin protection under the team lock;
  notes with strong ETags (`POST /notes`, `GET /notes`, `GET/PATCH/DELETE /notes/{noteId}`, `POST
  /notes/{noteId}/restore`: create, read, conditional update, trash with a 30-day recovery window, restore, list and
  search); shares to users and teams with implied `read` and canonical permissions (`/notes/{noteId}/shares`);
  comments with their own ETags (`/notes/{noteId}/comments`: readers list oldest first, commenters add, authors
  edit, owners delete); edit requests with an immutable base snapshot and a server-computed diff
  (`/notes/{noteId}/edit-requests`, `/edit-requests`, `/edit-requests/{requestId}`: submit, inspect, the note list
  and the inbox, revise, withdraw, reject, the three-way preview, the atomic merge, and peer approvals with
  `approve` and `revoke-approval`); co-owners and the review policy (`/notes/{noteId}/owners`,
  `/notes/{noteId}/owners/{userId}`, `/notes/{noteId}/review-policy`: the author adds and removes co-owners and
  chooses `self_merge` or `peer_approval`; a note with two or more owners refuses direct edits); and request
  comments (`/edit-requests/{requestId}/comments`: the owners and the proposer discuss a request without note
  `comment` permission). It also provides a `notes-api purge-expired` command for an external scheduler, a JSON
  request log with `X-Request-Id`, a Schemathesis conformance run over all 47 operations, an audit of the eighteen
  acceptance rows, a replay of every request-schema fixture in `tests/negative_cases.yaml` through the endpoint that
  uses it, a development token issuer (`python -m notes_api.dev_issuer`) for the compose stack and the smoke test,
  and `GET /healthz` and `GET /readyz` outside the contract for container probes. No contract change.

## [2.0.0] - 2026-09-13

Multi-ownership. Wire-compatible with 1.0.0: every 1.0.0 request still works and the `/v1` prefix is unchanged.
The major bump reflects new server behavior that implementers must handle, listed under Changed.

### Added

- Co-owners: `ownerIds` and `reviewPolicy` on `Note`, `NoteFields`, and `NoteSummary`, plus the `Ownership` tag
  with `POST /notes/{noteId}/owners`, `DELETE /notes/{noteId}/owners/{userId}`, and
  `PATCH /notes/{noteId}/review-policy`. The author administers owners and the policy; a co-owner may remove
  themselves. All three are conditional note mutations.
- Approvals: `POST /edit-requests/{requestId}/approve` and `POST /edit-requests/{requestId}/revoke-approval`;
  `approvals` and `requiredApprovals` on edit requests and inbox summaries; `rejectedBy` on rejected requests.
- Request comments: the `/edit-requests/{requestId}/comments` collection with list, create, get, update, and delete.
- Schemas `AddOwner`, `Approval`, `RequestComment`, `RequestCommentPage`, `ReviewMode`, and `ReviewPolicy`.
- Error codes `direct_edit_not_allowed`, `approval_required`, `duplicate_owner`, and `author_cannot_be_removed`.
- Checker checks 11 (every `ErrorCode` has a Problem example and vice versa) and 12 (ownership and approval
  invariants in examples).
- Apache License 2.0 (`LICENSE`), declared in `info.license`.
- This changelog and the contract versioning policy in the design guide.
- GitHub Actions: the contract checks on every push to `main` and every pull request, an oasdiff comparison
  against `main` that fails pull requests on client-breaking changes, and the API reference published to
  GitHub Pages.
- Instructions for running a Prism mock of the contract (README).

### Changed

- A note with two or more owners is protected: `PATCH /notes/{noteId}` refuses `title` and `body` with
  `409 direct_edit_not_allowed`, owners propose edits like everyone else, and the note's review policy
  (`self_merge` or `peer_approval`) decides when a request may merge.
- `POST /edit-requests/{requestId}/merge`: too few approvals is `409 approval_required`; `finalContent` is `422`
  under `peer_approval`; a non-owner's proposal under `peer_approval` needs two distinct owners.
- Note ETags also cover ownership and review policy, and recording or revoking an approval advances the
  request ETag.
- The explicit `jsonSchemaDialect` declaration is gone. It named the OpenAPI 3.1 default dialect, so nothing
  changes for readers of the document, and Prism skips request-body validation when it is present.

### Fixed

- Three example diffs (`EditRequestAwaitingApproval`, `EditRequestApproved`, and the closed request inside
  `MergeResultPeerApproval`) showed four context lines in `proposalDiff`; the contract requires three, as the
  other examples already did.

## [1.0.0] - 2026-09-12

Initial contract: 37 operations over users, teams, memberships, notes, shares, comments, and edit requests with
a three-way merge preview; the design guide; the contract checker with ten checks; schema fixtures.

[Unreleased]: https://github.com/timothy/notes/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/timothy/notes/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/timothy/notes/releases/tag/v1.0.0
