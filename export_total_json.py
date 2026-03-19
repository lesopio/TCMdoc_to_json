#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlite_store import connect_db, load_job_catalog_report, load_job_entries, load_job_metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export final combined JSON from SQLite.")
    parser.add_argument("--db", default="data/parser.db", help="SQLite database path.")
    parser.add_argument("--job-id", help="Specific job id. Defaults to latest job.")
    parser.add_argument("--output", default="data/final.json", help="Final combined JSON path.")
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
    job_row = conn.execute(
        "SELECT input_file, state, llm_enabled, metrics_json, catalog_report_json FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not job_row:
        raise RuntimeError(f"Job not found: {job_id}")

    entries = load_job_entries(conn, job_id)
    metrics = load_job_metrics(conn, job_id)
    catalog_report = load_job_catalog_report(conn, job_id)
    payload = {
        "meta": {
            "job_id": job_id,
            "input_file": job_row["input_file"],
            "state": job_row["state"],
            "llm_enabled": bool(job_row["llm_enabled"]),
            "entry_count": len(entries),
            "total_token_summary": metrics,
            "catalog_report": catalog_report,
        },
        "entries": entries,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
