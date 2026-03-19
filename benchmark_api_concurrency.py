#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

from parser_core import LLMTrace, OpenAICompatClient, load_env, load_llm_clients


BENCHMARK_PAYLOAD = json.dumps(
    {
        "task": "return_exact_json",
        "instructions": "Return a JSON object with keys ok, mode, reply. ok must be true. mode must be benchmark. reply must be pong.",
        "schema": {"ok": True, "mode": "benchmark", "reply": "pong"},
    },
    ensure_ascii=False,
)


def make_trace(client: OpenAICompatClient, label: str) -> LLMTrace:
    return LLMTrace(
        model=client.model,
        term=f"benchmark-{label}",
        pos="benchmark",
        raw_text_length=len(BENCHMARK_PAYLOAD),
        request_preview=BENCHMARK_PAYLOAD,
    )


def run_once(client: OpenAICompatClient, label: str) -> dict[str, Any]:
    trace = make_trace(client, label)
    started = time.perf_counter()
    try:
        result, trace = client.chat_json(BENCHMARK_PAYLOAD, trace)
        wall_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "wall_ms": wall_ms,
            "trace": asdict(trace),
            "result": result,
        }
    except Exception as exc:  # noqa: BLE001
        wall_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "wall_ms": wall_ms,
            "error": str(exc),
            "trace": asdict(trace),
        }


def summarize(level: int, results: list[dict[str, Any]]) -> dict[str, Any]:
    successes = [item for item in results if item["ok"]]
    failures = [item for item in results if not item["ok"]]
    wall_times = [item["wall_ms"] for item in results]
    prompt_tokens = [
        item["trace"]["metrics"].get("actual_prompt_tokens") or 0
        for item in successes
    ]
    completion_tokens = [
        item["trace"]["metrics"].get("actual_completion_tokens") or 0
        for item in successes
    ]
    total_tokens = [
        item["trace"]["metrics"].get("actual_total_tokens") or 0
        for item in successes
    ]
    duration_s = sum(wall_times) / 1000.0
    return {
        "concurrency": level,
        "requests": len(results),
        "successes": len(successes),
        "failures": len(failures),
        "success_rate": round(len(successes) / len(results), 4) if results else 0.0,
        "avg_wall_ms": round(statistics.mean(wall_times), 2) if wall_times else None,
        "p50_wall_ms": round(statistics.median(wall_times), 2) if wall_times else None,
        "max_wall_ms": max(wall_times) if wall_times else None,
        "aggregate_prompt_tokens": sum(prompt_tokens) or None,
        "aggregate_completion_tokens": sum(completion_tokens) or None,
        "aggregate_total_tokens": sum(total_tokens) or None,
        "avg_total_tokens": round(statistics.mean(total_tokens), 2) if total_tokens else None,
        "requests_per_sec_by_sum": round(len(results) / duration_s, 3) if duration_s > 0 else None,
        "token_per_sec_by_sum": round(sum(total_tokens) / duration_s, 3) if duration_s > 0 and total_tokens else None,
        "errors": [item.get("error", "") for item in failures][:5],
    }


def benchmark_level(level: int, total_requests: int, clients: list[OpenAICompatClient]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=level) as executor:
        futures = []
        for index in range(total_requests):
            client = clients[index % len(clients)]
            futures.append(executor.submit(run_once, client, f"c{level}-r{index + 1}"))
        for future in as_completed(futures):
            results.append(future.result())
    elapsed_s = time.perf_counter() - started
    summary = summarize(level, results)
    summary["batch_elapsed_s"] = round(elapsed_s, 3)
    summary["requests_per_sec_by_batch"] = round(total_requests / elapsed_s, 3) if elapsed_s > 0 else None
    total_tokens = summary.get("aggregate_total_tokens") or 0
    summary["token_per_sec_by_batch"] = round(total_tokens / elapsed_s, 3) if elapsed_s > 0 and total_tokens else None
    return {"summary": summary, "results": results}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark OpenAI-compatible API concurrency.")
    parser.add_argument("--env-file", action="append", default=[".env"], help="Environment file. Repeat to test multiple accounts.")
    parser.add_argument("--concurrency", default="1,2,4", help="Comma-separated concurrency levels.")
    parser.add_argument("--requests", type=int, default=6, help="Total requests per concurrency level.")
    parser.add_argument("--output", default="data/concurrency_benchmark.json", help="Output JSON path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_files = [Path(item) for item in args.env_file]
    clients: list[OpenAICompatClient] = []
    for env_file in env_files:
        env = load_env(env_file)
        env_clients = load_llm_clients(env)
        if not env_clients:
            raise RuntimeError(f"API env not enabled: {env_file}")
        clients.extend(env_clients)

    levels = [int(item.strip()) for item in args.concurrency.split(",") if item.strip()]
    payload: dict[str, Any] = {
        "env_files": [str(path) for path in env_files],
        "model": clients[0].model if clients else "",
        "base_url": clients[0].base_url if clients else "",
        "requests_per_level": args.requests,
        "levels": [],
    }

    print(f"Benchmarking model={payload['model']} levels={levels} requests_per_level={args.requests}")
    for level in levels:
        result = benchmark_level(level, args.requests, clients)
        payload["levels"].append(result["summary"])
        summary = result["summary"]
        print(
            f"[concurrency={level}] success={summary['successes']}/{summary['requests']} "
            f"batch_elapsed={summary['batch_elapsed_s']}s rps={summary['requests_per_sec_by_batch']} "
            f"token/s={summary['token_per_sec_by_batch']}"
        )
        if summary["errors"]:
            print("  errors:", summary["errors"])

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved benchmark report to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
