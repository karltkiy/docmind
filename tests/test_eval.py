"""Unit tests for the offline evaluation harness.

These tests never touch PostgreSQL, the embedding provider or the HTTP API:
they exercise the pure metric helpers and the static golden dataset only.
"""

from __future__ import annotations

import argparse
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
    content_tokens,
    context_recall,
    cosine_similarity,
    empty_context_rate,
    fact_coverage,
    false_positive_rate,
    file_sha256,
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
    split_sentences,
    token_f1,
    validate_dataset,
    word_tokens,
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
        actual = file_sha256(CORPUS_DIR / filename)
        assert actual == checksum, f"{filename}: stale sha256 in the dataset"


def test_file_sha256_is_independent_of_line_endings(tmp_path: Path) -> None:
    """Checksums must not depend on Git's checkout line endings (CRLF vs LF)."""
    lf_path = tmp_path / "lf.md"
    crlf_path = tmp_path / "crlf.md"
    lf_path.write_bytes(b"first line\nsecond line\n")
    crlf_path.write_bytes(b"first line\r\nsecond line\r\n")

    assert file_sha256(lf_path) == file_sha256(crlf_path)
    assert file_sha256(lf_path) == hashlib.sha256(b"first line\nsecond line\n").hexdigest()


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


# ----------------------------------------------------------------------
# Score aggregation and report rendering
# ----------------------------------------------------------------------
def _golden_case(
    case_id: str = "q-1",
    *,
    tag: str = "policy",
    answerable: bool = True,
    top_k: int | None = None,
    evidence: tuple[tuple[str, str], ...] = (),
    facts: tuple[str, ...] = (),
) -> eval_module.GoldenCase:
    return eval_module.GoldenCase(
        id=case_id,
        question="How long do refunds take?",
        tag=tag,
        difficulty="easy",
        top_k=top_k,
        document_ids=None,
        expected_documents=("refund-policy.md",) if answerable else (),
        expected_evidence=tuple(
            eval_module.Evidence(filename=name, must_contain=needle) for name, needle in evidence
        ),
        expected_answer="Refunds take ten business days.",
        expected_answer_facts=facts,
        answerable=answerable,
        notes="",
    )


def _chunk(filename: str, excerpt: str) -> dict[str, Any]:
    return {"filename": filename, "chunk_index": 0, "score": 0.9, "excerpt": excerpt}


def test_text_helpers_split_sentences_and_drop_stopwords() -> None:
    assert split_sentences("First one. Second one!\nThird?") == [
        "First one.",
        "Second one!",
        "Third?",
    ]
    assert word_tokens("Refunds ARE issued, 10 days!") == ["refunds", "are", "issued", "10", "days"]
    assert content_tokens("and the refund is ready") == ["refund", "ready"]
    assert retrieved_filenames([SimpleNamespace()], {}) == []


def test_score_case_without_answer_leaves_answer_metrics_empty() -> None:
    case = _golden_case(evidence=(("refund-policy.md", "ten business days"),))
    result = eval_module._score_case(
        case,
        top_k=4,
        retrieved=[_chunk("refund-policy.md", "Refunds take ten business days.")],
        answer=None,
        latency_ms=12.34,
    )

    assert result.hit == 1.0
    assert result.context_recall == 1.0
    assert result.lexical_faithfulness is None
    assert result.to_dict()["latency_ms"] == 12.3


def test_score_case_with_answer_fills_answer_metrics() -> None:
    case = _golden_case(facts=("ten business days",))
    result = eval_module._score_case(
        case,
        top_k=2,
        retrieved=[_chunk("refund-policy.md", "Refunds take ten business days.")],
        answer="Refunds take ten business days.",
        latency_ms=50.0,
        response_time_ms=64.0,
        semantic=1.0,
    )

    assert result.semantic_faithfulness == 1.0
    assert result.token_f1 == 1.0
    assert result.fact_coverage == 1.0
    assert result.refusal_detected is False


def test_score_case_prefers_full_content_over_truncated_excerpt() -> None:
    """Evidence past the 300-character excerpt boundary must still be found."""
    full_text = ("padding " * 60) + "Refunds take ten business days."
    assert "ten business days" not in full_text[:300]

    case = _golden_case(evidence=(("refund-policy.md", "ten business days"),))
    with_content = eval_module._score_case(
        case,
        top_k=1,
        retrieved=[
            {
                "filename": "refund-policy.md",
                "chunk_index": 0,
                "score": 0.9,
                "excerpt": full_text[:300],
                "_content": full_text,
            }
        ],
        answer=None,
        latency_ms=1.0,
    )
    assert with_content.context_recall == 1.0

    excerpt_only = eval_module._score_case(
        case,
        top_k=1,
        retrieved=[_chunk("refund-policy.md", full_text[:300])],
        answer=None,
        latency_ms=1.0,
    )
    assert excerpt_only.context_recall == 0.0


