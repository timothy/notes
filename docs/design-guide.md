# Notes API design guide

This contract describes a notes service for one shared workspace with several teams. The machine-readable source of truth is [`../openapi.yaml`](../openapi.yaml), using OpenAPI 3.1.2. This guide explains the rules behind it. Both the operation descriptions and the schemas are normative. Where this prose and the OpenAPI document disagree, the OpenAPI document wins and this guide has a bug.

The deliverable is a contract, not a running backend. The example hostname is a placeholder. Authentication belongs to an external identity provider; teams, permissions, notes, comments, and edit requests belong to this service.

## 1. Core model

| Entity | Key fields | Relationships and invariants |
| --- | --- | --- |
| User | `id`, `displayName`, `createdAt` | A trusted `(issuer, subject)` maps to exactly one local user. The external identity key is internal. `GET /me` returns the caller's own profile. |
| Team | `id`, `name`, timestamps | Has memberships and may receive note shares. Names need not be globally unique. |
| Membership | `teamId`, `userId`, `role`, `joinedAt`, `updatedAt` | Unique per team/user. Role is `admin` or `member`. Every existing team has at least one admin. |
| Note | `id`, `authorId`, `ownerIds`, `reviewPolicy`, `title`, `body`, `tags`, timestamps, `deletedAt`, `expiresAt`, `isOwner`, `effectivePermissions` | `authorId` is the original author: a permanent owner and the note's administrator. `ownerIds` lists every owner, the author first and then co-owners in the order added (1–20). A note with more than one owner is **protected**. Notes belong to people, never to a team. `isOwner` and `effectivePermissions` are server-computed, read-only, and describe the caller. |
| Review policy | `mode`, `requiredApprovals` | Part of the note, set by the author. `self_merge` (the default, `requiredApprovals: null`) or `peer_approval` with `requiredApprovals` from 1 to the owner count. Only matters while the note is protected. |
| Share | `id`, `noteId`, `recipient`, `permissions`, timestamps | Exactly one user or team recipient (`recipient: {type, id}`). Unique per note/recipient type/recipient ID. |
| Comment | `id`, `noteId`, `authorId`, `body`, timestamps | Flat Markdown comment; no reply hierarchy. Authorship is immutable. |
| Edit request | `id`, `noteId`, `noteTitle`, `proposerId`, `status`, `requiredApprovals`, `approvals`, `baseContent`, `proposedContent`, `explanation`, `proposalDiff`, timestamps, `closedAt`, `rejectedBy`, `rejectionReason`, `mergeRecord` | Belongs to one note and proposer. Does not change live content until an owner merges it. `requiredApprovals` is its effective requirement and `approvals` the explicit approvals it holds; both are live while open and frozen at closing. |
| Approval | `userId`, `approvedAt` | Part of an edit request: an owner other than the proposer approving the current `proposedContent`. Sorted `approvedAt ASC, userId ASC`. |
| Request comment | `id`, `requestId`, `authorId`, `body`, timestamps | Flat Markdown comment on an edit request; a sibling collection that never changes the request's ETag. |
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
| Owners per note | 1–20, the author included |
| Required approvals | 1 up to the current number of owners (`peer_approval` only) |

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
| Owners' intrinsic rights | Yes | Yes | Yes | Yes; on a protected note, through an edit request that satisfies the review policy |

Share inputs may contain `read`, `comment`, and `propose_edit` in any combination of at least one unique value; `['read']` creates a read-only share. The server adds implied `read` when it is omitted and returns canonical order: `read`, `comment`, `propose_edit`, omitting ungranted capabilities. An empty permission set is invalid; revoke the share to remove it. A permission update replaces the set instead of appending to it. Recipient identity cannot be changed by PATCH.

Effective permissions are the union of direct user shares and shares to teams of which the user is currently a member. Removing one path leaves permissions supplied by other paths intact. A proposal permission never implies permission to comment on the note (request comments are governed separately; see section 3). Read access includes existing comments, even when the user cannot add one. Every note representation reports the caller's own `effectivePermissions` in canonical form and an `isOwner` flag; every owner sees all three permissions and `isOwner: true`. These fields describe only the caller, never other recipients.

Only owners can edit tags, edit content directly (single-owner notes only; see section 3), inspect or manage sharing, trash/restore a note, preview a merge, or approve, merge, or reject a request. Sharing to any owner is redundant and rejected with `422`, as is sharing to an unknown recipient. Owners may share with any existing workspace team, including teams they do not belong to. Grants confer no ownership or ownership-transfer rights.

