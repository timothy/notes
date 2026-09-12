# Notes API

A contract-first REST backend for a note-taking service shared among several small teams. This repository holds the contract, not a running server: an OpenAPI 3.1.2 document, the normative design guide behind it, and a checker that keeps the two honest.

## Why nobody gets write access to someone else's notes

Many people now take notes with AI and LLM assistance, and the quality of those notes differs wildly from one person to the next. People who put in the effort end up with high-quality notes. People who do not let a lot of AI slop seep into theirs.

Shared write access would let the second group overwrite the first. Someone generating slop at volume could easily paste over notes that someone else worked hard to make good, and nothing in a "write" permission distinguishes a careful edit from a careless one.

So this service never lets an owner grant full write access to their notes. The most another person can get is `propose_edit`. That is a deliberate choice about ownership:

- **Ownership is enforced, not just recorded.** A note belongs to its owners, and only they can change what it says. Version 2 lets the author add co-owners; the moment a note has two owners it becomes protected, and even its owners' edits go through review.
- **Owners are the quality gate.** Others propose. An owner reviews, then accepts, rejects, or modifies the proposal before anything lands. On a protected note the review policy decides whether one owner may merge alone (`self_merge`) or peers must approve first (`peer_approval`).
- **Rejection is feedback.** A proposer whose change is turned down, ideally with a reason, has to think the problem through instead of blindly submitting whatever a model produced.

Ownership matters more now, not less, because the cost of producing plausible-looking text has decreased. The one thing that still costs effort is judgment, and this design keeps judgment where it belongs.

Version 2 adds multi-ownership without weakening the guarantee: no share ever grants write, and on a peer-approval note no change lands without at least two distinct owners taking part.

## How the contract enforces it

| Principle | Where it shows up in the contract |
| --- | --- |
| No write permission exists | Shares grant only `read`, `comment`, and `propose_edit`. Direct edits, sharing, trash and restore, preview, approve, merge, and reject belong to owners and cannot be delegated. Only the author adds or removes owners or sets the review policy. Team admins inherit nothing. |
| Protected notes have no back door | With two or more owners, `PATCH /notes/{noteId}` refuses title and body changes (`direct_edit_not_allowed`). Owners propose like everyone else, see the diff, and merge only when the policy is satisfied. |
| Peer approval means two people | Under `peer_approval` a request needs the configured approvals from owners other than the proposer, and a merger counts as one. A non-owner's proposal always needs two distinct owners, so a single co-owner cannot launder edits through a collaborator. |
| Proposals never touch the live note | An edit request captures a server-side snapshot of the note as its base, stores the proposal and a diff, and leaves the note, its version, and its timestamps unchanged until the owner merges. |
| The owner sees exactly what would change | Preview runs a three-way merge of base, current, and proposed. Conflicts are reported, never silently resolved in either direction. |
| Accept, reject, or modify | Merge commits the clean candidate as-is, or the owner supplies complete `finalContent` carrying their own edits or conflict resolutions. Reject closes the request with an optional reason. |
| Attribution survives review | The merged content, resulting note version, merger, and time are recorded separately from the proposal, so the proposal stays visible exactly as it was submitted. Approvals are frozen on the closed request, and a rejection records who rejected it. |
| Revoking access does not erase history | Submitted proposals survive revocation. The owner can still review or merge them, and the record of what was proposed remains. |

Sections 2 and 3 of [the design guide](docs/design-guide.md) spell out the rules in full.

## Trade-offs this accepts

- Close collaborators pay a review step even for trivial fixes. Comments are the low-friction channel; edits always go through the owner.
- There is no live co-editing. A single-owner note's owner is a bottleneck by design; co-ownership spreads that load but adds a review step for the owners themselves.
- Ownership never transfers. The author stays a permanent owner and administers the owner list and policy, so peer approval guards against careless or unilateral edits, not against a hostile author.

These are the price of the guarantee above.

## Repository layout

| Path | Purpose |
| --- | --- |
| `openapi.yaml` | The contract and source of truth. Operation descriptions and schemas are normative. |
| `docs/design-guide.md` | The rules behind the contract: model, permissions, edit requests and merges, lifecycle, HTTP conventions, and acceptance scenarios. |
| `scripts/validate_contract.py` | Twelve static checks that keep the spec and the guide consistent with each other. |
| `tests/negative_cases.yaml` | Payloads that must fail or must pass schema validation. |
| `requirements-dev.txt` | Dependencies for the checker. |
| `redocly.yaml` | Configuration for the optional Redocly lint. |
| `.github/workflows/contract.yml` | GitHub Actions workflow that runs the checker and the Redocly lint on every push to `main` and every pull request. |

## Running the checks

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/validate_contract.py
```

Optional second opinion:

```sh
npx @redocly/cli lint openapi.yaml
```

GitHub Actions runs both commands on every push to `main` and every pull request (`.github/workflows/contract.yml`).

The checks are static. Authorization, atomicity, and the merge algorithm are verified by the acceptance scenarios in section 6 of the design guide once a server exists.
