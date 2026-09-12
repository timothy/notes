# Notes API design guide

This contract describes a notes service for one shared workspace with several teams. The machine-readable source of truth is [`../openapi.yaml`](../openapi.yaml), using OpenAPI 3.1.2. This guide explains the rules behind it. Both the operation descriptions and the schemas are normative. Where this prose and the OpenAPI document disagree, the OpenAPI document wins and this guide has a bug.

The deliverable is a contract, not a running backend. The example hostname is a placeholder. Authentication belongs to an external identity provider; teams, permissions, notes, comments, and edit requests belong to this service.

## 1. Core model

| Entity | Key fields | Relationships and invariants |
| --- | --- | --- |
| User | `id`, `displayName`, `createdAt` | A trusted `(issuer, subject)` maps to exactly one local user. The external identity key is internal. `GET /me` returns the caller's own profile. |
| Team | `id`, `name`, timestamps | Has memberships and may receive note shares. Names need not be globally unique. |
| Membership | `teamId`, `userId`, `role`, `joinedAt`, `updatedAt` | Unique per team/user. Role is `admin` or `member`. Every existing team has at least one admin. |
| Note | `id`, `authorId`, `title`, `body`, `tags`, timestamps, `deletedAt`, `expiresAt`, `isOwner`, `effectivePermissions` | `authorId` is its immutable owner. A note belongs to a person, not a team. `isOwner` and `effectivePermissions` are server-computed, read-only, and describe the caller. |
| Share | `id`, `noteId`, `recipient`, `permissions`, timestamps | Exactly one user or team recipient (`recipient: {type, id}`). Unique per note/recipient type/recipient ID. |
| Comment | `id`, `noteId`, `authorId`, `body`, timestamps | Flat Markdown comment; no reply hierarchy. Authorship is immutable. |
| Edit request | `id`, `noteId`, `noteTitle`, `proposerId`, `status`, `baseContent`, `proposedContent`, `explanation`, `proposalDiff`, timestamps, `closedAt`, `rejectionReason`, `mergeRecord` | Belongs to one note and proposer. Does not change live content until its owner merges it. |
| Merge record | `content`, `noteETag`, `mergedBy`, `mergedAt` | Part of a merged request (`mergeRecord`), separate from the submitted proposal. |

Identifiers are UUIDs. Times are RFC 3339 UTC strings ending in `Z`. The server assigns identities, IDs, and timestamps; request schemas reject attempts to set them. On first authenticated access, provision a local profile using the validated identity, never by matching email addresses. Concurrent first access must not create duplicate local users. `GET /me` returns the provisioned profile so a client learns its own ID. The directory exposes registered users' IDs, display names, and creation times, not tokens, emails, or provider subjects.

Content is Markdown source. Store it as text, preserve meaningful whitespace, and do not execute embedded HTML. Renderers must escape or sanitize untrusted source, including comments, explanations, and diffs. The API does not return pre-rendered HTML. Diff comparisons are textual and case-sensitive; they do not interpret Markdown structure.

### Input limits

| Input | Contract limit |
| --- | --- |
| Note title | 1–200 Unicode code points, with a non-whitespace character |
| Note body | 0–1,000,000 Unicode code points |
| Comment body | 1–10,000 Unicode code points, nonblank |
| Request explanation / rejection reason | At most 10,000 Unicode code points |
| Team name | 1–100 Unicode code points, nonblank |
| Tags | At most 50 unique strings, each 1–64 code points; no boundary whitespace or newlines |
| Tag filter values | At most 10 per query |
| Search query | 1–200 code points when supplied |
| Page limit | Default 25; minimum 1; maximum 100 |

Tags are case-sensitive, note-level values managed by the owner. Their array order is retained. Input JSON objects reject unknown fields. Partial updates require at least one supported property, leave omitted properties unchanged, and replace supplied arrays. Null is rejected for nonnullable properties; a nullable request explanation may be set to null to clear it. Creating a note requires a title; omitted body and tags default to `""` and `[]`. Adding a team member without a role defaults to `member`.

## 2. Authentication and authorization

Every operation requires an external-provider bearer **access token**, not an ID token. The deployment configures trusted issuer(s), audience, verification/introspection, expiry, and provider-supported revocation checks. The contract allows JWT or opaque tokens. Never derive team permissions from stale token role claims: use current local memberships and shares.