Any note owner can delete any comment but cannot rewrite someone else's comment. Other users need current `comment` permission to create, edit, or delete their own comments. Losing that permission while retaining read access preserves read access to the comment, not mutation rights. Deleting an individual comment is permanent.

### Owners and the review policy

Every note has exactly one author and up to nineteen co-owners. The author administers ownership: only they add a co-owner (`POST /notes/{noteId}/owners`), remove one (`DELETE /notes/{noteId}/owners/{userId}`), or set the review policy (`PATCH /notes/{noteId}/review-policy` with a complete `ReviewPolicy`), and the author can never be removed (`409` with code `author_cannot_be_removed`). A co-owner may remove themselves. These are conditional note mutations: they require `If-Match` with the note ETag, advance the note ETag and `updatedAt`, return the note, and are `409` with code `note_not_active` on a trashed note. Adding an existing owner is `409` with code `duplicate_owner`; an unknown user, a twenty-first owner, or `requiredApprovals` above the current owner count is `422` with a field error. A share the new owner already holds stays in place, redundant while they own the note and effective again if they leave. Co-owners and readers attempting administration receive `403`; a `userId` that is not an owner is `404`.

All owners are equal reviewers: each may propose, approve, preview, merge, and reject as section 3 describes. Trashing removes shares but never owners, so every owner can read a trashed note and restore it. When co-owners leave and one owner remains, the note stops being protected and direct edits resume; its open requests stay open and mergeable by that owner as in version 1. The policy may be set on a single-owner note (only `1` is valid there) and takes effect once the note is protected; if owners later leave, the stored value persists and each request's effective requirement clamps to the owners available.

### Teams

Any registered user can create a team and atomically becomes its first admin. All registered users can discover team metadata, and `GET /teams?scope=mine` narrows the list to teams the caller belongs to. Only members can list memberships. Admins may rename/delete teams and add existing users (an existing membership is `409` with code `duplicate_membership`; an unknown user ID is `422`), remove members, or change roles. A non-admin can remove only their own membership. Validate last-admin protection atomically, including concurrent attempts to leave or demote admins. Teams, memberships, and shares carry no ETag, so their mutations are unconditional.

Deleting a team removes its memberships and grants addressed to it. Notes, comments, and submitted requests survive because those belong to people. Team admins do not inherit note editing, moderation, sharing, or merging rights.

### Visibility and revocation

Authenticate first. Resolve access and nested-resource ownership before returning content, lifecycle details, ETags, or conflict data. A missing resource and one the caller cannot discover both return `404`; a caller who can see a resource but cannot perform the operation receives `403`. Nonmembers may discover team metadata, so member-list access returns `403`, not a hidden-team response.

For paths containing both a note ID and a comment/share ID, verify that the child belongs to that note. Never authorize against one note and fetch a child from another. Edit-request detail is more restricted than the note: only the note's owners and its proposer with current read access may inspect it. Other note readers get `404`. Their note-scoped request list contains no other people's requests.

Check authorization on every request, including each page. Serialize mutation authorization with grant, membership, ownership, review-policy, and note-lifecycle changes: if revocation wins the race, an unauthorized mutation must not commit. Revocation does not erase already submitted proposals. Owners may review and merge those submissions even if the proposer no longer has access. A proposer who retains only read may inspect or withdraw their open request but cannot revise it. Without read access they cannot inspect or withdraw it.

All authenticated responses, including errors, use `Cache-Control: no-store`. This prevents HTTP caches from serving protected content after access changes; it cannot retract content somebody has already read.

## 3. Edit requests and merges

### Submission and revision

The proposer sends `baseNoteETag` and a complete `proposedContent` object containing `title` and `body`, plus an optional explanation. The server checks the note version and permission/lifecycle atomically and captures the trusted current title/body as the immutable base (`baseContent`). A stale submission returns `412`; the client must fetch the current note and reconcile its local work before resubmitting. There is no endpoint for submitting against arbitrary historical versions or supplying trusted base text. Owners may also submit requests against their own notes, and on a protected note they must; such a request is treated like any other, except that its proposer cannot approve it.

The request starts `open`. It leaves the note, note ETag, and note timestamps unchanged. Its detail contains the base snapshot, current proposal, explanation, and a server-computed unified diff (`proposalDiff`). The inbox lists summaries, including the note's current `noteTitle` and the request's `requiredApprovals` and `approvals`; the client fetches detail to display the diff. There is no email or event-stream delivery in this version.

