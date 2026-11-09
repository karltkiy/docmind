"""Streamlit rendering for the demo's *Metrics & evidence* view.

The page shows two kinds of numbers and nothing else:

* committed measurement fixtures (see :mod:`demo.metrics_data` and
  ``demo/fixtures/README.md``), and
* one live snapshot from ``GET /api/v1/metrics``, which reads the in-process
  Prometheus registry and never touches a database, a filesystem or a model.

Rendering therefore costs **$0** in provider tokens, and the page never calls
the chat endpoint.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from demo.metrics_data import (
    DEFAULT_MAX_AGE_DAYS,
    Fixture,
    FixtureError,
    LiveMetrics,
    age_days,
    format_ms,
    format_percent,
    format_uptime,
    is_stale,
    last_error,
    last_status,
    load_fixture,
    load_live_metrics,
)

HERO = (
    "Every number comes from committed measurement fixtures or from "
    "`GET /api/v1/metrics`, which performs no model calls. "
    "Rendering this page costs $0 in provider tokens."
)

REPRODUCE_COMMANDS = """# 1. Retrieval quality on the 25-case golden dataset (needs a live stack)
python -m src.eval --mode retrieval --fail-under-hit-rate 0.85 --json-only

# 2. Static metrics: LOC, endpoints, dependencies, tests, coverage
python -m benchmarks.static_metrics --with-coverage

# 3. In-process micro-benchmarks (no network, no model calls)
python -m benchmarks.bench --iterations 2000 --warmup 200

