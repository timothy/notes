"""Actual curl requests with contract validation and a redacted, reproducible transcript."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from notes_api.contract import Contract

MISSING = object()
Response = tuple[Any, dict[str, str]]


def e(response: Response) -> str:
    return response[1]["etag"]


def b(response: Response) -> Any:
    return response[0]


class CurlSession:
    def __init__(self, base: str, output: Path, contract: Contract, tokens: dict[str, str]) -> None:
        self.base = base
        self.output = output
        self.contract = contract
        self.tokens = tokens
        self.records: list[dict[str, Any]] = []
        output.mkdir(parents=True, exist_ok=True)
        (output / "curl-transcript.md").write_text("# Container API verification\n\nTokens are redacted.\n")

    def call(
        self,
        label: str,
        method: str,
        path: str,
        who: str | None = "ada",
        *,
        data: Any = MISSING,
        etag: str | None = None,
        expected: int = 200,
        extra: list[str] | None = None,
        raw: str | None = None,
        media: str = "application/json",
    ) -> Response:
        index = len(self.records) + 1
        hp, bp = self.output / f"{index:03d}.headers", self.output / f"{index:03d}.body"
        args = ["curl", "-sS", "--max-time", "20", "-X", method, self.base + path]
        if who:
            args += ["-H", "Authorization: Bearer " + self.tokens.get(who, "not-a-token")]
        if etag is not None:
            args += ["-H", "If-Match: " + etag]
        if data is not MISSING or raw is not None:
            payload = raw if raw is not None else json.dumps(data, ensure_ascii=True, separators=(",", ":"))
            args += ["-H", "Content-Type: " + media, "--data-binary", payload]
        for header in extra or []:
            args += ["-H", header]
        args += ["-D", str(hp), "-o", str(bp), "-w", "%{http_code}"]
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(f"curl failed for {label}: {result.stderr}")
        status = int(result.stdout)
        headers_raw = hp.read_text()
        headers = {
            name.lower(): value
            for line in headers_raw.splitlines()
            if ": " in line
            for name, value in [line.split(": ", 1)]
        }
        body_raw = bp.read_text()
        body = json.loads(body_raw) if body_raw else None
        errors = []
        if status != expected:
            errors.append(f"expected {expected}, got {status}")
        for name in ("cache-control", "x-request-id"):
            if name not in headers:
                errors.append(f"missing {name}")
        if headers.get("cache-control") != "no-store":
            errors.append("missing no-store")
        operation = None
        for template, item in self.contract.document["paths"].items():
            pattern = re.sub(r"\{[^}]+\}", "[^/]+", "/v1" + template)
            if not re.fullmatch(pattern, path.split("?")[0]) or method.lower() not in item:
                continue
            op = item[method.lower()]
            operation = op["operationId"]
            response = op["responses"].get(str(status))
            if response is None:
                errors.append("undeclared response status")
                break
            response = self.resolve(response)
            for name, declaration in response.get("headers", {}).items():
                declaration = self.resolve(declaration)
                value = headers.get(name.lower())
                if value is None and declaration.get("required"):
                    errors.append(f"missing {name}")
                elif value is not None:
                    errors.extend(
                        str(error) for error in self.contract.validate_instance(declaration["schema"], value)
                    )
            content = response.get("content", {})
            if content:
                media_type = headers.get("content-type", "").split(";")[0]
                if media_type not in content:
                    errors.append("wrong content type")
                else:
                    errors.extend(
                        str(error)
                        for error in self.contract.validate_instance(content[media_type]["schema"], body)
                    )
            elif body_raw:
                errors.append("unexpected response body")
            break
        command = shlex.join(args)
        for persona, token in self.tokens.items():
            command = command.replace(token, "${" + persona.upper() + "_TOKEN}")
            header = "Authorization: Bearer ${" + persona.upper() + "_TOKEN}"
            command = command.replace(shlex.quote(header), '"' + header + '"')
        record = dict(
            number=index,
            label=label,
            method=method,
            path=path,
            status=status,
            operation=operation,
            command=command,
            headers=headers_raw,
            body=body,
            errors=errors,
        )
        self.records.append(record)
        (self.output / "results.json").write_text(json.dumps(self.records, indent=2))
        with (self.output / "curl-transcript.md").open("a") as transcript:
            transcript.write(
                f"\n## {index}. {label}\n\n```sh\n{command}\n```\n\n```http\n{headers_raw}\n{body_raw}\n```\n"
            )
        print(f"{index:03d} {status} {label}", flush=True)
        assert not errors, f"{label}: {errors}"
        return body, headers

    def resolve(self, value: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in value:
            ref = value["$ref"].split("/")
            value = self.contract.document["components"][ref[-2]][ref[-1]]
        return value
