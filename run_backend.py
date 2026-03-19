#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    print("Starting backend on http://127.0.0.1:8000")
    return subprocess.call([sys.executable, "server.py"], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
