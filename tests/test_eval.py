"""Unit tests for the offline evaluation harness.

These tests never touch PostgreSQL, the embedding provider or the HTTP API:
they exercise the pure metric helpers and the static golden dataset only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src import eval as eval_module
from src.eval import (
    DatasetError,
    context_recall,
    cosine_similarity,
    empty_context_rate,
    fact_coverage,
    false_positive_rate,
    hit_at_k,
    lexical_faithfulness,
    load_dataset,
    normalize_text,
    percentile,
    reciprocal_rank,
    refusal_detected,
    retrieved_filenames,
    rouge_l,
    semantic_faithfulness,
    token_f1,
    validate_dataset,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "eval" / "golden_dataset.json"
CORPUS_DIR = ROOT / "eval" / "corpus"


# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------
def test_normalize_text_lowercases_and_collapses_whitespace() -> None:
    assert normalize_text("  REPUND   Policy\n") == "repund policy"
    assert normalize_text("ＲＥＦＵＮＤ\tPolicy") == "refund policy"


# ----------------------------------------------------------------------
# Retrieval metrics
# ----------------------------------------------------------------------
def _hits(ids: list[str], mapping: dict[str, str]) -> list[str]:
    results = [
        SimpleNamespace(chunk=SimpleNamespace(document_id=doc_id), score=1.0 - index / 10)
        for index, doc_id in enumerate(ids)
    ]
    return retrieved_filenames(results, mapping)


def test_retrieved_filenames_preserves_order() -> None:
    names = _hits(["doc-b", "doc-a"], {"doc-a": "a.md", "doc-b": "b.md"})
    assert names == ["b.md", "a.md"]


def test_hit_at_k_respects_the_cutoff() -> None:
    names = _hits(["doc-b", "doc-a"], {"doc-a": "a.md", "doc-b": "b.md"})
    assert hit_at_k(names, ["a.md"], 1) == 0.0
    assert hit_at_k(names, ["a.md"], 2) == 1.0
    assert hit_at_k(names, ["a.md"], 0) == 0.0
    assert hit_at_k(names, [], 4) == 0.0


def test_reciprocal_rank_uses_the_first_relevant_hit() -> None:
    names = _hits(["doc-b", "doc-a"], {"doc-a": "a.md", "doc-b": "b.md"})
    assert reciprocal_rank(names, ["a.md"]) == pytest.approx(0.5)
    assert reciprocal_rank(names, ["a.md", "b.md"]) == 1.0
    assert reciprocal_rank(names, ["missing.md"]) == 0.0
    assert reciprocal_rank(names, []) == 0.0


def test_context_recall_counts_present_evidence() -> None:
    contexts = ["Refunds are issued within TEN business days."]
    assert context_recall(["ten business days"], contexts) == 1.0
    assert context_recall(["ten business days"], ["nothing relevant"]) == 0.0
    assert context_recall(["ten business days", "missing fact"], contexts) == 0.5
    assert context_recall([], contexts) == 1.0


def test_empty_context_and_false_positive_rates() -> None:
    assert empty_context_rate([True, False, False, True]) == 0.5
    assert empty_context_rate([]) == 0.0
    assert false_positive_rate([False, False, True]) == pytest.approx(1 / 3)
    assert false_positive_rate([]) == 0.0


# ----------------------------------------------------------------------
# Answer metrics
# ----------------------------------------------------------------------
def test_lexical_faithfulness_grounded_answer_scores_one() -> None:
    contexts = ["Refunds are issued within ten business days."]
    answer = "Refunds are issued within ten business days."
    assert lexical_faithfulness(answer, contexts) == 1.0


def test_lexical_faithfulness_hallucination_scores_zero() -> None:
    contexts = ["Refunds are issued within ten business days."]
    answer = "Banana smoothies require frozen fruit."
    assert lexical_faithfulness(answer, contexts) == 0.0


def test_semantic_faithfulness_uses_the_cosine_threshold() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    assert semantic_faithfulness([[1.0, 0.0]], [[1.0, 0.0]], threshold=0.72) == 1.0
    assert semantic_faithfulness([[1.0, 0.0]], [[0.0, 1.0]], threshold=0.72) == 0.0
    assert semantic_faithfulness([[1.0, 0.2]], [[1.0, 0.0]], threshold=0.99) == 0.0
    assert semantic_faithfulness([[1.0, 0.2]], [[1.0, 0.0]], threshold=0.72) == 1.0
    assert semantic_faithfulness([], [[1.0, 0.0]]) == 1.0
    assert semantic_faithfulness([[1.0, 0.0]], []) == 0.0


def test_token_f1_and_rouge_l() -> None:
    reference = "Refunds are issued within ten business days"
    assert token_f1(reference, reference) == 1.0
    assert token_f1("banana smoothie", "refund window") == 0.0
    assert token_f1("", "") == 1.0
    assert token_f1("", reference) == 0.0

    assert rouge_l(reference, reference) == 1.0
    assert rouge_l("banana smoothie", "refund window") == 0.0
    assert rouge_l("", "") == 1.0
    assert rouge_l("", reference) == 0.0


def test_refusal_detected() -> None:
    assert refusal_detected("I don't know.") is True
    assert refusal_detected("I do not know the answer.") is True
    assert refusal_detected("Refunds are issued within ten business days.") is False
    assert refusal_detected("") is False


def test_fact_coverage() -> None:
    answer = "The refund is issued within ten business days to the original method."
    assert fact_coverage(answer, ["ten business days"]) == 1.0
    assert fact_coverage(answer, ["ten business days", "30 days"]) == 0.5
    assert fact_coverage(answer, []) == 1.0


# ----------------------------------------------------------------------
# Report helpers
# ----------------------------------------------------------------------
def test_percentile_linear_interpolation() -> None:
    assert percentile([1, 2, 3, 4], 0) == 1.0
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 100) == 4.0
    assert percentile([10, 20, 30, 40], 95) == pytest.approx(38.5)
    assert percentile([42], 95) == 42.0
    assert percentile([], 50) == 0.0
    with pytest.raises(ValueError):
        percentile([1, 2, 3], 101)


# ----------------------------------------------------------------------
# Dataset validation
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def raw_dataset() -> dict[str, Any]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def test_dataset_has_expected_shape(raw_dataset: dict[str, Any]) -> None:
    cases = raw_dataset["cases"]
    assert 20 <= len(cases) <= 30
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))

    answerable = [case for case in cases if case["answerable"]]
    negative = [case for case in cases if not case["answerable"]]
    assert len(answerable) == 22
    assert len(negative) == 3
    assert sum(1 for case in cases if case["difficulty"] == "hard") == 4
    assert {case["tag"] for case in cases} == {
        "policy",
        "sla",
        "pricing",
        "onboarding",
        "security",
        "negative",
    }


def test_negative_cases_are_declared_consistently(raw_dataset: dict[str, Any]) -> None:
    for case in raw_dataset["cases"]:
        if case["answerable"]:
            continue
        assert case["tag"] == "negative"
        assert case["expected_documents"] == []
        assert case["expected_evidence"] == []
        assert case["expected_answer"] == "I don't know."


def test_evidence_exists_in_the_corpus(raw_dataset: dict[str, Any]) -> None:
    for case in raw_dataset["cases"]:
        for evidence in case["expected_evidence"]:
            path = CORPUS_DIR / evidence["filename"]
            assert path.is_file(), f"{case['id']}: missing corpus file {path.name}"
            haystack = normalize_text(path.read_text(encoding="utf-8"))
            needle = evidence["must_contain"]
            assert needle == normalize_text(needle), f"{case['id']}: must_contain not normalised"
            assert needle in haystack, f"{case['id']}: {needle!r} absent from {path.name}"


def test_corpus_checksums_match_the_files(raw_dataset: dict[str, Any]) -> None:
    recorded = {entry["filename"]: entry["sha256"] for entry in raw_dataset["corpus"]}
    on_disk = {path.name for path in CORPUS_DIR.glob("*.md")}
    assert set(recorded) == on_disk
    for filename, checksum in recorded.items():
        actual = hashlib.sha256((CORPUS_DIR / filename).read_bytes()).hexdigest()
        assert actual == checksum, f"{filename}: stale sha256 in the dataset"


def test_load_dataset_returns_typed_cases(raw_dataset: dict[str, Any]) -> None:
    dataset = load_dataset(DATASET_PATH)
    assert dataset.version == raw_dataset["version"]
    assert len(dataset.cases) == len(raw_dataset["cases"])
    assert len(dataset.corpus) == len(raw_dataset["corpus"])
    assert eval_module.DEFAULT_SEMANTIC_THRESHOLD == pytest.approx(0.72)


def _raw_case(case_id: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "question": "Question?",
        "tag": "policy",
        "expected_documents": ["refund-policy.md"],
        "expected_evidence": [],
        "expected_answer": "Answer.",
        "answerable": True,
    }


def _raw_dataset(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "corpus": [{"filename": "refund-policy.md", "sha256": "0" * 64}],
        "cases": cases,
    }


def test_validate_dataset_rejects_duplicate_ids() -> None:
    with pytest.raises(DatasetError, match="duplicate id"):
        validate_dataset(_raw_dataset([_raw_case("dup"), _raw_case("dup")]))


def test_validate_dataset_rejects_empty_cases() -> None:
    empty_cases: dict[str, Any] = {
        "version": 1,
        "corpus": [{"filename": "a.md", "sha256": "0"}],
        "cases": [],
    }
    with pytest.raises(DatasetError, match="non-empty list"):
        validate_dataset(empty_cases)


def test_validate_dataset_ignores_unknown_fields() -> None:
    case = _raw_case("q-unknown")
    case["future_field"] = {"anything": True}
    validate_dataset(_raw_dataset([case]))


def test_parser_exposes_every_documented_flag() -> None:
    """The CLI surfaces the flags documented in README and eval/README."""
    parser = eval_module.build_parser()
    flags = {option for action in parser._actions for option in action.option_strings}

    assert {
        "--dataset",
        "--corpus-dir",
        "--out",
        "--mode",
        "--top-k",
        "--base-url",
        "--seed",
        "--wait-timeout",
        "--fail-under-hit-rate",
        "--semantic",
        "--json-only",
    } <= flags


def test_module_imports_without_application_environment() -> None:
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"DATABASE_URL", "POSTGRES_PASSWORD", "OPENAI_API_KEY"}
    }
    completed = subprocess.run(
        [sys.executable, "-c", "import src.eval"],
        cwd=ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