Only the proposer can revise an open proposal while retaining `propose_edit`. The revision requires the request ETag, not the note ETag, and supplies a complete `proposedContent`, an `explanation` (a string, or `null` to clear it), or both. The base never changes; a revised proposal is still compared to that base. The server recomputes the diff and advances the request ETag on an effective change. A change to `proposedContent` deletes every approval; an explanation-only revision keeps them. Identical-to-base proposals are rejected with `422`. Owner edits use `finalContent` during preview/merge and must not replace the stored proposal. This API retains the latest submitted proposal, not a history of every intermediate proposal revision.

### Three-way preview

An owner calls `POST /edit-requests/{requestId}/preview`, optionally supplying complete `finalContent`. The preview reads a coherent pair of note/request versions and returns `requestETag` and `currentNoteETag` with its result. It changes no data, timestamps, or versions and reserves nothing. Previewing a closed request returns `409` with code `request_not_open`; previewing against a trashed note returns `409` with code `note_not_active`.

Compare these three versions:

- **Base:** the immutable snapshot captured when the request was submitted.
- **Current:** the live note at preview time.
- **Proposed:** the request's latest proposed title/body.

For the title, if current equals proposed, keep it; otherwise if one side equals the base, take the changed side; otherwise report a title conflict. Merge the body using a deterministic line-based three-way text merge with Git-style conflict detection. Preserve source text and line endings; do not parse, trim, or rewrite Markdown to resolve differences. Identical edits are compatible. Incompatible changes to a common segment, including competing insertions at the same location, require owner resolution. Do not silently favor current or proposed text.

`proposalDiff` compares base to proposed, using virtual files `base/title`, `proposed/title`, `base/body`, and `proposed/body`. `mergeDiff` compares current to the candidate using the `current` and `candidate` labels. Both are `{title, body}` pairs of unified-diff strings. Use unified diff syntax with three context lines and standard missing-final-newline notation where needed. An unchanged field has an empty diff string. Title diffing treats its string as one value with a virtual terminating newline; stored text remains unchanged.

Without `finalContent`, a clean preview returns `canMerge: true`, complete `candidate`, empty `conflicts`, `proposalDiff`, and `mergeDiff`. An automatic conflict is a successful preview (`200`) with `canMerge: false`, null candidate/diff, and structured conflicts. Each conflict contains the `field`, a `baseRange`, and the base/current/proposed segments. Body ranges use one-based, half-open base-line coordinates; an empty range denotes an insertion. A title conflict has a null `baseRange`.

With `finalContent`, the complete supplied title/body becomes the candidate. The preview returns its current-to-final diff with `canMerge: true` and `usedFinalContent: true`, retaining any automatic conflicts for review. Supplying complete final content is explicit owner resolution; do not attempt to infer resolution by scanning for conflict-marker-like strings that may legitimately appear in a note. Under `peer_approval` on a protected note, `finalContent` is refused by preview and merge with `422` (pointer `/finalContent`); see **Approvals** below. `canMerge` describes content only.

### Protected notes and review modes

A note with more than one owner is protected. `PATCH /notes/{noteId}` naming `title` or `body` is refused whole with `409` and code `direct_edit_not_allowed` (a tags-only PATCH succeeds), and the protection check runs inside the write transaction so an owner added between read and write is caught. Owners change content the way everyone else does: an edit request with a diff, then a merge. The review policy decides when the merge may happen:

- `self_merge`: any owner merges alone after reviewing the diff. The two-step submit-then-merge is the speed bump; no approval is required and each request's `requiredApprovals` is `0`.
- `peer_approval`: the request needs approvals from owners other than the proposer. Each request reports its **effective** `requiredApprovals`: `min(N, owners other than the proposer)` when the proposer is an owner, and `max(2, min(N, owner count))` when the proposer is not, where `N` is the policy value. The invariant is that **no change lands on a peer-approval note without at least two distinct current owners taking part, as proposer, approver, or merger**; without the `max(2, …)` clause a single co-owner could grant `propose_edit` to anyone, have them submit the change, and merge it alone.

Single-owner notes are never protected: their `requiredApprovals` is `0` and version 1 behavior applies unchanged. Peer approval guards against unilateral edits by co-owners and careless edits by the author. It does not guard against a hostile author, who administers ownership and policy and can lower the bar; that is the price of keeping administration simple.

### Approvals

An approval is approval of `proposedContent` against `baseContent`. The merge applies that delta three-way to the live note, as in Git, and the merger sees `mergeDiff`; approvals are not invalidated by other merges on the note. `POST /edit-requests/{requestId}/approve` is for owners other than the proposer (the proposer receives `403`; anyone who cannot inspect the request receives `404`), requires `If-Match` with the request ETag, records the caller in `approvals`, and advances the request ETag and `updatedAt`. Repeating it is a no-op returning the existing representation and ETag. `POST /edit-requests/{requestId}/revoke-approval` deletes the caller's own approval under the same rules, and is a no-op when there is none. Both are `409` with code `request_not_open` on a closed request. Approvals are recorded even when not required (`self_merge`, or a single-owner note).

