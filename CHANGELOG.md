# Changelog

All notable changes to the Notes API contract are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers are the document's `info.version`
under the policy in the design guide ([section 4, Contract versioning](docs/design-guide.md#contract-versioning)).
Each release is an annotated tag `v<version>` on `main`, so a frozen copy of any release is available at
`https://raw.githubusercontent.com/timothy/notes/<tag>/openapi.yaml`.

## [Unreleased]

### Added

- Repository: a reference server under `server/` (FastAPI, work in progress), shipped as a container image (`Dockerfile`:
  non-root, read-only, one uvicorn process). `compose.yaml` runs PostgreSQL 17, the migration, and the API locally;
  `.github/workflows/image.yml` lints, scans, and smoke-tests the image on every pull request. The server requires
  `DATABASE_URL` and the `OIDC_*` settings, verifies bearer tokens (RS256 and ES256, inline JWKS or JWKS URL), serves
  `GET /me`, `GET /users`, and `GET /users/{userId}` with keyset cursors, the team operations (`/teams`,
  `/teams/{teamId}`) and the membership operations (`/teams/{teamId}/members`, `/teams/{teamId}/members/{userId}`)
  with last-admin protection under the team lock, note creation, reading, and conditional updates with strong ETags
  (`POST /notes`, `GET /notes/{noteId}`, `PATCH /notes/{noteId}`), a JSON request log with `X-Request-Id`, a
  Schemathesis conformance run over every implemented operation, carries a development token issuer
  (`python -m notes_api.dev_issuer`) for the compose stack and the smoke test, and answers `GET /healthz` and
  `GET /readyz` outside the contract for container probes. No contract change.

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
