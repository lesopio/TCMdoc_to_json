#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlite_store import connect_db, load_job_metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export token summary from SQLite.")
    parser.add_argument("--db", default="data/parser.db", help="SQLite database path.")
    parser.add_argument("--job-id", help="Specific job id. Defaults to latest job.")
    parser.add_argument("--output", default="data/token_summary.json", help="Output JSON path.")
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
    metrics = load_job_metrics(conn, job_id)
    payload = {"job_id": job_id, "token_summary": metrics}
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
