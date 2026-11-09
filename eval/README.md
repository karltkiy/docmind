# Evaluation Quick Start

- [`golden_dataset.json`](golden_dataset.json) — 25 cases (22 answerable, 3
  negative) plus the SHA-256 checksums of the corpus.
- [`corpus/`](corpus) — six markdown documents that the dataset references.
- `reports/latest.json` — output of the last run (git-ignored). Override with
  `--out`.

```bash
# 1. Upload and index the corpus through the API (idempotent).
python -m src.eval --mode retrieval --seed

# 2. Retrieval baseline; fails below 85% hit@4.
python -m src.eval --mode retrieval --fail-under-hit-rate 0.85 --json-only

# 3. Optional end-to-end run against a live API (SSE answers).
python -m src.eval --mode e2e --base-url http://127.0.0.1:8000
```

The same steps are available as `make eval-seed`, `make eval` and
`make eval-e2e`. Runs need PostgreSQL/pgvector and an embedding provider; `e2e`
also needs an LLM provider. The methodology, metric formulas, thresholds and
troubleshooting live in [`../docs/EVALUATION.md`](../docs/EVALUATION.md).
