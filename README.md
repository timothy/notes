# Notes API

A contract-first REST backend for a note-taking service shared among several small teams. This repository holds the contract, not a running server: an OpenAPI 3.1.2 document, the normative design guide behind it, and a checker that keeps the two honest.

## Why nobody gets write access to someone else's notes

Many people now take notes with AI and LLM assistance, and the quality of those notes differs wildly from one person to the next. People who put in the effort end up with high-quality notes. People who do not let a lot of AI slop seep into theirs.

Shared write access would let the second group overwrite the first. Someone generating slop at volume could easily paste over notes that someone else worked hard to make good, and nothing in a "write" permission distinguishes a careful edit from a careless one.

So this service never lets an owner grant full write access to their notes. The most another person can get is `propose_edit`. That is a deliberate choice about ownership:

- **Ownership is enforced, not just recorded.** A note belongs to one person, and only that person can change what it says.
- **The owner is the sole quality gate for their own work.** Others propose. The owner reviews, then accepts, rejects, or modifies the proposal before anything lands.
- **Rejection is feedback.** A proposer whose change is turned down, ideally with a reason, has to think the problem through instead of blindly submitting whatever a model produced.

Ownership matters more now, not less, because the cost of producing plausible-looking text has decreased. The one thing that still costs effort is judgment, and this design keeps judgment where it belongs.

Version 2 will explore what it looks like to have multi-ownership of a single resource.

## How the contract enforces it

| Principle | Where it shows up in the contract |
| --- | --- |
| No write permission exists | Shares grant only `read`, `comment`, and `propose_edit`. Direct edits, sharing, trash and restore, preview, merge, and reject belong to the owner and cannot be delegated. Team admins do not inherit them. |
| Proposals never touch the live note | An edit request captures a server-side snapshot of the note as its base, stores the proposal and a diff, and leaves the note, its version, and its timestamps unchanged until the owner merges. |
| The owner sees exactly what would change | Preview runs a three-way merge of base, current, and proposed. Conflicts are reported, never silently resolved in either direction. |
| Accept, reject, or modify | Merge commits the clean candidate as-is, or the owner supplies complete `finalContent` carrying their own edits or conflict resolutions. Reject closes the request with an optional reason. |
| Attribution survives review | The merged content, resulting note version, merger, and time are recorded separately from the proposal, so the proposal stays visible exactly as it was submitted. |
| Revoking access does not erase history | Submitted proposals survive revocation. The owner can still review or merge them, and the record of what was proposed remains. |

Sections 2 and 3 of [the design guide](docs/design-guide.md) spell out the rules in full.

## Trade-offs this accepts

- Close collaborators pay a review step even for trivial fixes. Comments are the low-friction channel; edits always go through the owner.
- There is no live co-editing, and the owner is a bottleneck by design. When the owner is away, proposals wait in their inbox.
- Ownership never transfers in this version. A note stays with the person who wrote it.

These are the price of the guarantee above. I will think through what multi-ownership looks like for version 2 in hopes of mitigating these draw backs.

## Repository layout

| Path | Purpose |
| --- | --- |
| `openapi.yaml` | The contract and source of truth. Operation descriptions and schemas are normative. |
| `docs/design-guide.md` | The rules behind the contract: model, permissions, edit requests and merges, lifecycle, HTTP conventions, and acceptance scenarios. |
| `scripts/validate_contract.py` | Ten static checks that keep the spec and the guide consistent with each other. |
| `tests/negative_cases.yaml` | Payloads that must fail or must pass schema validation. |
| `requirements-dev.txt` | Dependencies for the checker. |
| `redocly.yaml` | Configuration for the optional Redocly lint. |

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

The checks are static. Authorization, atomicity, and the merge algorithm are verified by the acceptance scenarios in section 6 of the design guide once a server exists.