### Independent note permissions

| Effective permissions | Read note and comments | Add comments | Propose changes | Directly edit or merge |
| --- | --- | --- | --- | --- |
| `read` | Yes | No | No | No |
| `read`, `comment` | Yes | Yes | No | No |
| `read`, `propose_edit` | Yes | No | Yes | No |
| `read`, `comment`, `propose_edit` | Yes | Yes | Yes | No |
| Owner's intrinsic rights | Yes | Yes | Yes | Yes |

Share inputs may contain `read`, `comment`, and `propose_edit` in any combination of at least one unique value; `['read']` creates a read-only share. The server adds implied `read` when it is omitted and returns canonical order: `read`, `comment`, `propose_edit`, omitting ungranted capabilities. An empty permission set is invalid; revoke the share to remove it. A permission update replaces the set instead of appending to it. Recipient identity cannot be changed by PATCH.

Effective permissions are the union of direct user shares and shares to teams of which the user is currently a member. Removing one path leaves permissions supplied by other paths intact. A proposal permission never implies permission to comment. Read access includes existing comments, even when the user cannot add one. Every note representation reports the caller's own `effectivePermissions` in canonical form and an `isOwner` flag; the owner sees all three permissions and `isOwner: true`. These fields describe only the caller, never other recipients.

Only the owner can edit note content/tags, inspect or manage sharing, trash/restore a note, preview a merge, merge a request, or reject a request. Sharing directly to the owner is redundant and rejected with `422`, as is sharing to an unknown recipient. Owners may share with any existing workspace team, including teams they do not belong to. Grants confer no ownership or ownership-transfer rights.

The note owner can delete any comment but cannot rewrite someone else's comment. Other users need current `comment` permission to create, edit, or delete their own comments. Losing that permission while retaining read access preserves read access to the comment, not mutation rights. Deleting an individual comment is permanent.

### Teams

Any registered user can create a team and atomically becomes its first admin. All registered users can discover team metadata, and `GET /teams?scope=mine` narrows the list to teams the caller belongs to. Only members can list memberships. Admins may rename/delete teams and add existing users (an existing membership is `409` with code `duplicate_membership`; an unknown user ID is `422`), remove members, or change roles. A non-admin can remove only their own membership. Validate last-admin protection atomically, including concurrent attempts to leave or demote admins. Teams, memberships, and shares carry no ETag, so their mutations are unconditional.

Deleting a team removes its memberships and grants addressed to it. Notes, comments, and submitted requests survive because those belong to people. Team admins do not inherit note editing, moderation, sharing, or merging rights.

### Visibility and revocation

Authenticate first. Resolve access and nested-resource ownership before returning content, lifecycle details, ETags, or conflict data. A missing resource and one the caller cannot discover both return `404`; a caller who can see a resource but cannot perform the operation receives `403`. Nonmembers may discover team metadata, so member-list access returns `403`, not a hidden-team response.

For paths containing both a note ID and a comment/share ID, verify that the child belongs to that note. Never authorize against one note and fetch a child from another. Edit-request detail is more restricted than the note: only the owner and its proposer with current read access may inspect it. Other note readers get `404`. Their note-scoped request list contains no other people's requests.

Check authorization on every request, including each page. Serialize mutation authorization with grant, membership, and note-lifecycle changes: if revocation wins the race, an unauthorized mutation must not commit. Revocation does not erase already submitted proposals. The owner may review and merge those submissions even if the proposer no longer has access. A proposer who retains only read may inspect or withdraw their open request but cannot revise it. Without read access they cannot inspect or withdraw it.

All authenticated responses, including errors, use `Cache-Control: no-store`. This prevents HTTP caches from serving protected content after access changes; it cannot retract content somebody has already read.

## 3. Edit requests and merges

### Submission and revision

The proposer sends `baseNoteETag` and a complete `proposedContent` object containing `title` and `body`, plus an optional explanation. The server checks the note version and permission/lifecycle atomically and captures the trusted current title/body as the immutable base (`baseContent`). A stale submission returns `412`; the client must fetch the current note and reconcile its local work before resubmitting. There is no endpoint for submitting against arbitrary historical versions or supplying trusted base text. An owner may also submit a request against their own note; it is treated like any other.

