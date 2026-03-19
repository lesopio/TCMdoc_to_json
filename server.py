#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from parser_core import ProcessingOptions, ProcessingResult, discover_input, process_document
from sqlite_store import connect_db, load_job_catalog_report, load_job_entries, load_job_metrics


ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
FRONTEND_DIST = ROOT / "frontend" / "dist"

app = Flask(__name__, static_folder=str(FRONTEND_DIST), static_url_path="")


@dataclass
class Job:
    id: str
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    snapshot: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    listeners: list[queue.Queue] = field(default_factory=list)
    result: ProcessingResult | None = None
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    pause_event: threading.Event = field(default_factory=threading.Event)


JOBS: dict[str, Job] = {}


def jobs_path(job_id: str) -> Path:
    path = JOBS_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def emit(job: Job, payload: dict[str, Any]) -> None:
    payload = {"timestamp": time.time(), **payload}
    with job.lock:
        job.updated_at = time.time()
        job.events.append(payload)
        job.snapshot.update(payload)
        for listener in list(job.listeners):
            listener.put(payload)


def build_job_summary(job: Job) -> dict[str, Any]:
    result_summary = None
    if job.result is not None:
        result_summary = {
            "entry_count": job.result.entry_count,
            "output_path": job.result.output_path,
            "database_path": job.result.database_path,
            "staging_path": job.result.staging_path,
            "review_path": job.result.review_path,
            "catalog_report_path": job.result.catalog_report_path,
            "metrics_summary": job.result.metrics_summary,
        }
    return {
        "id": job.id,
        "state": job.state,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "error": job.error,
        "snapshot": job.snapshot,
        "result": result_summary,
    }


