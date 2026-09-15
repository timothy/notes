# Notes API server

Reference implementation of the Notes API contract in [`../openapi.yaml`](../openapi.yaml), version 2.0.0. The contract is the source of truth: request bodies are validated by the spec's own JSON Schemas, typed models are generated from the document, every response in the test suite is checked against it, and every fixture in [`../tests/negative_cases.yaml`](../tests/negative_cases.yaml) is replayed through the endpoint that uses it. All 47 operations are implemented; the plan and the task list that produced the server are in [`../tasks/`](../tasks/).

- [Development](#development): setup, the gate, both databases, regenerating models, a local server.
- [How the server enforces the contract](#how-the-server-enforces-the-contract): validation, the check ladder, ETags, locking, the test harness, the hook seam, the conformance suite.
- [Behaviour by resource](#behaviour-by-resource): what each resource does and the choices pinned where the contract leaves one.
- [Operations](#operations): the container, configuration, authentication, the request log, probes, migrations, the purge command, deployment constraints.

The package is `src/notes_api`: `main.py` builds the application (`create_app`); `config.py` reads the settings; `contract.py` loads the document and validates bodies with its schemas; `generated/schemas.py` holds the models generated from it; `auth/` verifies bearer tokens and provisions users; `http/` has the body parser, the Problem responses, the dependencies, the request log, the probes; `routers/` registers one module per resource, one handler per operation, named after the operationId in snake_case; `services/` holds the rules and the writes; `models.py`, `db.py`, `uow.py`, `etags.py`, and `cursors.py` are the persistence, the transaction seam, the version tokens, and the keyset cursors; `merge/` is the three-way merge and the unified diff; `serializers.py` renders responses; `cli.py` is the operator command and `dev_issuer.py` the development identity provider. Tests live in `tests/` (one module per resource or concern, `test_flows.py` for the design guide's example workflows, `test_races.py` for concurrency, `tests/merge/` for the engine) with the conformance suite in `tests/conformance/`.

## Development

```sh
cd server
uv sync --locked
uv run ruff format src tests scripts && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

That line is the gate every change passes before it is pushed: formatting, lint, `mypy --strict` over `src`, `tests`, and `scripts`, and the suite. Python 3.12 and [uv](https://docs.astral.sh/uv/) are the only prerequisites; the suite needs no Docker.

### Tests on both databases

The suite runs on SQLite by default: each test gets a fresh file database in its temporary directory. To run the same suite against PostgreSQL, point `NOTES_API_TEST_DATABASE_URL` at an empty database; each test then drops and recreates the schema there. With the compose database from the repository root:

```sh
docker compose up -d db
docker compose exec -T db createdb -U notes notes_test
cd server && NOTES_API_TEST_DATABASE_URL=postgresql+psycopg://notes:notes@127.0.0.1:5432/notes_test uv run pytest
```

or with a throwaway container:

```sh
docker run -d --rm --name notes-pg -p 127.0.0.1:55432:5432 -e POSTGRES_USER=notes -e POSTGRES_PASSWORD=notes -e POSTGRES_DB=notes_test postgres:17.11
NOTES_API_TEST_DATABASE_URL=postgresql+psycopg://notes:notes@127.0.0.1:55432/notes_test uv run pytest
docker stop notes-pg
```

CI runs both in [`.github/workflows/server.yml`](../.github/workflows/server.yml): the `checks` job on SQLite and the `postgres` job against a PostgreSQL 17 service container. The PostgreSQL job is the proof for everything dialect-specific: `tests/test_schema.py` runs `alembic upgrade head`, `alembic check` (the models match the migration), and `downgrade base` against that database; the locking selects run as written (`FOR UPDATE` and `FOR SHARE`, which SQLite ignores); and the thread-based race tests run against it. The image workflow adds a second migration proof by running the real `migrate` container against PostgreSQL 17 (see [Smoke test](#smoke-test)).

### Regenerating the models

`src/notes_api/generated/schemas.py` is generated from the contract by datamodel-code-generator and committed:

```sh
uv run scripts/gen_models.py            # rewrite the file
uv run scripts/gen_models.py --check    # exit 1 with a diff when the committed file drifts
```

`tests/test_generated_models.py` runs the check in the suite, so both CI jobs fail when the contract and the committed models disagree. The models are a typed convenience for handlers, never the validator: the spec's JSON Schemas validate every body (see [Validation](#validation)), because the generator cannot express `if/then`, `minProperties`, enums of arrays, or ordered `uniqueItems`.

### Running a local server

The compose stack in [Build and run](#build-and-run) is the complete local deployment. To run the server from the checkout instead, against the compose database:

```sh
docker compose up -d db                                        # from the repository root
cd server
uv run python -m notes_api.dev_issuer env > ../.env            # a development identity provider (gitignored)
export DATABASE_URL=postgresql+psycopg://notes:notes@127.0.0.1:5432/notes
uv run --env-file ../.env alembic upgrade head
uv run --env-file ../.env uvicorn --factory notes_api.main:create_app --port 8000
```

and, in another shell from `server/`:

```sh
TOKEN=$(uv run --env-file ../.env python -m notes_api.dev_issuer token --sub ada --name 'Ada Okafor')
curl -si -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/me
```

The same `.env` serves the compose stack, so the two can be used interchangeably. `--env-file` is uv's; the server itself reads only the environment.

## How the server enforces the contract

### Validation

Handlers are plain `def` functions in FastAPI's threadpool, and bodies are never declared as FastAPI parameters, so the order of checks is the contract's, not the framework's. `http/bodies.py::parse_body` reads the body and answers `415` for anything but `application/json`, `400 malformed_request` for unparseable JSON (or a missing body when one is required), then `422 validation_failed` with one `errors[]` entry per violation, each with `location: body`, an RFC 6901 pointer, and a message, straight from the spec's schema for that operation (`contract.py::Contract.validate_body`). An optional body that is omitted validates as `{}`. The one rule beyond the schemas: no string in a body or in the `q` and `tag` query parameters may contain U+0000, because PostgreSQL text cannot store it (found by the conformance run); it is `422` at the field's pointer with detail `must not contain NUL characters`, `location: query` for parameters. Query, header, and path parameters are validated to `422` naming the parameter; a malformed `If-Match` is `400`; an invalid cursor is `400 invalid_cursor`.

### The check ladder

Every handler resolves in the same order, so the same request always gets the same answer and nothing leaks to a caller who may not see the resource:

1. `401` for a missing or invalid token (`WWW-Authenticate: Bearer`).
2. `422` for an invalid path or query parameter.
3. The body: `415`, `400`, `422` as above. Bodies are validated before visibility and authorization, so a malformed body is `422` for anyone, and no lock is held while a body is read.
4. `404` for a resource the caller cannot see, including one that exists but is hidden, expired, or reached through the wrong parent (a comment under another note, a request comment under another request, a share under another note).
5. `403` for a forbidden action on a visible resource.
6. The precondition: `428` for a missing `If-Match`, `400` for a weak tag, a wildcard, or a list.
7. `412` when the version does not match; the version check precedes every lifecycle check, so a stale ETag on a trashed note or a closed request is `412`.
8. `409` for the lifecycle (`note_not_active`, `request_not_open`, `note_already_active`), `request_not_open` before `note_not_active` because a closed record is immutable whatever happens to its note.
9. Semantic `422` (an unknown user or recipient, a proposal identical to its base, `requiredApprovals` above the owner count, `finalContent` under peer approval).
10. Business `409` (`duplicate_share`, `duplicate_membership`, `duplicate_owner`, `direct_edit_not_allowed`, `approval_required`, `merge_conflict`, `last_admin`, `author_cannot_be_removed`).
11. The write, and the response with the new `ETag`.

Steps 4 to 11 run inside one transaction with the locks below. An unknown route or an undeclared method on a known path is `404 not_found`; the server never emits a status the contract does not declare.

### Versions

ETags are strong, opaque, quoted 32-hex tokens (`etags.new_version()`), one per note, comment, request comment, and edit request. A note's ETag covers its content, tags, lifecycle metadata, ownership, and review policy; a request's covers its stored state but not the live `noteTitle` and `requiredApprovals`. Creating a share or comment, recording or deleting an approval, and every request comment leave the note's ETag alone; recording or revoking an approval advances the request's. A mutation that changes nothing (a PATCH with the current values, a repeated approval, a repeated trash) is a `200` or `204` no-op that returns the existing representation and ETag with `updatedAt` unchanged. Every version mismatch is `412 precondition_failed` with a detail naming the input that was stale (`If-Match`, `baseNoteETag`, or `expectedNoteETag`).

### Locking

Every read and write runs inside `uow.transaction(session, op)` on a fresh session, and the final checks are serialized with the write, never made from ETags fetched earlier:

- Lock order is note, then edit request, then child row (comment, share, approval, owner); team, then membership. Team mutations never lock a note and note-scoped mutations never lock a team, so no cycle exists.
- Every note-scoped mutation locks the note row `FOR UPDATE`, even when it does not write the note (a comment insert serializes on its note). Child mutations whose authorization came from a share or a membership also lock those rows `FOR SHARE`, so a revocation that wins the race prevents the mutation from committing.
- Request-scoped mutations read the request's `note_id` as a scalar, lock the note with the caller's access rows, then load the request row for the first time `FOR UPDATE` with `populate_existing`: a row loaded before the note lock would keep stale attributes on PostgreSQL even after a later locking select, so nothing reads it earlier. Reads take the request and its note in one joined select.
- On PostgreSQL every locking select is a bare `SELECT ... WHERE id = ?` before any join, because `FOR UPDATE` rejects the nullable side of an outer join. SQLite ignores the clause and relies on write serialization instead: the engine emits `BEGIN IMMEDIATE` when a transaction starts, turns on WAL and foreign keys, and waits up to five seconds on a busy database (`db.py`).
- `removeOwner` additionally locks the note's open requests before deleting the leaver's approvals and advancing those requests' versions. `mergeEditRequest` locks note then request and rechecks both versions, both states, the owner set, and the approval count before writing. Preview locks nothing: one joined select is its coherent pair. Provisioning locks nothing: the insert runs under a savepoint and a constraint violation re-selects the winner.

### The test harness

Every test gets its own application and database (`tests/conftest.py`): a SQLite file in the test's temporary directory, or the schema dropped and recreated in the PostgreSQL database named by `NOTES_API_TEST_DATABASE_URL`. A `FakeClock` pinned at 2026-09-13T12:00Z moves only when a test advances it, so timestamps, expiry, and ordering are exact. A local issuer signs tokens for four personas, Ada Okafor, Ben Ortiz, Cara Nakamura, and Dan Whitfield, each a distinct user provisioned on first contact. `tests/contract_client.py::ContractClient` wraps the test client and checks every response against the document: the status must be declared for the operation, the headers and media type must match, and the body must validate against the declared schema; an undeclared status or an off-schema body fails the test that provoked it. Tests that prove a row of the design guide's section 6 table carry `@pytest.mark.acceptance("<row>")`, registered under `--strict-markers`, and `tests/test_acceptance_audit.py` fails when any of the eighteen rows has no test or a test names a row the table does not have (it skips itself when pytest was pointed at specific paths or filtered with `-k` or `-m`, so single-file runs cannot trip it).

### The hook seam and race tests

`uow.hooks.before_begin(op)` runs before every transaction with the snake_case operationId (`update_note`, `merge_edit_request`, ...) and is a no-op in production. A race test replaces it (the `restore_hooks` fixture puts the no-op back) with a competitor that runs its own request through the API before the primary transaction begins, which proves that every check the service makes lives inside the transaction: a merge whose request was revised in the hook is `412`, a comment whose share was downgraded is `403`, an owner added between read and PATCH makes the body PATCH `409 direct_edit_not_allowed`. A hook that itself opens transactions does not trigger the hook again. [`tests/test_races.py`](tests/test_races.py) also runs true concurrency with threads and a barrier on the file SQLite database (two merges of two requests on one note, a merge racing a revision on one request ETag, an approval racing a revision, two admins demoting each other, the last admin leaving twice), asserting that exactly one transition wins; the PostgreSQL job runs the same tests.

### The conformance suite

[`tests/conformance/test_schemathesis.py`](tests/conformance/test_schemathesis.py) runs Schemathesis over every operation the contract declares: requests are generated from the contract, valid and deliberately invalid, and each response must be a declared status with the declared headers, media type, and body schema. `OPERATIONS` records the 47 operation ids and a test keeps it equal to the document. The fixture seeds a note, a team, a membership, a co-owner, a share, a comment, an open edit request, and a request comment, and per-operation parameter overrides point `removeOwner` at the co-owner and the request-comment item operations at the request comment, so every operation addresses a real resource. Two checks are excluded on purpose: `positive_data_acceptance`, because it rejects the ladder's own `400`, `412`, `422`, and `428` answers to schema-valid but semantically wrong input, and `ignored_auth`, because `tests/test_auth.py` already pins the `401` and the check would triple every successful request.

[`tests/conformance/test_negative_replay.py`](tests/conformance/test_negative_replay.py) replays [`../tests/negative_cases.yaml`](../tests/negative_cases.yaml) through the endpoints. `tests/test_contract_loader.py` already proves each fixture's verdict through `Contract.validate_body`; the replay proves the endpoints use the right schema and never refuse a valid body. It reads the body-taking operations and their schemas from the document (nineteen operations; a new one joins by itself), seeds a note with two owners, a proposal-only share, an open request, comments, a team, and an unattached user, substitutes the fixtures' placeholder identifiers and ETags with the live ones, and sends every must-fail payload (which must be `422 validation_failed` with body-located errors) and every must-pass payload (which must land with a 2xx) to its operation: 76 cases. Two inventory tests pin the fixture schemas that describe responses rather than request bodies and the three body-taking operations whose schema has no fixtures (`updateTeam`, `updateComment`, `updateEditRequestComment`); the fixture file belongs to the contract, so the server pins the gap rather than filling it.

## Behaviour by resource

Where the contract leaves a choice, the server's choice is fixed by a test and recorded in the resource's section below. Choices that belong to no single resource:

- An unknown route or an undeclared method on a known path is `404 not_found`; the server never emits a status the contract does not declare.
- A missing `typ` header on a token is accepted; a present one must name an access token (`at+jwt` or `JWT`, case-insensitive).
- `displayName` precedence is `name`, `preferred_username`, `user-<sub prefix>`, and it is never refreshed after provisioning.
- Cursors are bound to the caller as well as the collection, filters, and limit; another user cannot continue your page, and continuing with a changed filter is `400 invalid_cursor`.
- No string in a request body or in the `q` and `tag` query parameters may contain U+0000 (see [Validation](#validation)).

### Users and teams

`GET /me` provisions the caller on first contact (see [Authentication](#authentication)); `GET /users` and `GET /users/{userId}` are the directory, with keyset cursors. Teams (`/teams`, `/teams/{teamId}`) and memberships (`/teams/{teamId}/members`, `/teams/{teamId}/members/{userId}`) carry no ETags. Membership mutations check that the caller may act (`403`) before whether the target membership exists (`404`), so a nonmember cannot probe who belongs to a team; a nonmember deleting their own absent membership is `404`. A rename to the same name and a role change to the same role are `200` no-ops that leave `updatedAt` unchanged. A team's mutations lock the team row first, so two admins demoting or removing each other, or the last admin leaving twice, are decided one at a time: the second attempt is `409 last_admin` when it would leave no admin, or `403` when the first attempt already took the caller's admin role.

### Notes and lists

Only owners (the author and any co-owners) may change a note, manage its shares, trash and restore it. `GET /notes/{noteId}` never returns `403`: a note the caller cannot read is `404`, a trashed note is visible to its owners only, and an expired note to nobody. A `PATCH /notes/{noteId}` that changes nothing returns the existing representation and ETag with `updatedAt` unchanged; the version check (`412`) precedes the lifecycle check (`409`), so a stale ETag on a trashed note is `412`. Trash and restore: a repeated `DELETE` with the trash ETag is `204` with the same ETag and never extends the recovery period; the version check precedes both the idempotent repeat and the lifecycle check; `POST /notes/{noteId}/restore` ignores any request body and content type; at `expiresAt` exactly, reads, restore, and repeated trash are `404`.

`GET /notes` lists and searches: a repeated `q` parameter and duplicate `tag` values are `422` naming the parameter; the cursor is bound to every filter, so continuing with a changed filter is `400 invalid_cursor`; `scope=shared&state=trashed` and `teamId` with `state=trashed` are empty pages from the same query, not special cases. Search folds with `casefold()` and does not NFC-normalize.

### Shares

Anyone who is not an owner holds the union of their direct share and the shares addressed to teams they currently belong to: `read` is implied by any share, `comment` and `propose_edit` come from the share. Team roles grant nothing beyond the share. Every note representation reports the caller's own `effectivePermissions` in canonical order and an `isOwner` flag. A share's recipient must exist and must not already own the note (`422` at `/recipient/id`); a second share for the same recipient is `409 duplicate_share`; shares carry no ETag, change nothing about the note, and vanish when the note is trashed. On a trashed note, creating a share is `409 note_not_active`; because trashing deleted every share, the owner's list is an empty page and `GET`, `PATCH`, and `DELETE` of a former share are `404`. A share reached through another note's path is `404`. A `PATCH` that leaves the permission set unchanged keeps `updatedAt`.

### Comments

Anyone who can read a note can list and read its comments (`GET /notes/{noteId}/comments`, oldest first by `createdAt ASC, id ASC`, and `GET /notes/{noteId}/comments/{commentId}`), including recipients without `comment` permission. Adding one (`POST`) needs current `comment` permission, which owners always have; a read-only or proposal-only recipient gets `403`. Each comment carries its own strong ETag: `POST` answers `201` with `Location` and `ETag`, and `PATCH` and `DELETE` require `If-Match` with it. Only the author edits a comment, and only while they still hold `comment` permission; an owner cannot rewrite someone else's comment (`403`) but may delete any comment, and the author may delete their own under the same permission rule. Deletion is permanent (`204`, no body). A `PATCH` with an identical body is a `200` no-op that keeps the ETag and `updatedAt`. A comment id that does not belong to the note in the path is `404` before any `403` (every reader may list a note's comments, so there is nothing to hide, and the author rule needs the row); the body is validated before the precondition, `403` precedes `428`, and the version check (`412`) precedes the lifecycle check (`409 note_not_active`), as for notes. Comments never change the note's ETag or `updatedAt`. On a trashed note, owners still list and read the comments while adding, editing, and deleting are `409 note_not_active`; other former readers see `404`; the comments survive trash and restore.

### Edit requests

A proposer with current `propose_edit` permission (owners always have it) submits a complete `proposedContent` against the note's ETag with `POST /notes/{noteId}/edit-requests`. The server checks `baseNoteETag` against the live note before the lifecycle (a stale value is `412` and creates nothing), refuses a proposal identical to the note (`422` at `/proposedContent`), copies the note's title and body as the request's immutable base, and leaves the note, its ETag, and its timestamps untouched. The response is `201` with the flat `Location: /v1/edit-requests/{requestId}` and the request's own ETag, which covers the stored request but not the live `noteTitle` and `requiredApprovals`.

Only the note's owners and the proposer, while they can still read the note, may inspect a request (`GET /edit-requests/{requestId}`, which computes `proposalDiff` on read); every other caller, including other readers of the note, gets `404`. `GET /notes/{noteId}/edit-requests` lists a note's requests with one `status` (default `open`): every request for an owner, their own for a proposer, an empty page for other readers (from the same query, so an invalid cursor is still `400`). `GET /edit-requests` is the inbox: `view=incoming` (default) lists requests on notes the caller owns, their own proposals there included; `view=outgoing` lists what they proposed on notes they can still read; `state=trashed` selects the caller's own unexpired trash, which is owners only, so `view=outgoing&state=trashed` holds only requests an owner proposed on their own notes. Owners of a trashed note list and read its requests (the note-scoped list has no `state` filter). Summaries omit content and diffs and carry the note's current title.

The proposer revises an open request with `PATCH /edit-requests/{requestId}` and the request's ETag while they hold `propose_edit`: the base never changes; "a change to `proposedContent`" means the stored proposal actually differs afterwards, so a changed proposal deletes every approval, re-sending the current proposal with a new explanation keeps them, an identical resubmission keeps the ETag and `updatedAt`, and `explanation: ""` is stored as the empty string, distinct from `null`. The proposer withdraws with read access alone (`POST .../withdraw`, which never reads the request body), and any owner rejects (`POST .../reject`, optional `reason`: an empty body under any `Content-Type` counts as omitted, a JSON `null` body is `422` at pointer `""`, and `reason: null` and an omitted `reason` both render `rejectionReason: null`). An owner who proposed on their own note may withdraw and reject that request; an owner who did not propose gets `403` on withdraw. Closing sets `closedAt`, freezes `requiredApprovals` and `approvals`, and advances the request ETag, so an old ETag is `412` afterwards and the current one `409 request_not_open`; a trashed note is `409 note_not_active`; the version check precedes both, and `request_not_open` precedes `note_not_active`. `requiredApprovals` follows the design guide: `0` on a single-owner or `self_merge` note, otherwise `min(N, owners - 1)` for an owner's proposal and `max(2, min(N, owners))` for anyone else's.

### Preview and merge

An owner previews an open request with `POST /edit-requests/{requestId}/preview`, optionally with complete `finalContent` (an empty body under any `Content-Type` counts as omitted; a JSON `null` body is `422` at pointer `""`). The preview reads the request and its note in one select, runs the design guide's three-way merge (base, current, proposed; the title as one value, the body line by line with Git-style conflicts, nothing resolved silently in either side's favour), and reports the two versions it compared as `requestETag` and `currentNoteETag` together with the candidate and its `mergeDiff` when the merge is clean, or `canMerge: false` with structured conflicts, which stay in the response for review when `finalContent` resolves them. It changes nothing.

`POST /edit-requests/{requestId}/merge` needs the reviewed request ETag in `If-Match` and the reviewed note ETag as `expectedNoteETag`; both mismatches are `412` with a detail naming the input, the request's first. In one transaction under the note and request locks the server rechecks ownership, both versions, the open and active states, the approval count against the current owners and policy (`409 approval_required`; `0` under `self_merge` or a single owner; the stored approvals by current owners other than the proposer, plus one when the merger is neither the proposer nor an approver), and the candidate: `finalContent` when given, otherwise the clean three-way result, unresolved conflicts being `409 merge_conflict`. The lifecycle `409`s precede `approval_required`, which precedes `merge_conflict`. It writes only the note's title and body (tags and owners stay), advances the note's ETag and `updatedAt` even when the candidate equals the live content, and closes the request as `merged` with a `mergeRecord` (the committed content, the note's new ETag, the merger, the time) while `proposedContent` and `proposalDiff` stay as submitted and `approvals` and `requiredApprovals` freeze. The response is a `MergeResult` (the note as the merger sees it, with `isOwner: true` and every permission, `noteETag`, the closed request) whose `ETag` header is the request's. Merging one request neither closes nor rebases the others; their next previews compare against the updated note. Under `peer_approval` on a protected note `finalContent` is `422` at `/finalContent` in both operations, even when it equals the automatic candidate or the request has no conflicts.

### Approvals

An owner other than the proposer approves an open request with `POST /edit-requests/{requestId}/approve` and the request's ETag; the proposer is `403` with the contract's self-approval detail whether or not they own the note, checked before the owner rule; anyone else who is not an owner is `403`, and anyone who cannot inspect the request is `404`. An approval is of the current `proposedContent`: it advances the request's ETag and `updatedAt`, and `POST .../revoke-approval` deletes the caller's own approval under the same rules, except that the proposer gets the default `403` detail. Neither reads a request body; a repeat of either is a `200` no-op keeping the ETag and `updatedAt`, after the `412` and the two `409`s. Approvals are recorded even where the policy does not require them (`self_merge`, a single owner). They are deleted when the proposal's content changes or the approver stops being an owner, and frozen when the request closes. At merge time the count is the stored approvals by current owners other than the proposer, plus one when the merger is neither the proposer nor an approver.

### Owners and the review policy

Every note has one author and up to nineteen co-owners. The author alone adds a co-owner (`POST /notes/{noteId}/owners` with the note's ETag) and sets the review policy (`PATCH /notes/{noteId}/review-policy` with a complete `ReviewPolicy`: `self_merge` with `requiredApprovals: null`, or `peer_approval` with an integer from 1 up to the current owner count). The author may remove any co-owner and a co-owner only themselves (`DELETE /notes/{noteId}/owners/{userId}`); the author can never be removed (`409 author_cannot_be_removed`). All three are conditional note mutations: they advance the note's ETag and `updatedAt`, return the note, and are `409 note_not_active` on a trashed note. `ownerIds` lists the author first and then the co-owners in the order they were added; positions continue from the highest one in use, so removals leave gaps and the order survives them. A share the new owner already holds stays in place, redundant while they own the note and effective again if they leave.

`POST /notes/{noteId}/owners` checks an unknown user (`422` at `/userId`) before an existing owner (`409 duplicate_owner`) before the twenty-first owner (`422`). `DELETE /notes/{noteId}/owners/{userId}` resolves the target before the caller's right, like a mis-nested child: a `userId` that is not an owner is `404` for anyone who can see the note (`ownerIds` is public to every reader, so nothing is hidden), then a caller who is neither the author nor the target is `403`, then the author as target is the `409` after the version and lifecycle checks; the body is never read. A co-owner who removes themselves receives their own view of the note: `isOwner: false` and the permissions of any share they still hold, or `["read"]` when they hold none, because that response is the last read they get. A review-policy `PATCH` that changes nothing is a `200` no-op keeping the ETag and `updatedAt`; `requiredApprovals` above the owner count is `422` with detail "requiredApprovals cannot exceed the number of owners." and "the note has N owner(s)" at `/requiredApprovals`; the policy may be set on a single-owner note, where only `1` is valid, and the stored policy persists when owners leave.

With two or more owners the note is protected: `PATCH /notes/{noteId}` refuses `title` and `body` whole with `409 direct_edit_not_allowed` (tags may still change), content changes go through edit requests, and the review policy decides when they merge. When co-owners leave and one owner remains, direct edits resume, open requests stay open and mergeable, and each request's effective `requiredApprovals` is recomputed from the owners available. Adding an owner or changing the policy never touches an open request's ETag (`requiredApprovals` is a live view outside it); removing an owner deletes their approvals from every open request on the note inside the same transaction and gives only the requests that lost an approval a new ETag; closed requests are frozen.

### Request comments

The inspectors of an edit request, its note's owners and its proposer while they can still read the note, talk about it under `GET/POST /edit-requests/{requestId}/comments` and `GET/PATCH/DELETE /edit-requests/{requestId}/comments/{commentId}`; everyone else is `404`. The inspect rule alone governs: no note `comment` or `propose_edit` permission is needed, so a propose-only proposer can answer review feedback. Request comments have their own ETags (`PATCH` and `DELETE` require `If-Match`), sort oldest first, must belong to the request in the path (`404` before any `403`), and are accepted on closed requests while the note is active, for create, edit, and delete alike. The author edits their own comment (an owner who authored one edits it as its author) and any owner deletes any. They never advance the request's ETag or the note's. On a trashed note the owners still list and read them while every request-comment mutation is `409 note_not_active` after the `412` check, and everyone else is `404`.

## Operations

### Container

The image is the delivery unit: one non-root, read-only image built from the repository root (the server needs `openapi.yaml`), running a single uvicorn process per container and scaled with replicas. The same image runs migrations as a separate command. Base images are pinned by digest in the [`Dockerfile`](../Dockerfile) and Dependabot proposes updates weekly; because Docker Hub rebuilds the base only occasionally, the runtime stage also applies Debian's security updates at build time, and CI fails the image on any fixable CRITICAL or HIGH finding. The image is built and smoke-tested on every pull request; it is not published to a registry.

#### Prerequisite on macOS with Homebrew Docker

`docker build` needs BuildKit for the Dockerfile's cache and bind mounts. Homebrew's Docker CLI does not ship the buildx plugin, so install it once and link it where the CLI looks, the same way the compose plugin is linked:

```sh
brew install docker-buildx
mkdir -p ~/.docker/cli-plugins && ln -sfn "$(brew --prefix)/opt/docker-buildx/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx
docker buildx version
```

`docker compose build` works without it, because Compose bundles BuildKit.

#### Build and run

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

#### Smoke test

```sh
docker build -t notes-api:dev .
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

### Probes

- `GET /healthz`: liveness. `200 {"status":"ok"}` with no dependencies, so a database incident never restarts the fleet.
- `GET /readyz`: readiness. `SELECT 1` through the pool. `200` with `checks.database: ok`, or `503` with `checks.database: unavailable` and no connection details.

Both paths are outside `/v1`, are not part of the contract, and are unauthenticated, so the ingress must not route them. Kubernetes uses `httpGet` probes on the two paths; compose's own healthcheck calls `/readyz` with the image's Python, because the slim image has no curl.

### Migrations

Run `alembic upgrade head` as a one-shot job with the same image before rolling out the image that needs it, never at process start, where replicas would race:

```sh
docker run --rm -e DATABASE_URL=postgresql+psycopg://... notes-api:dev alembic -c /app/alembic.ini upgrade head
```

A migration must stay compatible with the image currently running, because old and new replicas share the schema during a rollout. Alembic reads `DATABASE_URL` through the same `Settings` as the server. `tests/test_schema.py` proves on every run that the migration and the models produce the same schema and that the downgrade works, on SQLite and, in CI, on PostgreSQL.

### Purging expired notes

Trashed notes expire exactly 30 × 24 hours after `deletedAt`; from that instant they are `404` for everyone, purge or no purge. Storage is reclaimed by `notes-api purge-expired`, a console script in the image's virtualenv that deletes every note at or past its expiry together with everything that hangs off it (owners, tags, shares, comments, edit requests, approvals, request comments) and prints `purged N expired notes`. Run it from an external scheduler (a cron job or a Kubernetes CronJob) with the same image and the same environment as the server; it reads the same settings and refuses to run without them. Locally: `docker compose run --rm -T api notes-api purge-expired`.

### Deployment constraints

The image assumes what the smoke test asserts. A Kubernetes `securityContext` should state the same: `runAsNonRoot: true`, `runAsUser` and `runAsGroup` 10001, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, capabilities `drop: [ALL]`, `seccompProfile: RuntimeDefault`, an `emptyDir` (memory) at `/tmp`. uvicorn drains in-flight requests for up to 20 seconds on SIGTERM, so `terminationGracePeriodSeconds` should be at least 30, with a short `preStop` sleep so the load balancer stops routing before the signal arrives.

### What the image contains

`/app/.venv` (the locked dependencies and `notes_api`, bytecode precompiled), `/app/openapi.yaml`, `/app/alembic.ini`, and `/app/alembic/`, all root-owned and read-only to the app user. Debian security updates are applied in the runtime stage; nothing else is installed. It does not contain uv, the tests, dev dependencies, a shell entrypoint, or curl. The uncompressed image is about 85 MB.
