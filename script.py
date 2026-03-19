#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from parser_core import ProcessingOptions, discover_input, process_document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse 黄帝内经大词典 TXT into structured JSON.")
    parser.add_argument("--input", help="Input TXT path. Defaults to the only *.txt in cwd.")
    parser.add_argument("--output", default="data/output.json", help="Final structured JSON path.")
    parser.add_argument("--staging", default="data/staging_rule_split.json", help="Rule-based staging JSON path.")
    parser.add_argument("--review", default="data/review_queue.json", help="Review queue JSON path.")
    parser.add_argument("--catalog-report", default="data/catalog_report.json", help="Catalog audit JSON path.")
    parser.add_argument("--job-state", default="data/job_state.json", help="Job state snapshot JSON path.")
    parser.add_argument("--entry-output-dir", default="data/entries", help="Per-entry JSON output directory.")
    parser.add_argument("--database", default="data/parser.db", help="SQLite database path.")
    parser.add_argument("--env-file", default=".env", help="Env file path.")
    parser.add_argument("--limit", type=int, help="Only process the first N entries after filtering.")
    parser.add_argument("--only-term", help="Only process a single term.")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM refinement and use rule-based output.")
    parser.add_argument("--resume", action="store_true", help="Resume from existing output.json entries by term.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep between LLM requests.")
    parser.add_argument("--concurrency", type=int, help="LLM worker concurrency. Defaults to env or auto.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = discover_input(args.input)
    result = process_document(
        ProcessingOptions(
            input_path=input_path,
            output_path=Path(args.output),
            staging_path=Path(args.staging),
            review_path=Path(args.review),
            catalog_report_path=Path(args.catalog_report),
            job_state_path=Path(args.job_state),
            entry_output_dir=Path(args.entry_output_dir),
            database_path=Path(args.database),
            env_file=Path(args.env_file),
            limit=args.limit,
            only_term=args.only_term,
            skip_llm=args.skip_llm,
            resume=args.resume,
            sleep=args.sleep,
            concurrency=args.concurrency,
        )
    )

    print(f"Parsed entries: {result.entry_count}")
    print(f"Skipped by resume: {result.skipped_by_resume}")
    print(f"LLM enabled: {result.llm_enabled}")
    print(f"Output JSON: {result.output_path}")
    print(f"Entry JSON dir: {result.entry_output_dir}")
    print(f"SQLite DB: {result.database_path}")
    print(f"Staging JSON: {result.staging_path}")
    print(f"Review JSON: {result.review_path}")
    print(f"Catalog report JSON: {result.catalog_report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
