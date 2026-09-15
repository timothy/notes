# Notes API

A REST backend for a note-taking service shared among several small teams, built with Contract-Driven Development (CDD) and a container-first approach. This repository holds the contract and its reference server: an OpenAPI 3.1.2 document, the normative design guide behind it, a checker that keeps the two honest, and under `server/` a FastAPI implementation of every operation that ships as a container image.

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

## Two design choices

Two decisions shape everything else here: Contract-Driven Development (CDD), where the contract is written, reviewed, and released before the code that implements it, and a container-first approach, where the container image is the unit of delivery.

### Contract-Driven Development (CDD)

In CDD the contract is the product and the code follows it. Here the contract is `openapi.yaml` together with [the design guide](docs/design-guide.md). They were written, reviewed, and released (1.0.0, then 2.0.0) before a line of server code existed, and they change only through their own process: the twelve-check validator, the Redocly lint, the oasdiff gate that fails a pull request on a client-breaking change, and a semantic version on the document. The server under `server/` implements the contract and is never allowed to bend it: request bodies are validated at runtime by the spec's own JSON Schemas, the typed models are generated from the document with a drift check, and the test client checks every response against the declared status, headers, media type, and schema.

What this buys:

- **Behaviour is reviewed as a document, not discovered in code.** Permissions, merge semantics, the error vocabulary, and the acceptance scenarios were argued over in prose and examples, where they are cheap to change, before they became expensive to change in a schema and a service.
- **Clients never wait for the server.** Prism serves the contract's examples as a mock, so client work started before a backend existed and continues while the server lands slice by slice.
- **The server cannot drift.** The spec validates requests at runtime, the models are generated from it, every test response is validated against it, and Schemathesis runs over all 47 operations. A response the contract does not declare fails the test that produced it.
- **Compatibility is enforced by a machine.** The `/v1` prefix is the compatibility line, `info.version` is semantic, and oasdiff fails any pull request that would break a correctly written client.
- **Examples do triple duty.** The same examples are served by the mock, validated by the checker against their schemas, and used as byte-exact fixtures in the server's tests; the three-way merge reproduces the spec's diffs character for character.
- **Documentation is the source, not a copy.** The API reference at https://hweean.com/notes/ is rendered from `openapi.yaml`, so it cannot go stale, and the acceptance table in the design guide doubles as the server's test plan.

The price: a change in behaviour touches the contract, the guide, the checker's tables, and the changelog before it touches code, and the server implements what the document says even where a shortcut would be easier.

### Container-first approach

In a container-first approach the server's deliverable is a container image, not a checkout. One image is built from the repository root (it carries `openapi.yaml`, because the contract is a runtime dependency), pinned by digest to its base images, run as an unprivileged user on a read-only filesystem, and started as a single uvicorn process. The same image applies migrations as a separate step. `compose.yaml` runs PostgreSQL, the migration, and the API with one command; a smoke script asserts the container's contract; and CI lints the Dockerfile, builds the image, scans it for fixable vulnerabilities, and runs that smoke test on every pull request.

What this buys:

- **One artifact from laptop to production.** The image that passes the smoke test in CI is the image that runs locally through compose and would run in a cluster. There is no "works on my machine" and no drift between environments.
- **The runtime is proven on every pull request.** The smoke test checks what a deployment would otherwise discover the hard way: the process runs as uid 10001 with no capabilities and no privilege escalation, the filesystem is read-only, the probes answer, readiness follows the database down and back up, the schema is at head and migrating again is a no-op, and SIGTERM produces a clean exit.
- **Security is the default posture.** Non-root, read-only, no capabilities, no shell entrypoint, no uv or test tooling or curl in the runtime, base images pinned by digest and moved by Dependabot, Debian security updates applied at build time, and a Trivy gate that fails the build on any fixable critical or high finding.
- **Operations are explicit.** Configuration comes from the environment, and a misconfigured container refuses to start instead of running on a stray SQLite file; migrations run once, before the rollout, never at process start; liveness and readiness are separate endpoints, so a database incident takes replicas out of rotation without restarting them.
- **Onboarding is one command.** `docker compose up --build --wait` gives a contributor the full stack, PostgreSQL included and on the same version CI tests against, with nothing installed but Docker.
- **Deploy anywhere that runs OCI images.** Kubernetes, Compose, or any other runtime. The constraints the image assumes are written down in [server/README.md](server/README.md) instead of living in someone's head.

The price: building needs Docker with BuildKit, the image is rebuilt when a base image moves, and the inner loop for tests stays on uv because it is faster than a container on macOS.

## Repository layout

