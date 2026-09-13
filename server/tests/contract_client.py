"""A test client that checks every response against the contract.

Each call is matched back to the route template that served it, the template is looked up in
``openapi.yaml``, and the response must be a declared status with the declared headers, media type,
and body schema. A path that matches no route must produce the contract's 404 Problem. Any violation
raises ``ContractViolation`` so the test fails at the request that broke the contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Match

from notes_api.contract import Contract
from tests.support import Persona

API_PREFIX = "/v1"


class ContractViolation(AssertionError):
    pass


class ContractClient:
    def __init__(self, app: FastAPI, *, auth: Persona | None = None) -> None:
        self.app = app
        self.contract: Contract = app.state.contract
        self.default_auth = auth
        self._client = TestClient(app, raise_server_exceptions=False)

    def request(
        self,
        method: str,
        path: str,
        *,
        auth: Persona | Literal[False] | None = None,
        json: Any = None,
        content: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
        if_match: str | None = None,
    ) -> httpx.Response:
        sent_headers = dict(headers or {})
        persona = self.default_auth if auth is None else (None if auth is False else auth)
        if persona is not None:
            sent_headers.update(persona.headers)
        if if_match is not None:
            sent_headers["If-Match"] = if_match
        response: httpx.Response = self._client.request(
            method, path, json=json, content=content, headers=sent_headers, params=params
        )
        self.check(method, path, response)
        return response

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)

    # -- validation -------------------------------------------------------------------------------

    def check(self, method: str, path: str, response: httpx.Response) -> None:
        method = method.upper()
        template = self._route_template(method, path)
        where = f"{method} {path} -> {response.status_code}"
        if template is None:
            self._expect(
                response.status_code == 404, f"{where}: matched no route, so it must be the 404 Problem"
            )
            self._check_body(self.contract.schemas["Problem"], response, where)
            return
        contract_path = template.removeprefix(API_PREFIX)
        operation = self.contract.document["paths"].get(contract_path, {}).get(method.lower())
        if operation is None:
            raise ContractViolation(f"{method} {template} is not in the contract")
        declared = operation["responses"].get(str(response.status_code))
        self._expect(
            declared is not None, f"{where}: undeclared status {response.status_code} for {contract_path}"
        )
        spec = self._resolve(declared, "responses")
        self._check_headers(spec.get("headers", {}), response, where)
        content = spec.get("content")
        if not content:
            self._expect(
                response.content == b"", f"{where}: a response without declared content must have no body"
            )
            return
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        self._expect(
            media_type in content, f"{where}: media type {media_type!r} is not declared ({sorted(content)})"
        )
        self._check_body(content[media_type]["schema"], response, where)

    def _route_template(self, method: str, path: str) -> str | None:
        scope = {
            "type": "http",
            "method": method,
            "path": path.split("?", 1)[0],
            "root_path": "",
            "path_params": {},
        }
        for route in self.app.routes:
            if isinstance(route, APIRoute):
                match, _ = route.matches(scope)
                if match == Match.FULL:
                    return route.path
        return None

    def _check_headers(self, headers: Mapping[str, Any], response: httpx.Response, where: str) -> None:
        for name, definition in headers.items():
            spec = self._resolve(definition, "headers")
            value = response.headers.get(name)
            if value is None:
                self._expect(not spec.get("required", False), f"{where}: required header {name} is missing")
                continue
            errors = self.contract.validate_instance(spec.get("schema", {}), value)
            self._expect(not errors, f"{where}: header {name}={value!r} violates its schema: {errors}")

    def _check_body(self, schema: Mapping[str, Any], response: httpx.Response, where: str) -> None:
        try:
            body = response.json()
        except ValueError as exc:
            raise ContractViolation(f"{where}: body is not JSON: {exc}") from exc
        errors = self.contract.validate_instance(schema, body)
        self._expect(not errors, f"{where}: body violates the schema: {errors}")

    def _resolve(self, node: Mapping[str, Any], component_kind: str) -> Mapping[str, Any]:
        if "$ref" in node:
            resolved: Mapping[str, Any] = self.contract.document["components"][component_kind][
                node["$ref"].split("/")[-1]
            ]
            return resolved
        return node

    @staticmethod
    def _expect(condition: bool, message: str) -> None:
        if not condition:
            raise ContractViolation(message)
