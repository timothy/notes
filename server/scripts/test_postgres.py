"""Run pytest against a disposable PostgreSQL database, ignoring ambient test database URLs."""

from __future__ import annotations

import os
import subprocess
import sys

from isolated_stack import ROOT, IsolatedStack


def main() -> int:
    with IsolatedStack("postgres", database_only=True) as stack:
        stack.start()
        env = {**os.environ, "NOTES_API_TEST_DATABASE_URL": stack.database_url}
        return subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=ROOT / "server", env=env)


if __name__ == "__main__":
    raise SystemExit(main())