def test_case_result_to_dict_strips_private_retrieval_keys() -> None:
    result = eval_module._score_case(
        _golden_case(evidence=(("refund-policy.md", "ten business days"),)),
        top_k=1,
        retrieved=[
            {
                "filename": "refund-policy.md",
                "chunk_index": 0,
                "score": 0.9,
                "excerpt": "Refunds take ten business days.",
                "_content": "Refunds take ten business days. " + "x" * 500,
            }
        ],
        answer=None,
        latency_ms=1.0,
    )

    payload = result.to_dict()
    assert payload["retrieved"] == [
        {
            "filename": "refund-policy.md",
            "chunk_index": 0,
            "score": 0.9,
            "excerpt": "Refunds take ten business days.",
        }
    ]
    assert all(not key.startswith("_") for item in payload["retrieved"] for key in item)


def _scored_cases() -> tuple[eval_module.CaseResult, eval_module.CaseResult]:
    good = eval_module._score_case(
        _golden_case("q-good", facts=("ten business days",)),
        top_k=4,
        retrieved=[_chunk("refund-policy.md", "Refunds take ten business days.")],
        answer="Refunds take ten business days.",
        latency_ms=10.0,
        response_time_ms=20.0,
    )
    negative = eval_module._score_case(
        _golden_case("q-negative", tag="negative", answerable=False),
        top_k=4,
        retrieved=[],
        answer="I don't know.",
        latency_ms=30.0,
    )
    return good, negative


def test_aggregate_splits_answerable_and_negative_cases() -> None:
    good, negative = _scored_cases()
    summary = eval_module.aggregate([good, negative], with_answers=True)

    assert summary["cases"] == 2
    assert summary["answerable_cases"] == 1
    assert summary["negative_cases"] == 1
    assert summary["error_cases"] == 0
    assert summary["hit_at_k"] == 1.0
    assert summary["empty_context_rate"] == 0.0
    assert summary["false_positive_rate"] == 0.0
    assert summary["refusal_rate"] == 1.0
    assert summary["latency_ms"] == {"mean": 20.0, "p50": 20.0, "p95": 29.0, "max": 30.0}
    assert summary["server_response_time_ms"] == {
        "mean": 20.0,
        "p50": 20.0,
        "p95": 20.0,
        "max": 20.0,
    }


def test_aggregate_without_answers_keeps_answer_metrics_empty() -> None:
    good, negative = _scored_cases()
    summary = eval_module.aggregate([good, negative], with_answers=False)

    assert summary["lexical_faithfulness"] is None
    assert summary["token_f1"] is None
    assert summary["fact_coverage"] is None


def test_aggregate_by_tag_and_failures_localise_regressions() -> None:
    good, negative = _scored_cases()
    missing = eval_module._score_case(
        _golden_case("q-missing", tag="sla"),
        top_k=4,
        retrieved=[_chunk("other.md", "unrelated")],
        answer="Completely different words here.",
        latency_ms=5.0,
    )
    failing = eval_module.CaseResult(
        case=_golden_case("q-error"),
        top_k=1,
        retrieved=[],
        error="boom",
    )

    by_tag = eval_module.aggregate_by_tag([good, negative, missing, failing], with_answers=True)
    assert sorted(by_tag) == ["negative", "policy", "sla"]
    assert by_tag["policy"]["cases"] == 2
    assert by_tag["sla"]["cases"] == 1
    assert by_tag["negative"]["cases"] == 1

    failures = eval_module.collect_failures([good, negative, missing, failing], with_answers=True)
    reasons = {failure["id"]: failure["reason"] for failure in failures}
    assert reasons["q-error"] == "boom"
    assert "expected document not retrieved" in reasons["q-missing"]
    assert "answer not grounded in context" in reasons["q-missing"]
    assert "q-good" not in reasons


def test_collect_failures_flags_negative_cases_that_answer() -> None:
    leaking = eval_module._score_case(
        _golden_case("q-leak", tag="negative", answerable=False),
        top_k=4,
        retrieved=[_chunk("refund-policy.md", "Refunds take ten business days.")],
        answer="Refunds take ten business days.",
        latency_ms=1.0,
    )
    reasons = {
        item["id"]: item["reason"]
        for item in eval_module.collect_failures([leaking], with_answers=True)
    }

    assert "negative case returned context" in reasons["q-leak"]
    assert "non-refusal answer" in reasons["q-leak"]