The request starts `open`. It leaves the note, note ETag, and note timestamps unchanged. Its detail contains the base snapshot, current proposal, explanation, and a server-computed unified diff (`proposalDiff`). The inbox lists summaries, including the note's current `noteTitle`; the client fetches detail to display the diff. There is no email or event-stream delivery in this version.

Only the proposer can revise an open proposal while retaining `propose_edit`. The revision requires the request ETag, not the note ETag, and supplies a complete `proposedContent`, an `explanation` (a string, or `null` to clear it), or both. The base never changes; a revised proposal is still compared to that base. The server recomputes the diff and advances the request ETag on an effective change. Identical-to-base proposals are rejected with `422`. Owner edits use `finalContent` during preview/merge and must not replace the stored proposal. This API retains the latest submitted proposal, not a history of every intermediate proposal revision.

### Three-way preview

The owner calls `POST /edit-requests/{requestId}/preview`, optionally supplying complete `finalContent`. The preview reads a coherent pair of note/request versions and returns `requestETag` and `currentNoteETag` with its result. It changes no data, timestamps, or versions and reserves nothing. Previewing a closed request returns `409` with code `request_not_open`; previewing against a trashed note returns `409` with code `note_not_active`.

Compare these three versions:

- **Base:** the immutable snapshot captured when the request was submitted.
- **Current:** the live note at preview time.
- **Proposed:** the request's latest proposed title/body.

For the title, if current equals proposed, keep it; otherwise if one side equals the base, take the changed side; otherwise report a title conflict. Merge the body using a deterministic line-based three-way text merge with Git-style conflict detection. Preserve source text and line endings; do not parse, trim, or rewrite Markdown to resolve differences. Identical edits are compatible. Incompatible changes to a common segment, including competing insertions at the same location, require owner resolution. Do not silently favor current or proposed text.

`proposalDiff` compares base to proposed, using virtual files `base/title`, `proposed/title`, `base/body`, and `proposed/body`. `mergeDiff` compares current to the candidate using the `current` and `candidate` labels. Both are `{title, body}` pairs of unified-diff strings. Use unified diff syntax with three context lines and standard missing-final-newline notation where needed. An unchanged field has an empty diff string. Title diffing treats its string as one value with a virtual terminating newline; stored text remains unchanged.

Without `finalContent`, a clean preview returns `canMerge: true`, complete `candidate`, empty `conflicts`, `proposalDiff`, and `mergeDiff`. An automatic conflict is a successful preview (`200`) with `canMerge: false`, null candidate/diff, and structured conflicts. Each conflict contains the `field`, a `baseRange`, and the base/current/proposed segments. Body ranges use one-based, half-open base-line coordinates; an empty range denotes an insertion. A title conflict has a null `baseRange`.

With `finalContent`, the complete supplied title/body becomes the candidate. The preview returns its current-to-final diff with `canMerge: true` and `usedFinalContent: true`, retaining any automatic conflicts for review. Supplying complete final content is explicit owner resolution; do not attempt to infer resolution by scanning for conflict-marker-like strings that may legitimately appear in a note.

### Atomic approval and merge

`POST /edit-requests/{requestId}/merge` requires:

- `If-Match`: the reviewed **request** ETag.
- `expectedNoteETag`: the reviewed **note** ETag in the JSON body.
- Optional `finalContent`: the complete owner-edited title/body.

Without `finalContent`, recompute the clean three-way candidate for those exact versions and merge it. Unresolved automatic conflicts return `409` with code `merge_conflict`. With `finalContent`, commit the explicitly approved content, including owner edits or conflict resolutions.

In one transaction, recheck owner authorization, both versions, active note/open request state, and content validation; update only note title/body; preserve current tags and ownership; advance note ETag/timestamp; record actual merged content, resulting note ETag, merger and merge time in `mergeRecord`; and close the request as `merged` with `closedAt` set. A merge is a recorded write and advances the note version even if its candidate already equals live content. Preserve `proposedContent` and the base-to-proposal diff. Any failed check leaves both resources untouched.