# 4. Rebuild the fixtures the page reads, then verify them
python -m demo.export_fixtures
python -m demo.export_fixtures --check"""

_WORKER_CAPTION = (
    "Ingestion histograms are recorded inside the Arq worker process, so the API "
    "snapshot never contains them: the `ingestion_jobs` counters below are only "
    "non-zero when this very API process handled an upload."
)


def _fixture(kind: str, label: str) -> Fixture | None:
    """Load one fixture, reporting a readable error instead of crashing."""
    try:
        return load_fixture(kind)
    except FixtureError as exc:
        st.error(f"The **{label}** fixture could not be loaded: {exc}")
        return None


@st.cache_data(ttl=30, show_spinner=False)
def _live_snapshot() -> LiveMetrics | None:
    """Cached ``GET /api/v1/metrics`` call (cleared by the Refresh button)."""
    return load_live_metrics()


def _number(value: object, template: str = "{:.4f}") -> str:
    """Format a float defensively; ``None`` becomes ``n/a``."""
    if value is None:
        return "n/a"
    try:
        return template.format(float(value))
    except (TypeError, ValueError):
        return "n/a"


def _ratio(value: object, *, digits: int = 1) -> str:
    """Format a 0..1 ratio as a percentage."""
    if value is None:
        return "n/a"
    try:
        return format_percent(float(value), digits=digits)
    except (TypeError, ValueError):
        return "n/a"


def _ingestion_card(micro: Fixture | None) -> tuple[str, str]:
    """Return ``(value, help)`` for the ingestion card.

    Ingestion runs in the worker process, so the committed fixtures cannot
    contain it; the card says so rather than inventing a number.
    """
    for entry in (micro.payload.get("not_measured") if micro else None) or []:
        metric = str(entry.get("metric", ""))
        if "ingestion" in metric.lower():
            return "n/a", str(entry.get("reason", ""))
    return "n/a", "Ingestion runs in the worker process and is not measured in-process."


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _render_overview(
    static: Fixture | None, retrieval: Fixture | None, micro: Fixture | None
) -> None:
    """Headline metric cards: quality, latency, engineering."""
    summary = (retrieval.payload.get("summary") if retrieval else None) or {}
    latency = summary.get("latency_ms") or {}
    coverage = (static.payload.get("coverage") if static else None) or {}
    tests = (static.payload.get("tests") if static else None) or {}
    api = (static.payload.get("api") if static else None) or {}
    ingestion_value, ingestion_help = _ingestion_card(micro)

    first_row = st.columns(4)
    first_row[0].metric(
        "hit@k",
        _ratio(summary.get("hit_at_k")),
        help="Share of answerable cases whose expected document is in the top-k context.",
    )
    first_row[1].metric(
        "MRR",
        _number(summary.get("mrr")),
        help="Mean reciprocal rank of the first relevant document (1.0 is best).",
    )
    first_row[2].metric(
        "Context recall",
        _ratio(summary.get("context_recall")),
        help="Share of expected evidence strings found in the retrieved context.",
    )
    first_row[3].metric(
        "Retrieval p95",
        format_ms(latency.get("p95")),
        help="95th percentile retrieval latency from the eval run (not an SLA).",
    )

    second_row = st.columns(4)
    second_row[0].metric("Ingestion median", ingestion_value, help=ingestion_help)
    second_row[1].metric(
        "Tests collected",
        str(tests.get("collected", "n/a")),
        help="Pytest collection count recorded by the static-metrics run.",
    )
    second_row[2].metric(
        "Coverage (src/)",
        f"{_number(coverage.get('coverage_percent_exact'), '{:.2f}')}%",
        help="Line+branch coverage of src/ at the time of the measurement.",
    )
    second_row[3].metric(
        "API endpoints",
        str(api.get("endpoint_count", "n/a")),
        help="APIRoute endpoints discovered by walking app.routes.",
    )


def _render_retrieval_quality(retrieval: Fixture | None) -> None:
    """Per-tag retrieval table plus the dataset/limitations expander."""
    st.subheader("Retrieval quality")
    if retrieval is None:
        st.caption("No retrieval fixture is available, so this section is empty.")
        return

    payload = retrieval.payload
    by_tag = payload.get("by_tag") or {}
    dataset = payload.get("dataset") or {}
    configuration = payload.get("configuration") or {}
    negatives = payload.get("negatives") or {}
    summary = payload.get("summary") or {}

    rows = [
        {
            "tag": tag,
            "cases": block.get("cases"),
            "hit@k": _ratio(block.get("hit_at_k")),
            "MRR": _number(block.get("mrr")),
            "context recall": _ratio(block.get("context_recall")),
            "false positive rate": _ratio(block.get("false_positive_rate")),
            "latency p50": format_ms((block.get("latency_ms") or {}).get("p50")),
            "latency p95": format_ms((block.get("latency_ms") or {}).get("p95")),
        }
        for tag, block in by_tag.items()
    ]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True)
    else:
        st.caption("The fixture carries no per-tag breakdown.")

    st.caption(
        f"{dataset.get('cases', 0)} self-built cases "
        f"({dataset.get('answerable', 0)} answerable, {dataset.get('negative', 0)} negative) "
        f"over {dataset.get('corpus_documents', 0)} documents — a smoke-level regression "
        "signal, not an industry benchmark."
    )
    false_positive_rate = summary.get("false_positive_rate")
    if false_positive_rate is not None:
        st.caption(
            f"False-positive rate {_ratio(false_positive_rate)}: "
            f"{negatives.get('returned_context', 0)} of {negatives.get('cases', 0)} negative "
            "cases still received context, because the product has no similarity cut-off."
        )

    with st.expander("Dataset & limitations"):
        st.markdown(
            "\n".join(
                [
                    f"- **Dataset**: `{dataset.get('path', 'n/a')}` "
                    f"(version {dataset.get('version', 'n/a')}, "
                    f"{dataset.get('json_bytes', 0)} bytes)",
                    f"- **Dataset SHA-256**: `{dataset.get('sha256', 'n/a')}`",
                    f"- **top_k**: {configuration.get('top_k', 'n/a')}",
                    f"- **chunk_size / chunk_overlap**: "
                    f"{configuration.get('chunk_size', 'n/a')} / "
                    f"{configuration.get('chunk_overlap', 'n/a')} words",
                    f"- **Embedding**: {configuration.get('embedding_provider', 'n/a')} "
                    f"`{configuration.get('embedding_model', 'n/a')}` "
                    f"({configuration.get('embedding_dim', 'n/a')} dims)",
                    f"- **Generation model (not used in retrieval mode)**: "
                    f"{configuration.get('llm_provider', 'n/a')} "
                    f"`{configuration.get('llm_model', 'n/a')}`",
                ]
            )
        )
        st.markdown("**Limitations stated by the fixture**")
        for limitation in retrieval.limitations:
            st.markdown(f"- {limitation}")


def _stage_ms(value: object, unit: str) -> float | None:
    """Convert a stage median to milliseconds so one chart can compare stages."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / 1000.0 if unit.startswith("µs") else number


