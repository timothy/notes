# Notes API

A REST backend for a note-taking service shared among several small teams. Two design choices shape the project: **contract-based development** and **container-first delivery**. They make the intended behaviour and the runtime explicit, reviewable, and testable.

## Two design choices

The thinking behind both choices is simple: decide what the service promises, then make those promises verifiable in the artifact we run. The contract defines how clients can rely on the API. The container defines how the implementation is packaged and operated. Together, they favour clarity and correctness throughout development, testing, and deployment.

### Contract-based development

Contract-based development, called Contract-Driven Development (CDD) elsewhere in this repository, starts with the public behaviour. [`openapi.yaml`](openapi.yaml) and [the design guide](docs/design-guide.md) are the source of truth. The contract's first two releases preceded the server; the FastAPI implementation under `server/` follows that contract.

**Why this is a good fit:**

- **A shared definition of correct behaviour.** Clients, the server, tests, and reviewers work from the same operations, schemas, permissions, status codes, headers, and examples. Questions such as who may merge a proposal or what a stale ETag means have an explicit answer.
- **Design decisions are cheaper to review early.** Writing permissions, failure cases, lifecycle rules, and acceptance scenarios first exposes ambiguity before it spreads into handlers, database code, and client assumptions. The guide preserves the reasoning for future contributors.
- **Clients can develop independently.** Prism serves the contract's examples as a mock. Client work and integration planning can proceed before the corresponding server implementation is ready.
- **The contract participates in enforcement.** Request bodies are validated at runtime with the OpenAPI document's own JSON Schemas. Generated typed models help implementation, and a regeneration check detects drift in the committed models.
- **Conformance has concrete evidence.** The test client checks responses against declared statuses, headers, media types, and schemas. Schemathesis exercises all 47 operations, and request-schema fixtures are replayed through their endpoints. Acceptance and race tests cover rules that schemas alone cannot express.
- **Compatibility changes are visible.** Semantic document versions, the `/v1` compatibility line, and the oasdiff pull-request gate make detected client-breaking changes explicit. A deliberate break follows the documented versioning process.
- **Examples do several jobs.** Examples support review, mock responses, schema validation, and exact server fixtures, including the merge engine's diffs. Concrete inputs and outputs help expose disagreements that prose can hide.
- **Documentation stays close to implementation.** The [API reference](https://hweean.com/notes/) is rendered from `openapi.yaml`, and an audit connects the guide's acceptance rows to tests. Updating one source reduces the number of independently maintained descriptions.
- **Implementation can evolve behind a stable interface.** Clients depend on the published behaviour. Internal refactoring, database changes, or a future implementation can be judged against the same contract and acceptance scenarios.

**Clarity:** the contract makes the service's promises inspectable before anyone reads the implementation. **Correctness:** runtime validation and automated checks turn many of those promises into executable requirements. A schema-valid response can still contain a business-rule bug, so authorization, transaction, and merge tests remain essential.

**The tradeoff:** more design work happens up front, and a behaviour change may require coordinated updates to the contract, guide, validator expectations, fixtures, generated models, and changelog. Schema tooling also has limits; generated models cannot replace the contract's validator. That effort is worthwhile here because ownership, review, and concurrency rules are central to the product. Making them precise early reduces integration rework and gives later changes a clear standard to meet.

### Container-first approach

The container image is the unit of delivery. It is built from the repository root and includes the application, locked runtime dependencies, migrations, and `openapi.yaml`, which is a runtime dependency. Compose runs PostgreSQL, applies migrations with that same application image, and starts the API. CI builds, scans, smoke-tests, and exercises the image through the API.

**Why this is a good fit:**

- **A consistent runtime across environments.** The image packages Python, dependencies, application code, and the contract together. Running a tested image elsewhere reduces differences caused by host installations and makes failures easier to reproduce.
- **Simple onboarding.** `make up` builds the image, creates missing development credentials, starts PostgreSQL, applies migrations, and waits for readiness. Starting the service needs Docker with Compose, Make, and Bash; it does not require a host Python environment.
- **Dependency isolation and deliberate updates.** The multi-stage build installs from `server/uv.lock`, base images are pinned by digest, and Dependabot proposes base-image updates. Each project can carry its own runtime without conflicting with other local projects.
- **Faster repeat builds.** Dependency layers are separate from source and contract copies, and build caches are used locally and in CI. Routine edits can reuse the expensive dependency installation work.
- **Deployment assumptions are tested early.** The smoke test checks the process user, read-only filesystem, dropped capabilities, probes, database readiness transitions, migrations, and graceful shutdown. End-to-end curl workflows exercise the built service across all 47 operations.
- **A smaller, restricted runtime.** The runtime excludes uv and test tooling and runs as uid 10001. Compose adds a read-only root filesystem, drops capabilities, and prevents privilege escalation. CI scans for fixable high and critical vulnerabilities; these controls reduce exposure and catch known problems.
- **Explicit operations.** Required configuration comes from the environment, missing settings prevent startup, and migrations run as a separate step before the API. Separate liveness and readiness probes distinguish a running process from one ready to serve requests.
- **Clear release and recovery boundaries.** Packaging code, dependencies, and the contract together gives a deployment a specific artifact to identify and promote. Reusing a previously tested image can simplify application rollback when the database schema remains compatible. CI currently verifies images without publishing them to a registry.
- **Portability with documented requirements.** The image can be deployed to a compatible container runtime using the configuration, database, storage, and security requirements in [server/README.md](server/README.md#operations). The service's operating assumptions travel with the repository.
- **A fast development loop remains available.** Host-based uv checks and tests provide quick feedback, while Compose, PostgreSQL tests, smoke tests, and end-to-end tests verify the delivery environment. Container-first keeps the delivered artifact central while allowing practical development tools.

**Clarity:** the Dockerfile, Compose configuration, and operating guide show what the service needs to run. **Correctness:** image-level checks exercise packaging and lifecycle behaviour that an in-process test cannot establish. Containers reduce environment differences; host architecture, configuration, networking, and database state still matter.

**The tradeoff:** Docker and BuildKit add setup, build time, disk use, and maintenance. Security updates still require rebuilding and testing; the build applies current Debian updates, so separate builds are not guaranteed to be byte-identical. Deployments must supply configuration and preserve database compatibility through rollouts and rollbacks. This is worthwhile because the project tests its delivery assumptions continuously, and keeps host-based tests for quick feedback.

### Why the two choices work together

| Goal | What the two choices provide |
| --- | --- |
| **Clarity** | An explicit API promise and an explicit runtime. Reviewers can inspect both the intended behaviour and the conditions under which it runs. |
| **Correctness** | Checks at complementary levels: contract validation, acceptance and concurrency tests, tests on both databases, and smoke and end-to-end checks against the image. |
| **Good tradeoffs** | Up-front specification work and build maintenance buy repeatable checks, less integration guesswork, easier onboarding, and more controlled change. Fast local tests keep routine development practical. |

The contract travels inside the image, so the delivered server validates requests against the document packaged with it. The aim is to make mistakes easier to detect and changes easier to reason about. [AGENTS.md](AGENTS.md) turns these choices into working guidance for coding agents.

## What I'd add with more time

With more time, I would experiment with gRPC/protobuf internals and explore extracting the document and review workflow into a microservice for agentic systems. These are future directions, building on the same contract-based and container-first principles.

### Experiment with gRPC/protobuf internals

I would try gRPC with Protocol Buffers for internal service-to-service communication. I would define those interfaces in `.proto` contracts and evaluate generated types, payload size, latency, debugging, and schema evolution against the current approach. I would measure that tradeoff before committing to the architecture.

### An agentic zero-trust framework

Independent review is a useful foundation for agentic workflows. I would extend the API into an agentic communication and documentation system where contributions go through a review process before becoming accepted documents:

- **Specialized review agents.** Agents trained specifically for review would verify each proposed document or revision before it is committed, checking it against the task's requirements and supporting evidence.
- **Gatekeeper verification.** Gatekeeper LLMs, or other automated checks, would double-check agentic output and return actionable feedback before a contribution is accepted. A contributor could revise its proposal and submit it for review again.
- **Agents with complementary strengths.** Agents stronger in different areas would cross-check one another, aiming to catch mistakes a single agent might miss and reduce errors in the accepted result.

The zero-trust principle I would apply is that an agent's output begins as an untrusted proposal. Each agent would have a verified identity and narrowly scoped permissions; producing a document would not itself grant authority to accept it. Review findings would feed the ownership and approval rules, and an authorized actor would commit an accepted change. Proposals, feedback, approvals, and the final decision would remain attributable and inspectable.

Architecturally, I would investigate making this workflow a microservice that plugs into a larger agentic system. It would own documents, proposals, review state, and acceptance rules, while contributors and reviewers could participate as separate services. That boundary would let other workflows reuse the same review process. I would evaluate review quality, latency, and model cost to decide where additional reviewers earn their place.

### Extend contract-first development to events

To support that microservice architecture, I would add an AsyncAPI document alongside the OpenAPI contract. OpenAPI would describe the HTTP operations, and AsyncAPI would describe the event-driven interactions: for example, a proposal being submitted, review feedback becoming available, or a contribution being accepted or rejected.

I would define message schemas, producers and consumers, correlation identifiers, and compatibility rules before implementing those interactions. Delivery behaviour would also need explicit rules for retries, duplicate messages, and ordering, backed by integration tests. This would extend the contract-first approach to an event-driven microservice architecture, giving independently running agents a shared description of how to communicate and advance a contribution through review.

-------------------

## Current design: Why nobody gets write access to someone else's notes

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
| `AGENTS.md` | Project guidance for coding agents: design principles, implementation conventions, and verification commands. |
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
| `Makefile` | Common development and verification commands; `make help` lists targets. |
| `server/scripts/e2e.py` | Isolated, contract-checked curl workflows covering all 47 operations and audit regressions. |
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

**Startup prerequisites:** Docker Engine with Compose 2.24 or newer, Make, and Bash. Docker must be running. The examples also use curl.

From a fresh clone, run:

```sh
make up
TOKEN=$(make -s token)
curl -si -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/me
make down
```

`make up` builds the shared image, safely bootstraps a gitignored `.env`, starts PostgreSQL, applies migrations, and waits for API readiness. Bootstrap preserves existing identity credentials and adds a missing cursor key. `make down` preserves the database volume. Set `NOTES_API_PORT` and `NOTES_API_DB_PORT` if ports 8000 and 5432 are busy.

The complete sequence without Make is:

```sh
docker compose build
server/scripts/bootstrap.sh
docker compose up --wait --no-build
TOKEN=$(docker compose run --rm --no-deps -T api python -m notes_api.dev_issuer token --sub ada --name 'Ada Okafor')
curl -si -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/me
docker compose down
```

The development issuer provisions each distinct token subject on first contact. A deployment uses a real identity provider and supplies `CURSOR_SIGNING_KEY`: 32 random bytes encoded as 64 hexadecimal characters. All replicas share the key. Cursors expire 24 hours after issuance; key rotation invalidates existing cursors, and clients restart pagination after `400 invalid_cursor`. See the [configuration and rollout guidance](server/README.md#configuration).

### Development commands

**Testing prerequisites:** uv and Python 3.12; PostgreSQL, smoke, and end-to-end verification also need Docker/Compose. Smoke and end-to-end verification need curl and Bash. Node.js/npm is needed only for the separate Redocly documentation lint.

| Target | Purpose |
| --- | --- |
| `make help` | List targets and prerequisites. |
| `make build` | Build the image shared by the API and migrations. |
| `make bootstrap` | Build and atomically create missing development settings with private file permissions. |
| `make up` / `make down` | Bootstrap and start with readiness checks / stop while preserving data. |
| `make logs` | Follow application logs. |
| `make token SUB=ada NAME="Ada Okafor"` | Mint a development token (these are the defaults). |
| `make check` | Locked dependency setup, non-mutating format check, lint, typing, generated-model drift, and contract checks. |
| `make test` | SQLite suite; ignores an inherited PostgreSQL test URL. |
| `make test-postgres` | Suite against a new disposable PostgreSQL stack. |
| `make smoke` | Build and verify the production image in an isolated stack. |
| `make e2e` | Build and run the curl workflows against an isolated stack. |
| `make verify` | Run checks, SQLite, PostgreSQL, smoke, and end-to-end verification in order. |

PostgreSQL, smoke, and end-to-end runners each use a unique Compose project, temporary environment file, and automatically allocated localhost ports. They remove only their own resources. Curl transcripts redact tokens and are saved outside the repository; the runner prints the artifact directory.

Only the contract's routes under `/v1` and the two probes `/healthz` and `/readyz` exist. The server implements every operation in the contract: the user directory (`/me`, `/users`), teams with memberships (`/teams`), notes (`/notes`: create, read, conditional update, trash and restore, list and search), shares (`/notes/{noteId}/shares`), comments (`/notes/{noteId}/comments`), edit requests (`/notes/{noteId}/edit-requests`, `/edit-requests`: submit, inspect, list, revise, withdraw, reject, preview, merge, approve, revoke approval), ownership (`/notes/{noteId}/owners`, `/notes/{noteId}/review-policy`: co-owners and the review policy), and request comments (`/edit-requests/{requestId}/comments`). Every operation runs in the Schemathesis conformance suite, every request-schema fixture in `tests/negative_cases.yaml` is replayed through its endpoint, and an audit keeps every acceptance row of the design guide covered by a test. [server/README.md](server/README.md) covers development on both databases, how the server enforces the contract (validation, the check ladder, locking, the test harness, the hook seam), the behaviour of each resource, and operations: configuration, authentication, migrations, probes, and deployment constraints.

## Releases and versioning

Each release is an annotated tag `v<info.version>` on `main` with an entry in [CHANGELOG.md](CHANGELOG.md), so `https://raw.githubusercontent.com/timothy/notes/v2.0.0/openapi.yaml` is the frozen 2.0.0 document. The rendered API reference for `main` is published at https://hweean.com/notes/ by `.github/workflows/docs.yml`.

The `/v1` path prefix is the compatibility line for clients and `info.version` is the document's semantic version; section 4 of the design guide ("Contract versioning") defines both. Pull requests fail when oasdiff finds a client-breaking change against `main`. A deliberate break ships under a new prefix with a major version bump and carries the `breaking-change` label, which turns the failure into a report.

## License

Apache License 2.0. See [LICENSE](LICENSE).