The response contains `note`, `noteETag`, and the closed `editRequest`. Its HTTP `ETag` belongs to the edit request. Approval and merge are the same action: there is no intermediate approved state that could become stale.

### Lifecycle and races

| Current state | Action | Actor | Result |
| --- | --- | --- | --- |
| `open` | Revise proposal | Proposer with `propose_edit` | Remains `open`; immutable base retained |
| `open` | Preview | Note owner | No mutation |
| `open` | Merge | Note owner | `merged`; note updated atomically |
| `open` | Reject | Note owner | `rejected`, optional `rejectionReason`, no note change |
| `open` | Withdraw | Proposer with current read access | `withdrawn`, no note change |
| Any closed state | Mutate, preview, or reopen | Nobody | `409` with code `request_not_open`; closed records are immutable |

All mutations require an active note; a trashed note returns `409` with code `note_not_active`. Multiple requests may remain open. Merging one does not close or rebase the others; their next previews compare against the updated note. If two merges compete, the loser of the note-version check gets `412`. If the proposer revises during review, the request-version check prevents merging unreviewed changes. If a request was rejected, withdrawn, or merged, an old ETag returns `412`; using the current ETag with an invalid transition returns `409` with code `request_not_open`.

No custom idempotency key is provided. Repeating POST creation can create another resource; clients should not blindly retry after an ambiguous transport failure. Repeating a merge with its old request ETag cannot merge twice. After a lost merge response, read the request: a `merged` record supplies the committed result and attribution.

## 4. Note lifecycle, lists, and HTTP conventions

### Trash and restoration

Owner DELETE uses the active note ETag, removes shares, sets `deletedAt`, and sets `expiresAt` to exactly 30 × 24 hours later. Advance the note ETag and `updatedAt`. The `204` response carries the new trash `ETag`, so the owner can restore without another read. Comments and requests remain intact but frozen. The owner can read the trashed note and its related records; everyone else receives `404`. A repeated DELETE with the current trash ETag returns `204` without extending the recovery period.

Before expiry, restore with the trash note ETag. Clear deletion timestamps, advance its version, preserve comments and request states, and keep all shares absent. A formerly shared note therefore becomes private. Open requests again appear in the owner's active-note inbox, but their proposers do not regain access unless the owner re-shares the note. The restore advances the note ETag even when title/body are unchanged.

At `now >= expiresAt`, deny all reads and restoration with `404`, independently of cleanup scheduling. Permanent cleanup removes the note and its comments, requests, snapshots, and merge records. Restoring an already active note returns `409` with code `note_already_active`; no early permanent-delete endpoint is provided. Shares cannot be changed while a note is trashed: creating one returns `409` with code `note_not_active`, and the removed ones no longer exist.

### Lists and search

All collections return `{items, nextCursor}` without total counts. Cursors are opaque and tied to caller, collection, filters, and limit. Continue with the same query settings and cursor; invalid, expired, tampered, or mismatched cursors return `400`. Permission checks happen on every page. Paging is a live view, not a snapshot: new earlier entries may require refreshing the first page, and revoked/deleted entries disappear.

Notes, requests, teams, users, and shares sort by `createdAt DESC, id DESC`. Memberships sort by `joinedAt DESC, userId DESC`. Comments sort by `createdAt ASC, id ASC`. Use the ID tie-breaker consistently for equal timestamps. Note lists omit bodies; fetch individual notes for full content and their ETags. List items never carry versions; conditional mutations always start from an individual read.

Note filters combine with AND except that `q` matches title OR body. Query matching is a Unicode-aware case-insensitive literal substring with no stemming, regular expressions, or search language. `tag` may be repeated up to 10 times; a note must carry every listed tag. Tag matching is exact and case-sensitive. `scope` defaults to `all`; `mine` selects owned notes and `shared` selects accessible non-owned notes. `state` defaults to `active`; `trashed` always restricts to the owner, so `scope=shared&state=trashed` is empty.

`teamId` selects notes that currently have a share addressed to that team. It cannot grant access by itself; a direct recipient can match that filter even if they are not a team member. Unknown/unmatched IDs yield an empty authorized result. Because trashing removes all shares, a team filter combined with trash yields no notes. Return each note once despite overlapping grants.

