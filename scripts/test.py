"""Run tests with an isolated temporary directory inside the checkout."""

from pathlib import Path
import subprocess
import sys
from uuid import uuid4


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    artifacts = root / "artifacts"
    artifacts.mkdir(exist_ok=True)
    target = artifacts / f"pytest-{uuid4().hex}"
    raise SystemExit(
        subprocess.call(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                "--basetemp",
                str(target),
                *sys.argv[1:],
            ],
            cwd=root,
        )
    )