def test_build_report_and_render_summary() -> None:
    good, negative = _scored_cases()
    report = eval_module.build_report({"mode": "retrieval"}, [good, negative], with_answers=True)

    assert report["meta"] == {"mode": "retrieval"}
    assert report["summary"]["cases"] == 2
    assert report["by_tag"]["policy"]["cases"] == 1
    assert [case["id"] for case in report["cases"]] == ["q-good", "q-negative"]

    text = eval_module.render_summary(report)
    assert "DocMind evaluation: mode=retrieval cases=2" in text
    assert "hit_at_k" in text
    assert "latency ms p50/p95/max" in text
    assert "failures" in text


def test_ensure_corpus_indexed_reports_missing_and_failed_documents() -> None:
    dataset = eval_module.GoldenDataset(
        version=1,
        corpus=(eval_module.CorpusEntry(filename="refund-policy.md", sha256="0" * 64),),
        cases=(_golden_case(),),
        raw={},
    )
    indexed = eval_module.DocumentIndex(
        completed={"refund-policy.md": "doc-1"},
        name_by_id={"doc-1": "refund-policy.md"},
        failed=set(),
    )
    eval_module._ensure_corpus_indexed(dataset, indexed)

    broken = eval_module.DocumentIndex(completed={}, name_by_id={}, failed={"refund-policy.md"})
    with pytest.raises(eval_module.EvalError, match="not fully indexed") as excinfo:
        eval_module._ensure_corpus_indexed(dataset, broken)
    assert "Failed documents: refund-policy.md" in str(excinfo.value)


def test_resolve_top_k_prefers_cli_then_case_then_settings() -> None:
    settings = SimpleNamespace(RETRIEVAL_TOP_K=7)
    args = argparse.Namespace(top_k=3)
    assert eval_module._resolve_top_k(_golden_case(top_k=5), args, settings) == 3

    args = argparse.Namespace(top_k=None)
    assert eval_module._resolve_top_k(_golden_case(top_k=5), args, settings) == 5
    assert eval_module._resolve_top_k(_golden_case(), args, settings) == 7


def test_auth_headers_are_only_present_with_a_configured_key() -> None:
    assert eval_module._auth_headers(SimpleNamespace(API_KEY="", API_KEY_HEADER="X-API-Key")) == {}
    headers = eval_module._auth_headers(
        SimpleNamespace(API_KEY="test-key", API_KEY_HEADER="X-API-Key")
    )
    assert headers == {"X-API-Key": "test-key"}


def test_retry_after_seconds_is_parsed_defensively() -> None:
    def response(value: str | None) -> SimpleNamespace:
        headers = {} if value is None else {"Retry-After": value}
        return SimpleNamespace(headers=headers)

    assert eval_module._retry_after_seconds(response("5")) == 5.0
    assert eval_module._retry_after_seconds(response("0")) == 1.0
    assert eval_module._retry_after_seconds(response("later")) == 60.0
    assert eval_module._retry_after_seconds(response(None)) == 60.0