The global request inbox uses `view=incoming|outgoing` (default incoming), a single `status` (default open), and associated-note `state` (default active). Incoming means the caller owns the note; outgoing means the caller proposed the request and still has read access. Trashed requests are owner-visible only. The note-scoped request list also defaults to open and returns only requests the caller may inspect.

### Versions and response codes

Note ETags cover note content, tags, and lifecycle metadata. Comment and edit-request ETags cover their own representations. Changing shares, memberships, comments, or requests does not advance the note ETag. Preview reads do not advance any ETag. An effective PATCH advances the affected resource's version; a no-op PATCH may return its existing representation and ETag. Versions are opaque, quoted strong tags, not client-incremented numbers.

Conditional existing-resource mutations accept one strong ETag in `If-Match`; wildcards, weak tags, and lists return `400`. Restore uses its note's validator; request actions use their request's validator. Resource creation needs no `If-Match`; proposal creation instead requires `baseNoteETag` in its input. Creation is not an exception to permission/lifecycle checks. Missing required JSON fields, including `expectedNoteETag` or `baseNoteETag`, are `422`; missing required conditional headers are `428`. ETag strings carried in JSON bodies must be exactly one quoted strong tag; a malformed value is a `422` field error, while a malformed `If-Match` header is `400`. Invalid query, header, or path parameter values are `422` with `errors[].location` naming the location; an invalid cursor is `400`.

Evaluate authentication and visibility first, then the supplied preconditions before attempting a state transition. Do not send version or conflict hints to unauthorized callers. The server must serialize the final checks with the write; fetching two ETags before a transaction is insufficient protection.

| Code | Meaning |
| --- | --- |
| `200` | Successful read, update, preview, or lifecycle transition |
| `201` | Created resource; include `Location`; notes, comments, and requests also include `ETag` |
| `204` | Successful deletion with no body; trashing a note also includes its new `ETag` |
| `400` | Malformed JSON/header or invalid cursor |
| `401` | Missing/invalid access token; include a bearer `WWW-Authenticate` challenge |
| `403` | Forbidden action on a visible resource |
| `404` | Missing, hidden, expired, or incorrectly nested resource |
| `409` | Invalid lifecycle transition (`note_not_active`, `note_already_active`, `request_not_open`), duplicate share or membership (`duplicate_share`, `duplicate_membership`), last-admin violation (`last_admin`), or unresolved merge conflict (`merge_conflict`) |
| `412` | Submitted/reviewed note or request version no longer matches |
| `415` | Unsupported request media type |
| `422` | Schema/field violation in the body or in a query, header, or path parameter, unknown input field, malformed ETag string in a JSON body, empty proposal, or other invalid content |
| `428` | Missing required `If-Match` header |

Success bodies use `application/json`; errors use `application/problem+json` with stable `type` and `code`. `type` is `https://notes-api.example.com/problems/{code}`. The complete `code` vocabulary is the `ErrorCode` enum in the OpenAPI document: the general codes `unauthenticated`, `forbidden`, `not_found`, `malformed_request`, `invalid_cursor`, `unsupported_media_type`, `validation_failed`, `precondition_required`, and `precondition_failed`, plus the `409` codes above. Return field issues using `errors` with location/pointer/detail; for body issues `pointer` is a JSON Pointer, and for query, header, and path issues it is the parameter name. Human-readable detail is explanatory text, never a client control-flow key. Error responses must not expose internal traces, note contents, or recipient data beyond the caller's permissions.

## 5. Example client workflow

These are complete request bodies validated against the named schemas. Routes below are relative to `https://notes-api.example.com/v1`; all require `Authorization: Bearer <access-token>`. IDs and ETags are illustrative, not live data. The complete response examples, including diffs and closed requests, are in `openapi.yaml`.

### Create and share

The owner sends `POST /notes`:

<!-- schema: CreateNote -->
```json
{
  "title": "Release checklist",
  "body": "## Release\n\n- Run tests\n- Deploy\n",
  "tags": ["release"]
}
```

The response is `201` with note ID `44444444-4444-4444-8444-444444444444`, its canonical Location, and `ETag: "note-v1"`.

The owner grants proposal-only access using `POST /notes/44444444-4444-4444-8444-444444444444/shares`:

