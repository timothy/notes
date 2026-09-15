"""Reproduce all 47 operations and the audit edges with curl against disposable containers."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from curl_client import CurlSession, Response, b, e
from isolated_stack import ROOT, IsolatedStack

from notes_api.contract import Contract


def exercise(api: CurlSession) -> Response:
    call, tokens = api.call, api.tokens
    call("Liveness", "GET", "/healthz", None)
    call("Readiness", "GET", "/readyz", None)
    call("Missing bearer token", "GET", "/v1/me", None, expected=401)
    call("Invalid bearer token", "GET", "/v1/me", "invalid", expected=401)
    users = {p: b(call("Provision " + p, "GET", "/v1/me", p))["id"] for p in tokens}
    call("Directory", "GET", "/v1/users")
    call("Get public user", "GET", "/v1/users/" + users["ben"])
    page = call("Directory page one", "GET", "/v1/users?limit=2")
    cursor = b(page)["nextCursor"]
    call("Directory page two", "GET", "/v1/users?" + urlencode({"limit": 2, "cursor": cursor}))
    call(
        "Cursor bound to caller",
        "GET",
        "/v1/users?" + urlencode({"limit": 2, "cursor": cursor}),
        "ben",
        expected=400,
    )
    call("Invalid cursor", "GET", "/v1/notes?cursor=garbage", expected=400)
    call("Limit validation", "GET", "/v1/notes?limit=101", expected=422)
    call("Path validation", "GET", "/v1/notes/not-a-uuid", expected=422)
    call("Unknown route", "GET", "/v1/nope", None, expected=404)
    call("Undeclared method", "DELETE", "/v1/me", expected=404)
    call("No local Swagger UI", "GET", "/docs", None, expected=404)

    team = call("Create team", "POST", "/v1/teams", data={"name": "E2E Team"}, expected=201)
    tid = b(team)["id"]
    tp = "/v1/teams/" + tid
    call("List teams", "GET", "/v1/teams?scope=mine")
    call("Read team", "GET", tp)
    call("Rename team", "PATCH", tp, data={"name": "E2E Review Team"})
    call("List initial admin", "GET", tp + "/members")
    call("Add member default role", "POST", tp + "/members", data={"userId": users["ben"]}, expected=201)
    call("Duplicate membership", "POST", tp + "/members", data={"userId": users["ben"]}, expected=409)
    call("Promote member", "PATCH", tp + "/members/" + users["ben"], data={"role": "admin"})
    call("Demote member", "PATCH", tp + "/members/" + users["ben"], data={"role": "member"})
    call("Last admin cannot leave", "DELETE", tp + "/members/" + users["ada"], expected=409)

    call("Missing required title", "POST", "/v1/notes", data={}, expected=422)
    call("Unknown JSON property", "POST", "/v1/notes", data={"title": "Invalid", "extra": True}, expected=422)
    call("Malformed JSON", "POST", "/v1/notes", raw='{"title":', expected=400)
    call(
        "Unsupported media type",
        "POST",
        "/v1/notes",
        raw='{"title":"Invalid"}',
        media="text/plain",
        expected=415,
    )
    note = call(
        "Create note",
        "POST",
        "/v1/notes",
        data={"title": "E2E Release", "body": "First line\nSecond line\n", "tags": ["release", "e2e"]},
        expected=201,
    )
    nid = b(note)["id"]
    np = "/v1/notes/" + nid
    call("Read note", "GET", np)
    call("Hidden note", "GET", np, "dan", expected=404)
    call("Missing If-Match", "PATCH", np, data={"title": "Changed"}, expected=428)
    call("Weak If-Match", "PATCH", np, data={"title": "Changed"}, etag="W/" + e(note), expected=400)
    call("Stale If-Match", "PATCH", np, data={"title": "Changed"}, etag='"stale"', expected=412)
    note = call("Update single-owner note", "PATCH", np, data={"title": "E2E Release v2"}, etag=e(note))
    no_op = call("No-op keeps ETag", "PATCH", np, data={"title": "E2E Release v2"}, etag=e(note))
    assert e(no_op) == e(note)
    call("Search and AND tags", "GET", "/v1/notes?q=release&tag=release&tag=e2e")
    call("Duplicate query value", "GET", "/v1/notes?q=release&q=e2e", expected=422)
    call("Duplicate tag value", "GET", "/v1/notes?tag=e2e&tag=e2e", expected=422)
    share = call(
        "Share with collaborator",
        "POST",
        np + "/shares",
        data={"recipient": {"type": "user", "id": users["ben"]}, "permissions": ["comment", "propose_edit"]},
        expected=201,
    )
    sp = np + "/shares/" + b(share)["id"]
    call("Read shared note", "GET", np, "ben")
    call(
        "Recipient cannot edit directly",
        "PATCH",
        np,
        "ben",
        data={"body": "Bypass"},
        etag=e(note),
        expected=403,
    )
    call("List shares", "GET", np + "/shares")
    call("Read share", "GET", sp)
    call(
        "Duplicate share",
        "POST",
        np + "/shares",
        data={"recipient": {"type": "user", "id": users["ben"]}, "permissions": ["read"]},
        expected=409,
    )
    call("Downgrade share", "PATCH", sp, data={"permissions": ["read"]})
    call("Read-only cannot comment", "POST", np + "/comments", "ben", data={"body": "Blocked"}, expected=403)
    call("Restore share permissions", "PATCH", sp, data={"permissions": ["comment", "propose_edit"]})
    comment = call(
        "Collaborator comments",
        "POST",
        np + "/comments",
        "ben",
        data={"body": "Review this note"},
        expected=201,
    )
    cp = np + "/comments/" + b(comment)["id"]
    call("List comments", "GET", np + "/comments")
    call("Get comment", "GET", cp)
    call(
        "Owner cannot rewrite collaborator comment",
        "PATCH",
        cp,
        data={"body": "Overwrite"},
        etag=e(comment),
        expected=403,
    )
    comment = call(
        "Author updates comment", "PATCH", cp, "ben", data={"body": "Reviewed this note"}, etag=e(comment)
    )

    request = call(
        "Collaborator proposes edit",
        "POST",
        np + "/edit-requests",
        "ben",
        data={
            "baseNoteETag": e(note),
            "proposedContent": {"title": "E2E Release v2", "body": "First line\nReviewed second line\n"},
            "explanation": "Clarify line two",
        },
        expected=201,
    )
    rp = "/v1/edit-requests/" + b(request)["id"]
    call("List note requests", "GET", np + "/edit-requests")
    call("Incoming inbox", "GET", "/v1/edit-requests?view=incoming")
    call("Outgoing inbox", "GET", "/v1/edit-requests?view=outgoing", "ben")
    call("Read edit request", "GET", rp)
    request = call(
        "Revise request explanation",
        "PATCH",
        rp,
        "ben",
        data={"explanation": "Ready for review"},
        etag=e(request),
    )
    rc = call(
        "Proposer request comment",
        "POST",
        rp + "/comments",
        "ben",
        data={"body": "Please approve"},
        expected=201,
    )
    rcp = rp + "/comments/" + b(rc)["id"]
    call("List request comments", "GET", rp + "/comments")
    call("Read request comment", "GET", rcp)
    rc = call(
        "Edit request comment", "PATCH", rcp, "ben", data={"body": "Please review and approve"}, etag=e(rc)
    )
    call("Delete request comment", "DELETE", rcp, "ben", etag=e(rc), expected=204)
    preview = call("Clean merge preview", "POST", rp + "/preview")
    assert b(preview)["canMerge"] is True
    call("Proposer cannot preview", "POST", rp + "/preview", "ben", expected=403)
    note = call("Add co-owner", "POST", np + "/owners", data={"userId": users["cara"]}, etag=e(note))
    call(
        "Protected direct edit blocked",
        "PATCH",
        np,
        data={"body": "Bypass review"},
        etag=e(note),
        expected=409,
    )
    note = call(
        "Set peer approval policy",
        "PATCH",
        np + "/review-policy",
        data={"mode": "peer_approval", "requiredApprovals": 1},
        etag=e(note),
    )
    call(
        "Non-owner proposal needs two owners",
        "POST",
        rp + "/merge",
        data={"expectedNoteETag": e(note)},
        etag=e(request),
        expected=409,
    )
    call(
        "Peer approval forbids finalContent",
        "POST",
        rp + "/preview",
        data={"finalContent": {"title": "E2E Release v2", "body": "Custom"}},
        expected=422,
    )
    request = call("Co-owner approves", "POST", rp + "/approve", "cara", etag=e(request))
    request = call("Co-owner revokes approval", "POST", rp + "/revoke-approval", "cara", etag=e(request))
    request = call("Co-owner approves again", "POST", rp + "/approve", "cara", etag=e(request))
    merged = call(
        "Second owner merges", "POST", rp + "/merge", data={"expectedNoteETag": e(note)}, etag=e(request)
    )
    assert b(merged)["note"]["body"] == "First line\nReviewed second line\n"
    note = call("Read committed note", "GET", np)
    request = call("Read frozen merged request", "GET", rp)
    call(
        "Closed request immutable",
        "PATCH",
        rp,
        "ben",
        data={"explanation": "Late"},
        etag=e(request),
        expected=409,
    )
    note = call("Remove co-owner", "DELETE", np + "/owners/" + users["cara"], etag=e(note))

    def new_request(title: str) -> Response:
        return call(
            title,
            "POST",
            np + "/edit-requests",
            "ben",
            data={"baseNoteETag": e(note), "proposedContent": {"title": title, "body": b(note)["body"]}},
            expected=201,
        )

    reject = new_request("Request to reject")
    reject_path = "/v1/edit-requests/" + b(reject)["id"]
    call(
        "Owner rejects with reason",
        "POST",
        reject_path + "/reject",
        data={"reason": "Keep current title"},
        etag=e(reject),
    )
    withdraw = new_request("Request to withdraw")
    call(
        "Proposer withdraws",
        "POST",
        "/v1/edit-requests/" + b(withdraw)["id"] + "/withdraw",
        "ben",
        etag=e(withdraw),
    )
    conflict = new_request("Proposed conflicting title")
    fp = "/v1/edit-requests/" + b(conflict)["id"]
    note = call(
        "Concurrent owner title edit", "PATCH", np, data={"title": "Owner conflicting title"}, etag=e(note)
    )
    call("Conflicting merge preview", "POST", fp + "/preview")
    call(
        "Conflict prevents automatic merge",
        "POST",
        fp + "/merge",
        data={"expectedNoteETag": e(note)},
        etag=e(conflict),
        expected=409,
    )
    final_content = {"title": "Resolved title", "body": b(note)["body"]}
    call("Owner previews conflict resolution", "POST", fp + "/preview", data={"finalContent": final_content})
    call(
        "Owner merges conflict resolution",
        "POST",
        fp + "/merge",
        data={"expectedNoteETag": e(note), "finalContent": final_content},
        etag=e(conflict),
    )
    note = call("Read resolved note", "GET", np)

    call("Delete note comment", "DELETE", cp, "ben", etag=e(comment), expected=204)
    call("Deleted comment is gone", "GET", cp, expected=404)
    call("Revoke direct share", "DELETE", sp, expected=204)
    call("Share revocation hides note", "GET", np, "ben", expected=404)
    call(
        "Share with team",
        "POST",
        np + "/shares",
        data={"recipient": {"type": "team", "id": tid}, "permissions": ["read"]},
        expected=201,
    )
    call("Team membership grants read", "GET", np, "ben")
    call("Remove team member", "DELETE", tp + "/members/" + users["ben"], expected=204)
    call("Membership removal revokes read", "GET", np, "ben", expected=404)
    call("Delete team", "DELETE", tp, expected=204)
    call("Deleted team is gone", "GET", tp, expected=404)
    call(
        "Share before trash",
        "POST",
        np + "/shares",
        data={"recipient": {"type": "user", "id": users["ben"]}, "permissions": ["read"]},
        expected=201,
    )
    trashed = call("Trash note", "DELETE", np, etag=e(note), expected=204)
    call("Repeated trash is idempotent", "DELETE", np, etag=e(trashed), expected=204)
    call("Owner sees trash", "GET", np)
    call("Former recipient cannot see trash", "GET", np, "ben", expected=404)
    call("List own trash", "GET", "/v1/notes?scope=mine&state=trashed")
    call("Trashed note cannot be edited", "PATCH", np, data={"title": "No"}, etag=e(trashed), expected=409)
    note = call("Restore note", "POST", np + "/restore", etag=e(trashed))
    call("Restore retains no shares", "GET", np + "/shares")
    call("Restore does not restore access", "GET", np, "ben", expected=404)
    call("Already active restore", "POST", np + "/restore", etag=e(note), expected=409)
    return note


def audit_edges(api: CurlSession, note: Response, stack: IsolatedStack) -> None:
    call = api.call
    np = "/v1/notes/" + b(note)["id"]
    call(
        "Lone surrogate is a field error",
        "POST",
        "/v1/notes",
        raw='{"title":"\\ud800","body":"AUDIT-PRIVATE-BODY"}',
        expected=422,
    )
    call(
        "Invalid property name is a field error",
        "POST",
        "/v1/notes",
        raw='{"title":"valid","\\ud800":"invalid property"}',
        expected=422,
    )
    call("NUL remains rejected", "POST", "/v1/notes", data={"title": "NUL", "body": "a\u0000b"}, expected=422)
    call(
        "Valid Unicode is preserved",
        "POST",
        "/v1/notes",
        data={"title": "Café 🚀", "body": "AUDIT-PRIVATE-BODY"},
        expected=201,
    )
    page = call("Signed cursor first page", "GET", "/v1/users?limit=2")
    cursor = b(page)["nextCursor"]
    parts = cursor.split(".")
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    payload["k"][0] = 0
    altered = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    forged = f"{parts[0]}.{altered}.{parts[2]}"
    call(
        "Tampered signed cursor",
        "GET",
        "/v1/users?" + urlencode({"limit": 2, "cursor": forged}),
        expected=400,
    )
    call(
        "Legacy unsigned cursor",
        "GET",
        "/v1/users?" + urlencode({"limit": 2, "cursor": altered}),
        expected=400,
    )
    with stack.replica() as replica_url:
        original = api.base
        try:
            api.base = replica_url
            call(
                "Another replica accepts the cursor",
                "GET",
                "/v1/users?" + urlencode({"limit": 2, "cursor": cursor}),
            )
        finally:
            api.base = original
    for additional in [e(note), '"stale"']:
        call(
            "Repeated If-Match is rejected",
            "PATCH",
            np,
            data={"title": "Rejected"},
            etag=e(note),
            extra=["If-Match: " + additional],
            expected=400,
        )
    assert b(call("Rejected headers leave note unchanged", "GET", np)) == b(note)
    call("Trailing slash is 404", "GET", "/v1/notes/", None, expected=404)
    call(
        "Search text stays private",
        "GET",
        "/v1/notes?q=AUDIT-PRIVATE-SEARCH",
        extra=["X-Request-Id: audit-log-verification"],
    )
    logs = stack.compose("logs", "--no-color", "api")
    for secret in ["AUDIT-PRIVATE-SEARCH", "AUDIT-PRIVATE-BODY", *api.tokens.values()]:
        assert secret not in logs, "private input reached container stdout"
    assert "audit-log-verification" in logs
    assert "HTTP/1.1" not in logs, "Uvicorn access logging is enabled"
    for line in logs.splitlines():
        if " | {" in line:
            entry = json.loads(line.split(" | ", 1)[1])
            assert entry["path"] not in {"/readyz", "/healthz"}
    (api.output / "container.log").write_text(logs)


def smoke_isolation(api: CurlSession, stack: IsolatedStack, note: Response) -> None:
    """A control stack survives both successful and failed smoke runs with hostile ambient overrides."""
    before_env = stack.env_file.read_bytes()
    before_id = stack.compose("ps", "-q", "api")
    volume = stack.project + "_pgdata"
    before_volume = subprocess.check_output(["docker", "volume", "inspect", volume], text=True)
    env = {
        **os.environ,
        "NOTES_API_IMAGE": stack.image,
        "COMPOSE_PROJECT_NAME": stack.project,
        "NOTES_API_PORT": stack.address("api", 8000).rsplit(":", 1)[1],
        "OIDC_ISSUER": "https://ambient.invalid",
        "CURSOR_SIGNING_KEY": "invalid-ambient-key",
    }
    real_curl = shutil.which("curl")
    assert real_curl is not None
    for fail in [False, True]:
        print(
            f"Verifying control stack survives {'failed' if fail else 'successful'} smoke cleanup", flush=True
        )
        with tempfile.TemporaryDirectory(prefix="notes-api-curl-shim-") as directory:
            if fail:
                shim = Path(directory) / "curl"
                shim.write_text(
                    '#!/bin/sh\ncase "$*" in *"/healthz"*) exit 22;; esac\nexec '
                    + shlex.quote(real_curl)
                    + ' "$@"\n'
                )
                shim.chmod(0o700)
            result = subprocess.run(
                [str(ROOT / "server/scripts/smoke_image.sh")],
                cwd=ROOT,
                env={**env, "PATH": directory + os.pathsep + env.get("PATH", "")},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        redacted = re.sub(
            r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<redacted-jwt>", result.stdout
        )
        (api.output / f"smoke-{'failure' if fail else 'success'}.log").write_text(redacted)
        assert (result.returncode != 0) == fail, redacted
        assert stack.env_file.read_bytes() == before_env
        assert stack.compose("ps", "-q", "api") == before_id
        assert subprocess.check_output(["docker", "volume", "inspect", volume], text=True) == before_volume
        current = api.call("Control note survives smoke cleanup", "GET", "/v1/notes/" + b(note)["id"])
        assert b(current) == b(note)
        match = re.search(r"smoke project: (notes-api-smoke-[a-f0-9]+)", result.stdout)
        assert match is not None, "smoke runner did not identify its disposable project"
        for resource in ["container", "volume"]:
            leftovers = subprocess.check_output(
                ["docker", resource, "ls", "-q", "--filter", "label=com.docker.compose.project=" + match[1]],
                text=True,
            )
            assert not leftovers.strip(), f"smoke left {resource} resources behind"


def main() -> None:
    configured_output = os.environ.get("NOTES_API_ARTIFACT_DIR")
    output = Path(configured_output) if configured_output else Path(tempfile.mkdtemp(prefix="notes-api-e2e-"))
    with IsolatedStack("e2e") as stack:
        stack.start()
        tokens = {
            subject: stack.token(subject, name)
            for subject, name in [
                ("ada", "Ada Okafor"),
                ("ben", "Ben Ortiz"),
                ("cara", "Cara Nakamura"),
                ("dan", "Dan Whitfield"),
            ]
        }
        api = CurlSession(stack.url, output, Contract.load(ROOT / "openapi.yaml"), tokens)
        note = exercise(api)
        audit_edges(api, note, stack)
        smoke_isolation(api, stack, note)
        operations = {
            op["operationId"]
            for item in api.contract.document["paths"].values()
            for method, op in item.items()
            if method in {"get", "post", "patch", "delete"}
        }
        covered = {r["operation"] for r in api.records if r["operation"] and 200 <= r["status"] < 300}
        assert covered == operations, f"unexercised operations: {operations - covered}"
        summary = {
            "requests": len(api.records),
            "successful_operations": len(covered),
            "failures": 0,
            "smoke_isolation": "success and failure preserve control data",
        }
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary))
    print(f"Redacted verification artifacts: {output}")


if __name__ == "__main__":
    main()
