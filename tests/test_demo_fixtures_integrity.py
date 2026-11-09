"""Integrity checks on the committed demo fixtures.

These assertions are the guard rail that keeps the metrics page honest: no
credential-shaped strings, no absolute paths, a dataset checksum that still
matches the repository, and a ``--check`` run that proves the committed
fixtures are exactly what the exporter would produce today.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "demo" / "fixtures"
FIXTURE_FILES = ("metrics_static.json", "retrieval_eval.json", "microbenchmarks.json")
EVAL_REPORTS_DIR = REPO_ROOT / "eval" / "reports"
GOLDEN_DATASET = REPO_ROOT / "eval" / "golden_dataset.json"

FORBIDDEN_SUBSTRINGS = ("api_key", "authorization", "openai_api_key", "sk-")
ABSOLUTE_PATH_MARKERS = ("c:\\", "/home/", "/opt/")
MAX_FIXTURE_BYTES = 32 * 1024


def _read(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_exists_and_is_json(name: str) -> None:
    document = _read(name)

    assert document["schema_version"] == 1
    assert document["kind"] == name.removesuffix(".json")
    assert document["limitations"]
    assert document["provenance"]["git_commit"]
    assert document["provenance"]["source_command"]
    assert set(document["provenance"]["environment"]) >= {
        "python",
        "platform",
        "processor",
        "cpu_count",
    }


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_contains_no_credentials(name: str) -> None:
    text = (FIXTURES_DIR / name).read_text(encoding="utf-8").lower()

    leaks = [needle for needle in FORBIDDEN_SUBSTRINGS if needle in text]
    assert leaks == []


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_contains_no_absolute_paths(name: str) -> None:
    text = (FIXTURES_DIR / name).read_text(encoding="utf-8").lower()

    leaks = [marker for marker in ABSOLUTE_PATH_MARKERS if marker in text]
    assert leaks == []


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_fits_the_size_budget(name: str) -> None:
    size = (FIXTURES_DIR / name).stat().st_size

    assert size <= MAX_FIXTURE_BYTES


def test_dataset_checksum_matches_the_repository() -> None:
    document = _read("retrieval_eval.json")
    dataset = document["payload"]["dataset"]
    expected = hashlib.sha256(GOLDEN_DATASET.read_bytes()).hexdigest()

    assert dataset["sha256"] == expected
    assert dataset["json_bytes"] == GOLDEN_DATASET.stat().st_size
    assert dataset["path"] == "eval/golden_dataset.json"


def test_false_positive_rate_matches_the_eval_report() -> None:
    reports = sorted(EVAL_REPORTS_DIR.glob("retrieval-fixed-*.json"))
    assert reports, "no eval report to compare the fixture against"
    report = json.loads(reports[-1].read_text(encoding="utf-8"))

    fixture = _read("retrieval_eval.json")["payload"]

    assert fixture["summary"]["false_positive_rate"] == report["summary"]["false_positive_rate"]
    assert fixture["negatives"]["false_positive_rate"] == report["summary"]["false_positive_rate"]
    assert fixture["negatives"]["cases"] == report["summary"]["negative_cases"]


def test_retrieval_aggregates_match_the_eval_report() -> None:
    reports = sorted(EVAL_REPORTS_DIR.glob("retrieval-fixed-*.json"))
    report = json.loads(reports[-1].read_text(encoding="utf-8"))
    fixture = _read("retrieval_eval.json")["payload"]

    for key in ("hit_at_k", "mrr", "context_recall", "empty_context_rate"):
        assert fixture["summary"][key] == report["summary"][key]
    assert fixture["summary"]["latency_ms"]["p95"] == report["summary"]["latency_ms"]["p95"]
    assert set(fixture["by_tag"]) == set(report["by_tag"])


def test_microbenchmarks_match_the_benchmark_run() -> None:
    run = json.loads(
        (REPO_ROOT / "benchmarks" / "results" / "latest.json").read_text(encoding="utf-8")
    )
    fixture = _read("microbenchmarks.json")["payload"]

    chunker = run["benchmarks"]["chunker"]["median"]
    stage = next(item for item in fixture["stages"] if item["name"].startswith("chunker"))
    assert stage["p50"] == chunker["p50"]
    assert fixture["platform"]["cpu_count"] == run["meta"]["cpu_count"]


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_limitations_are_honest(name: str) -> None:
    limitations = _read(name)["limitations"]
    lowered = " ".join(limitations).lower()

    assert len(limitations) >= 2
    assert "not an industry benchmark" in lowered
    assert "not an sla" in lowered


def test_export_check_passes() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "demo.export_fixtures", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert "ok:" in completed.stdout.lower()