Approvals are stored state. A revision that changes `proposedContent` deletes all of them; an explanation-only revision keeps them. Removing an owner deletes that user's approvals from every open request on the note inside the removal transaction, advancing those requests' ETags, so every listed approver is a current owner other than the proposer. Closing a request freezes `approvals` and `requiredApprovals`; later ownership or policy changes do not touch closed records. A rejected request records who rejected it in `rejectedBy`.

At merge time the count is the stored approvals plus one when the merger is neither the proposer nor already an approver: merging is itself an explicit approval. Fewer than `requiredApprovals` is `409` with code `approval_required` and nothing changes. `approvals` holds explicit approvals only, so the audit record of a merged request is `approvals` plus `mergeRecord.mergedBy`. Under `peer_approval`, `finalContent` is refused by preview and merge with `422` (pointer `/finalContent`): approvers approved the proposal, so the merge commits exactly its three-way candidate, and conflicts are resolved by the proposer revising (which deletes the approvals) or by a fresh proposal. Under `self_merge` and on single-owner notes `finalContent` behaves as described above.

### Atomic approval and merge

`POST /edit-requests/{requestId}/merge` requires:

- `If-Match`: the reviewed **request** ETag.
- `expectedNoteETag`: the reviewed **note** ETag in the JSON body.
- Optional `finalContent`: the complete owner-edited title/body.

Without `finalContent`, recompute the clean three-way candidate for those exact versions and merge it. Unresolved automatic conflicts return `409` with code `merge_conflict`. With `finalContent`, commit the explicitly approved content, including owner edits or conflict resolutions.

In one transaction, recheck owner authorization, the approval count against the current owners and policy, both versions, active note/open request state, and content validation; update only note title/body; preserve current tags and ownership; advance note ETag/timestamp; record actual merged content, resulting note ETag, merger and merge time in `mergeRecord`; freeze `approvals` and `requiredApprovals`; and close the request as `merged` with `closedAt` set. A merge is a recorded write and advances the note version even if its candidate already equals live content. Preserve `proposedContent` and the base-to-proposal diff. Any failed check leaves both resources untouched.

The response contains `note`, `noteETag`, and the closed `editRequest`. Its HTTP `ETag` belongs to the edit request. On a single-owner note approval and merge are still one action. On a protected note approvals are an intermediate state bound to the proposal version; they cannot go stale because a content revision deletes them.

### Lifecycle and races

| Current state | Action | Actor | Result |
| --- | --- | --- | --- |
| `open` | Revise proposal | Proposer with `propose_edit` | Remains `open`; immutable base retained |
| `open` | Approve | Owner other than the proposer | Remains `open`; approval recorded, request ETag advances |
| `open` | Revoke approval | The approver | Remains `open`; approval deleted, request ETag advances |
| `open` | Owner removed or policy changed | Author, or a co-owner leaving | Remains `open`; the leaver's approvals are deleted and `requiredApprovals` is recomputed |
| `open` | Preview | Any owner | No mutation |
| `open` | Merge | Any owner, once approvals suffice | `merged`; note updated atomically |
| `open` | Reject | Any owner | `rejected`, `rejectedBy` set, optional `rejectionReason`, no note change |
| `open` | Withdraw | Proposer with current read access | `withdrawn`, no note change |
| Any closed state | Mutate, preview, or reopen | Nobody | `409` with code `request_not_open`; closed records are immutable |

All mutations require an active note; a trashed note returns `409` with code `note_not_active`. Multiple requests may remain open. Merging one does not close or rebase the others; their next previews compare against the updated note. If two merges compete, the loser of the note-version check gets `412`. If the proposer revises during review, the request-version check prevents merging unreviewed changes. If an owner approves while the proposer revises, only one wins the request-version check: a winning revision deletes the approvals, and a winning approval makes the proposer re-read. If an owner is removed while a merge that relied on their approval is in flight, the removal advanced the request ETag, so the merge is `412` and is re-evaluated against the remaining approvals. If a request was rejected, withdrawn, or merged, an old ETag returns `412`; using the current ETag with an invalid transition returns `409` with code `request_not_open`.

No custom idempotency key is provided. Repeating POST creation can create another resource; clients should not blindly retry after an ambiguous transport failure. Repeating a merge with its old request ETag cannot merge twice. After a lost merge response, read the request: a `merged` record supplies the committed result and attribution.

