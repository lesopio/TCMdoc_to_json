#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from sqlite_store import connect_db, load_job_entries


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned or "untitled"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export per-entry JSON files from SQLite.")
    parser.add_argument("--db", default="data/parser.db", help="SQLite database path.")
    parser.add_argument("--job-id", help="Specific job id. Defaults to latest job.")
    parser.add_argument("--output-dir", default="data/exported_entries", help="Output directory.")
    return parser.parse_args()


def resolve_job_id(conn, provided: str | None) -> str:
    if provided:
        return provided
    row = conn.execute("SELECT id FROM jobs ORDER BY updated_at DESC LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("No jobs found in database")
    return row["id"]


def main() -> None:
    args = parse_args()
    conn = connect_db(Path(args.db))
    job_id = resolve_job_id(conn, args.job_id)
    entries = load_job_entries(conn, job_id)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        target = output_dir / f"{safe_filename(entry['term'])}.json"
        target.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")

    print(output_dir)


if __name__ == "__main__":
    main()
