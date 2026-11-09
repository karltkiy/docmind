# DocMind static metrics

Generated: 2026-11-09T16:37:13+00:00  
Python: 3.14.8  
Platform: Windows-11-10.0.26300-SP0

## Lines of code

Methodology: line based: total_lines = every text line; non_empty_lines = lines whose strip() is truthy; non_comment_lines = non-empty lines that do not start with '#' (Python docstrings count as code lines)

| Area | Path | Suffix | Files | Total lines | Non-empty | Non-comment |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| src | `src` | .py | 30 | 4147 | 3498 | 3371 |
| tests | `tests` | .py | 20 | 3358 | 2577 | 2512 |
| eval_python | `eval` | .py | 0 | 0 | 0 | 0 |
| eval_json | `eval` | .json | 1 | 428 | 428 | 428 |
| eval_markdown | `eval` | .md | 7 | 258 | 186 | 146 |
| demo | `demo` | .py | 8 | 3612 | 3079 | 2931 |
| benchmarks | `benchmarks` | .py | 4 | 1175 | 1046 | 1039 |
| alembic_versions | `alembic/versions` | .py | 1 | 105 | 91 | 85 |

Python files total: 63

## API endpoints

Endpoints (`APIRoute`): 10

| Method | Count |
| --- | ---: |
| DELETE | 1 |
| GET | 6 |
| HEAD | 1 |
| POST | 2 |

Paths:

- `GET /`
- `POST /api/v1/chat/completions`
- `GET /api/v1/documents`
- `POST /api/v1/documents/upload`
- `DELETE /api/v1/documents/{document_id}`
- `GET /api/v1/documents/{document_id}/status`
- `GET /api/v1/metrics`
- `GET /health`
- `HEAD /health` (schema hidden)
- `GET /metrics` (schema hidden)

## Prometheus registry

- Families total: 14
- DocMind families: 10
- Samples total: 12
- DocMind samples: 2

DocMind metric families: `docmind_app_info`, `docmind_chat_streams`, `docmind_chat_time_to_first_token_seconds`, `docmind_http_request_duration_seconds`, `docmind_http_requests`, `docmind_ingestion_duration_seconds`, `docmind_ingestion_jobs`, `docmind_ready`, `docmind_retrieval_duration_seconds`, `docmind_started_at_seconds`

## Tests and coverage

- Tests collected: 241
- Coverage TOTAL: 70% (520 missed of 1881 statements)
- Passed / skipped / failed: 239 / 2 / None

## Dependencies

- Runtime: 20
- Dev: 7
- Demo: 2

## Make targets

Targets: 20

## Golden dataset

- Cases: 25 (answerable 22, negative 3)
- Corpus documents: 6
- Corpus words: 1538
- JSON size: 16251 bytes (15.87 KiB)