## 4. Note lifecycle, lists, and HTTP conventions

### Trash and restoration

An owner's DELETE uses the active note ETag, removes shares (owners are kept), sets `deletedAt`, and sets `expiresAt` to exactly 30 × 24 hours later. Advance the note ETag and `updatedAt`. The `204` response carries the new trash `ETag`, so the owner can restore without another read. Comments and requests remain intact but frozen. Owners can read the trashed note and its related records; everyone else receives `404`. A repeated DELETE with the current trash ETag returns `204` without extending the recovery period.

Before expiry, restore with the trash note ETag. Clear deletion timestamps, advance its version, preserve comments and request states, and keep all shares absent. A formerly shared note therefore becomes private to its owners. Open requests again appear in the owners' active-note inboxes, but their proposers do not regain access unless an owner re-shares the note. The restore advances the note ETag even when title/body are unchanged.

At `now >= expiresAt`, deny all reads and restoration with `404`, independently of cleanup scheduling. Permanent cleanup removes the note and its comments, requests, snapshots, and merge records. Restoring an already active note returns `409` with code `note_already_active`; no early permanent-delete endpoint is provided. Shares cannot be changed while a note is trashed: creating one returns `409` with code `note_not_active`, and the removed ones no longer exist.

### Lists and search

All collections return `{items, nextCursor}` without total counts. Cursors are opaque and tied to caller, collection, filters, and limit. The reference server signs cursors and expires each one 24 hours after issuance. Continue with the same query settings and cursor; invalid, expired, tampered, or mismatched cursors return `400`. Permission checks happen on every page. Paging is a live view, not a snapshot: new earlier entries may require refreshing the first page, and revoked/deleted entries disappear.

Notes, requests, teams, users, and shares sort by `createdAt DESC, id DESC`. Memberships sort by `joinedAt DESC, userId DESC`. Comments and request comments sort by `createdAt ASC, id ASC`; approvals by `approvedAt ASC, userId ASC`; `ownerIds` lists the author first, then co-owners in the order added. Use the ID tie-breaker consistently for equal timestamps. Note lists omit bodies; fetch individual notes for full content and their ETags. List items never carry versions; conditional mutations always start from an individual read.

Note filters combine with AND except that `q` matches title OR body. Query matching is a Unicode-aware case-insensitive literal substring with no stemming, regular expressions, or search language. `tag` may be repeated up to 10 times; a note must carry every listed tag. Tag matching is exact and case-sensitive. `scope` defaults to `all`; `mine` selects notes the caller owns (as author or co-owner) and `shared` selects accessible non-owned notes. `state` defaults to `active`; `trashed` always restricts to the owner, so `scope=shared&state=trashed` is empty.

`teamId` selects notes that currently have a share addressed to that team. It cannot grant access by itself; a direct recipient can match that filter even if they are not a team member. Unknown/unmatched IDs yield an empty authorized result. Because trashing removes all shares, a team filter combined with trash yields no notes. Return each note once despite overlapping grants.