def _render_latency(retrieval: Fixture | None, micro: Fixture | None) -> None:
    """Stage timings as a table plus a comparable bar chart."""
    st.subheader("Latency")
    stages = (micro.payload.get("stages") if micro else None) or []
    if not stages:
        st.caption("No micro-benchmark fixture is available, so this section is empty.")
        return

    rows = [
        {
            "stage": stage.get("name"),
            "unit": stage.get("unit"),
            "mean": _number(stage.get("mean"), "{:,.3f}"),
            "p50": _number(stage.get("p50"), "{:,.3f}"),
            "p95": _number(stage.get("p95"), "{:,.3f}"),
        }
        for stage in stages
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True)

    chart_rows = [
        {
            "stage": str(stage.get("name")),
            "p50 (ms)": _stage_ms(stage.get("p50"), str(stage.get("unit", ""))),
            "p95 (ms)": _stage_ms(stage.get("p95"), str(stage.get("unit", ""))),
        }
        for stage in stages
    ]
    chart = pd.DataFrame(chart_rows).set_index("stage")
    st.bar_chart(chart)
    st.caption(
        "µs stages are converted to ms so the chart shares one axis. "
        "In-process timings from a single machine — a signal, not an SLA."
    )

    if retrieval is not None:
        latency = (retrieval.payload.get("summary") or {}).get("latency_ms") or {}
        st.caption(
            "Retrieval over the golden dataset: "
            f"mean {format_ms(latency.get('mean'))}, "
            f"p50 {format_ms(latency.get('p50'))}, "
            f"p95 {format_ms(latency.get('p95'))}, "
            f"max {format_ms(latency.get('max'))}."
        )


def _render_engineering(static: Fixture | None) -> None:
    """Code size, dependency and CI-run facts."""
    st.subheader("Engineering & CI")
    if static is None:
        st.caption("No static-metrics fixture is available, so this section is empty.")
        return

    payload = static.payload
    loc = payload.get("loc") or {}
    areas = loc.get("areas") or []
    if areas:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "area": area.get("area"),
                        "files": area.get("files"),
                        "total lines": area.get("total_lines"),
                        "non-empty": area.get("non_empty_lines"),
                        "non-comment": area.get("non_comment_lines"),
                    }
                    for area in areas
                ]
            ),
            hide_index=True,
        )
    st.caption(str(loc.get("methodology", "")))

    dependencies = payload.get("dependencies") or {}
    prometheus = payload.get("prometheus") or {}
    make = payload.get("make") or {}
    st.markdown(
        "\n".join(
            [
                f"- **Dependencies**: {dependencies.get('runtime', 0)} runtime, "
                f"{dependencies.get('dev', 0)} dev, {dependencies.get('demo', 0)} demo",
                f"- **Prometheus**: {prometheus.get('families_docmind', 0)} DocMind "
                f"families of {prometheus.get('families_total', 0)} total",
                f"- **Make targets**: {make.get('target_count', 0)}",
            ]
        )
    )
    with st.expander("Make targets"):
        st.write(", ".join(str(target) for target in make.get("targets") or []) or "n/a")


