#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"


def resolve_npm() -> str:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found in PATH")
    return npm


def main() -> int:
    npm = resolve_npm()
    print("Starting frontend on http://127.0.0.1:5173")
    return subprocess.call([npm, "run", "dev"], cwd=FRONTEND_DIR)


if __name__ == "__main__":
    raise SystemExit(main())
