#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY,
          input_file TEXT NOT NULL,
          state TEXT NOT NULL,
          llm_enabled INTEGER NOT NULL DEFAULT 0,
          only_term TEXT,
          limit_count INTEGER,
          skip_llm INTEGER NOT NULL DEFAULT 0,
          resume_enabled INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          metrics_json TEXT NOT NULL DEFAULT '{}',
          catalog_report_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS catalog_terms (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL,
          term TEXT NOT NULL,
          normalized_term TEXT NOT NULL,
          FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_catalog_terms_job_id ON catalog_terms(job_id);
        CREATE INDEX IF NOT EXISTS idx_catalog_terms_norm ON catalog_terms(job_id, normalized_term);

        CREATE TABLE IF NOT EXISTS entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL,
          term TEXT NOT NULL,
          normalized_term TEXT NOT NULL,
          pinyin TEXT NOT NULL DEFAULT '',
          aliases_json TEXT NOT NULL DEFAULT '[]',
          raw_header TEXT NOT NULL DEFAULT '',
          raw_text TEXT NOT NULL DEFAULT '',
          entry_json TEXT NOT NULL DEFAULT '{}',
          output_path TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'completed',
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          UNIQUE(job_id, normalized_term),
          FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_entries_job_id ON entries(job_id);
        CREATE INDEX IF NOT EXISTS idx_entries_norm ON entries(job_id, normalized_term);

        CREATE TABLE IF NOT EXISTS senses (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entry_id INTEGER NOT NULL,
          pos TEXT NOT NULL,
          definitions_json TEXT NOT NULL DEFAULT '[]',
          raw_text TEXT NOT NULL DEFAULT '',
          llm_used INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(entry_id) REFERENCES entries(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_senses_entry_id ON senses(entry_id);

        CREATE TABLE IF NOT EXISTS examples (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sense_id INTEGER NOT NULL,
          source TEXT NOT NULL DEFAULT '',
          text TEXT NOT NULL,
          FOREIGN KEY(sense_id) REFERENCES senses(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_examples_sense_id ON examples(sense_id);

        CREATE TABLE IF NOT EXISTS review_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL,
          term TEXT NOT NULL,
          pos TEXT NOT NULL,
          stage TEXT NOT NULL,
          reason TEXT NOT NULL,
          raw_text TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL,
          FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_review_items_job_id ON review_items(job_id);

        CREATE TABLE IF NOT EXISTS llm_traces (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL,
          term TEXT NOT NULL,
          pos TEXT NOT NULL,
          model TEXT NOT NULL DEFAULT '',
          raw_text_length INTEGER NOT NULL DEFAULT 0,
          request_preview TEXT NOT NULL DEFAULT '',
          response_preview TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '',
          retries INTEGER NOT NULL DEFAULT 0,
          metrics_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL,
          FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_llm_traces_job_id ON llm_traces(job_id);
        """
    )
    conn.commit()


def upsert_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    input_file: str,
    state: str,
    llm_enabled: bool,
    only_term: str | None,
    limit_count: int | None,
    skip_llm: bool,
    resume_enabled: bool,
    metrics_json: dict[str, Any],
    catalog_report_json: dict[str, Any],
) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO jobs (
          id, input_file, state, llm_enabled, only_term, limit_count, skip_llm, resume_enabled,
          created_at, updated_at, metrics_json, catalog_report_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          input_file=excluded.input_file,
          state=excluded.state,
          llm_enabled=excluded.llm_enabled,
          only_term=excluded.only_term,
          limit_count=excluded.limit_count,
          skip_llm=excluded.skip_llm,
          resume_enabled=excluded.resume_enabled,
          updated_at=excluded.updated_at,
          metrics_json=excluded.metrics_json,
          catalog_report_json=excluded.catalog_report_json
        """,
        (
            job_id,
            input_file,
            state,
            1 if llm_enabled else 0,
            only_term,
            limit_count,
            1 if skip_llm else 0,
            1 if resume_enabled else 0,
            now,
            now,
            json.dumps(metrics_json, ensure_ascii=False),
            json.dumps(catalog_report_json, ensure_ascii=False),
        ),
    )
    conn.commit()


def replace_catalog_terms(conn: sqlite3.Connection, job_id: str, catalog_terms: list[tuple[str, str]]) -> None:
    conn.execute("DELETE FROM catalog_terms WHERE job_id = ?", (job_id,))
    conn.executemany(
        "INSERT INTO catalog_terms (job_id, term, normalized_term) VALUES (?, ?, ?)",
        [(job_id, term, normalized) for term, normalized in catalog_terms],
    )
    conn.commit()


def replace_review_items(conn: sqlite3.Connection, job_id: str, items: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM review_items WHERE job_id = ?", (job_id,))
    now = time.time()
    conn.executemany(
        """
        INSERT INTO review_items (job_id, term, pos, stage, reason, raw_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                job_id,
                item.get("term", ""),
                item.get("pos", ""),
                item.get("stage", ""),
                item.get("reason", ""),
                item.get("raw_text", ""),
                now,
            )
            for item in items
        ],
    )
    conn.commit()


def replace_entry(conn: sqlite3.Connection, job_id: str, entry: dict[str, Any], output_path: str) -> None:
    now = time.time()
    cursor = conn.execute(
        """
        INSERT INTO entries (
          job_id, term, normalized_term, pinyin, aliases_json, raw_header, raw_text, entry_json, output_path, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
        ON CONFLICT(job_id, normalized_term) DO UPDATE SET
          term=excluded.term,
          pinyin=excluded.pinyin,
          aliases_json=excluded.aliases_json,
          raw_header=excluded.raw_header,
          raw_text=excluded.raw_text,
          entry_json=excluded.entry_json,
          output_path=excluded.output_path,
          status='completed',
          updated_at=excluded.updated_at
        """,
        (
            job_id,
            entry.get("term", ""),
            entry.get("normalized_term", ""),
            entry.get("pinyin", ""),
            json.dumps(entry.get("aliases", []), ensure_ascii=False),
            entry.get("raw_header", ""),
            entry.get("raw_text", ""),
            json.dumps(entry, ensure_ascii=False),
            output_path,
            now,
            now,
        ),
    )
    if cursor.lastrowid:
        entry_id = cursor.lastrowid
    else:
        entry_id = conn.execute(
            "SELECT id FROM entries WHERE job_id = ? AND normalized_term = ?",
            (job_id, entry.get("normalized_term", "")),
        ).fetchone()[0]

    conn.execute("DELETE FROM senses WHERE entry_id = ?", (entry_id,))
    for sense in entry.get("senses", []):
        sense_cur = conn.execute(
            """
            INSERT INTO senses (entry_id, pos, definitions_json, raw_text, llm_used)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                sense.get("pos", ""),
                json.dumps(sense.get("definitions", []), ensure_ascii=False),
                sense.get("raw_text", ""),
                1 if sense.get("llm_used") else 0,
            ),
        )
        sense_id = sense_cur.lastrowid
        conn.executemany(
            "INSERT INTO examples (sense_id, source, text) VALUES (?, ?, ?)",
            [
                (sense_id, example.get("source", ""), example.get("text", ""))
                for example in sense.get("examples", [])
            ],
        )
    conn.commit()


def insert_llm_trace(conn: sqlite3.Connection, job_id: str, trace: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO llm_traces (
          job_id, term, pos, model, raw_text_length, request_preview, response_preview, error, retries, metrics_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            trace.get("term", ""),
            trace.get("pos", ""),
            trace.get("model", ""),
            trace.get("raw_text_length", 0),
            trace.get("request_preview", ""),
            trace.get("response_preview", ""),
            trace.get("error", ""),
            trace.get("retries", 0),
            json.dumps(trace.get("metrics", {}), ensure_ascii=False),
            time.time(),
        ),
    )
    conn.commit()


def load_job_entries(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT entry_json FROM entries WHERE job_id = ? ORDER BY term COLLATE NOCASE",
        (job_id,),
    ).fetchall()
    return [json.loads(row["entry_json"]) for row in rows]


def load_job_metrics(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT metrics_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return json.loads(row["metrics_json"]) if row and row["metrics_json"] else {}


def load_job_catalog_report(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT catalog_report_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return json.loads(row["catalog_report_json"]) if row and row["catalog_report_json"] else {}


def load_job_review_items(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT term, pos, stage, reason, raw_text
        FROM review_items
        WHERE job_id = ?
        ORDER BY id
        """,
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]