def _render_live_counters() -> None:
    """Live ``GET /api/v1/metrics`` snapshot with an explicit refresh."""
    st.subheader("Live counters")
    st.caption(
        "Read from `GET /api/v1/metrics`: an in-process Prometheus snapshot. "
        "The endpoint performs no database, LLM or filesystem access."
    )

    if st.button("Refresh live counters"):
        _live_snapshot.clear()

    live = _live_snapshot()
    if live is None:
        status = last_status()
        reason = last_error() or "The API did not answer."
        if status in (401, 403):
            st.warning(
                f"{reason} Set `DOCMIND_API_KEY` for the Streamlit container to see "
                "live counters; the value is never displayed or logged."
            )
        else:
            st.warning(
                f"Live counters are unavailable: {reason} "
                "Every number above still comes from the committed fixtures."
            )
        return

    row = st.columns(4)
    row[0].metric("Uptime", format_uptime(live.uptime_seconds))
    row[1].metric("Ready", "yes" if live.ready else "no")
    row[2].metric("Version", live.version)
    row[3].metric(
        "HTTP requests",
        str(live.totals.get("http_requests_total", 0)),
        help="Requests observed by this API process since it started.",
    )
    st.caption(
        f"Environment `{live.environment}` · snapshot generated "
        f"{live.generated_at.isoformat(timespec='seconds')}"
    )

    if live.counters_disabled:
        st.info(
            f"The API has been up for {format_uptime(live.uptime_seconds)} but has "
            "recorded no HTTP requests, so the counters below are still at zero. "
            "That is expected for a freshly started process, not an error."
        )

    if live.endpoints:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "method": endpoint.method,
                        "path": endpoint.path,
                        "requests": endpoint.requests,
                        "p95": format_ms(endpoint.p95_ms),
                    }
                    for endpoint in live.endpoints
                ]
            ),
            hide_index=True,
        )
    st.caption(_WORKER_CAPTION)


def _render_reproduction() -> None:
    """The exact commands that regenerate every number on the page."""
    st.subheader("How to reproduce")
    st.code(REPRODUCE_COMMANDS, language="bash")


def _render_provenance(fixtures: list[Fixture]) -> None:
    """Commit, timestamps, models and hardware behind the fixtures."""
    st.subheader("Provenance")
    if not fixtures:
        st.caption("No fixture provenance to show.")
        return

    rows = []
    for fixture in fixtures:
        provenance = fixture.provenance
        rows.append(
            {
                "fixture": fixture.kind,
                "commit": provenance.git_commit[:7],
                "dirty tree": "yes" if provenance.git_dirty else "no",
                "measured at": fixture.generated_at.isoformat(timespec="seconds"),
                "age (days)": f"{age_days(fixture.generated_at):.1f}",
                "source command": provenance.source_command,
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True)

    environment = fixtures[0].provenance.environment
    st.caption(
        f"Hardware: {environment.get('processor', 'n/a')} · "
        f"{environment.get('cpu_count', 'n/a')} logical CPUs · "
        f"{environment.get('platform', 'n/a')} · Python {environment.get('python', 'n/a')}"
    )
    st.caption(
        "A short commit plus the export timestamp identify the exact working tree. "
        "'dirty tree = yes' means the measurement artifacts were produced from a "
        "checkout with local modifications."
    )

    stale = [fixture for fixture in fixtures if is_stale(fixture)]
    if stale:
        names = ", ".join(fixture.kind for fixture in stale)
        st.warning(
            f"Stale fixtures: {names}. They are older than the configured maximum age "
            f"(default {DEFAULT_MAX_AGE_DAYS} days, override with "
            "`DOCMIND_FIXTURE_MAX_AGE_DAYS`). Re-run `python -m demo.export_fixtures` "
            "after re-measuring before quoting these numbers."
        )


def render_metrics_page() -> None:
    """Render the whole *Metrics & evidence* view."""
    st.header("Metrics & evidence")
    st.info(HERO)

    static = _fixture("metrics_static", "static metrics")
    retrieval = _fixture("retrieval_eval", "retrieval evaluation")
    micro = _fixture("microbenchmarks", "micro-benchmarks")

    _render_overview(static, retrieval, micro)
    st.divider()
    _render_retrieval_quality(retrieval)
    st.divider()
    _render_latency(retrieval, micro)
    st.divider()
    _render_engineering(static)
    st.divider()
    _render_live_counters()
    st.divider()
    _render_reproduction()
    st.divider()
    _render_provenance([fixture for fixture in (static, retrieval, micro) if fixture is not None])
