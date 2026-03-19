#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
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
    raw = input("Default concurrency [8]: ").strip()
    default_concurrency = raw or "8"
    backend_env = dict(os.environ)
    backend_env["DEFAULT_CONCURRENCY"] = default_concurrency
    frontend_env = dict(os.environ)
    frontend_env["VITE_DEFAULT_CONCURRENCY"] = default_concurrency

    print("Starting backend in a new window...")
    subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=ROOT,
        env=backend_env,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )

    time.sleep(1)

    print("Starting frontend in a new window...")
    subprocess.Popen(
        [npm, "run", "dev"],
        cwd=FRONTEND_DIR,
        env=frontend_env,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )

    print("Backend:  http://127.0.0.1:8000")
    print("Frontend: http://127.0.0.1:5173")
    print(f"Default concurrency: {default_concurrency}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
