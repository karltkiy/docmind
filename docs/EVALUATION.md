# Evaluation Methodology

DocMind ships an offline harness that scores retrieval and end-to-end answers
against a versioned **golden dataset**. This document describes the dataset
format, the exact metric definitions, the two run modes, the recommended CI
thresholds and the known limitations. For the short version see
[`eval/README.md`](../eval/README.md); for a high-level overview see the
[Performance & Evaluation](../README.md#performance--evaluation) section of the
README.

The implementation lives in [`src/eval.py`](../src/eval.py) and is run as:

```bash
python -m src.eval --help
```

## Golden dataset

The dataset is [`eval/golden_dataset.json`](../eval/golden_dataset.json). It is
versioned (`version: 1`), records the corpus with checksums, and carries 25
cases — 22 answerable (4 of them tagged `difficulty: "hard"`) and 3 negative —
spread across the `policy`, `sla`, `pricing`, `onboarding`, `security` and
`negative` tags. The corpus itself is six markdown files at 221–280 words each
in [`eval/corpus/`](../eval/corpus).

### Corpus entries

Each entry in `corpus[]` pins one file:

| Field | Meaning |
| --- | --- |
| `filename` | File name inside `eval/corpus/` |
| `sha256` | Lower-case hex SHA-256 of the file's raw bytes |

The digest exists so a silently edited source document cannot invalidate a
baseline without anyone noticing. `verify_corpus_files()` recomputes every
digest and aborts the run with `sha256 mismatch` when the bytes drift.

To recalculate digests after an intentional corpus edit:

```powershell
# Windows PowerShell
Get-FileHash -Algorithm SHA256 eval/corpus/*.md
```

```bash
# Linux / macOS
sha256sum eval/corpus/*.md
```

Copy the resulting digests into `corpus[].sha256`. Regenerate baselines at the
same time: changing a source document changes both retrieval and answers.

### Case fields

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier (`q-001`…), unique across the dataset |
| `question` | Query sent to retrieval or to the chat endpoint |
| `tag` | Topic used for the `by_tag` breakdown |
| `difficulty` | `easy` / `medium` / `hard` (reporting only) |
| `top_k` | Optional per-case retrieval depth; `--top-k` overrides it for every case, otherwise `RETRIEVAL_TOP_K` is used |
| `document_ids` | Optional UUID allow-list to scope the search to specific documents |
| `expected_documents` | File names that count as relevant for `hit@k` / `MRR` |
| `expected_evidence` | `{filename, must_contain}` needles for `context_recall` |
| `expected_answer` | Reference answer used by `token_f1` / `rouge_l` |
| `expected_answer_facts` | Literal phrases that must appear in the answer (`fact_coverage`) |
| `answerable` | `false` for negative cases (no documents, no evidence allowed) |
| `notes` | Human-readable rationale |

### `must_contain` rules

`must_contain` is matched as a **substring**, not as a fuzzy phrase. Both the
needle and the haystack pass through `normalize_text()`, which applies:

1. Unicode **NFKC** normalization (compatibility characters are folded);
2. **lower-casing**;
3. **whitespace collapsing** (any run of whitespace becomes a single space).

So `"Within 30   Days of  Purchase"` matches `"within 30 days of purchase"`,
but a hyphen or a word change does not. Keep needles short and literal — one
distinctive phrase per expected fact.

## Metric definitions

All per-case values are averaged over the **answerable** cases unless stated
otherwise. Metrics that need an answer are only computed in `e2e` mode; in
`retrieval` mode they are reported as `null`.

### Retrieval metrics

Let `R` be the ordered list of retrieved file names, `E` the expected
documents, `k` the case depth, and `A` the set of answerable cases.

- **`hit@k`** — `(1 / |A|) · Σ 1[E ∩ R[:k] ≠ ∅]`. `1.0` when at least one
  expected document appears in the first `k` hits, otherwise `0.0`.
- **`MRR`** — mean reciprocal rank: `(1 / |A|) · Σ (1 / rank)`, where `rank` is
  the 1-based position of the first expected document, `0.0` when none matches.
- **`context_recall`** — `(1 / |E_evidence|) · Σ 1[needle ∈ normalize(⋃ excerpts)]`.
  The share of `expected_evidence.must_contain` needles found in the
  concatenated retrieved excerpts. An empty evidence list is vacuously `1.0`.
- **`empty_context_rate`** — share of answerable cases whose retrieval returned
  zero chunks.
- **`false_positive_rate`** — share of negative (`answerable: false`) cases that
  returned **any** context. The ideal value is `0.0`: a question the corpus
  cannot answer should not produce citations.

### Answer metrics (e2e only)

Let `P` be the predicted answer and `C` the retrieved excerpts.

- **`lexical_faithfulness`** — coverage of answer content words by the context
  vocabulary. Stopwords and single-character tokens are dropped; the remaining
  tokens must occur literally in `C`. The documented grounding target is a
  sentence with at least 4 content words whose coverage is **≥ 0.6**; numbers
  and dates are checked by direct occurrence (they are ordinary content tokens).
  An answer with no content tokens scores `1.0`. Cases under `0.5` are listed
  as `answer not grounded in context`.
- **`semantic_faithfulness`** — embedding-based grounding, enabled with
  `--semantic`. Each answer sentence is embedded and compared with every
  retrieved excerpt; a sentence counts as supported when its **maximum cosine
  similarity is ≥ 0.72** (`DEFAULT_SEMANTIC_THRESHOLD`). The metric is the share
  of supported sentences. It needs the configured embedding provider and is
  reported as `null` when embeddings are unavailable.
- **`token_f1`** — multiset token F1 against `expected_answer`, using **all**
  word/number tokens (stopwords included):
  `prec = |P ∩ E| / |P|`, `rec = |P ∩ E| / |E|`,
  `F1 = 2·prec·rec / (prec + rec)`.
- **`rouge_l`** — F-measure over the longest common subsequence of tokens:
  `prec = LCS / |P|`, `rec = LCS / |E|`, `F = 2·prec·rec / (prec + rec)`.
- **`refusal_detected`** — `true` when the normalized answer contains an
  explicit refusal pattern such as `i don't know`, `i cannot answer`,
  `not enough information` or `unable to answer`.
- **`refusal_rate`** — share of **negative** cases whose answer is a refusal.
  A correct negative case refuses instead of hallucinating.
- **`fact_coverage`** — share of `expected_answer_facts` found verbatim
  (after normalization) in the answer.

### Latency and percentile

- **`percentile(values, p)`** — linear interpolation between closest ranks:
  with sorted values, `position = (n - 1) · p / 100`, then interpolate between
  `floor(position)` and `ceil(position)`. This is the NumPy default convention;
  it is the same helper used for `p50` / `p95`, not the bucket approximation
  used by the runtime metrics endpoint.
- **`latency_ms`** — client-measured case duration in milliseconds
  (`mean`/`p50`/`p95`/`max` in `summary.latency_ms`).
- **`server_response_time_ms`** — the `X-Response-Time-Ms` header captured from
  the SSE response in `e2e` mode (TTFB, not the full stream).

Per-case results are aggregated into `summary`, repeated per topic in
`by_tag`, and every failing case is explained in `failures`. Case-level errors
(`error` field) are counted in `error_cases` and make the process exit non-zero.

## Modes: `retrieval` vs `e2e`

| Aspect | `--mode retrieval` | `--mode e2e` |
| --- | --- | --- |
| Path under test | Local pipeline: embed query → `VectorStore.search_chunks` | Public HTTP API: `POST /api/v1/chat/completions` (SSE) |
| Requires PostgreSQL/pgvector | Yes | Yes (behind the API) |
| Requires LLM provider | No | Yes |
| Requires embedding provider | Yes | Yes (the API needs it; `--semantic` adds direct calls) |
| Determinism | High (only embeddings can drift) | Low (sampling, model version, load) |
| Cost | Embeddings only | Embeddings + generation |
| Answer metrics | `null` | Populated |
| Seeds the corpus with `--seed` | Yes, through the upload API | Yes, through the upload API |

Both modes validate the dataset first and check corpus checksums. `--seed` is
idempotent: files already indexed are skipped, files stuck in `failed` must be
deleted before re-seeding. After uploading, the harness polls
`/api/v1/documents/{id}/status` until every file is `completed`, bounded by
`--wait-timeout` (default 180 s).

## Recommended thresholds and CI usage

- Gate **only retrieval** in CI: `--fail-under-hit-rate 0.85` (the `make eval`
  target) is stable because it does not involve the LLM.
- Do **not** gate `lexical_faithfulness`, `semantic_faithfulness`, `token_f1`
  or `rouge_l` until you have at least **5 recorded runs**. LLM output is noisy;
  a single low run is not a regression.
- Compare runs only with identical corpus checksums, chunking settings
  (`CHUNK_SIZE`/`CHUNK_OVERLAP`), embedding model and top-k.
- Treat `error_cases > 0` as a hard failure — the harness already returns exit
  code `1` in that case.

Exit codes: `0` success, `1` threshold breach, case errors or a fatal
environmental error (missing database, unindexed corpus, checksum mismatch).

## Limitations

- **Lexical faithfulness underestimates paraphrase.** A correct answer phrased
  differently from the excerpts scores low; it rewards verbatim grounding.
- **Semantic faithfulness is threshold-sensitive.** A cosine cut-off of `0.72`
  may accept loose paraphrases or reject terse but correct sentences; validate
  it against a sample before trusting the number.
- **`hit@k` depends on chunking.** Smaller chunks can split the answer evidence
  across hits; larger chunks can pull unrelated text into the window. Always
  report the chunking settings with the result.
- **`e2e` is non-deterministic.** Temperature, provider version and load change
  answers between runs. Store the report and compare distributions, not single
  numbers.
- **Rate limits during seeding.** The upload endpoint is rate-limited; the
  harness honours `Retry-After` and retries (up to 20 attempts), so a bulk seed
  can take minutes. Raise `UPLOAD_RATE_LIMIT_REQUESTS` or wait, do not loop
  manually.
- **The runtime metrics endpoint is separate.** `GET /api/v1/metrics` reports
  process-local histograms with bucket-interpolated percentiles; evaluation
  reports use exact per-case samples and `percentile()` interpolation.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `corpus does not match the dataset: ... sha256 mismatch` | Corpus file edited after the digest was recorded | Recompute digests (above) and update `golden_dataset.json` |
| `the retrieval corpus is not fully indexed in PostgreSQL` | The files were never uploaded, or are still processing | Run `python -m src.eval --mode retrieval --seed` and raise `--wait-timeout` |
| `cannot connect to PostgreSQL` | No database or wrong `DATABASE_URL` | Start the stack (`docker compose up -d db`) and check the connection settings |
| `cannot load application settings` | Missing provider credentials | Set them in `.env`; retrieval also needs the embedding provider |
| Run is slow with no `--seed` | Unnecessary re-upload is not the cause — embedding/case volume is | Drop `--seed` when the corpus is already indexed; reduce `--top-k` for a smoke run |
| `e2e` answers differ every run | LLM sampling | Expected; compare aggregates over several runs |
| `429` during seeding | Upload rate limit | Let the harness retry, or tune `UPLOAD_RATE_LIMIT_REQUESTS`/`RATE_LIMIT_WINDOW_SECONDS` |

## Environment constraints

The pure metric helpers and dataset validation are covered by
[`tests/test_eval.py`](../tests/test_eval.py) and run without a database or
network. A real `retrieval` or `e2e` run additionally requires **PostgreSQL with
pgvector** and a configured **embedding provider** (`e2e` also needs an LLM
provider and a running API). In a clean checkout without those dependencies only
the offline checks (and `--help`) are expected to succeed.
