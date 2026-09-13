"""The committed generated models match the contract, and the check script detects drift."""

import subprocess
import sys
from pathlib import Path

from notes_api.generated import schemas

SERVER_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SERVER_DIR / "scripts" / "gen_models.py"
COMMITTED = SERVER_DIR / "src" / "notes_api" / "generated" / "schemas.py"


def run_check(output: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT), "--check", "--output", str(output)]
    return subprocess.run(command, capture_output=True, text=True, cwd=SERVER_DIR)


def test_committed_models_match_a_fresh_generation() -> None:
    result = run_check(COMMITTED)
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_fails_when_the_committed_file_drifts(tmp_path: Path) -> None:
    drifted = tmp_path / "schemas.py"
    drifted.write_text(COMMITTED.read_text(encoding="utf-8") + "\n# hand edit\n", encoding="utf-8")
    result = run_check(drifted)
    assert result.returncode == 1
    assert "hand edit" in result.stdout


def test_generated_request_models_forbid_unknown_fields() -> None:
    assert schemas.CreateNote.model_config.get("extra") == "forbid"
    assert schemas.Content.model_config.get("extra") == "forbid"
