"""Disposable Compose stacks shared by image, PostgreSQL, and curl verification.

Only this module's unique project is ever removed. A temporary project directory also keeps Compose
from reading the checkout's .env; shell OIDC and Compose overrides cannot bleed into the test stack.
"""

from __future__ import annotations

import http.client
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

ROOT = Path(__file__).resolve().parents[2]


class IsolatedStack:
    def __init__(self, purpose: str, *, database_only: bool = False) -> None:
        self.project = f"notes-api-{purpose}-{uuid.uuid4().hex[:12]}"
        self.database_only = database_only
        self.image = os.environ.get("NOTES_API_IMAGE", "notes-api:dev")
        self._temp = tempfile.TemporaryDirectory(prefix=self.project + "-")
        self.directory = Path(self._temp.name)
        self.env_file = self.directory / ".env"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COMPOSE_", "OIDC_", "NOTES_DEV_")) and key != "CURSOR_SIGNING_KEY"
        }
        self.env.update(NOTES_API_IMAGE=self.image, NOTES_API_PORT="0", NOTES_API_DB_PORT="0")
        self.command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            str(self.directory),
            "--env-file",
            str(self.env_file),
            "-f",
            str(ROOT / "compose.yaml"),
        ]

    def __enter__(self) -> Self:
        try:
            raw = (
                ""
                if self.database_only
                else subprocess.check_output(
                    ["docker", "run", "--rm", self.image, "python", "-m", "notes_api.dev_issuer", "env"],
                    text=True,
                    env=self.env,
                )
            )
            self.env_file.touch(mode=0o600)
            self.env_file.write_text(raw)
            return self
        except BaseException:
            self._temp.cleanup()
            raise

    def __exit__(self, *exc: object) -> None:
        try:
            self.compose("down", "-v", "--remove-orphans")
        finally:
            self._temp.cleanup()

    def compose(self, *args: str, check: bool = True, input: str | None = None) -> str:
        result = subprocess.run(
            [*self.command, *args],
            cwd=ROOT,
            env=self.env,
            input=input,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            raise RuntimeError(f"Compose {args[0]} failed: {result.stderr}")
        return result.stdout

    def start(self) -> None:
        services = ["db"] if self.database_only else []
        self.compose("up", "--wait", "--wait-timeout", "180", "--no-build", *services)

    def address(self, service: str, port: int) -> str:
        return self.compose("port", service, str(port)).strip()

    @property
    def url(self) -> str:
        return "http://" + self.address("api", 8000)

    @property
    def database_url(self) -> str:
        return f"postgresql+psycopg://notes:notes@{self.address('db', 5432)}/notes"

    def token(self, subject: str, name: str) -> str:
        return self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "api",
            "python",
            "-m",
            "notes_api.dev_issuer",
            "token",
            "--sub",
            subject,
            "--name",
            name,
        ).strip()

    @contextmanager
    def replica(self) -> Iterator[str]:
        name = self.project + "-replica"
        try:
            self.compose("run", "--rm", "-d", "--no-deps", "--name", name, "-p", "127.0.0.1::8000", "api")
            address = subprocess.check_output(["docker", "port", name, "8000"], text=True).strip()
            url = "http://" + address
            for _ in range(100):
                try:
                    with urllib.request.urlopen(url + "/readyz", timeout=1):
                        break
                except (urllib.error.URLError, OSError, http.client.HTTPException):
                    time.sleep(0.2)
            else:
                raise RuntimeError("second API replica did not become ready")
            yield url
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
