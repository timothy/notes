"""Bootstrap preserves existing identities and never leaves partial credential files."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    scripts = tmp_path / "server/scripts"
    scripts.mkdir(parents=True)
    shutil.copy(Path(__file__).resolve().parents[1] / "scripts/bootstrap.sh", scripts / "bootstrap.sh")
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "if os.environ.get('FAIL_DOCKER'): sys.exit(1)\n"
        "if 'env' in sys.argv: print(\"OIDC_ISSUER='original'\\nNOTES_DEV_ISSUER_KEY='test-only'\")\n"
        "print('CURSOR_SIGNING_KEY=' + '01' * 32)\n"
    )
    docker.chmod(0o700)
    return tmp_path


def bootstrap(checkout: Path, *, fail: bool = False) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": str(checkout / "bin") + os.pathsep + os.environ.get("PATH", "")}
    if fail:
        env["FAIL_DOCKER"] = "1"
    return subprocess.run(
        ["bash", str(checkout / "server/scripts/bootstrap.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_bootstrap_is_private_and_idempotent(checkout: Path) -> None:
    assert bootstrap(checkout).returncode == 0
    env_file = checkout / ".env"
    original = env_file.read_bytes()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert bootstrap(checkout).returncode == 0
    assert env_file.read_bytes() == original
    assert not list(checkout.glob(".env.bootstrap.*"))


def test_existing_identity_gets_only_the_missing_cursor_key(checkout: Path) -> None:
    env_file = checkout / ".env"
    original = b"# User settings\nOIDC_ISSUER='custom'\nNOTES_DEV_ISSUER_KEY='keep-this'"
    env_file.write_bytes(original)
    assert bootstrap(checkout).returncode == 0
    assert env_file.read_bytes() == original + b"\nCURSOR_SIGNING_KEY=" + b"01" * 32 + b"\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


@pytest.mark.parametrize("existing", [False, True])
def test_failed_generation_does_not_modify_existing_credentials(checkout: Path, existing: bool) -> None:
    env_file = checkout / ".env"
    if existing:
        env_file.write_text("OIDC_ISSUER='keep-this'\n")
    assert bootstrap(checkout, fail=True).returncode != 0
    if existing:
        assert env_file.read_text() == "OIDC_ISSUER='keep-this'\n"
    else:
        assert not env_file.exists()
    assert not list(checkout.glob(".env.bootstrap.*"))
