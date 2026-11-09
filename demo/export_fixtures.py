"""Build the committed demo fixtures from real measurement artifacts.

The demo's *Metrics & evidence* view must never invent a number, so every
value it shows is copied from an artifact produced by an actual run:

===============  ===========================================================
Fixture          Source artifact
===============  ===========================================================
metrics_static   ``benchmarks/static_metrics.json``
microbenchmarks  ``benchmarks/results/latest.json``
retrieval_eval   the newest ``eval/reports/retrieval-fixed-*.json``
===============  ===========================================================

Only aggregates are copied. Raw ``cases[]`` / ``failures[]`` arrays, excerpts,
transcripts, filesystem paths and any credential-shaped string are dropped,
and the result is written with a fixed layout so the output is diffable.

The dataset checksum comes from ``eval/golden_dataset.json`` and the git
commit/dirty flag from the working tree, so a reader can tell exactly which
code produced the numbers.

Usage::

    python -m demo.export_fixtures            # (re)write demo/fixtures/*.json
    python -m demo.export_fixtures --check    # verify, write nothing

``--check`` is what CI (and ``make demo-fixtures``) runs: it rebuilds the
fixtures in memory and fails when the committed ones are out of date. Git
provenance (commit, dirty flag, export timestamp) is intentionally excluded
from that comparison — it changes with every commit, while the measurements
do not.

The source artifacts above are git-ignored, so a clean checkout has none of
them. In that case ``--check`` cannot re-derive the numbers and instead
validates what is committed: schema version, kind, provenance, limitations,
the credential/path filter and the size budget. The value-by-value comparison
still runs wherever a measurement was performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow ``python demo/export_fixtures.py``
    sys.path.insert(0, str(REPO_ROOT))

from demo.metrics_data import FIXTURE_FILES  # noqa: E402

#: Fixture schema version written by this script.
SCHEMA_VERSION = 1

#: Hard ceiling for a single fixture file.
MAX_FIXTURE_BYTES = 32 * 1024

FIXTURES_DIR = REPO_ROOT / "demo" / "fixtures"
STATIC_METRICS_PATH = REPO_ROOT / "benchmarks" / "static_metrics.json"
MICROBENCH_PATH = REPO_ROOT / "benchmarks" / "results" / "latest.json"
EVAL_REPORTS_DIR = REPO_ROOT / "eval" / "reports"
GOLDEN_DATASET_PATH = REPO_ROOT / "eval" / "golden_dataset.json"
CONFIG_SOURCE_PATH = REPO_ROOT / "src" / "config.py"

#: Provenance fields that legitimately differ between two exports.
VOLATILE_PROVENANCE_KEYS = ("git_commit", "git_dirty", "exported_at")

#: Strings that must never appear in a committed fixture.
FORBIDDEN_SUBSTRINGS = (
    "api_key",
    "authorization",
    "openai_api_key",
    "sk-",
    "c:\\",
    "/home/",
    "/opt/",
)

#: Caveats that apply to every number in this directory.
SHARED_LIMITATIONS: tuple[str, ...] = (
    "25-case self-built retrieval set, not an industry benchmark",
    "no similarity cut-off, negative cases still receive top-k context",
    "latency measured on a single machine, not an SLA",
    "hit@k depends on chunk_size/top_k/embedding model",
)

_STATIC_LIMITATIONS: tuple[str, ...] = SHARED_LIMITATIONS + (
    "static metrics are a snapshot of one working tree, not a code-quality score",
    "line counts include Python docstrings as code lines",
)

_RETRIEVAL_LIMITATIONS: tuple[str, ...] = SHARED_LIMITATIONS + (
    "retrieval ran against a local Ollama stack (nomic-embed-text + llama3.1:8b), "
    "so the hardware block describes the exporting host, not the graded service",
    "faithfulness, token-F1 and ROUGE are not reported: the retrieval run answers "
    "no questions and spends no LLM tokens",
)

_MICROBENCH_LIMITATIONS: tuple[str, ...] = SHARED_LIMITATIONS + (
    "micro-benchmarks run in-process on one machine; they are not a load test",
    "percentiles are reconstructed from histogram buckets and are approximate",
    "ingestion latency needs a live database, an embedding provider and the Arq "
    "worker, so it is not part of these fixtures",
)

_INT_DEFAULT_RE = re.compile(
    r"^\s+(CHUNK_SIZE|CHUNK_OVERLAP|RETRIEVAL_TOP_K):\s*int\s*=\s*(\d+)", re.MULTILINE
)
_PROVIDER_DIM_RE = re.compile(r'^\s+"(openai|ollama)":\s*(\d+),\s*$', re.MULTILINE)
_PROVIDER_MODEL_RE = re.compile(r'^\s+"(openai|ollama)":\s*"([^"]+)",\s*$', re.MULTILINE)


class ExportError(RuntimeError):
    """Raised when an artifact is missing or does not have the expected shape."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _rel(path: Path) -> str:
    """Repository-relative POSIX path (never an absolute path)."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON artifact or abort with an actionable message."""
    if not path.is_file():
        raise ExportError(
            f"missing source artifact: {_rel(path)}. "
            "Re-run the measurement that produces it (see the commands below)."
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExportError(f"{_rel(path)} is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    if not isinstance(document, dict):
        raise ExportError(f"{_rel(path)} must contain a JSON object")
    return document


def _git(*args: str) -> str:
    """Run a git command inside the repository root and return stdout."""
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ExportError(
            f"git {' '.join(args)} failed in {REPO_ROOT.name}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _git_provenance() -> tuple[str, bool]:
    """Return ``(HEAD sha, working tree dirty?)`` for the exporting checkout."""
    commit = _git("rev-parse", "HEAD").strip()
    if not commit:
        raise ExportError("git rev-parse HEAD returned an empty commit hash")
    dirty = bool(_git("status", "--porcelain").strip())
    return commit, dirty


def _newest_report() -> Path:
    """Return the most recent ``eval/reports/retrieval-fixed-*.json`` file."""
    candidates = sorted(EVAL_REPORTS_DIR.glob("retrieval-fixed-*.json"))
    if not candidates:
        raise ExportError(
            f"no eval/reports/retrieval-fixed-*.json found in {_rel(EVAL_REPORTS_DIR)}. "
            "Run 'python -m src.eval --mode retrieval --json-only' first."
        )
    return candidates[-1]


def _config_block(text: str, name: str) -> str:
    """Return the body of a module-level ``{...}`` literal called ``name``."""
    _, separator, tail = text.partition(name)
    if not separator:
        raise ExportError(f"{_rel(CONFIG_SOURCE_PATH)} has no {name} mapping")
    body, closed, _ = tail.partition("}")
    if not closed:
        raise ExportError(f"{name} in {_rel(CONFIG_SOURCE_PATH)} is not closed")
    return body


def _product_defaults() -> dict[str, Any]:
    """Read chunking/retrieval defaults and per-provider embedding widths.

    These are product defaults, not measurements: they are read from the
    committed configuration source so the fixture can state the exact
    chunking and vector width the documented commands use.
    """
    text = CONFIG_SOURCE_PATH.read_text(encoding="utf-8")
    resolved: dict[str, Any] = {
        name.lower(): int(value) for name, value in _INT_DEFAULT_RE.findall(text)
    }
    resolved["embedding_dims"] = {
        provider: int(value)
        for provider, value in _PROVIDER_DIM_RE.findall(
            _config_block(text, "_DEFAULT_EMBEDDING_DIMS")
        )
    }
    resolved["embedding_models"] = dict(
        _PROVIDER_MODEL_RE.findall(_config_block(text, "_DEFAULT_EMBEDDING_MODELS"))
    )
    return resolved


def _sha256_file(path: Path) -> str:
    """SHA-256 of a file, streamed to stay memory-friendly."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _environment(static_meta: dict[str, Any], micro_meta: dict[str, Any]) -> dict[str, Any]:
    """Assemble the mandatory provenance environment block."""
    try:
        return {
            "python": str(static_meta["python"]),
            "platform": str(static_meta["platform"]),
            "processor": str(static_meta["processor"]),
            "cpu_count": int(micro_meta["cpu_count"]),
        }
    except KeyError as exc:
        raise ExportError(f"measurement artifacts do not record {exc}") from exc


def _envelope(
    *,
    kind: str,
    generated_at: str,
    source_command: str,
    source_artifacts: list[str],
    limitations: tuple[str, ...],
    payload: dict[str, Any],
    environment: dict[str, Any],
    commit: str,
    dirty: bool,
) -> dict[str, Any]:
    """Wrap a payload with the provenance block every fixture must carry."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "generated_at": generated_at,
        "provenance": {
            "git_commit": commit,
            "git_dirty": dirty,
            "source_command": source_command,
            "source_artifacts": source_artifacts,
            "environment": environment,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "limitations": list(limitations),
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------
def _metrics_static_payload(static: dict[str, Any]) -> dict[str, Any]:
    """Copy the static-metrics aggregates, dropping every path."""
    loc = static.get("loc") or {}
    areas = [
        {
            "area": name,
            "files": int(entry.get("files", 0)),
            "total_lines": int(entry.get("total_lines", 0)),
            "non_empty_lines": int(entry.get("non_empty_lines", 0)),
            "non_comment_lines": int(entry.get("non_comment_lines", 0)),
        }
        for name, entry in sorted((loc.get("areas") or {}).items())
    ]
    api = static.get("api") or {}
    prometheus = static.get("prometheus") or {}
    dependencies = static.get("dependencies") or {}
    make = static.get("make") or {}
    dataset = static.get("dataset") or {}
    tests = static.get("tests") or {}
    coverage = static.get("coverage") or {}
    return {
        "loc": {
            "methodology": str(loc.get("methodology", "")),
            "areas": areas,
            "python_files": {
                str(name): int(value)
                for name, value in sorted((loc.get("python_files") or {}).items())
            },
        },
        "api": {
            "endpoint_count": int(api.get("endpoint_count", 0)),
            "by_method": {
                str(method): int(count)
                for method, count in sorted((api.get("by_method") or {}).items())
            },
        },
        "prometheus": {
            "families_total": int(prometheus.get("families_total", 0)),
            "families_docmind": int(prometheus.get("families_docmind", 0)),
            "samples_total": int(prometheus.get("samples_total", 0)),
            "samples_docmind": int(prometheus.get("samples_docmind", 0)),
        },
        "dependencies": {
            "runtime": int(dependencies.get("runtime", 0)),
            "dev": int(dependencies.get("dev", 0)),
            "demo": int(dependencies.get("demo", 0)),
        },
        "make": {
            "target_count": int(make.get("target_count", 0)),
            "targets": [str(target) for target in make.get("targets") or []],
        },
        "dataset": {
            "cases": int(dataset.get("cases", 0)),
            "answerable": int(dataset.get("answerable", 0)),
            "negative": int(dataset.get("negative", 0)),
            "corpus_documents": int(dataset.get("corpus_documents", 0)),
            "corpus_words": int(dataset.get("corpus_words", 0)),
            "json_bytes": int(dataset.get("json_bytes", 0)),
        },
        "tests": {
            "collected": int(tests.get("collected", 0)),
            "exit_code": int(tests.get("exit_code", 0)),
        },
        "coverage": {
            "statements": int(coverage.get("statements", 0)),
            "missed": int(coverage.get("missed", 0)),
            "coverage_percent": int(coverage.get("coverage_percent", 0)),
            "coverage_percent_exact": float(coverage.get("coverage_percent_exact", 0.0)),
            "passed": int(coverage.get("passed", 0)),
            "skipped": int(coverage.get("skipped", 0)),
        },
    }


def _aggregate_block(source: dict[str, Any]) -> dict[str, Any]:
    """Keep only the aggregate metric keys of a summary/by-tag block."""
    return {
        "cases": int(source.get("cases", 0)),
        "answerable_cases": int(source.get("answerable_cases", 0)),
        "negative_cases": int(source.get("negative_cases", 0)),
        "error_cases": int(source.get("error_cases", 0)),
        "hit_at_k": source.get("hit_at_k"),
        "mrr": source.get("mrr"),
        "context_recall": source.get("context_recall"),
        "empty_context_rate": source.get("empty_context_rate"),
        "false_positive_rate": source.get("false_positive_rate"),
        "latency_ms": {
            key: (source.get("latency_ms") or {}).get(key) for key in ("mean", "p50", "p95", "max")
        },
    }


def _retrieval_payload(
    report: dict[str, Any], report_path: Path, defaults: dict[str, Any], dataset: dict[str, Any]
) -> dict[str, Any]:
    """Copy retrieval aggregates, the dataset checksum and the run configuration."""
    meta = report.get("meta") or {}
    summary = report.get("summary") or {}
    by_tag = report.get("by_tag") or {}
    cases = report.get("cases") or []

    top_k_values = sorted({int(case["top_k"]) for case in cases if "top_k" in case})
    provider = str(meta.get("embedding_provider") or "openai")
    dims = defaults.get("embedding_dims") or {}
    embedding_dim = dims.get(provider) if isinstance(dims, dict) else None
    default_models = defaults.get("embedding_models") or {}
    embedding_model = str(
        meta.get("embedding_model")
        or (default_models.get(provider) if isinstance(default_models, dict) else "")
        or ""
    )
    if embedding_dim is None:
        raise ExportError(
            f"could not resolve the embedding width for provider '{provider}' from "
            f"{_rel(CONFIG_SOURCE_PATH)}"
        )

    negative_cases = int(summary.get("negative_cases", 0))
    false_positive_rate = summary.get("false_positive_rate")
    returned_context = (
        round(float(false_positive_rate) * negative_cases) if false_positive_rate is not None else 0
    )

    return {
        "summary": _aggregate_block(summary),
        "by_tag": {str(tag): _aggregate_block(block) for tag, block in sorted(by_tag.items())},
        "dataset": {
            "path": _rel(GOLDEN_DATASET_PATH),
            "version": int(dataset.get("version", 0)),
            "sha256": _sha256_file(GOLDEN_DATASET_PATH),
            "json_bytes": GOLDEN_DATASET_PATH.stat().st_size,
            "cases": int(summary.get("cases", 0)),
            "answerable": int(summary.get("answerable_cases", 0)),
            "negative": negative_cases,
            "corpus_documents": int(meta.get("corpus_files", 0)),
        },
        "configuration": {
            "top_k": top_k_values[-1] if top_k_values else int(defaults.get("retrieval_top_k", 0)),
            "chunk_size": int(defaults.get("chunk_size", 0)),
            "chunk_overlap": int(defaults.get("chunk_overlap", 0)),
            "embedding_provider": provider,
            "embedding_model": embedding_model,
            "embedding_dim": int(embedding_dim),
            "llm_provider": str(meta.get("llm_provider") or ""),
            "llm_model": str(meta.get("llm_model") or ""),
            "semantic_scoring": bool(meta.get("semantic", False)),
            "source": (
                "providers from the eval report meta; chunk_size/chunk_overlap/embedding_dim "
                "from product defaults in src/config.py"
            ),
        },
        "negatives": {
            "cases": negative_cases,
            "returned_context": returned_context,
            "false_positive_rate": false_positive_rate,
            "note": (
                "The product has no similarity cut-off: every query receives top-k "
                "context, so all negative cases are counted as false positives."
            ),
        },
        "report": {
            "path": _rel(report_path),
            "dataset_version": int(meta.get("dataset_version", 0)),
            "duration_ms": float(meta.get("duration_ms", 0.0)),
        },
    }


def _microbench_payload(micro: dict[str, Any]) -> dict[str, Any]:
    """Copy the micro-benchmark medians and derived throughput figures."""
    meta = micro.get("meta") or {}
    benchmarks = micro.get("benchmarks") or {}

    def median(name: str) -> dict[str, Any]:
        block = benchmarks.get(name) or {}
        return block.get("median") or {}

    def derived(name: str) -> dict[str, Any]:
        block = benchmarks.get(name) or {}
        return block.get("derived") or {}

    middleware = benchmarks.get("middleware_overhead") or {}
    delta = middleware.get("delta") or {}
    baseline = (middleware.get("baseline") or {}).get("median") or {}
    instrumented = (middleware.get("instrumented") or {}).get("median") or {}

    stages = [
        {
            "name": "HTTP middleware (instrumented)",
            "unit": "µs/request",
            "mean": instrumented.get("mean"),
            "p50": instrumented.get("p50"),
            "p95": instrumented.get("p95"),
        },
        {
            "name": "metrics snapshot()",
            "unit": "µs/call",
            "mean": median("snapshot").get("mean"),
            "p50": median("snapshot").get("p50"),
            "p95": median("snapshot").get("p95"),
        },
        {
            "name": "eval scoring (25 cases)",
            "unit": "ms/dataset run",
            "mean": median("eval_scoring").get("mean"),
            "p50": median("eval_scoring").get("p50"),
            "p95": median("eval_scoring").get("p95"),
        },
        {
            "name": "eval report build",
            "unit": "ms/report",
            "mean": median("build_report").get("mean"),
            "p50": median("build_report").get("p50"),
            "p95": median("build_report").get("p95"),
        },
        {
            "name": "chunker (0.95 MiB text)",
            "unit": "ms/pass",
            "mean": median("chunker").get("mean"),
            "p50": median("chunker").get("p50"),
            "p95": median("chunker").get("p95"),
        },
        {
            "name": "corpus sha256 (6 files)",
            "unit": "ms/file",
            "mean": median("corpus_sha256").get("mean"),
            "p50": median("corpus_sha256").get("p50"),
            "p95": median("corpus_sha256").get("p95"),
        },
    ]

    return {
        "platform": {
            "python": str(meta.get("python", "")),
            "platform": str(meta.get("platform", "")),
            "processor": str(meta.get("processor", "")),
            "cpu_count": int(meta.get("cpu_count", 0)),
            "iterations": int(meta.get("iterations", 0)),
            "warmup": int(meta.get("warmup", 0)),
            "repeats": int(meta.get("repeats", 0)),
        },
        "stages": stages,
        "throughput": {
            "chunker_mib_per_sec": derived("chunker").get("mib_per_sec"),
            "chunker_chunks_per_sec": derived("chunker").get("chunks_per_sec"),
            "snapshot_ops_per_sec": median("snapshot").get("ops_per_sec"),
            "eval_cases_per_sec": derived("eval_scoring").get("cases_per_sec"),
            "report_build_ops_per_sec": median("build_report").get("ops_per_sec"),
            "corpus_files_per_sec": derived("corpus_sha256").get("files_per_sec"),
        },
        "middleware_overhead": {
            "baseline_p50_us": baseline.get("p50"),
            "instrumented_p50_us": instrumented.get("p50"),
            "delta_p50_us": (delta.get("p50") or {}).get("delta"),
            "delta_mean_pct": (delta.get("mean") or {}).get("delta_pct"),
            "delta_p95_pct": (delta.get("p95") or {}).get("delta_pct"),
        },
        "not_measured": [
            {
                "metric": "ingestion median (parse → chunk → embed → database write)",
                "reason": (
                    "runs in the Arq worker process against a live PostgreSQL/pgvector and "
                    "an embedding provider, so it cannot be reproduced from committed "
                    "in-process benchmarks"
                ),
            }
        ],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build_fixtures() -> dict[str, dict[str, Any]]:
    """Build every fixture document in memory."""
    static = _read_json(STATIC_METRICS_PATH)
    micro = _read_json(MICROBENCH_PATH)
    report_path = _newest_report()
    report = _read_json(report_path)
    dataset = _read_json(GOLDEN_DATASET_PATH)
    defaults = _product_defaults()
    commit, dirty = _git_provenance()

    static_meta = static.get("meta") or {}
    micro_meta = micro.get("meta") or {}
    report_meta = report.get("meta") or {}
    environment = _environment(static_meta, micro_meta)

    static_generated_at = str(static_meta.get("generated_at") or "")
    micro_generated_at = str(micro_meta.get("generated_at") or "")
    report_generated_at = str(report_meta.get("generated_at") or "")
    for label, value in (
        ("benchmarks/static_metrics.json meta.generated_at", static_generated_at),
        ("benchmarks/results/latest.json meta.generated_at", micro_generated_at),
        ("eval report meta.generated_at", report_generated_at),
    ):
        if not value:
            raise ExportError(f"{label} is missing; the fixture provenance would be a guess")

    return {
        "metrics_static": _envelope(
            kind="metrics_static",
            generated_at=static_generated_at,
            source_command="python -m benchmarks.static_metrics --with-coverage",
            source_artifacts=[_rel(STATIC_METRICS_PATH)],
            limitations=_STATIC_LIMITATIONS,
            payload=_metrics_static_payload(static),
            environment=environment,
            commit=commit,
            dirty=dirty,
        ),
        "retrieval_eval": _envelope(
            kind="retrieval_eval",
            generated_at=report_generated_at,
            source_command=(
                "python -m src.eval --mode retrieval --fail-under-hit-rate 0.85 --json-only"
            ),
            source_artifacts=[_rel(report_path), _rel(GOLDEN_DATASET_PATH)],
            limitations=_RETRIEVAL_LIMITATIONS,
            payload=_retrieval_payload(report, report_path, defaults, dataset),
            environment=environment,
            commit=commit,
            dirty=dirty,
        ),
        "microbenchmarks": _envelope(
            kind="microbenchmarks",
            generated_at=micro_generated_at,
            source_command="python -m benchmarks.bench --iterations 2000 --warmup 200",
            source_artifacts=[_rel(MICROBENCH_PATH)],
            limitations=_MICROBENCH_LIMITATIONS,
            payload=_microbench_payload(micro),
            environment=environment,
            commit=commit,
            dirty=dirty,
        ),
    }


def _serialize(document: dict[str, Any]) -> str:
    """Deterministic two-space JSON with a trailing newline."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _assert_clean(kind: str, text: str) -> None:
    """Reject a fixture that contains a credential-shaped string or a path."""
    lowered = text.lower()
    leaks = [needle for needle in FORBIDDEN_SUBSTRINGS if needle in lowered]
    if leaks:
        raise ExportError(f"fixture '{kind}' contains forbidden content: {', '.join(leaks)}")
    size = len(text.encode("utf-8"))
    if size > MAX_FIXTURE_BYTES:
        raise ExportError(
            f"fixture '{kind}' is {size} bytes, above the {MAX_FIXTURE_BYTES}-byte budget"
        )


def _strip_volatile(document: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``document`` without git commit, dirty flag and export timestamp."""
    clone = json.loads(json.dumps(document))
    provenance = clone.get("provenance")
    if isinstance(provenance, dict):
        for key in VOLATILE_PROVENANCE_KEYS:
            provenance.pop(key, None)
    return clone


def write_fixtures(directory: Path = FIXTURES_DIR) -> int:
    """Write every fixture and return a process exit code."""
    directory.mkdir(parents=True, exist_ok=True)
    documents = build_fixtures()
    for kind, document in documents.items():
        text = _serialize(document)
        _assert_clean(kind, text)
        path = directory / FIXTURE_FILES[kind]
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {_rel(path)} ({len(text.encode('utf-8')) / 1024:.1f} KiB)")
    print("done: 3 fixtures, no secrets, no absolute paths")
    return 0


def _missing_sources() -> list[str]:
    """Return repository-relative names of measurement artifacts this checkout lacks."""
    missing: list[str] = []
    if not STATIC_METRICS_PATH.is_file():
        missing.append(_rel(STATIC_METRICS_PATH))
    if not MICROBENCH_PATH.is_file():
        missing.append(_rel(MICROBENCH_PATH))
    if not sorted(EVAL_REPORTS_DIR.glob("retrieval-fixed-*.json")):
        missing.append(f"{_rel(EVAL_REPORTS_DIR)}/retrieval-fixed-*.json")
    return missing


def _check_committed_only(directory: Path, missing: list[str]) -> int:
    """Validate the committed fixtures when the source artifacts are absent."""
    problems: list[str] = []
    for kind, filename in FIXTURE_FILES.items():
        path = directory / filename
        if not path.is_file():
            problems.append(f"{_rel(path)}: missing")
            continue
        try:
            text = path.read_text(encoding="utf-8")
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            problems.append(f"{_rel(path)}: not valid JSON ({exc.msg})")
            continue
        if not isinstance(document, dict):
            problems.append(f"{_rel(path)}: must contain a JSON object")
            continue
        if document.get("kind") != kind:
            problems.append(f"{_rel(path)}: 'kind' is not {kind!r}")
        for key in ("schema_version", "generated_at", "provenance", "limitations", "payload"):
            if key not in document:
                problems.append(f"{_rel(path)}: missing '{key}'")
        try:
            _assert_clean(kind, text)
        except ExportError as exc:
            problems.append(str(exc))
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    print(
        "ok: committed fixtures are structurally valid; value comparison skipped "
        f"because source artifacts are absent ({', '.join(missing)})"
    )
    return 0


def check_fixtures(directory: Path = FIXTURES_DIR) -> int:
    """Compare a fresh export with the committed fixtures; write nothing."""
    missing = _missing_sources()
    if missing:
        return _check_committed_only(directory, missing)

    documents = build_fixtures()
    problems: list[str] = []
    for kind, document in documents.items():
        path = directory / FIXTURE_FILES[kind]
        if not path.is_file():
            problems.append(f"{_rel(path)}: missing")
            continue
        try:
            committed = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{_rel(path)}: not valid JSON ({exc.msg})")
            continue
        text = _serialize(document)
        try:
            _assert_clean(kind, text)
        except ExportError as exc:
            problems.append(str(exc))
            continue
        if _strip_volatile(committed) != _strip_volatile(document):
            problems.append(
                f"{_rel(path)}: content differs from a fresh export. "
                "Run 'python -m demo.export_fixtures' and commit the result."
            )
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    print("ok: committed fixtures match a fresh export (git provenance excluded)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m demo.export_fixtures``."""
    parser = argparse.ArgumentParser(
        description="Build the demo measurement fixtures from real artifacts.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed fixtures without writing (CI mode)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=FIXTURES_DIR,
        help="directory to write the fixtures into (default: demo/fixtures)",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            return check_fixtures(args.output_dir)
        return write_fixtures(args.output_dir)
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
