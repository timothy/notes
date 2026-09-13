"""The package declares which contract version it implements, and it must match the document."""

from pathlib import Path

import yaml

import notes_api

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_contract_version_matches_openapi_document() -> None:
    spec = yaml.safe_load((REPO_ROOT / "openapi.yaml").read_text())
    assert spec["info"]["version"] == notes_api.CONTRACT_VERSION