The global request inbox uses `view=incoming|outgoing` (default incoming), a single `status` (default open), and associated-note `state` (default active). Incoming means the caller owns the note (an owner's own proposals therefore appear in both views); outgoing means the caller proposed the request and still has read access. Trashed requests are owner-visible only. The note-scoped request list also defaults to open and returns only requests the caller may inspect.

### Versions and response codes

Note ETags cover note content, tags, lifecycle metadata, ownership, and review policy. Comment, request-comment, and edit-request ETags cover their own stored state; an edit request's `noteTitle` and `requiredApprovals` are live views of its note and are excluded. Changing shares, memberships, comments, request comments, or requests does not advance the note ETag. Recording or deleting an approval advances the request ETag; request comments never do. Preview reads do not advance any ETag. An effective PATCH advances the affected resource's version; a no-op PATCH may return its existing representation and ETag. Versions are opaque, quoted strong tags, not client-incremented numbers.

Conditional existing-resource mutations accept one strong ETag in `If-Match`; wildcards, weak tags, lists, and repeated header fields (even identical ones) return `400`. Restore, owner changes, and review-policy changes use the note's validator; request actions, including approve and revoke, use their request's validator; request-comment edits use the comment's. Resource creation needs no `If-Match`; proposal creation instead requires `baseNoteETag` in its input. Creation is not an exception to permission/lifecycle checks. Missing required JSON fields, including `expectedNoteETag` or `baseNoteETag`, are `422`; missing required conditional headers are `428`. ETag strings carried in JSON bodies must be exactly one quoted strong tag; a malformed value is a `422` field error, while a malformed `If-Match` header is `400`. Invalid query, header, or path parameter values are `422` with `errors[].location` naming the location; an invalid cursor is `400`.

Evaluate authentication and visibility first, then the supplied preconditions before attempting a state transition. Do not send version or conflict hints to unauthorized callers. The server must serialize the final checks with the write; fetching two ETags before a transaction is insufficient protection.

| Code | Meaning |
| --- | --- |
| `200` | Successful read, update, preview, or lifecycle transition |
| `201` | Created resource; include `Location`; notes, comments, and requests also include `ETag` |
| `204` | Successful deletion with no body; trashing a note also includes its new `ETag` |
| `400` | Malformed JSON/header or invalid cursor |
| `401` | Missing/invalid access token; include a bearer `WWW-Authenticate` challenge |
| `403` | Forbidden action on a visible resource, including a proposer approving their own request |
| `404` | Missing, hidden, expired, or incorrectly nested resource |
| `409` | Invalid lifecycle transition (`note_not_active`, `note_already_active`, `request_not_open`), direct edit of a protected note (`direct_edit_not_allowed`), too few approvals (`approval_required`), duplicate share, membership, or owner (`duplicate_share`, `duplicate_membership`, `duplicate_owner`), last-admin or author-removal violation (`last_admin`, `author_cannot_be_removed`), or unresolved merge conflict (`merge_conflict`) |
| `412` | Submitted/reviewed note or request version no longer matches |
| `415` | Unsupported request media type |
| `422` | Schema/field violation in the body or in a query, header, or path parameter, unknown input field, malformed ETag string in a JSON body, empty proposal, `finalContent` under peer approval, an unknown or twenty-first owner, `requiredApprovals` above the owner count, or other invalid content |
| `428` | Missing required `If-Match` header |

Success bodies use `application/json`; errors use `application/problem+json` with stable `type` and `code`. `type` is `https://notes-api.example.com/problems/{code}`. The complete `code` vocabulary is the `ErrorCode` enum in the OpenAPI document: the general codes `unauthenticated`, `forbidden`, `not_found`, `malformed_request`, `invalid_cursor`, `unsupported_media_type`, `validation_failed`, `precondition_required`, and `precondition_failed`, plus the `409` codes above. Return field issues using `errors` with location/pointer/detail; for body issues `pointer` is a JSON Pointer, and for query, header, and path issues it is the parameter name. Human-readable detail is explanatory text, never a client control-flow key. Error responses must not expose internal traces, note contents, or recipient data beyond the caller's permissions.

### Contract versioning

The `/v1` prefix on every route and `Location` value is the compatibility line for clients. It changes only for a change that would break a correctly written client: removing or renaming an operation or a response property, adding a required request property, narrowing an accepted input, or changing the status code or meaning of an existing success response. Such a change ships under a new prefix, and the previous prefix keeps working for an announced deprecation period. Adding operations, optional request properties, response properties, or `ErrorCode` values is compatible, so clients must ignore properties they do not know and must handle an unknown `code` by its HTTP status.

`info.version` is the semantic version of this contract document. The major number advances when implementers must revisit existing behavior, as 2.0.0 did by letting any note become protected and making `PATCH /notes/{noteId}` refuse title and body on one, even though every 1.0.0 client request still works under `/v1`. The minor number advances for compatible additions, and the patch number for clarifications and example fixes that leave the wire format alone; repository tooling changes do not move the version. A change that moves the compatibility line always advances the major number as well. Each release is an annotated tag `v<info.version>` on `main` and has an entry in `CHANGELOG.md`, and pull requests fail when oasdiff finds a client-breaking change against `main` (section 6).

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

### Protect a note and merge with peer approval

Ada (`11111111-1111-4111-8111-111111111111`) creates a second note, `bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb` "Incident runbook", which comes back with `ETag: "runbook-v1"`, one owner, and the default `self_merge` policy. She adds Cara (`99999999-9999-4999-8999-999999999999`) with `POST /notes/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/owners` and `If-Match: "runbook-v1"`:

<!-- schema: AddOwner -->
```json
{"userId": "99999999-9999-4999-8999-999999999999"}
```

The response is the note with `ownerIds` `[Ada, Cara]` and `ETag: "runbook-v2"`; the note is now protected. Ada then requires one peer approval with `PATCH /notes/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/review-policy` and `If-Match: "runbook-v2"`:

<!-- schema: ReviewPolicy -->
```json
{"mode": "peer_approval", "requiredApprovals": 1}
```

The note returns with `ETag: "runbook-v3"`. If Ada now sends `PATCH /notes/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb` with a new `body`, she receives `409` with code `direct_edit_not_allowed`. Instead she submits an edit request against `baseNoteETag: "\"runbook-v3\""` adding a rollback step. It is created as `cccccccc-cccc-4ccc-8ccc-cccccccccccc` with `ETag: "proposal-v1"`, `requiredApprovals: 1` (one owner other than Ada), and empty `approvals`. Cara sees it in her incoming inbox and comments with `POST /edit-requests/cccccccc-cccc-4ccc-8ccc-cccccccccccc/comments`:

<!-- schema: CreateComment -->
```json
{"body": "Looks good. Can you also say who declares the incident over?"}
```

Satisfied, Cara approves with `POST /edit-requests/cccccccc-cccc-4ccc-8ccc-cccccccccccc/approve` and `If-Match: "proposal-v1"`. The request returns with her approval and `ETag: "proposal-v2"`. Had Ada tried to merge before this, she would have received `409` with code `approval_required`; had she supplied `finalContent`, `422` with pointer `/finalContent`. Ada previews (`requestETag: "\"proposal-v2\""`, `currentNoteETag: "\"runbook-v3\""`) and merges with `If-Match: "proposal-v2"`:

<!-- schema: MergeEditRequest -->
```json
{"expectedNoteETag": "\"runbook-v3\""}
```

The count is one stored approval plus nothing for Ada, who is the proposer, so the merge lands: the note becomes `"runbook-v4"`, the request closes as `merged` with `ETag: "proposal-v3"`, and its `approvals` still list Cara next to `mergeRecord.mergedBy: Ada`. Alternatively Cara could have merged directly without approving; her merge counts as the one required approval. Had Ben (`22222222-2222-4222-8222-222222222222`), a non-owner with `propose_edit`, proposed the same change, its `requiredApprovals` would be `2`, so Ada and Cara would both have to take part. Finally, if Cara removes herself with `DELETE /notes/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/owners/99999999-9999-4999-8999-999999999999` and the current note ETag, the note returns with one owner and `"runbook-v5"`; it is no longer protected and Ada can edit directly again.

## 6. Verification and implementation acceptance

Run the reproducible contract checks from the project root (Python 3.10+):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/validate_contract.py
```

GitHub Actions runs this checker and the Redocly lint on every push to `main` and every pull request. Pull requests additionally compare `openapi.yaml` with `main` using [oasdiff](https://github.com/oasdiff/oasdiff) and fail on client-breaking changes as defined under [Contract versioning](#contract-versioning) in section 4. The `breaking-change` pull request label turns that failure into a report and is reserved for changes that move the compatibility line to a new path prefix. The workflow is `.github/workflows/contract.yml`.

The checker validates the OpenAPI document, local references, every component schema, unique operation IDs, example payloads in both files (every media-type example in `openapi.yaml` and every `<!-- schema: X -->` JSON block in this guide), authentication coverage, conditional mutation coverage, endpoint inventory, required response headers and media types, the must-fail / must-pass schema fixtures in `tests/negative_cases.yaml`, the `ErrorCode` vocabulary against the Problem examples, and the ownership and approval invariants in the examples that JSON Schema cannot express. It does **not** execute a backend or prove authorization, transactions, or the merge algorithm. Those require the following acceptance scenarios when a service is implemented.

| Area | Required scenarios and observable results |
| --- | --- |
| Permission combinations | Exercise read-only, comment-only input, proposal-only input, and both. Implied read is returned; proposal-only comment creation fails. Unknown/empty/duplicate permission entries fail schema validation. `effectivePermissions` and `isOwner` match the caller's actual rights. |
| Owner control | Recipients cannot directly PATCH a note, manage shares or owners, trash/restore, preview, approve, reject, or merge. Team admins do not gain those rights. Co-owners cannot add or remove other owners or change the policy; only the author can, and the author cannot be removed. |
| Protected notes | With two or more owners, a PATCH naming title or body is refused whole with `direct_edit_not_allowed` while a tags-only PATCH succeeds, and an owner added between read and PATCH is caught inside the transaction. Removing the second-to-last owner restores direct edits and leaves open requests mergeable. Owner and policy changes advance the note ETag and fail stale `baseNoteETag`/`expectedNoteETag` values. A twenty-first owner and `requiredApprovals` above the owner count are `422`. |
| Approvals | Only owners other than the proposer can approve; the proposer gets `403` and non-inspectors `404`; re-approving and revoking a missing approval are no-ops. A content revision deletes approvals, an explanation-only revision keeps them, and owner removal deletes that owner's approvals on open requests with those request ETags advancing. Merge counts stored approvals plus a non-proposing merger; too few is `approval_required` with nothing written; under `peer_approval` a non-owner's proposal needs two distinct owners even with `requiredApprovals: 1`; `finalContent` is `422` under `peer_approval` and works under `self_merge`. Closed requests freeze `approvals` and `requiredApprovals`; a rejected request records `rejectedBy`. |
| Request comments | Owners and the proposer can comment without note `comment` permission; other readers get `404`. Authors edit their own, owners delete any, a trashed note freezes them, closed requests still accept them, and no request comment changes the request ETag. |
| Comments | Comment authors need current comment permission for edits/deletes; owner may delete others' comments but cannot edit them. Read-only users can read comments. |
| Overlapping grants | Removing a direct grant preserves capabilities from a team; removing membership preserves a remaining direct grant. Losing the final read path hides the note and the proposer's request. |
| Directory and teams | External identities provision once under concurrent access; `GET /me` returns the same profile. Nonmembers cannot list membership. Concurrent removal/demotion cannot leave an existing team without an admin. Team deletion preserves authored content. `scope=mine` lists only the caller's teams. |
| Isolation | Lists/search/inbox and all cursor pages reveal only authorized entries. Swapping a nested comment/share ID cannot access another note. Third-party note readers cannot inspect another person's proposal. |
| Submission | A valid proposal captures the server base and leaves the note unchanged. Stale base ETag fails without creating a request. Forged base content, tags, ownership, and empty changes are rejected. |
| Text merge | Unchanged current note accepts the proposal. Changes in separate regions combine; identical changes appear once. Incompatible title changes, overlapping replacement/deletion, and competing insertions are conflicts. Cover empty bodies, Unicode, CRLF/LF text, missing final newline, and empty base ranges. |
| Owner adjustments | Preview has no side effects. Complete final content can refine a clean proposal or resolve conflicts. Merged content and attribution are stored separately from the submitted proposal. Current tags are preserved. |
| Review races | A note edit or proposal revision after preview yields 412. Two competing merges cannot overwrite each other. Concurrent withdrawal/rejection versus merge permits only one transition. An approval and a revision racing on the same request ETag permit only one. Authorization revoked before a mutation commits prevents the unauthorized commit. |
| Lifecycle | Only permitted actors reject/withdraw; read-only proposers may withdraw. Closed records cannot be mutated/reopened (`request_not_open`). Repeated merge with an old ETag cannot create a second merge. Read the record after an ambiguous response. |
| Revoked proposers | Owner can still review/merge a valid prior submission. A proposer without read cannot view it; a proposer without propose_edit cannot revise it. |
| Lists | AND filters, title-or-body search, repeated exact tags, overlapping-share deduplication, timestamp ties, limit boundaries, cursor misuse, revoked access between pages, and empty shared+trash/team+trash results. Inbox summaries carry the note's current title. |
| Trash | Trashing atomically removes shares and freezes comments/requests, and the 204 carries the trash ETag. Only owner can inspect preserved records. Restore is private and preserves request states. At the exact expiry boundary, reads and restoration fail before physical purge. |
| Atomicity/errors | Invalid final content, stale versions, missing preconditions, conflicts, and failed authorization leave note/request unchanged and use the documented Problem Details status and `code`. No private content leaks in errors. |

## 7. Scope and references

Version 2 adds co-owners, protected notes, the review policy with `self_merge` and `peer_approval`, approvals with approve and revoke actions, `rejectedBy`, and request comments. This version has no public links, email invitations, email notifications, realtime event stream, attachments, folders, live co-editing, account deletion, ownership transfer or author removal, approval notifications beyond the inbox, an "awaiting my approval" inbox filter, review of tags, per-owner permissions, an audit log of ownership or policy changes, standalone revision-history API, SQL schema/migrations, or server implementation. Snapshot persistence is required for edit requests and their attribution; it does not expose general historical note access. The owner inbox is the notification mechanism, suitable for clients to poll. Four conveniences were added while drafting the contract and are part of this version: `GET /me`, caller permissions on note representations, the team `scope` filter, and `noteTitle` in inbox summaries.

The contract uses [OpenAPI 3.1.2](https://spec.openapis.org/oas/v3.1.2.html), [HTTP conditional requests](https://www.rfc-editor.org/rfc/rfc9110.html#section-13), [428 Precondition Required](https://www.rfc-editor.org/rfc/rfc6585.html#section-3), and [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457.html). Merge and diff behavior is informed by [Git three-way file merging](https://git-scm.com/docs/git-merge-file) and [Git diff formats](https://git-scm.com/docs/diff-format); this contract does not require Git repositories or a Git hosting integration.