<!-- schema: CreateShare -->
```json
{
  "recipient": {"type": "user", "id": "22222222-2222-4222-8222-222222222222"},
  "permissions": ["propose_edit"]
}
```

The share response includes `["read", "propose_edit"]`. This user can read existing comments but receives `403` if they try to add one. To also allow comments, the owner PATCHes that share with:

<!-- schema: UpdateShare -->
```json
{"permissions": ["comment", "propose_edit"]}
```

The canonical response includes all three permissions. A now-authorized recipient can POST to the note's `/comments` collection:

<!-- schema: CreateComment -->
```json
{"body": "Please check the error rate after deployment."}
```

To revoke that share, the owner DELETEs its `/shares/{shareId}` resource. Any independent team/user grants continue to apply.

### Submit and discover an edit request

After reading the note and its ETag, the proposer sends `POST /notes/44444444-4444-4444-8444-444444444444/edit-requests`:

<!-- schema: CreateEditRequest -->
```json
{
  "baseNoteETag": "\"note-v1\"",
  "proposedContent": {
    "title": "Release checklist",
    "body": "## Release\n\n- Run tests\n- Review metrics\n- Deploy\n"
  },
  "explanation": "Add a metrics check before deployment."
}
```

The service returns `201`, `Location: /v1/edit-requests/77777777-7777-4777-8777-777777777777`, and `ETag: "request-v1"`. The live note still has `ETag: "note-v1"` and its original body.

The owner polls `GET /edit-requests?view=incoming&status=open&state=active`, then GETs the individual request. Its body diff is:

```diff
--- base/body
+++ proposed/body
@@ -1,4 +1,5 @@
 ## Release
 
 - Run tests
+- Review metrics
 - Deploy
```

### Preview and merge unchanged or with owner edits

The owner calls `POST /edit-requests/77777777-7777-4777-8777-777777777777/preview` without a body, or with `{}`. If neither version has changed, the preview returns `requestETag: "\"request-v1\""` and `currentNoteETag: "\"note-v1\""`, a clean candidate, and its diff against the live note.

To merge that candidate unchanged, POST to the request's `/merge` action with `If-Match: "request-v1"`:

<!-- schema: MergeEditRequest -->
```json
{"expectedNoteETag": "\"note-v1\""}
```

Alternatively, the owner first previews their own edits:

<!-- schema: PreviewEditRequest -->
```json
{
  "finalContent": {
    "title": "Release checklist",
    "body": "## Release\n\n- Run tests\n- Review metrics and error rates\n- Deploy\n"
  }
}
```

Then merge with the versions returned by that preview. Assuming they are still the example versions, send `If-Match: "request-v1"` and:

<!-- schema: MergeEditRequest -->
```json
{
  "expectedNoteETag": "\"note-v1\"",
  "finalContent": {
    "title": "Release checklist",
    "body": "## Release\n\n- Run tests\n- Review metrics and error rates\n- Deploy\n"
  }
}
```

These are alternative merge paths, not sequential merges of the same request. Success returns `200` with the new note and closed request. The request's proposed body still says “Review metrics”; its merge record contains “Review metrics and error rates”. Tags remain `["release"]`. If either reviewed version changed, fetch and preview again after `412`. With an unresolved automatic conflict, inspect the structured conflict and supply complete owner-approved content before retrying.

### Trash and restore privately

The owner reads the latest note ETag and sends DELETE to the note with that `If-Match`. All grants disappear immediately. The `204` response carries the new trash ETag, and the owner can POST `/notes/{noteId}/restore` with that value before `expiresAt`. Existing comments and request records survive, but no recipient regains access automatically.

## 6. Verification and implementation acceptance

