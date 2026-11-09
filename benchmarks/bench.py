"""In-process micro-benchmarks for DocMind (no network, no database, no LLM).

Everything measured here runs inside a single Python process. The script sets
safe dummy environment variables *before* importing application modules and
never calls a real provider, PostgreSQL or Redis. Real exported environment
variables keep precedence for validation but are never printed.

Run from the repository root::

    python -m benchmarks.bench --iterations 2000

The direct form ``python benchmarks/bench.py`` also works because the script
prepends the repository root to ``sys.path``.

Honesty note: these are micro-benchmarks on one machine, not production SLAs.
RAG latency (embeddings, LLM streaming, pgvector search) is *not* measured here
because it needs live infrastructure; see ``benchmarks/README.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks._env import configure_environment, isolated_cwd  # noqa: E402

configure_environment()

with isolated_cwd():
    from src.eval import (  # noqa: E402
        _score_case,
        aggregate,
        build_report,
        file_sha256,
        load_dataset,
        percentile,
    )
    from src.metrics import (  # noqa: E402
        CHAT_TTFT,
        INGESTION_DURATION,
        RETRIEVAL_DURATION,
        MetricsMiddleware,
        snapshot,
    )
    from src.services.chunker import TextChunker  # noqa: E402

_DATASET_PATH = ROOT / "eval" / "golden_dataset.json"
_CORPUS_DIR = ROOT / "eval" / "corpus"
_DEFAULT_JSON = ROOT / "benchmarks" / "results" / "latest.json"
_DEFAULT_MD = ROOT / "benchmarks" / "results" / "latest.md"
_STAT_KEYS = ("mean", "p50", "p95", "p99", "min", "max", "ops_per_sec")
_META: dict[str, Any] = {"mode": "benchmark", "dataset": "eval/golden_dataset.json"}


def _summarize(durations: list[float], elapsed: float) -> dict[str, float | int]:
    """Summarise per-operation durations (already scaled to the report unit)."""
    count = len(durations)
    return {
        "samples": count,
        "elapsed_seconds": elapsed,
        "mean": statistics.fmean(durations) if durations else 0.0,
        "p50": percentile(durations, 50),
        "p95": percentile(durations, 95),
        "p99": percentile(durations, 99),
        "min": min(durations) if durations else 0.0,
        "max": max(durations) if durations else 0.0,
        "ops_per_sec": (count / elapsed) if elapsed > 0 else 0.0,
    }


def _pack_rounds(rounds: list[dict[str, float | int]]) -> dict[str, Any]:
    """Report every repeat plus the median across repeats."""
    median = {
        key: statistics.median(float(round_stats[key]) for round_stats in rounds)
        for key in _STAT_KEYS
    }
    return {"repeats": len(rounds), "rounds": rounds, "median": median}


def _measure_sync(
    fn: Callable[[], Any],
    *,
    iterations: int,
    warmup: int,
    repeats: int,
    scale: float,
) -> dict[str, Any]:
    """Time ``fn`` ``iterations`` times per repeat after ``warmup`` calls."""
    rounds: list[dict[str, float | int]] = []
    for _ in range(repeats):
        for _ in range(warmup):
            fn()
        durations: list[float] = []
        started = time.perf_counter()
        for _ in range(iterations):
            tick = time.perf_counter()
            fn()
            durations.append((time.perf_counter() - tick) * scale)
        elapsed = time.perf_counter() - started
        rounds.append(_summarize(durations, elapsed))
    return _pack_rounds(rounds)


async def _measure_async(
    fn: Callable[[], Awaitable[None]],
    *,
    iterations: int,
    warmup: int,
    repeats: int,
    scale: float,
) -> dict[str, Any]:
    """Async variant of :func:`_measure_sync` for the ASGI loop."""
    rounds: list[dict[str, float | int]] = []
    for _ in range(repeats):
        for _ in range(warmup):
            await fn()
        durations: list[float] = []
        started = time.perf_counter()
        for _ in range(iterations):
            tick = time.perf_counter()
            await fn()
            durations.append((time.perf_counter() - tick) * scale)
        elapsed = time.perf_counter() - started
        rounds.append(_summarize(durations, elapsed))
    return _pack_rounds(rounds)


def _build_ping_app(*, with_metrics: bool) -> FastAPI:
    """Build a minimal app returning a tiny JSON payload."""
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    if with_metrics:
        app.add_middleware(MetricsMiddleware)
    return app


def _delta(base: Mapping[str, float], instrumented: Mapping[str, float], key: str) -> dict:
    """Return the absolute and relative difference for one statistic."""
    baseline_value = float(base[key])
    instrumented_value = float(instrumented[key])
    difference = instrumented_value - baseline_value
    percent = (difference / baseline_value * 100.0) if baseline_value else 0.0
    return {
        "baseline": baseline_value,
        "instrumented": instrumented_value,
        "delta": difference,
        "delta_pct": percent,
    }


async def _bench_middleware(args: argparse.Namespace) -> dict[str, Any]:
    """Compare the full ASGI cycle with and without ``MetricsMiddleware``."""
    baseline_app = _build_ping_app(with_metrics=False)
    instrumented_app = _build_ping_app(with_metrics=True)
    transport_baseline = ASGITransport(app=baseline_app)
    transport_instrumented = ASGITransport(app=instrumented_app)
    async with (
        httpx.AsyncClient(transport=transport_baseline, base_url="http://bench") as baseline,
        httpx.AsyncClient(transport=transport_instrumented, base_url="http://bench") as observed,
    ):

        async def baseline_call() -> None:
            await baseline.get("/ping")

        async def instrumented_call() -> None:
            await observed.get("/ping")

        probe = await observed.get("/ping")
        header_present = "x-response-time-ms" in probe.headers

        baseline_stats = await _measure_async(
            baseline_call,
            iterations=args.iterations,
            warmup=args.warmup,
            repeats=args.repeats,
            scale=1_000_000.0,
        )
        instrumented_stats = await _measure_async(
            instrumented_call,
            iterations=args.iterations,
            warmup=args.warmup,
            repeats=args.repeats,
            scale=1_000_000.0,
        )

    delta = {
        key: _delta(baseline_stats["median"], instrumented_stats["median"], key)
        for key in ("mean", "p50", "p95", "p99")
    }
    return {
        "unit": "microseconds_per_request",
        "header_present": header_present,
        "baseline": baseline_stats,
        "instrumented": instrumented_stats,
        "delta": delta,
    }


def _bench_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    """Measure ``snapshot()`` after populating the histograms."""
    for index in range(50):
        RETRIEVAL_DURATION.labels(outcome="success").observe(0.010 + (index % 5) * 0.002)
        CHAT_TTFT.labels(outcome="success").observe(0.050 + (index % 5) * 0.005)
        INGESTION_DURATION.labels(status="completed").observe(0.500 + (index % 5) * 0.100)
    return {
        "unit": "microseconds_per_call",
        **_measure_sync(
            snapshot,
            iterations=args.iterations,
            warmup=args.warmup,
            repeats=args.repeats,
            scale=1_000_000.0,
        ),
    }


def _synthetic_retrieved(case: Any) -> list[dict[str, Any]]:
    """Build retrieved chunks from a case's own expected evidence."""
    return [
        {
            "filename": evidence.filename,
            "chunk_index": index,
            "score": round(0.9 - 0.01 * index, 4),
            "excerpt": evidence.must_contain,
        }
        for index, evidence in enumerate(case.expected_evidence)
    ]


