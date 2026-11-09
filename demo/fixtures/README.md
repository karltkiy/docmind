# Demo measurement fixtures

The demo's *Metrics & evidence* view reads **only** these three files plus
`GET /api/v1/metrics`. Nothing on that page is typed by hand and nothing is
estimated.

| File | Produced from | Kind |
| --- | --- | --- |
| `metrics_static.json` | `benchmarks/static_metrics.json` | code size, endpoints, dependencies, tests, coverage |
| `retrieval_eval.json` | the newest `eval/reports/retrieval-fixed-*.json` + `eval/golden_dataset.json` | hit@k, MRR, context recall, false positives, retrieval latency |
| `microbenchmarks.json` | `benchmarks/results/latest.json` | in-process stage timings and throughput |

## Recorded transcripts (`transcripts/`)

The public demo's chat tab can run in **replay** mode: it plays back a recorded
answer instead of calling a model. Those recordings live here:

| Path | Contents |
| --- | --- |
| `transcripts/index.json` | JSON object mapping a *document filename* to a *transcript id* |
| `transcripts/<id>.json` | one recorded answer, validated by [`demo/replay.py`](../replay.py) |

A transcript carries the real `t_ms` of every SSE frame, the `sources` frame,
the question, the provider/model provenance, timing totals and
`answer_sha256` of the concatenated tokens. The schema is `schema_version: 1`,
`kind: "transcript"`; the loader rejects unknown frame types, non-monotonic
`t_ms`, a last frame that is not `done` (unless the transcript is a single
`error`), an empty frame list, a mismatched `answer_sha256`, credential-shaped
keys/values (`api_key`, `authorization`, `openai_api_key`, `sk-`) and absolute
paths.

**Policy: recordings are not shipped with the repository.** They can only be
created by an actual provider request, and inventing one would make the demo
dishonest. When no recording exists for the selected document, the UI says so,
lists whatever recordings are present and prints the command to create one; it
never substitutes a live call.

Record one transcript per document with:

```bash
python -m demo.record_transcript \
  --document refund-policy.md \
  --question "How long does a refund take?" \
  --id refund-window
```

This costs exactly one provider request and prints a warning to that effect.
Replaying the file is free. Commit `transcripts/*.json` and `index.json`
together when you want the deployment to serve the recording, or leave the
directory absent — that is a valid "nothing recorded" state, not an error.

## Provenance policy

Every fixture carries a `provenance` block that is mandatory — the loader
[`demo/metrics_data.py`](../metrics_data.py) rejects a fixture without it:

- `schema_version` (integer, currently `1`) and `kind`;
- `generated_at` — when the **measurement** ran, ISO-8601 with an offset;
- `provenance.git_commit` / `provenance.git_dirty` — the exporting checkout
  (`git_dirty: true` means the measurement artifacts were produced from a
  working tree with local modifications);
- `provenance.source_command` — the command that produced the raw artifact;
- `provenance.source_artifacts` — repository-relative paths only;
- `provenance.environment{python, platform, processor, cpu_count}`;
- `limitations[]` — non-empty, honest caveats shown in the UI.

Deliberately **not** copied from the raw artifacts: `cases[]`, `failures[]`,
retrieved excerpts, answers, transcripts, filesystem paths and anything
credential-shaped. A fixture that contains `api_key`, `authorization`,
`openai_api_key`, `sk-` or an absolute path is refused by the exporter, and
each file is capped at 32 KiB.

## Regenerate

```bash
# the measurement artifacts the fixtures are built from
python -m benchmarks.static_metrics --with-coverage
python -m benchmarks.bench --iterations 2000 --warmup 200
python -m src.eval --mode retrieval --fail-under-hit-rate 0.85 --json-only

# rebuild demo/fixtures/*.json
python -m demo.export_fixtures

# CI check: rebuild in memory and fail if the committed files are stale
python -m demo.export_fixtures --check
```

`make demo-fixtures` runs both steps. `--check` ignores git provenance
(commit, dirty flag, export timestamp), because those change with every commit
while the measurements do not.

## Shelf life

Fixtures decay. The page warns when a fixture is older than **120 days**; the
threshold can be changed with `DOCMIND_FIXTURE_MAX_AGE_DAYS`. Regenerate after
any change to chunking, `top_k`, the embedding model, the golden dataset or
the benchmark scripts — otherwise the page keeps telling an old story.

## Reading the numbers

- `hit@k` / `MRR` / context recall come from a **25-case self-built** set, not
  an industry benchmark.
- `false_positive_rate = 1.0` is not a bug in the fixture: the product has no
  similarity cut-off, so every negative case still receives top-k context.
- Latency numbers were measured on one machine; they are not an SLA. Retrieval
  latency comes from the eval report, the stage timings from the micro-benchmarks.
- Ingestion latency is **not** in these fixtures: it runs in the Arq worker
  against a live database and an embedding provider.