Run the reproducible contract checks from the project root (Python 3.10+):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/validate_contract.py
```

The checker validates the OpenAPI document, local references, every component schema, unique operation IDs, example payloads in both files (every media-type example in `openapi.yaml` and every `<!-- schema: X -->` JSON block in this guide), authentication coverage, conditional mutation coverage, endpoint inventory, required response headers and media types, and the must-fail / must-pass schema fixtures in `tests/negative_cases.yaml`. It does **not** execute a backend or prove authorization, transactions, or the merge algorithm. Those require the following acceptance scenarios when a service is implemented.

| Area | Required scenarios and observable results |
| --- | --- |
| Permission combinations | Exercise read-only, comment-only input, proposal-only input, and both. Implied read is returned; proposal-only comment creation fails. Unknown/empty/duplicate permission entries fail schema validation. `effectivePermissions` and `isOwner` match the caller's actual rights. |
| Owner control | Recipients cannot directly PATCH a note, manage shares, trash/restore, preview, reject, or merge. Team admins do not gain those rights. |
| Comments | Comment authors need current comment permission for edits/deletes; owner may delete others' comments but cannot edit them. Read-only users can read comments. |
| Overlapping grants | Removing a direct grant preserves capabilities from a team; removing membership preserves a remaining direct grant. Losing the final read path hides the note and the proposer's request. |
| Directory and teams | External identities provision once under concurrent access; `GET /me` returns the same profile. Nonmembers cannot list membership. Concurrent removal/demotion cannot leave an existing team without an admin. Team deletion preserves authored content. `scope=mine` lists only the caller's teams. |
| Isolation | Lists/search/inbox and all cursor pages reveal only authorized entries. Swapping a nested comment/share ID cannot access another note. Third-party note readers cannot inspect another person's proposal. |
| Submission | A valid proposal captures the server base and leaves the note unchanged. Stale base ETag fails without creating a request. Forged base content, tags, ownership, and empty changes are rejected. |
| Text merge | Unchanged current note accepts the proposal. Changes in separate regions combine; identical changes appear once. Incompatible title changes, overlapping replacement/deletion, and competing insertions are conflicts. Cover empty bodies, Unicode, CRLF/LF text, missing final newline, and empty base ranges. |
| Owner adjustments | Preview has no side effects. Complete final content can refine a clean proposal or resolve conflicts. Merged content and attribution are stored separately from the submitted proposal. Current tags are preserved. |
| Review races | A note edit or proposal revision after preview yields 412. Two competing merges cannot overwrite each other. Concurrent withdrawal/rejection versus merge permits only one transition. Authorization revoked before a mutation commits prevents the unauthorized commit. |
| Lifecycle | Only permitted actors reject/withdraw; read-only proposers may withdraw. Closed records cannot be mutated/reopened (`request_not_open`). Repeated merge with an old ETag cannot create a second merge. Read the record after an ambiguous response. |
| Revoked proposers | Owner can still review/merge a valid prior submission. A proposer without read cannot view it; a proposer without propose_edit cannot revise it. |
| Lists | AND filters, title-or-body search, repeated exact tags, overlapping-share deduplication, timestamp ties, limit boundaries, cursor misuse, revoked access between pages, and empty shared+trash/team+trash results. Inbox summaries carry the note's current title. |
| Trash | Trashing atomically removes shares and freezes comments/requests, and the 204 carries the trash ETag. Only owner can inspect preserved records. Restore is private and preserves request states. At the exact expiry boundary, reads and restoration fail before physical purge. |
| Atomicity/errors | Invalid final content, stale versions, missing preconditions, conflicts, and failed authorization leave note/request unchanged and use the documented Problem Details status and `code`. No private content leaks in errors. |

## 7. Scope and references

This version has no public links, email invitations, email notifications, realtime event stream, attachments, folders, live co-editing, account deletion, ownership transfer, standalone revision-history API, SQL schema/migrations, or server implementation. Snapshot persistence is required for edit requests and their attribution; it does not expose general historical note access. The owner inbox is the notification mechanism, suitable for clients to poll. Four conveniences were added while drafting the contract and are part of this version: `GET /me`, caller permissions on note representations, the team `scope` filter, and `noteTitle` in inbox summaries.

The contract uses [OpenAPI 3.1.2](https://spec.openapis.org/oas/v3.1.2.html), [HTTP conditional requests](https://www.rfc-editor.org/rfc/rfc9110.html#section-13), [428 Precondition Required](https://www.rfc-editor.org/rfc/rfc6585.html#section-3), and [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457.html). Merge and diff behavior is informed by [Git three-way file merging](https://git-scm.com/docs/git-merge-file) and [Git diff formats](https://git-scm.com/docs/diff-format); this contract does not require Git repositories or a Git hosting integration.