def test_write_report_creates_directories_and_wraps_errors(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "report.json"
    eval_module._write_report(target, {"summary": {"cases": 1}})
    assert json.loads(target.read_text(encoding="utf-8")) == {"summary": {"cases": 1}}

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(eval_module.EvalError, match="cannot write the report"):
        eval_module._write_report(blocker / "report.json", {})


def test_build_meta_never_leaks_secrets() -> None:
    settings = SimpleNamespace(
        EMBEDDING_PROVIDER="openai",
        embedding_model="text-embedding-3-small",
        LLM_PROVIDER="openai",
        llm_model="gpt-test",
        API_KEY="must-not-leak",
    )
    dataset = eval_module.GoldenDataset(
        version=1,
        corpus=(eval_module.CorpusEntry(filename="a.md", sha256="0" * 64),),
        cases=(_golden_case(),),
        raw={},
    )
    args = argparse.Namespace(
        mode="e2e",
        dataset="eval/golden_dataset.json",
        top_k=4,
        semantic=True,
        base_url="http://127.0.0.1:8000",
    )
    meta = eval_module._build_meta(args, settings, dataset, duration_ms=12.04)

    assert meta["mode"] == "e2e"
    assert meta["base_url"] == "http://127.0.0.1:8000"
    assert meta["dataset_version"] == 1
    assert meta["duration_ms"] == 12.0
    assert "must-not-leak" not in json.dumps(meta)

    args.mode = "retrieval"
    assert "base_url" not in eval_module._build_meta(args, settings, dataset, duration_ms=1.0)


def _corpus_dataset(tmp_path: Path, checksum: str | None = None) -> eval_module.GoldenDataset:
    path = tmp_path / "doc.md"
    path.write_bytes(b"line one\nline two\n")
    return eval_module.GoldenDataset(
        version=1,
        corpus=(eval_module.CorpusEntry(filename="doc.md", sha256=checksum or file_sha256(path)),),
        cases=(_golden_case(),),
        raw={},
    )


def test_verify_corpus_files_accepts_matching_checksums(tmp_path: Path) -> None:
    eval_module.verify_corpus_files(_corpus_dataset(tmp_path), tmp_path, required=True)


def test_verify_corpus_files_detects_drift_and_missing_files(tmp_path: Path) -> None:
    dataset = _corpus_dataset(tmp_path, checksum="0" * 64)
    with pytest.raises(eval_module.EvalError, match="sha256 mismatch"):
        eval_module.verify_corpus_files(dataset, tmp_path, required=True)

    (tmp_path / "doc.md").unlink()
    with pytest.raises(eval_module.EvalError, match="missing file"):
        eval_module.verify_corpus_files(dataset, tmp_path, required=True)


def test_verify_corpus_files_tolerates_a_missing_directory_when_optional(tmp_path: Path) -> None:
    dataset = _corpus_dataset(tmp_path)
    absent = tmp_path / "absent"
    eval_module.verify_corpus_files(dataset, absent, required=False)

    with pytest.raises(eval_module.EvalError, match="corpus directory not found"):
        eval_module.verify_corpus_files(dataset, absent, required=True)


def test_load_dataset_reports_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="dataset not found"):
        load_dataset(tmp_path / "absent.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(DatasetError, match="not valid JSON"):
        load_dataset(broken)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must be a JSON object"),
        (
            {"version": "1", "corpus": [{"filename": "a.md", "sha256": "0"}], "cases": []},
            "'version' must be an integer",
        ),
        ({"version": 1, "corpus": [], "cases": [_raw_case("q-1")]}, "non-empty list"),
        ({"version": 1, "corpus": ["nope"], "cases": [_raw_case("q-1")]}, "must be an object"),
        (
            {
                "version": 1,
                "corpus": [
                    {"filename": "a.md", "sha256": "0"},
                    {"filename": "a.md", "sha256": "0"},
                ],
                "cases": [_raw_case("q-1")],
            },
            "duplicate filename",
        ),
        (
            {
                "version": 1,
                "corpus": [{"filename": "", "sha256": "0"}],
                "cases": [_raw_case("q-1")],
            },
            "'filename' must be a non-empty string",
        ),
        (
            {"version": 1, "corpus": [{"filename": "a.md"}], "cases": [_raw_case("q-1")]},
            "'sha256' must be a non-empty string",
        ),
        (
            {"version": 1, "corpus": [{"filename": "a.md", "sha256": "0"}], "cases": ["nope"]},
            "must be an object",
        ),
    ],
)
def test_validate_dataset_reports_structural_problems(payload: Any, message: str) -> None:
    with pytest.raises(DatasetError, match=message):
        validate_dataset(payload)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        (
            {"question": "Q?", "tag": "policy", "expected_answer": "A.", "answerable": True},
            "'id' must be a non-empty string",
        ),
        (
            {
                "id": " ",
                "question": "Q?",
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": True,
            },
            "'id' must be a non-empty string",
        ),
        (
            {
                "id": "q-1",
                "question": 1,
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": True,
            },
            "'question' must be a string",
        ),
        (
            {
                "id": "q-1",
                "question": "Q?",
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": "no",
            },
            "'answerable' must be a boolean",
        ),
        (
            {
                "id": "q-1",
                "question": "Q?",
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": True,
                "top_k": 0,
            },
            "'top_k' must be a positive integer",
        ),
        (
            {
                "id": "q-1",
                "question": "Q?",
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": True,
                "document_ids": [1],
            },
            "'document_ids' must be null or a list of strings",
        ),
        (
            {
                "id": "q-1",
                "question": "Q?",
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": True,
                "expected_documents": "refund-policy.md",
            },
            "'expected_documents' must be a list of strings",
        ),
        (
            {
                "id": "q-1",
                "question": "Q?",
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": True,
                "expected_evidence": {"filename": "a.md"},
            },
            "'expected_evidence' must be a list",
        ),
        (
            {
                "id": "q-1",
                "question": "Q?",
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": True,
                "expected_evidence": [{"filename": "a.md"}],
            },
            "needs string",
        ),
        (
            {
                "id": "q-1",
                "question": "Q?",
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": True,
                "expected_answer_facts": [1],
            },
            "'expected_answer_facts' must be a list of strings",
        ),
        (
            {
                "id": "q-1",
                "question": "Q?",
                "tag": "policy",
                "expected_answer": "A.",
                "answerable": False,
                "expected_documents": ["a.md"],
            },
            "negative cases must not declare",
        ),
    ],
)
def test_validate_dataset_reports_case_problems(case: dict[str, Any], message: str) -> None:
    with pytest.raises(DatasetError, match=message):
        validate_dataset(_raw_dataset([case]))
