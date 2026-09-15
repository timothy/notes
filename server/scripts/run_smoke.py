"""Launch the shell smoke assertions in an isolated Compose project, even on assertion failure."""

from __future__ import annotations

import subprocess

from isolated_stack import ROOT, IsolatedStack


def main() -> int:
    with IsolatedStack("smoke") as stack:
        print(f"smoke project: {stack.project}", flush=True)
        env = {
            **stack.env,
            "NOTES_API_TEST_PROJECT": stack.project,
            "NOTES_API_TEST_DIRECTORY": str(stack.directory),
        }
        return subprocess.call([str(ROOT / "server/scripts/smoke_image_checks.sh")], cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
