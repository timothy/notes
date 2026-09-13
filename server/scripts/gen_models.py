#!/usr/bin/env python3
"""Generate the typed models from the contract, or check that the committed file is current.

    uv run scripts/gen_models.py            # rewrite src/notes_api/generated/schemas.py
    uv run scripts/gen_models.py --check    # exit 1 with a diff when the committed file drifts

The generated models are a typed convenience for handlers. They are never the validator: the spec's JSON
Schemas are (see ``notes_api.contract``), because datamodel-code-generator cannot express ``if/then``,
``minProperties``, enums of arrays, or ordered ``uniqueItems``.
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = SERVER_DIR.parent / "openapi.yaml"
DEFAULT_OUTPUT = SERVER_DIR / "src" / "notes_api" / "generated" / "schemas.py"
GENERATOR_FLAGS = [
    "--input-file-type",
    "openapi",
    "--output-model-type",
    "pydantic_v2.BaseModel",
    "--target-python-version",
    "3.12",
    "--use-annotated",
    "--field-constraints",
    "--use-standard-collections",
    "--use-union-operator",
    "--strict-nullable",
    "--disable-timestamp",
    "--formatters",
    "ruff-format",
]


def generate(output: Path) -> None:
    """Run datamodel-code-generator from this interpreter's environment into ``output``.

    Generation always happens in a temporary directory and is then copied, so the formatter the generator
    runs never picks up a project configuration and the result is identical wherever it is written.
    """
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "schemas.py"
        command = [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            "--input",
            str(CONTRACT_PATH),
            "--output",
            str(fresh),
        ]
        subprocess.run([*command, *GENERATOR_FLAGS], check=True)
        output.write_bytes(fresh.read_bytes())


def check(output: Path) -> int:
    """Return 0 when ``output`` equals a fresh generation, else print a unified diff and return 1."""
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "schemas.py"
        generate(fresh)
        expected = fresh.read_text(encoding="utf-8")
    actual = output.read_text(encoding="utf-8") if output.exists() else ""
    if actual == expected:
        print(f"{output} is up to date with {CONTRACT_PATH.name}")
        return 0
    diff = difflib.unified_diff(
        actual.splitlines(keepends=True), expected.splitlines(keepends=True), str(output), "fresh generation"
    )
    sys.stdout.writelines(diff)
    print(f"\n{output} differs from a fresh generation; run scripts/gen_models.py", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="generated file path")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.output)
    generate(args.output)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