def build_final_payload(job_id: str, db_path: Path) -> dict[str, Any]:
    conn = connect_db(db_path)
    row = conn.execute(
        "SELECT input_file, state, llm_enabled FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"Job not found in database: {job_id}")
    return {
        "meta": {
            "job_id": job_id,
            "input_file": row["input_file"],
            "state": row["state"],
            "llm_enabled": bool(row["llm_enabled"]),
            "entry_count": len(load_job_entries(conn, job_id)),
            "total_token_summary": load_job_metrics(conn, job_id),
            "catalog_report": load_job_catalog_report(conn, job_id),
        },
        "entries": load_job_entries(conn, job_id),
    }


def build_token_payload(job_id: str, db_path: Path) -> dict[str, Any]:
    conn = connect_db(db_path)
    return {"job_id": job_id, "token_summary": load_job_metrics(conn, job_id)}


def build_catalog_payload(job_id: str, db_path: Path) -> dict[str, Any]:
    conn = connect_db(db_path)
    return {"job_id": job_id, "catalog_report": load_job_catalog_report(conn, job_id)}


def run_job(job: Job, payload: dict[str, Any]) -> None:
    try:
        input_path = discover_input(payload.get("input"))
        job_dir = jobs_path(job.id)
        options = ProcessingOptions(
            input_path=input_path,
            output_path=job_dir / "output.json",
            staging_path=job_dir / "staging_rule_split.json",
            review_path=job_dir / "review_queue.json",
            catalog_report_path=job_dir / "catalog_report.json",
            job_state_path=job_dir / "job_state.json",
            entry_output_dir=job_dir / "entries",
            database_path=job_dir / "parser.db",
            job_id=job.id,
            env_file=ROOT / payload.get("envFile", ".env"),
            limit=payload.get("limit"),
            only_term=payload.get("onlyTerm"),
            skip_llm=bool(payload.get("skipLlm", False)),
            resume=bool(payload.get("resume", False)),
            sleep=float(payload.get("sleep", 0.0) or 0.0),
            concurrency=payload.get("concurrency") or int(os.getenv("DEFAULT_CONCURRENCY", "0") or 0) or None,
            pause_checker=job.pause_event.is_set,
            state_getter=lambda: job.state,
            output_dir=job_dir,
        )

        with job.lock:
            job.state = "running"

        def reporter(event: dict[str, Any]) -> None:
            emit(job, event)

        result = process_document(options, reporter=reporter)
        with job.lock:
            job.state = "completed"
            job.result = result
            job.snapshot = {
                **job.snapshot,
                "state": "completed",
                "metrics_summary": result.metrics_summary,
                "catalog_report": result.catalog_report,
                "last_llm_trace": result.last_llm_trace,
            }
    except Exception as exc:  # noqa: BLE001
        with job.lock:
            job.state = "failed"
            job.error = str(exc)
        emit(job, {"type": "job_failed", "state": "failed", "error": str(exc)})


@app.after_request
def add_cors_headers(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


@app.route("/api/health")
def health() -> Response:
    return jsonify({"ok": True})


@app.route("/api/jobs", methods=["POST", "OPTIONS"])
def create_job() -> Response:
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    payload = request.get_json(silent=True) or {}
    job = Job(id=uuid.uuid4().hex)
    JOBS[job.id] = job
    emit(job, {"type": "queued", "state": "queued"})
    thread = threading.Thread(target=run_job, args=(job, payload), daemon=True)
    thread.start()
    return jsonify({"jobId": job.id, "state": job.state})


@app.route("/api/jobs/<job_id>")
def get_job(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(build_job_summary(job))


@app.route("/api/jobs/<job_id>/pause", methods=["POST"])
def pause_job(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    with job.lock:
        if job.state not in {"running", "paused"}:
            return jsonify({"error": f"Job cannot be paused from state: {job.state}"}), 409
        job.pause_event.set()
        job.state = "paused"
    emit(job, {"type": "job_paused", "state": "paused"})
    return jsonify({"ok": True, "jobId": job_id, "state": "paused"})


@app.route("/api/jobs/<job_id>/resume", methods=["POST"])
def resume_job(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    with job.lock:
        if job.state != "paused":
            return jsonify({"error": f"Job cannot be resumed from state: {job.state}"}), 409
        job.pause_event.clear()
        job.state = "running"
    emit(job, {"type": "job_resumed", "state": "running"})
    return jsonify({"ok": True, "jobId": job_id, "state": "running"})


@app.route("/api/jobs/<job_id>/events")
def get_job_events(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404

    def stream() -> Any:
        listener: queue.Queue = queue.Queue()
        with job.lock:
            for event in job.events[-20:]:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            job.listeners.append(listener)
        try:
            while True:
                try:
                    event = listener.get(timeout=10)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("type") in {"job_completed", "job_failed"}:
                        break
                except queue.Empty:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            with job.lock:
                if listener in job.listeners:
                    job.listeners.remove(listener)

    return Response(stream(), mimetype="text/event-stream")


@app.route("/api/jobs/<job_id>/results")
def get_job_results(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    if job.result is None:
        return jsonify({"error": "Job result not ready"}), 409
    return jsonify(
        {
            "entry_count": job.result.entry_count,
            "review_queue": job.result.review_queue,
            "metrics_summary": job.result.metrics_summary,
            "last_llm_trace": job.result.last_llm_trace,
            "paths": {
                "output": job.result.output_path,
                "database": job.result.database_path,
                "staging": job.result.staging_path,
                "review": job.result.review_path,
                "catalog_report": job.result.catalog_report_path,
            },
        }
    )


@app.route("/api/jobs/<job_id>/catalog-report")
def get_catalog_report(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    if job.result is None:
        return jsonify({"error": "Catalog report not ready"}), 409
    return jsonify(job.result.catalog_report)


@app.route("/api/jobs/<job_id>/download/final-json")
def download_final_json(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None or job.result is None:
        return jsonify({"error": "Job result not ready"}), 409
    payload = build_final_payload(job_id, Path(job.result.database_path))
    export_dir = jobs_path(job_id) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / "final.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return send_file(target, as_attachment=True, download_name=f"{job_id}.final.json")


@app.route("/api/jobs/<job_id>/download/token-summary")
def download_token_summary(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None or job.result is None:
        return jsonify({"error": "Job result not ready"}), 409
    payload = build_token_payload(job_id, Path(job.result.database_path))
    export_dir = jobs_path(job_id) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / "token_summary.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return send_file(target, as_attachment=True, download_name=f"{job_id}.token_summary.json")


@app.route("/api/jobs/<job_id>/download/catalog-report")
def download_catalog_report(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None or job.result is None:
        return jsonify({"error": "Job result not ready"}), 409
    payload = build_catalog_payload(job_id, Path(job.result.database_path))
    export_dir = jobs_path(job_id) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / "catalog_report.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return send_file(target, as_attachment=True, download_name=f"{job_id}.catalog_report.json")


@app.route("/api/jobs/<job_id>/download/entries-zip")
def download_entries_zip(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if job is None or job.result is None:
        return jsonify({"error": "Job result not ready"}), 409
    entries_dir = Path(job.result.entry_output_dir)
    if not entries_dir.exists():
        return jsonify({"error": "Entry JSON directory not found"}), 404
    export_dir = jobs_path(job_id) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    archive_base = export_dir / "entries"
    archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=entries_dir)
    return send_file(archive_path, as_attachment=True, download_name=f"{job_id}.entries.zip")


@app.route("/")
def serve_index() -> Response:
    if FRONTEND_DIST.exists():
        return send_from_directory(FRONTEND_DIST, "index.html")
    return jsonify({"message": "Frontend not built yet. Run npm install && npm run build in frontend/."})


if __name__ == "__main__":
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)