def _score_once(cases: Sequence[Any]) -> list[Any]:
    """Score every case with its synthetic context and expected answer."""
    return [
        _score_case(
            case,
            top_k=case.top_k or 4,
            retrieved=_synthetic_retrieved(case),
            answer=case.expected_answer,
            latency_ms=0.0,
        )
        for case in cases
    ]


def _bench_eval_scoring(args: argparse.Namespace) -> dict[str, Any]:
    """Measure the pure-Python evaluation pipeline over the real dataset."""
    dataset = load_dataset(_DATASET_PATH)
    cases = list(dataset.cases)
    case_count = len(cases)

    def run_once() -> None:
        results = _score_once(cases)
        aggregate(results, with_answers=True)
        build_report(_META, results, with_answers=True)

    packed = _measure_sync(
        run_once,
        iterations=args.eval_iterations,
        warmup=min(args.warmup, 5),
        repeats=args.repeats,
        scale=1_000.0,
    )
    median = packed["median"]
    packed["unit"] = "milliseconds_per_dataset_run"
    packed["derived"] = {
        "cases": case_count,
        "ms_per_case": (median["mean"] / case_count) if case_count else 0.0,
        "cases_per_sec": median["ops_per_sec"] * case_count,
    }
    return packed


def _bench_build_report(args: argparse.Namespace) -> dict[str, Any]:
    """Measure ``build_report`` alone for the 25 pre-scored cases."""
    dataset = load_dataset(_DATASET_PATH)
    results = _score_once(list(dataset.cases))
    packed = _measure_sync(
        lambda: build_report(_META, results, with_answers=True),
        iterations=args.eval_iterations,
        warmup=min(args.warmup, 10),
        repeats=args.repeats,
        scale=1_000.0,
    )
    packed["unit"] = "milliseconds_per_report"
    packed["derived"] = {
        "cases": len(results),
        "ms_per_case": (packed["median"]["mean"] / len(results)) if results else 0.0,
    }
    return packed