| Path | Purpose |
| --- | --- |
| `openapi.yaml` | The contract and source of truth. Operation descriptions and schemas are normative. |
| `docs/design-guide.md` | The rules behind the contract: model, permissions, edit requests and merges, lifecycle, HTTP conventions, and acceptance scenarios. |
| `scripts/validate_contract.py` | Twelve static checks that keep the spec and the guide consistent with each other. |
| `tests/negative_cases.yaml` | Payloads that must fail or must pass schema validation; the server replays every request-schema fixture through the endpoint that uses it. |
| `requirements-dev.txt` | Dependencies for the checker. |
| `redocly.yaml` | Configuration for the optional Redocly lint. |
| `CHANGELOG.md` | Release notes for each contract version. |
| `LICENSE` | Apache License 2.0. |
| `.github/workflows/contract.yml` | GitHub Actions workflow that runs the checker and the Redocly lint on every push to `main` and every pull request, and fails pull requests on client-breaking changes found by oasdiff. |
| `.github/workflows/docs.yml` | GitHub Actions workflow that builds the API reference with Redocly on every pull request and publishes it to GitHub Pages from `main`. |
| `server/` | The reference server: a uv project (FastAPI, SQLAlchemy, Alembic). See [server/README.md](server/README.md). |
| `tasks/` | The approved server plan (`plan.md`) and its task checklist (`todo.md`). |
| `Dockerfile`, `.dockerignore` | The production image: non-root, read-only, one uvicorn process. The build context is the repository root because the server needs `openapi.yaml`. |
| `compose.yaml` | The local stack: PostgreSQL 17, the one-shot migration, then the API, with the hardening a deployment should use. |
| `server/scripts/smoke_image.sh` | Proves the container contract against a built image, locally and in CI. |
| `.github/workflows/server.yml` | GitHub Actions workflow that runs ruff, mypy, and the test suite on SQLite and on PostgreSQL. |
| `.github/workflows/image.yml` | GitHub Actions workflow that lints the Dockerfile, builds the image, scans it for fixable vulnerabilities, and runs the smoke test on every push to `main` and every pull request. |
| `.github/dependabot.yml` | Weekly updates for the digest-pinned base images in `Dockerfile` and `compose.yaml`. |

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

GitHub Actions runs both commands on every push to `main` and every pull request, and on pull requests also compares `openapi.yaml` with `main` using oasdiff (`.github/workflows/contract.yml`; see [Releases and versioning](#releases-and-versioning)).

The checks are static. Authorization, atomicity, and the merge algorithm are verified by the server's test suite, which is organised around the acceptance scenarios in section 6 of the design guide and validates every response against the contract; [server/README.md](server/README.md) explains how to run it.

## Running a mock

[Prism](https://github.com/stoplightio/prism) serves the contract's examples as a mock server, so client work does not wait for the server, which lands slice by slice:

```sh
npx --yes @stoplight/prism-cli@5.16.0 mock openapi.yaml --errors
```

- The mock listens on `http://127.0.0.1:4010` and serves routes without the `/v1` prefix, because Prism ignores server base paths. Point a client's base URL at `http://127.0.0.1:4010` where production would use `https://notes-api.example.com/v1`.
- Any `Authorization: Bearer` value is accepted; a request without one gets the contract's `401` example.
- Each response is the operation's first example. `Prefer: example=merged` selects a named example and `Prefer: code=412` selects another documented status.
- With `--errors`, an invalid body, a missing `If-Match`, or an unsupported content type is answered with the operation's `422` or `415` example instead of a logged warning. Prism reports every request-validation failure as `422`, even where the contract says `428`.
- The mock has no state. Creating a note returns the fixed example, and generated header values such as `Location` are placeholders.

## Running the server

With Docker installed and nothing else:

```sh
docker compose build
docker compose run --rm --no-deps -T api python -m notes_api.dev_issuer env > .env
docker compose up --wait
TOKEN=$(docker compose run --rm --no-deps -T api python -m notes_api.dev_issuer token --sub ada --name 'Ada Okafor')
curl -si -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/me
docker compose down -v
```

The first command builds the image. The second writes a gitignored `.env` with a development identity provider (the server refuses to start without one, and a deployment points it at a real provider instead). The third starts PostgreSQL, applies the schema, and starts the API on `127.0.0.1:8000`; then a minted token calls `GET /v1/me`, which provisions the caller on first contact. Only the contract's routes under `/v1` and the two probes `/healthz` and `/readyz` exist. The server implements every operation in the contract: the user directory (`/me`, `/users`), teams with memberships (`/teams`), notes (`/notes`: create, read, conditional update, trash and restore, list and search), shares (`/notes/{noteId}/shares`), comments (`/notes/{noteId}/comments`), edit requests (`/notes/{noteId}/edit-requests`, `/edit-requests`: submit, inspect, list, revise, withdraw, reject, preview, merge, approve, revoke approval), ownership (`/notes/{noteId}/owners`, `/notes/{noteId}/review-policy`: co-owners and the review policy), and request comments (`/edit-requests/{requestId}/comments`). Every operation runs in the Schemathesis conformance suite, every request-schema fixture in `tests/negative_cases.yaml` is replayed through its endpoint, and an audit keeps every acceptance row of the design guide covered by a test. [server/README.md](server/README.md) covers development on both databases, how the server enforces the contract (validation, the check ladder, locking, the test harness, the hook seam), the behaviour of each resource, and operations: configuration, authentication, migrations, probes, and deployment constraints.

## Releases and versioning

Each release is an annotated tag `v<info.version>` on `main` with an entry in [CHANGELOG.md](CHANGELOG.md), so `https://raw.githubusercontent.com/timothy/notes/v2.0.0/openapi.yaml` is the frozen 2.0.0 document. The rendered API reference for `main` is published at https://hweean.com/notes/ by `.github/workflows/docs.yml`.

The `/v1` path prefix is the compatibility line for clients and `info.version` is the document's semantic version; section 4 of the design guide ("Contract versioning") defines both. Pull requests fail when oasdiff finds a client-breaking change against `main`. A deliberate break ships under a new prefix with a major version bump and carries the `breaking-change` label, which turns the failure into a report.

## License

Apache License 2.0. See [LICENSE](LICENSE).
