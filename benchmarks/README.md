# DocMind Benchmarks

Reproducible **in-process** measurements for the DocMind portfolio story: static
code metrics plus a small micro-benchmark suite. Nothing here talks to
PostgreSQL, Redis, an embedding provider or an LLM, and nothing is committed as
a "production SLA".

All numbers below were produced by the commands in
[How to reproduce](#how-to-reproduce) on the machine listed in
[Measured environment](#measured-environment).

## Measured environment

| Field | Value |
| --- | --- |
| Python | 3.14.8 (CPython, MSC v.1944 64-bit) |
| OS | Windows-11-10.0.26300-SP0 |
| CPU | AMD64 Family 25 Model 68 Stepping 1, AuthenticAMD |
| Logical CPUs | 12 |
| Benchmark iterations | 2000 per repeat, 200 warmup, 5 repeats |
| Benchmark run | 2026-11-09T14:57:11+00:00 |
| Static metrics run | 2026-11-09T16:16:27+00:00 |

The repository uses a local `.venv`; the system `python` on this machine does not
have the project dependencies installed. The scripts themselves are
interpreter-agnostic but require the project's runtime dependencies.

## How to reproduce

From the repository root:

```bash
# Static metrics -> benchmarks/static_metrics.json + benchmarks/static_metrics.md
python -m benchmarks.static_metrics --with-coverage

# Micro-benchmarks -> benchmarks/results/latest.json + latest.md
python -m benchmarks.bench --iterations 2000 --warmup 200

# Curate both artifacts into demo/fixtures/*.json (aggregates only, no paths)
python -m demo.export_fixtures
```

`python benchmarks/bench.py` also works because both scripts prepend the
repository root to `sys.path`. The default `--iterations` is `1000`; the
`--repeats` default is `5`. `--eval-iterations` and `--chunk-iterations` control
the heavier benchmarks so they do not run thousands of times.

The scripts set dummy `DATABASE_URL`, `REDIS_URL`, `OPENAI_API_KEY` and provider
settings and import application modules from an empty temporary working
directory, so a real `.env` is never read and no secret is used. No network
calls are made.

## What is measured

1. **Middleware overhead** — full ASGI cycle through
   `httpx.AsyncClient(transport=ASGITransport(app=...))` for two identical
   FastAPI apps returning a tiny JSON payload: one without middleware, one with
   `MetricsMiddleware`.
2. **`snapshot()` cost** — time per call after populating the retrieval, TTFT
   and ingestion histograms.
3. **Evaluation scoring throughput** — the real `eval/golden_dataset.json`,
   with synthetic retrieved chunks built from each case's `expected_evidence`
   and its `expected_answer` used as the answer, through `_score_case` +
   `aggregate` + `build_report`.
4. **Chunker throughput** — `TextChunker().chunk_text()` on a ~1 MB expansion
   of the markdown corpus.
5. **Corpus integrity cost** — `file_sha256()` for the six corpus files.

## Static metrics

Line-count methodology (`benchmarks/static_metrics.py`): line based.
`total_lines` counts every text line; `non_empty_lines` counts lines whose
`strip()` is truthy; `non_comment_lines` counts non-empty lines that do not start
with `#`. Python docstrings are counted as code lines.

| Area | Files | Total lines | Non-empty | Non-comment |
| --- | ---: | ---: | ---: | ---: |
| `src/` (`.py`) | 30 | 4147 | 3498 | 3371 |
| `tests/` (`.py`) | 19 | 3242 | 2490 | 2427 |
| `eval/` (`.py`) | 0 | 0 | 0 | 0 |
| `eval/` (`.json`) | 1 | 428 | 428 | 428 |
| `eval/` (`.md`, README + 6 corpus files) | 7 | 258 | 186 | 146 |
| `demo/` (`.py`) | 8 | 3575 | 3049 | 2908 |
| `benchmarks/` (`.py`, includes these scripts) | 4 | 1175 | 1046 | 1039 |
| `alembic/versions/` (`.py`) | 1 | 105 | 91 | 85 |

Python files total: **62** (`src` 30, `tests` 19, `demo` 8, `benchmarks` 4,
`alembic/versions` 1, `eval` 0).

### API endpoints

**10 `APIRoute` endpoints** discovered by recursively walking `app.routes`:
recent FastAPI versions keep `include_router` as an `_IncludedRouter` wrapper
(`original_router` + `include_context.prefix`), so a flat `isinstance` scan would
miss every router path.

| Method | Count |
| --- | ---: |
| DELETE | 1 |
| GET | 6 |
| HEAD | 1 |
| POST | 2 |

Paths: `GET /`, `POST /api/v1/chat/completions`, `GET /api/v1/documents`,
`POST /api/v1/documents/upload`, `DELETE /api/v1/documents/{document_id}`,
`GET /api/v1/documents/{document_id}/status`, `GET /api/v1/metrics`,
`GET /health`, `HEAD /health` (schema hidden), `GET /metrics` (schema hidden).

### Prometheus registry

| Metric | Value |
| --- | ---: |
| Families total | 14 |
| DocMind families | 10 |
| Samples total | 12 |
| DocMind samples | 2 |

The 10 DocMind families are `docmind_app_info`, `docmind_chat_streams`,
`docmind_chat_time_to_first_token_seconds`,
`docmind_http_request_duration_seconds`, `docmind_http_requests`,
`docmind_ingestion_duration_seconds`, `docmind_ingestion_jobs`,
`docmind_ready`, `docmind_retrieval_duration_seconds`,
`docmind_started_at_seconds`. The low sample count at import is expected:
labelled counters/histograms materialise series only on their first
`.labels(...)` call; at import only the unlabeled `docmind_ready` and
`docmind_started_at_seconds` gauges exist.

### Tests, coverage, dependencies, Make

| Metric | Value |
| --- | ---: |
| Tests collected (`pytest --collect-only -q`) | 234 |
| Tests passed / skipped | 232 / 2 |
| Coverage TOTAL (`pytest --cov=src --cov-fail-under=60 -q`) | 70% (69.57% exact) |
| Statements | 1881 |
| Missed statements | 520 |
| Branches / partial branches | 492 / 54 |
| Runtime dependencies | 20 |
| Dev dependencies | 6 |
| Demo dependencies | 2 |
| Make targets | 20 |

The coverage gate (`>= 60%`) passes. Exact coverage fluctuates by a few tenths
of a percentage point between runs (observed 68.73–69.57%); the rounded TOTAL is
70%.

## Micro-benchmark results

Medians across 5 repeats; every repeat is warmed up first and timed with
`time.perf_counter()`. Percentiles use linear interpolation between ranks (the
same helper as `src/eval.py`).

### Middleware overhead — full ASGI cycle, microseconds per request

| Statistic | Baseline (us) | Instrumented (us) | Delta (us) | Delta (%) |
| --- | ---: | ---: | ---: | ---: |
| mean | 207.814 | 222.173 | +14.360 | +6.91% |
| p50 | 192.400 | 207.350 | +14.950 | +7.77% |
| p95 | 286.105 | 308.420 | +22.315 | +7.80% |
| p99 | 488.983 | 500.042 | +11.059 | +2.26% |

Requests/sec: baseline **4806.478**, instrumented **4496.338**. The
`X-Response-Time-Ms` header is present on instrumented responses. Both sides pay
the same `httpx.ASGITransport` overhead, so the **absolute delta (~14 us per
request)** is the meaningful part; because the baseline endpoint is trivial, the
relative overhead looks like ~7% rather than a fraction of a real RAG request.

### `snapshot()` cost

| Statistic | Value |
| --- | ---: |
| mean | 2238.396 us/call |
| p50 | 2166.550 us/call |
| p95 | 2519.455 us/call |
| p99 | 3023.177 us/call |
| max | 4510.000 us/call |
| Throughput | 446.687 calls/sec |

`snapshot()` re-collects the whole registry several times (once per
`_samples_for` lookup) and the default registry includes the process/platform
collectors, so it is a diagnostic endpoint, not a hot path. The number is
reported as measured rather than optimised.

### Evaluation scoring throughput (real 25-case dataset)

| Statistic | Value |
| --- | ---: |
| mean per full dataset run | 1.736 ms |
| p50 per full dataset run | 1.699 ms |
| p95 per full dataset run | 1.917 ms |
| p99 per full dataset run | 1.936 ms |
| ms per case | 0.069 ms |
| cases/sec | 14401.005 |

`build_report()` alone for the same 25 pre-scored cases: mean **0.142 ms**
(p50 0.135, p95 0.169, p99 0.177), **7042.254 reports/sec**. This is pure-Python
scoring (`token_f1`, `rouge_l`, lexical faithfulness, aggregation); it excludes
retrieval and generation.

### Chunker throughput

Input is a ~1 MB repetition of the six corpus markdown files.

| Statistic | Value |
| --- | ---: |
| Text size | 998482 bytes (0.952 MiB) |
| Chunks per pass | 352 |
| mean per pass | 27.955 ms |
| p50 per pass | 27.784 ms |
| p95 per pass | 28.349 ms |
| p99 per pass | 28.358 ms |
| Throughput | 35.714 MB/s (10^6 bytes) / 34.060 MiB/s |
| Chunks/sec | 12590.575 |

### Corpus integrity checks (`file_sha256`, 6 files)

| Statistic | Value |
| --- | ---: |
| mean per file | 0.084 ms |
| p50 per file | 0.079 ms |
| p95 per file | 0.107 ms |
| p99 per file | 0.137 ms |
| max per file | 0.417 ms |
| Files/sec | 11846.995 |

## What is not measured here

This suite deliberately does **not** measure the latency of the RAG pipeline
itself:

- **Embedding calls** to the provider (`text-embedding-3-small` or
  `nomic-embed-text`), including network round-trips and batching.
- **LLM time-to-first-token and token streaming** for chat answers.
- **pgvector search** latency over real embeddings and real index parameters.
- **End-to-end ingestion** (parsing, chunking, embedding, database writes,
  queue retries).
- **PostgreSQL/Redis** connection and query latency, and any network effects.

Those need live infrastructure and credentials, which is exactly why they are
out of scope for an offline, reproducible micro-benchmark. The project already
has the right tooling to close this gap when PostgreSQL and a provider are
available:

- `python -m src.eval --mode retrieval` scores retrieval on the same golden
  dataset against a live pgvector database.
- `python -m src.eval --mode e2e --base-url http://127.0.0.1:8000` streams real
  answers over SSE and records `X-Response-Time-Ms`.
- The running API exposes real request, retrieval, TTFT and ingestion
  histograms at `GET /api/v1/metrics` (JSON) and `GET /metrics` (Prometheus).

See [`docs/EVALUATION.md`](../docs/EVALUATION.md) for the dataset schema, metric
definitions, thresholds and limitations.

## Generated files

| File | Purpose | Tracked |
| --- | --- | --- |
| [`benchmarks/static_metrics.json`](static_metrics.json) | Machine-readable static metrics | yes |
| [`benchmarks/static_metrics.md`](static_metrics.md) | Human-readable static metrics | yes |
| `benchmarks/results/latest.json` | Machine-readable benchmark run of record | no (git-ignored) |
| `benchmarks/results/latest.md` | Human-readable benchmark run of record | no (git-ignored) |

`benchmarks/results/` is git-ignored, mirroring `eval/reports/`; regenerate it
with the commands above.