def _bench_chunker(args: argparse.Namespace) -> dict[str, Any]:
    """Measure ``TextChunker.chunk_text`` on a ~1 MB expansion of the corpus."""
    corpus = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_CORPUS_DIR.glob("*.md"))
    )
    base_bytes = len(corpus.encode("utf-8"))
    repeat = max(1, args.chunk_target_bytes // max(base_bytes, 1))
    text = (corpus + "\n") * repeat
    text_bytes = len(text.encode("utf-8"))
    chunker = TextChunker()
    chunks_per_pass = len(chunker.chunk_text(text))
    packed = _measure_sync(
        lambda: chunker.chunk_text(text),
        iterations=args.chunk_iterations,
        warmup=1,
        repeats=args.repeats,
        scale=1_000.0,
    )
    ops = float(packed["median"]["ops_per_sec"])
    packed["unit"] = "milliseconds_per_pass"
    packed["derived"] = {
        "text_bytes": text_bytes,
        "text_mib": round(text_bytes / 1024.0 / 1024.0, 3),
        "chunks_per_pass": chunks_per_pass,
        "mb_per_sec": round(text_bytes * ops / 1_000_000.0, 3),
        "mib_per_sec": round(text_bytes * ops / 1024.0 / 1024.0, 3),
        "chunks_per_sec": round(chunks_per_pass * ops, 3),
    }
    return packed


def _bench_sha256(args: argparse.Namespace) -> dict[str, Any]:
    """Measure ``file_sha256`` per corpus file."""
    files = sorted(_CORPUS_DIR.glob("*.md"))
    rounds: list[dict[str, float | int]] = []
    for _ in range(args.repeats):
        for path in files:
            file_sha256(path)
        durations: list[float] = []
        started = time.perf_counter()
        for _ in range(args.iterations):
            for path in files:
                tick = time.perf_counter()
                file_sha256(path)
                durations.append((time.perf_counter() - tick) * 1_000.0)
        elapsed = time.perf_counter() - started
        rounds.append(_summarize(durations, elapsed))
    packed = _pack_rounds(rounds)
    packed["unit"] = "milliseconds_per_file"
    packed["derived"] = {
        "files": len(files),
        "ms_per_file": packed["median"]["mean"],
        "files_per_sec": packed["median"]["ops_per_sec"],
    }
    return packed


def _environment(args: argparse.Namespace) -> dict[str, Any]:
    """Record the machine, interpreter and CLI settings with the results."""
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "python_full": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine() or "unknown",
        "cpu_count": os.cpu_count(),
        "iterations": args.iterations,
        "warmup": args.warmup,
        "repeats": args.repeats,
        "eval_iterations": args.eval_iterations,
        "chunk_iterations": args.chunk_iterations,
        "chunk_target_bytes": args.chunk_target_bytes,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Execute every benchmark and return the JSON-friendly payload."""
    return {
        "meta": _environment(args),
        "benchmarks": {
            "middleware_overhead": await _bench_middleware(args),
            "snapshot": _bench_snapshot(args),
            "eval_scoring": _bench_eval_scoring(args),
            "build_report": _bench_build_report(args),
            "chunker": _bench_chunker(args),
            "corpus_sha256": _bench_sha256(args),
        },
    }


def _f(value: Any, digits: int = 3) -> str:
    """Format a scalar for the Markdown tables."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return str(value)


def _stat_rows(stats: Mapping[str, Any], unit_label: str) -> list[str]:
    """Render mean/p50/p95/p99 rows for one measured distribution."""
    median = stats["median"]
    rows = [f"| Statistic | {unit_label} |", "| --- | ---: |"]
    for key in ("mean", "p50", "p95", "p99", "max"):
        rows.append(f"| {key} | {_f(median[key])} |")
    rows.append(f"| ops/sec | {_f(median['ops_per_sec'])} |")
    return rows


def render_markdown(result: Mapping[str, Any]) -> str:
    """Render the benchmark results as a Markdown report."""
    meta = result["meta"]
    benchmarks = result["benchmarks"]
    middleware = benchmarks["middleware_overhead"]
    snapshot_bench = benchmarks["snapshot"]
    eval_bench = benchmarks["eval_scoring"]
    report_bench = benchmarks["build_report"]
    chunker = benchmarks["chunker"]
    sha_bench = benchmarks["corpus_sha256"]

    lines = [
        "# DocMind in-process benchmarks",
        "",
        "> These are in-process micro-benchmarks on one machine, not production SLAs.",
        "> Embedding, LLM and pgvector latency are not measured here.",
        "",
        "## Environment",
        "",
        f"- Python: {meta['python']} ({meta['python_full']})",
        f"- Platform: {meta['platform']}",
        f"- Processor: {meta['processor']}",
        f"- CPU count: {meta['cpu_count']}",
        f"- Iterations: {meta['iterations']}, warmup: {meta['warmup']}, repeats: {meta['repeats']}",
        f"- Generated: {meta['generated_at']}",
        "",
        "## Middleware overhead (full ASGI cycle via `httpx.ASGITransport`)",
        "",
        "`X-Response-Time-Ms` header present on instrumented responses: "
        f"{middleware['header_present']}",
        "",
        "| Statistic | Baseline (us) | Instrumented (us) | Delta (us) | Delta (%) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for key in ("mean", "p50", "p95", "p99"):
        delta = middleware["delta"][key]
        lines.append(
            f"| {key} | {_f(delta['baseline'])} | {_f(delta['instrumented'])} | "
            f"{_f(delta['delta'])} | {_f(delta['delta_pct'], 2)} |"
        )
    lines.extend(
        [
            "",
            f"Requests/sec baseline: {_f(middleware['baseline']['median']['ops_per_sec'])}; "
            f"instrumented: {_f(middleware['instrumented']['median']['ops_per_sec'])}",
            "",
            "## `snapshot()` cost",
            "",
            "Microseconds per call after populating the retrieval, TTFT and ingestion histograms.",
            "",
            *_stat_rows(snapshot_bench, "us per call"),
            "",
            "## Evaluation scoring throughput",
            "",
            f"Real `eval/golden_dataset.json` ({eval_bench['derived']['cases']} cases), "
            "synthetic retrieved chunks from `expected_evidence`, answer = `expected_answer`.",
            "",
            *_stat_rows(eval_bench, "ms per dataset run"),
            "",
            f"- ms/case: {_f(eval_bench['derived']['ms_per_case'])}",
            f"- cases/sec: {_f(eval_bench['derived']['cases_per_sec'])}",
            "",
            "### `build_report()` alone",
            "",
            *_stat_rows(report_bench, "ms per report"),
            "",
            "## Chunker throughput",
            "",
            f"Text size: {chunker['derived']['text_bytes']} bytes "
            f"({chunker['derived']['text_mib']} MiB); "
            f"chunks per pass: {chunker['derived']['chunks_per_pass']}.",
            "",
            *_stat_rows(chunker, "ms per pass"),
            "",
            f"- MB/s (10^6 bytes): {_f(chunker['derived']['mb_per_sec'])}",
            f"- MiB/s (2^20 bytes): {_f(chunker['derived']['mib_per_sec'])}",
            f"- chunks/sec: {_f(chunker['derived']['chunks_per_sec'])}",
            "",
            "## Corpus integrity checks (`file_sha256`, 6 files)",
            "",
            *_stat_rows(sha_bench, "ms per file"),
            "",
            f"- files/sec: {_f(sha_bench['derived']['files_per_sec'])}",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.bench",
        description=(
            "Run in-process DocMind micro-benchmarks. No network, database, LLM "
            "or embedding provider is used."
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1000,
        help="measured iterations per repeat for fast benchmarks (default: 1000)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=100,
        help="warmup iterations before each repeat (default: 100)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=5,
        help="number of measured repeats; the median is reported (default: 5)",
    )
    parser.add_argument(
        "--eval-iterations",
        type=int,
        default=20,
        dest="eval_iterations",
        help="full evaluation-pipeline passes per repeat (default: 20)",
    )
    parser.add_argument(
        "--chunk-iterations",
        type=int,
        default=5,
        dest="chunk_iterations",
        help="~1 MB chunking passes per repeat (default: 5)",
    )
    parser.add_argument(
        "--chunk-target-bytes",
        type=int,
        default=1_000_000,
        dest="chunk_target_bytes",
        help="target size of the expanded chunking input in bytes (default: 1000000)",
    )
    parser.add_argument(
        "--json-out",
        default=str(_DEFAULT_JSON),
        dest="json_out",
        help=f"JSON output path (default: {_DEFAULT_JSON})",
    )
    parser.add_argument(
        "--markdown-out",
        default=str(_DEFAULT_MD),
        dest="markdown_out",
        help=f"Markdown output path (default: {_DEFAULT_MD})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the benchmarks, write the reports and return an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    for flag, value in (
        ("--iterations", args.iterations),
        ("--eval-iterations", args.eval_iterations),
        ("--chunk-iterations", args.chunk_iterations),
        ("--chunk-target-bytes", args.chunk_target_bytes),
        ("--repeats", args.repeats),
    ):
        if value <= 0:
            parser.error(f"{flag} must be a positive integer")
    if args.warmup < 0:
        parser.error("--warmup must be non-negative")

    result = asyncio.run(_run(args))
    json_path = Path(args.json_out)
    markdown_path = Path(args.markdown_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(result) + "\n", encoding="utf-8")

    middleware = result["benchmarks"]["middleware_overhead"]["delta"]["mean"]
    print(f"wrote {json_path} and {markdown_path}")
    print(
        "middleware mean overhead: "
        f"{_f(middleware['delta'])} us ({_f(middleware['delta_pct'], 2)}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
