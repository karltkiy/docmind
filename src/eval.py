"""Offline evaluation harness for the DocMind retrieval and chat pipelines.

The module offers two things:

* a set of pure metric helpers (no I/O) that are unit tested independently, and
* a command line entry point, ``python -m src.eval [options]``, that runs a
  golden dataset through either the retrieval pipeline (PostgreSQL + pgvector)
  or the public HTTP API (Server-Sent Events chat).

Importing this module never touches the network, the database or the
application settings; every heavy dependency is imported lazily where it is
needed so ``--help`` works in a bare environment and the metric layer stays
testable in isolation.

Report schema (written to ``--out``)::

    {
      "meta":     {...},
      "summary":  {...},
      "by_tag":   {"policy": {...}, "sla": {...}, ...},
      "failures": [{"id": "q-001", "reason": "..."}],
      "cases":    [{"id": ..., "retrieved": [...], "hit": 1.0, ...}]
    }

Percentiles use linear interpolation between closest ranks (the NumPy default):
``position = (n - 1) * p / 100`` and the neighbours are interpolated. See
:func:`percentile` for details.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import re
import sys
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("docmind.eval")

DEFAULT_DATASET = Path("eval/golden_dataset.json")
DEFAULT_CORPUS_DIR = Path("eval/corpus")
DEFAULT_OUT = Path("eval/reports/latest.json")
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_WAIT_TIMEOUT = 180.0
DEFAULT_SEMANTIC_THRESHOLD = 0.72
SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".csv", ".json", ".pdf"})
MAX_RATE_LIMIT_RETRIES = 20
SEED_POLL_SECONDS = 2.0
HTTP_TIMEOUT_SECONDS = 120.0

_EN_STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but if then than so of in on at to for from by with about as is are was
    were be been being do does did have has had it its this that these those there their they
    them he she his her we our you your i me my mine not no can could should would will shall
    may might must also into over under any all each every more most such only own same too
    very just up down out off again further once here when where which who whom what why how
    s t don doesn isn aren wasn weren won wouldn couldn shouldn
    """.split()
)
_RU_STOPWORDS: frozenset[str] = frozenset(
    """
    и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее
    мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть
    был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть
    надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда
    кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
    сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше
    тот через эти нас про всего них какая много разве три эту моя впрочем хорошо свою этой
    перед иногда лучше чуть том нельзя такой им более всегда конечно всю между
    """.split()
)
STOPWORDS: frozenset[str] = _EN_STOPWORDS | _RU_STOPWORDS

_TOKEN_RE = re.compile(r"[a-z0-9]+|[а-яё0-9]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_REFUSAL_PATTERNS: tuple[str, ...] = (
    "i don't know",
    "i do not know",
    "i don't have",
    "i do not have",
    "i cannot answer",
    "i can't answer",
    "cannot be answered",
    "not enough information",
    "no information",
    "unable to answer",
    "i am not able",
    "i'm not able",
)

_RETRIEVAL_KEYS: tuple[str, ...] = (
    "hit_at_k",
    "mrr",
    "context_recall",
    "empty_context_rate",
    "false_positive_rate",
)
_ANSWER_KEYS: tuple[str, ...] = (
    "lexical_faithfulness",
    "semantic_faithfulness",
    "token_f1",
    "rouge_l",
    "refusal_rate",
    "fact_coverage",
)


class EvalError(RuntimeError):
    """Raised when the harness cannot complete a run for an environmental reason."""


class DatasetError(ValueError):
    """Raised when the golden dataset is missing or structurally invalid."""


# ----------------------------------------------------------------------
# Text helpers and pure metrics
# ----------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """Return NFKC-normalised, lower-cased text with collapsed whitespace."""
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def split_sentences(text: str) -> list[str]:
    """Split text into sentences on punctuation followed by whitespace and on newlines."""
    sentences: list[str] = []
    for part in _SENTENCE_SPLIT_RE.split(text.strip()):
        cleaned = " ".join(part.split())
        if cleaned:
            sentences.append(cleaned)
    return sentences


def word_tokens(text: str) -> list[str]:
    """Return every lower-cased word/number token, stopwords included."""
    return _TOKEN_RE.findall(normalize_text(text))


def content_tokens(text: str) -> list[str]:
    """Return informative tokens: stopwords and single characters removed."""
    return [
        token
        for token in _TOKEN_RE.findall(normalize_text(text))
        if token not in STOPWORDS and len(token) > 1
    ]


def _lf_normalised(data: bytes) -> bytes:
    """Return ``data`` with every CRLF/CR line ending collapsed to LF.

    Git stores text files with LF (``* text=auto eol=lf`` in ``.gitattributes``),
    but a Windows working tree can still hold CRLF bytes for files written after
    the checkout. Hashing normalised content keeps the recorded corpus checksums
    stable on every platform and regardless of the checkout order.
    """
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def file_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's LF-normalised content."""
    return hashlib.sha256(_lf_normalised(path.read_bytes())).hexdigest()


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return the cosine similarity of two equal-length vectors (0.0 when degenerate)."""
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def semantic_faithfulness(
    sentence_vectors: Sequence[Sequence[float]],
    context_vectors: Sequence[Sequence[float]],
    *,
    threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
) -> float:
    """Return the share of answer sentences semantically covered by the context.

    A sentence counts as supported when its maximum cosine similarity against
    any context sentence reaches ``threshold``. Pure Python, no dependencies.
    """
    if not sentence_vectors:
        return 1.0
    if not context_vectors:
        return 0.0
    supported = 0
    for vector in sentence_vectors:
        best = max(cosine_similarity(vector, candidate) for candidate in context_vectors)
        if best >= threshold:
            supported += 1
    return supported / len(sentence_vectors)


def lexical_faithfulness(answer: str, contexts: Sequence[str]) -> float:
    """Return the share of answer content tokens that occur in the retrieved context."""
    answer_tokens = content_tokens(answer)
    if not answer_tokens:
        return 1.0
    context_vocabulary: set[str] = set()
    for context in contexts:
        context_vocabulary.update(content_tokens(context))
    supported = sum(1 for token in answer_tokens if token in context_vocabulary)
    return supported / len(answer_tokens)


def hit_at_k(retrieved: Sequence[str], expected: Sequence[str], k: int) -> float:
    """Return 1.0 when an expected document appears in the first ``k`` hits."""
    if k <= 0 or not expected:
        return 0.0
    expected_names = {normalize_text(name) for name in expected}
    for name in retrieved[:k]:
        if normalize_text(name) in expected_names:
            return 1.0
    return 0.0


def reciprocal_rank(retrieved: Sequence[str], expected: Sequence[str]) -> float:
    """Return 1/rank of the first relevant document, or 0.0 when none matches."""
    if not expected:
        return 0.0
    expected_names = {normalize_text(name) for name in expected}
    for rank, name in enumerate(retrieved, start=1):
        if normalize_text(name) in expected_names:
            return 1.0 / rank
    return 0.0


def context_recall(expected_needles: Sequence[str], retrieved_texts: Sequence[str]) -> float:
    """Return the share of expected evidence needles found in the retrieved context.

    An empty ``expected_needles`` list is vacuously satisfied (1.0).
    """
    if not expected_needles:
        return 1.0
    haystack = normalize_text(" ".join(retrieved_texts))
    found = sum(1 for needle in expected_needles if normalize_text(needle) in haystack)
    return found / len(expected_needles)


def empty_context_rate(has_results: Sequence[bool]) -> float:
    """Return the share of answerable cases for which retrieval returned nothing."""
    if not has_results:
        return 0.0
    return sum(0.0 if flag else 1.0 for flag in has_results) / len(has_results)


def false_positive_rate(returned_any: Sequence[bool]) -> float:
    """Return the share of negative cases that nonetheless returned context."""
    if not returned_any:
        return 0.0
    return sum(1.0 if flag else 0.0 for flag in returned_any) / len(returned_any)


def token_f1(prediction: str, reference: str) -> float:
    """Return token-level F1 with multiset overlap (identical strings score 1.0)."""
    predicted = word_tokens(prediction)
    expected = word_tokens(reference)
    if not predicted or not expected:
        return 1.0 if predicted == expected else 0.0
    overlap = sum((Counter(predicted) & Counter(expected)).values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall)


def _lcs_length(left: Sequence[str], right: Sequence[str]) -> int:
    """Return the length of the longest common subsequence of two token lists."""
    previous = [0] * (len(right) + 1)
    for token in left:
        current = [0]
        for index, other in enumerate(right, start=1):
            if token == other:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[index - 1]))
        previous = current
    return previous[-1]


def rouge_l(prediction: str, reference: str) -> float:
    """Return ROUGE-L F-measure over tokens (identical strings score 1.0)."""
    predicted = word_tokens(prediction)
    expected = word_tokens(reference)
    if not predicted or not expected:
        return 1.0 if predicted == expected else 0.0
    lcs = _lcs_length(predicted, expected)
    if lcs == 0:
        return 0.0
    precision = lcs / len(predicted)
    recall = lcs / len(expected)
    return 2 * precision * recall / (precision + recall)


def refusal_detected(answer: str) -> bool:
    """Return whether an answer is an explicit refusal (``I don't know`` style)."""
    normalized = normalize_text(answer)
    if not normalized:
        return False
    return any(pattern in normalized for pattern in _REFUSAL_PATTERNS)


def fact_coverage(answer: str, facts: Sequence[str]) -> float:
    """Return the share of expected facts present verbatim in the answer."""
    if not facts:
        return 1.0
    normalized = normalize_text(answer)
    found = sum(1 for fact in facts if normalize_text(fact) in normalized)
    return found / len(facts)


def percentile(values: Sequence[float], p: float) -> float:
    """Return the ``p``-th percentile using linear interpolation between ranks.

    The position is ``(n - 1) * p / 100``. For example, the median of
    ``[1, 2, 3, 4]`` is ``2.5`` and the 95th percentile of ``[10, 20, 30, 40]``
    is ``38.5``. An empty sequence yields ``0.0``.
    """
    if not 0.0 <= p <= 100.0:
        raise ValueError("percentile must be within [0, 100]")
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * p / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def _mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean, or 0.0 for an empty sequence."""
    return sum(values) / len(values) if values else 0.0


def _mean_optional(values: Iterable[float | None]) -> float | None:
    """Return the mean of the present values, or ``None`` when nothing is present."""
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _rounded(value: float | None, digits: int = 4) -> float | None:
    """Round a metric for the report, preserving ``None``."""
    return None if value is None else round(value, digits)


def _latency_stats(values: Sequence[float]) -> dict[str, float | None]:
    """Return mean/p50/p95/max latency in milliseconds."""
    if not values:
        return {"mean": None, "p50": None, "p95": None, "max": None}
    return {
        "mean": round(_mean(values), 1),
        "p50": round(percentile(values, 50), 1),
        "p95": round(percentile(values, 95), 1),
        "max": round(max(values), 1),
    }


def retrieved_filenames(results: Iterable[Any], filename_by_id: Mapping[str, str]) -> list[str]:
    """Map ordered ``SearchResult``-like objects to document filenames."""
    names: list[str] = []
    for result in results:
        chunk = getattr(result, "chunk", None)
        document_id = getattr(chunk, "document_id", None)
        name = filename_by_id.get(str(document_id))
        if name is not None:
            names.append(name)
    return names


# ----------------------------------------------------------------------
# Dataset model
# ----------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class Evidence:
    """One expected piece of evidence: a file and a normalised substring."""

    filename: str
    must_contain: str


@dataclass(slots=True, frozen=True)
class GoldenCase:
    """A single evaluation question and its expected outcome."""

    id: str
    question: str
    tag: str
    difficulty: str
    top_k: int | None
    document_ids: tuple[str, ...] | None
    expected_documents: tuple[str, ...]
    expected_evidence: tuple[Evidence, ...]
    expected_answer: str
    expected_answer_facts: tuple[str, ...]
    answerable: bool
    notes: str


@dataclass(slots=True, frozen=True)
class CorpusEntry:
    """A corpus file recorded in the dataset with its SHA-256 checksum."""

    filename: str
    sha256: str


@dataclass(slots=True, frozen=True)
class GoldenDataset:
    """A parsed, validated golden dataset."""

    version: int
    corpus: tuple[CorpusEntry, ...]
    cases: tuple[GoldenCase, ...]
    raw: dict[str, Any]


def _parse_case(raw: Mapping[str, Any]) -> GoldenCase:
    """Build a :class:`GoldenCase` from a validated raw mapping."""
    evidence = tuple(
        Evidence(filename=item["filename"], must_contain=item["must_contain"])
        for item in raw.get("expected_evidence", [])
    )
    document_ids = raw.get("document_ids")
    return GoldenCase(
        id=raw["id"],
        question=raw["question"],
        tag=raw["tag"],
        difficulty=str(raw.get("difficulty", "medium")),
        top_k=raw.get("top_k"),
        document_ids=tuple(document_ids) if isinstance(document_ids, list) else None,
        expected_documents=tuple(raw.get("expected_documents", [])),
        expected_evidence=evidence,
        expected_answer=raw["expected_answer"],
        expected_answer_facts=tuple(raw.get("expected_answer_facts", [])),
        answerable=bool(raw["answerable"]),
        notes=str(raw.get("notes", "")),
    )


def validate_dataset(data: Any) -> None:
    """Validate a raw golden-dataset payload, reporting every problem at once.

    Raises:
        DatasetError: When the payload is not a dataset, ``cases`` is empty,
            ids repeat, or an entry has the wrong shape. Unknown extra fields
            are ignored so the schema can evolve without breaking old runs.
    """
    if not isinstance(data, dict):
        raise DatasetError("dataset root must be a JSON object")
    problems: list[str] = []

    if not isinstance(data.get("version"), int):
        problems.append("'version' must be an integer")

    corpus = data.get("corpus")
    if not isinstance(corpus, list) or not corpus:
        problems.append("'corpus' must be a non-empty list")
        corpus = []
    known_files: set[str] = set()
    for index, entry in enumerate(corpus):
        if not isinstance(entry, dict):
            problems.append(f"corpus[{index}]: must be an object")
            continue
        filename = entry.get("filename")
        checksum = entry.get("sha256")
        if not isinstance(filename, str) or not filename:
            problems.append(f"corpus[{index}]: 'filename' must be a non-empty string")
        elif filename in known_files:
            problems.append(f"corpus[{index}]: duplicate filename {filename!r}")
        else:
            known_files.add(filename)
        if not isinstance(checksum, str) or not checksum:
            problems.append(f"corpus[{index}]: 'sha256' must be a non-empty string")

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        problems.append("'cases' must be a non-empty list")
        cases = []

    seen_ids: set[str] = set()
    for index, case in enumerate(cases):
        label = f"cases[{index}]"
        if not isinstance(case, dict):
            problems.append(f"{label}: must be an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            problems.append(f"{label}: 'id' must be a non-empty string")
        else:
            label = f"case {case_id!r}"
            if case_id in seen_ids:
                problems.append(f"{label}: duplicate id")
            seen_ids.add(case_id)
        for required in ("question", "tag", "expected_answer"):
            if not isinstance(case.get(required), str):
                problems.append(f"{label}: '{required}' must be a string")
        if not isinstance(case.get("answerable"), bool):
            problems.append(f"{label}: 'answerable' must be a boolean")

        top_k = case.get("top_k")
        if top_k is not None and (not isinstance(top_k, int) or top_k <= 0):
            problems.append(f"{label}: 'top_k' must be a positive integer when present")
        document_ids = case.get("document_ids")
        if document_ids is not None and (
            not isinstance(document_ids, list)
            or not all(isinstance(value, str) for value in document_ids)
        ):
            problems.append(f"{label}: 'document_ids' must be null or a list of strings")

        documents = case.get("expected_documents", [])
        if not isinstance(documents, list) or not all(isinstance(item, str) for item in documents):
            problems.append(f"{label}: 'expected_documents' must be a list of strings")
            documents = []
        evidence = case.get("expected_evidence", [])
        if not isinstance(evidence, list):
            problems.append(f"{label}: 'expected_evidence' must be a list")
            evidence = []
        for evidence_index, item in enumerate(evidence):
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("filename"), str)
                or not isinstance(item.get("must_contain"), str)
            ):
                problems.append(
                    f"{label}: expected_evidence[{evidence_index}] needs string "
                    "'filename' and 'must_contain'"
                )
        facts = case.get("expected_answer_facts", [])
        if not isinstance(facts, list) or not all(isinstance(item, str) for item in facts):
            problems.append(f"{label}: 'expected_answer_facts' must be a list of strings")
        if case.get("answerable") is False and (documents or evidence):
            problems.append(
                f"{label}: negative cases must not declare expected documents or evidence"
            )

    if problems:
        raise DatasetError("invalid golden dataset:\n  - " + "\n  - ".join(problems))


def load_dataset(path: Path) -> GoldenDataset:
    """Read and validate a golden dataset from ``path``.

    Raises:
        DatasetError: When the file is missing, not valid JSON or fails
            :func:`validate_dataset`.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetError(f"dataset not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"dataset {path} is not valid JSON: {exc}") from exc
    validate_dataset(raw)
    return GoldenDataset(
        version=int(raw["version"]),
        corpus=tuple(
            CorpusEntry(filename=entry["filename"], sha256=entry["sha256"])
            for entry in raw["corpus"]
        ),
        cases=tuple(_parse_case(case) for case in raw["cases"]),
        raw=raw,
    )


def verify_corpus_files(dataset: GoldenDataset, corpus_dir: Path, *, required: bool) -> None:
    """Check that every corpus file exists and matches its recorded checksum.

    Args:
        dataset: Loaded dataset with the expected files and digests.
        corpus_dir: Directory holding the markdown corpus.
        required: When ``True`` a missing directory is fatal; otherwise only a
            warning is logged (the checksums cannot be verified without it).
    """
    if not corpus_dir.is_dir():
        if required:
            raise EvalError(f"corpus directory not found: {corpus_dir}")
        logger.warning("Corpus directory %s is missing; checksums not verified.", corpus_dir)
        return
    problems: list[str] = []
    for entry in dataset.corpus:
        path = corpus_dir / entry.filename
        if not path.is_file():
            problems.append(f"missing file {entry.filename}")
            continue
        actual = file_sha256(path)
        if actual != entry.sha256:
            problems.append(f"{entry.filename}: sha256 mismatch (dataset has {entry.sha256})")
    if problems:
        raise EvalError("corpus does not match the dataset: " + "; ".join(problems))


# ----------------------------------------------------------------------
# Per-case result and aggregation
# ----------------------------------------------------------------------
@dataclass(slots=True)
class CaseResult:
    """Metrics and diagnostics for one evaluation case."""

    case: GoldenCase
    top_k: int
    retrieved: list[dict[str, Any]]
    hit: float = 0.0
    reciprocal_rank: float = 0.0
    context_recall: float = 0.0
    answer: str = ""
    lexical_faithfulness: float | None = None
    semantic_faithfulness: float | None = None
    token_f1: float | None = None
    rouge_l: float | None = None
    refusal_detected: bool | None = None
    fact_coverage: float | None = None
    latency_ms: float = 0.0
    response_time_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise the case result for the JSON report."""
        return {
            "id": self.case.id,
            "question": self.case.question,
            "tag": self.case.tag,
            "difficulty": self.case.difficulty,
            "answerable": self.case.answerable,
            "top_k": self.top_k,
            "expected_documents": list(self.case.expected_documents),
            "expected_evidence": [
                {"filename": evidence.filename, "must_contain": evidence.must_contain}
                for evidence in self.case.expected_evidence
            ],
            "expected_answer": self.case.expected_answer,
            "retrieved": self.retrieved,
            "hit": self.hit,
            "reciprocal_rank": self.reciprocal_rank,
            "context_recall": self.context_recall,
            "answer": self.answer,
            "lexical_faithfulness": self.lexical_faithfulness,
            "semantic_faithfulness": self.semantic_faithfulness,
            "token_f1": self.token_f1,
            "rouge_l": self.rouge_l,
            "refusal_detected": self.refusal_detected,
            "fact_coverage": self.fact_coverage,
            "latency_ms": round(self.latency_ms, 1),
            "response_time_ms": self.response_time_ms,
            "error": self.error,
        }


def _score_case(
    case: GoldenCase,
    *,
    top_k: int,
    retrieved: Sequence[dict[str, Any]],
    answer: str | None,
    latency_ms: float,
    response_time_ms: float | None = None,
    semantic: float | None = None,
) -> CaseResult:
    """Score one case from its retrieved chunks and (optionally) its answer."""
    names = [str(item["filename"]) for item in retrieved if item.get("filename")]
    contexts = [str(item.get("excerpt", "")) for item in retrieved]
    needles = [evidence.must_contain for evidence in case.expected_evidence]
    result = CaseResult(
        case=case,
        top_k=top_k,
        retrieved=list(retrieved),
        hit=hit_at_k(names, case.expected_documents, top_k),
        reciprocal_rank=reciprocal_rank(names, case.expected_documents),
        context_recall=context_recall(needles, contexts),
        latency_ms=latency_ms,
        response_time_ms=response_time_ms,
    )
    if answer is not None:
        result.answer = answer
        result.lexical_faithfulness = lexical_faithfulness(answer, contexts)
        result.semantic_faithfulness = semantic
        result.token_f1 = token_f1(answer, case.expected_answer)
        result.rouge_l = rouge_l(answer, case.expected_answer)
        result.refusal_detected = refusal_detected(answer)
        result.fact_coverage = fact_coverage(answer, case.expected_answer_facts)
    return result


def aggregate(case_results: Sequence[CaseResult], *, with_answers: bool) -> dict[str, Any]:
    """Aggregate case results into the report summary block."""
    answerable = [result for result in case_results if result.case.answerable]
    negative = [result for result in case_results if not result.case.answerable]
    scored = answerable if with_answers else []
    return {
        "cases": len(case_results),
        "answerable_cases": len(answerable),
        "negative_cases": len(negative),
        "error_cases": sum(1 for result in case_results if result.error is not None),
        "hit_at_k": _rounded(_mean([result.hit for result in answerable])),
        "mrr": _rounded(_mean([result.reciprocal_rank for result in answerable])),
        "context_recall": _rounded(_mean([result.context_recall for result in answerable])),
        "empty_context_rate": _rounded(
            empty_context_rate([bool(result.retrieved) for result in answerable])
        ),
        "false_positive_rate": _rounded(
            false_positive_rate([bool(result.retrieved) for result in negative])
        ),
        "lexical_faithfulness": _rounded(
            _mean_optional([result.lexical_faithfulness for result in scored])
        ),
        "semantic_faithfulness": _rounded(
            _mean_optional([result.semantic_faithfulness for result in scored])
        ),
        "token_f1": _rounded(_mean_optional([result.token_f1 for result in scored])),
        "rouge_l": _rounded(_mean_optional([result.rouge_l for result in scored])),
        "refusal_rate": _rounded(
            _mean_optional(
                [
                    1.0 if result.refusal_detected else 0.0
                    for result in negative
                    if result.refusal_detected is not None
                ]
            )
        ),
        "fact_coverage": _rounded(_mean_optional([result.fact_coverage for result in scored])),
        "latency_ms": _latency_stats([result.latency_ms for result in case_results]),
        "server_response_time_ms": _latency_stats(
            [
                result.response_time_ms
                for result in case_results
                if result.response_time_ms is not None
            ]
        ),
    }


def aggregate_by_tag(
    case_results: Sequence[CaseResult], *, with_answers: bool
) -> dict[str, dict[str, Any]]:
    """Aggregate metrics per tag so regressions can be localised to a topic."""
    tags = sorted({result.case.tag for result in case_results})
    return {
        tag: aggregate(
            [result for result in case_results if result.case.tag == tag],
            with_answers=with_answers,
        )
        for tag in tags
    }


def collect_failures(
    case_results: Sequence[CaseResult], *, with_answers: bool
) -> list[dict[str, str]]:
    """Return one human-readable entry per failed case."""
    failures: list[dict[str, str]] = []
    for result in case_results:
        if result.error is not None:
            failures.append({"id": result.case.id, "reason": result.error})
            continue
        reasons: list[str] = []
        if result.case.answerable:
            if result.hit < 1.0:
                reasons.append("expected document not retrieved")
            if result.context_recall < 1.0:
                reasons.append("expected evidence missing from context")
            if with_answers and (result.fact_coverage or 0.0) < 1.0:
                reasons.append("expected fact missing from answer")
            if with_answers and (result.lexical_faithfulness or 0.0) < 0.5:
                reasons.append("answer not grounded in context")
        else:
            if result.retrieved:
                reasons.append("negative case returned context")
            if with_answers and result.refusal_detected is False:
                reasons.append("negative case produced a non-refusal answer")
        if reasons:
            failures.append({"id": result.case.id, "reason": "; ".join(reasons)})
    return failures


def build_report(
    meta: Mapping[str, Any],
    case_results: Sequence[CaseResult],
    *,
    with_answers: bool,
) -> dict[str, Any]:
    """Assemble the full JSON report from the per-case results."""
    return {
        "meta": dict(meta),
        "summary": aggregate(case_results, with_answers=with_answers),
        "by_tag": aggregate_by_tag(case_results, with_answers=with_answers),
        "failures": collect_failures(case_results, with_answers=with_answers),
        "cases": [result.to_dict() for result in case_results],
    }


def _format_value(value: Any) -> str:
    """Format a metric for the human-readable stdout summary."""
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_summary(report: Mapping[str, Any]) -> str:
    """Render a compact plain-text summary (no third-party table libraries)."""
    meta = report["meta"]
    summary = report["summary"]
    lines = [
        f"DocMind evaluation: mode={meta.get('mode')} cases={summary['cases']} "
        f"(answerable={summary['answerable_cases']}, "
        f"negative={summary['negative_cases']}, errors={summary['error_cases']})"
    ]
    for key in (*_RETRIEVAL_KEYS, *_ANSWER_KEYS):
        lines.append(f"  {key:<21} {_format_value(summary.get(key))}")
    latency = summary["latency_ms"]
    lines.append(
        "  latency ms p50/p95/max "
        f"{_format_value(latency['p50'])}/{_format_value(latency['p95'])}/"
        f"{_format_value(latency['max'])}"
    )
    failures = report.get("failures", [])
    lines.append(f"  failures              {len(failures)}")
    for failure in failures[:10]:
        lines.append(f"    - {failure['id']}: {failure['reason']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Settings / database / HTTP plumbing
# ----------------------------------------------------------------------
def _load_settings() -> Any:
    """Import the application settings, converting a bad environment into CLI advice."""
    try:
        from .config import settings
    except Exception as exc:
        raise EvalError(
            "cannot load application settings: set DATABASE_URL (or POSTGRES_PASSWORD) "
            "and the provider credentials before running the harness. "
            f"Details: {exc}"
        ) from exc
    return settings


@dataclass(slots=True)
class DocumentIndex:
    """Snapshot of the documents stored in PostgreSQL."""

    completed: dict[str, str]
    name_by_id: dict[str, str]
    failed: set[str]


async def _check_database(engine: Any) -> None:
    """Fail with an actionable message when PostgreSQL is unreachable."""
    from sqlalchemy import text

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        raise EvalError(
            "cannot connect to PostgreSQL; the retrieval mode needs a live pgvector "
            "database. Check DATABASE_URL (or POSTGRES_* settings) and that the "
            f"database is running. Underlying error: {type(exc).__name__}: {exc}"
        ) from exc


async def _load_document_index(factory: Any) -> DocumentIndex:
    """Load a filename/id/status snapshot used to map chunks back to documents."""
    try:
        from sqlalchemy import select

        from .db.models import Document
    except ImportError as exc:
        raise EvalError(
            "the retrieval mode needs the application dependencies installed "
            "(SQLAlchemy, asyncpg, pgvector). Install the project first, for "
            f"example `pip install -e .`. Missing module: {exc.name}"
        ) from exc

    async with factory() as session:
        rows = (
            await session.execute(select(Document.id, Document.filename, Document.status))
        ).all()
    completed: dict[str, str] = {}
    name_by_id: dict[str, str] = {}
    failed: set[str] = set()
    for row in rows:
        document_id = str(row.id)
        name_by_id[document_id] = row.filename
        status = row.status.value if hasattr(row.status, "value") else str(row.status)
        if status == "completed":
            completed.setdefault(row.filename, document_id)
        elif status == "failed":
            failed.add(row.filename)
    return DocumentIndex(completed=completed, name_by_id=name_by_id, failed=failed)


def _ensure_corpus_indexed(dataset: GoldenDataset, index: DocumentIndex) -> None:
    """Raise an actionable error unless every dataset corpus file is indexed."""
    missing = [entry.filename for entry in dataset.corpus if entry.filename not in index.completed]
    if not missing:
        return
    failed_hint = f" Failed documents: {', '.join(sorted(index.failed))}." if index.failed else ""
    raise EvalError(
        "the retrieval corpus is not fully indexed in PostgreSQL: "
        f"{', '.join(missing)}.{failed_hint} Upload the corpus first, for example "
        "`python -m src.eval --seed --base-url http://127.0.0.1:8000`."
    )


def _resolve_top_k(case: GoldenCase, args: argparse.Namespace, settings: Any) -> int:
    """Resolve the effective retrieval depth: CLI flag, case override, then config."""
    if args.top_k is not None:
        return int(args.top_k)
    if case.top_k is not None:
        return int(case.top_k)
    return int(settings.RETRIEVAL_TOP_K)


def _auth_headers(settings: Any) -> dict[str, str]:
    """Return the API-key header when the server requires authentication.

    The value is never logged or written to the report.
    """
    if not settings.API_KEY:
        return {}
    return {settings.API_KEY_HEADER: settings.API_KEY}


# ----------------------------------------------------------------------
# Seeding through the public HTTP API
# ----------------------------------------------------------------------
async def _list_remote_documents(client: Any) -> dict[str, dict[str, Any]]:
    """Return stored documents keyed by filename (newest entry wins)."""
    response = await client.get("/api/v1/documents", params={"limit": 200})
    response.raise_for_status()
    documents = response.json()
    by_name: dict[str, dict[str, Any]] = {}
    for document in reversed(documents if isinstance(documents, list) else []):
        if isinstance(document, dict) and isinstance(document.get("filename"), str):
            by_name[document["filename"]] = document
    return by_name


def _retry_after_seconds(response: Any) -> float:
    """Read a ``Retry-After`` header defensively."""
    raw = response.headers.get("Retry-After", "60")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 60.0


async def _upload_with_retry(client: Any, path: Path) -> dict[str, Any]:
    """Upload one file, waiting out HTTP 429 responses as instructed."""
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
        response = await client.post(
            "/api/v1/documents/upload",
            files={"file": (path.name, path.read_bytes(), content_type)},
        )
        if response.status_code == 202:
            return dict(response.json())
        if response.status_code == 429:
            delay = _retry_after_seconds(response)
            logger.warning(
                "%s: upload rate limited, waiting %.0fs (attempt %d/%d).",
                path.name,
                delay,
                attempt,
                MAX_RATE_LIMIT_RETRIES,
            )
            await asyncio.sleep(delay)
            continue
        response.raise_for_status()
    raise EvalError(
        f"{path.name}: still rate limited after {MAX_RATE_LIMIT_RETRIES} upload attempts"
    )


async def _wait_for_indexing(client: Any, pending: Mapping[str, str], timeout: float) -> None:
    """Poll document status until every pending document is indexed."""
    remaining = dict(pending)
    deadline = time.monotonic() + timeout
    while remaining and time.monotonic() < deadline:
        for filename, document_id in list(remaining.items()):
            response = await client.get(f"/api/v1/documents/{document_id}/status")
            if response.status_code == 404:
                raise EvalError(f"{filename}: document {document_id} disappeared during indexing")
            response.raise_for_status()
            info = response.json()
            status = info.get("status")
            if status == "completed":
                logger.info("%s: indexed (%s chunks).", filename, info.get("chunk_count", 0))
                remaining.pop(filename)
            elif status == "failed":
                raise EvalError(f"{filename}: indexing failed: {info.get('error')}")
        if remaining:
            await asyncio.sleep(SEED_POLL_SECONDS)
    if remaining:
        raise EvalError(
            f"timed out after {timeout:.0f}s waiting for indexing of: "
            f"{', '.join(sorted(remaining))}"
        )


async def seed_corpus(client: Any, args: argparse.Namespace, dataset: GoldenDataset) -> None:
    """Upload the dataset corpus (idempotently) and wait until it is indexed."""
    corpus_dir = Path(args.corpus_dir)
    verify_corpus_files(dataset, corpus_dir, required=True)
    existing = await _list_remote_documents(client)
    pending: dict[str, str] = {}
    for entry in dataset.corpus:
        known = existing.get(entry.filename)
        if known is not None and int(known.get("chunk_count") or 0) > 0:
            logger.info("%s: already indexed, skipping upload.", entry.filename)
            continue
        if known is not None and known.get("status") == "failed":
            raise EvalError(
                f"{entry.filename}: stored in a failed state ({known.get('error')}); "
                "delete it before seeding again"
            )
        if known is not None:
            logger.info("%s: already queued, waiting for indexing.", entry.filename)
            pending[entry.filename] = str(known["id"])
            continue
        uploaded = await _upload_with_retry(client, corpus_dir / entry.filename)
        pending[entry.filename] = str(uploaded["id"])
    await _wait_for_indexing(client, pending, args.wait_timeout)


# ----------------------------------------------------------------------
# Runners
# ----------------------------------------------------------------------
async def _evaluate_retrieval_case(
    case: GoldenCase,
    factory: Any,
    vector_store: Any,
    engine: Any,
    index: DocumentIndex,
    top_k: int,
) -> CaseResult:
    """Embed one question, search the vector store and score the hits."""
    started = time.perf_counter()
    try:
        embedding = await engine.get_embedding(case.question)
        document_ids = (
            [uuid.UUID(value) for value in case.document_ids] if case.document_ids else None
        )
        async with factory() as session:
            hits = await vector_store.search_chunks(
                session=session,
                query_embedding=embedding,
                top_k=top_k,
                document_ids=document_ids,
            )
        latency_ms = (time.perf_counter() - started) * 1000.0
        retrieved = [
            {
                "filename": index.name_by_id.get(str(hit.chunk.document_id)),
                "chunk_index": int(hit.chunk.chunk_index),
                "score": round(float(hit.score), 4),
                "excerpt": str(hit.chunk.content)[:300],
            }
            for hit in hits
        ]
        return _score_case(
            case, top_k=top_k, retrieved=retrieved, answer=None, latency_ms=latency_ms
        )
    except Exception as exc:
        logger.exception("Case %s failed during retrieval.", case.id)
        return CaseResult(
            case=case,
            top_k=top_k,
            retrieved=[],
            latency_ms=(time.perf_counter() - started) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
        )


async def _run_retrieval(
    args: argparse.Namespace, settings: Any, dataset: GoldenDataset
) -> list[CaseResult]:
    """Run the dataset through the local retrieval pipeline."""
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from .services.rag_engine import rag_engine
        from .services.vector_store import VectorStore
    except ImportError as exc:
        raise EvalError(
            "the retrieval mode needs the application dependencies installed "
            "(SQLAlchemy, asyncpg, pgvector). Install the project first, for "
            f"example `pip install -e .`. Missing module: {exc.name}"
        ) from exc

    if args.seed:
        await _seed_via_http(args, settings, dataset)
    else:
        verify_corpus_files(dataset, Path(args.corpus_dir), required=False)

    try:
        db_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    except Exception as exc:
        raise EvalError(f"cannot create the database engine: {exc}") from exc
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    results: list[CaseResult] = []
    try:
        await _check_database(db_engine)
        index = await _load_document_index(factory)
        _ensure_corpus_indexed(dataset, index)
        await rag_engine.startup()
        try:
            for case in dataset.cases:
                top_k = _resolve_top_k(case, args, settings)
                results.append(
                    await _evaluate_retrieval_case(
                        case, factory, VectorStore, rag_engine, index, top_k
                    )
                )
        finally:
            await rag_engine.shutdown()
    finally:
        await db_engine.dispose()
    return results


async def _seed_via_http(args: argparse.Namespace, settings: Any, dataset: GoldenDataset) -> None:
    """Seed the corpus over HTTP before a retrieval run."""
    try:
        import httpx
    except ImportError as exc:
        raise EvalError(
            "seeding needs httpx installed; install the project dependencies first."
        ) from exc

    try:
        async with httpx.AsyncClient(
            base_url=args.base_url.rstrip("/"),
            headers=_auth_headers(settings),
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as client:
            await seed_corpus(client, args, dataset)
    except httpx.HTTPError as exc:
        raise EvalError(f"seeding through {args.base_url} failed: {exc}") from exc


async def _iter_sse_frames(response: Any) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded ``data:`` frames from an SSE response."""
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        try:
            frame = json.loads(payload)
        except json.JSONDecodeError:
            logger.debug("Skipping malformed SSE payload: %s", payload)
            continue
        if isinstance(frame, dict):
            yield frame


async def _semantic_faithfulness_for(
    engine: Any, answer: str, contexts: Sequence[str]
) -> float | None:
    """Embed the answer sentences and context excerpts, then score faithfulness."""
    sentences = split_sentences(answer)
    usable_contexts = [context for context in contexts if context.strip()]
    if not sentences or not usable_contexts:
        return None
    texts = [*sentences, *usable_contexts]
    try:
        vectors = await engine.get_embeddings(texts)
    except Exception:
        logger.exception("Semantic faithfulness failed; recording null.")
        return None
    if len(vectors) != len(texts):
        logger.warning("Embedding count mismatch; skipping semantic faithfulness.")
        return None
    return semantic_faithfulness(vectors[: len(sentences)], vectors[len(sentences) :])


async def _evaluate_e2e_case(
    client: Any, case: GoldenCase, top_k: int, embedder: Any | None
) -> CaseResult:
    """Ask the chat endpoint for an answer and score the SSE sources and tokens."""
    payload: dict[str, Any] = {"query": case.question, "top_k": top_k}
    if case.document_ids:
        payload["document_ids"] = list(case.document_ids)
    started = time.perf_counter()
    sources: list[dict[str, Any]] = []
    response_time_ms: float | None = None
    try:
        async with client.stream("POST", "/api/v1/chat/completions", json=payload) as response:
            response.raise_for_status()
            header = response.headers.get("x-response-time-ms")
            if header is not None:
                response_time_ms = float(header)
            tokens: list[str] = []
            async for frame in _iter_sse_frames(response):
                frame_type = frame.get("type")
                if frame_type == "sources":
                    sources = [
                        item for item in frame.get("content") or [] if isinstance(item, dict)
                    ]
                elif frame_type == "token":
                    tokens.append(str(frame.get("content") or ""))
                elif frame_type == "error":
                    raise EvalError(f"chat stream returned an error: {frame.get('message')}")
        latency_ms = (time.perf_counter() - started) * 1000.0
        answer = "".join(tokens)
        semantic: float | None = None
        if embedder is not None:
            semantic = await _semantic_faithfulness_for(
                embedder, answer, [str(item.get("excerpt", "")) for item in sources]
            )
        retrieved = [
            {
                "filename": item.get("filename"),
                "chunk_index": item.get("chunk_index"),
                "score": item.get("score"),
                "excerpt": item.get("excerpt", ""),
            }
            for item in sources
        ]
        return _score_case(
            case,
            top_k=top_k,
            retrieved=retrieved,
            answer=answer,
            latency_ms=latency_ms,
            response_time_ms=response_time_ms,
            semantic=semantic,
        )
    except Exception as exc:
        logger.exception("Case %s failed during the end-to-end run.", case.id)
        return CaseResult(
            case=case,
            top_k=top_k,
            retrieved=sources,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            response_time_ms=response_time_ms,
            error=f"{type(exc).__name__}: {exc}",
        )


async def _run_e2e(
    args: argparse.Namespace, settings: Any, dataset: GoldenDataset
) -> list[CaseResult]:
    """Run the dataset against a live DocMind API over HTTP."""
    try:
        import httpx
    except ImportError as exc:
        raise EvalError(
            "the e2e mode needs httpx installed; install the project dependencies first."
        ) from exc

    results: list[CaseResult] = []
    try:
        async with httpx.AsyncClient(
            base_url=args.base_url.rstrip("/"),
            headers=_auth_headers(settings),
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as client:
            if args.seed:
                await seed_corpus(client, args, dataset)
            else:
                verify_corpus_files(dataset, Path(args.corpus_dir), required=False)
            embedder = None
            if args.semantic:
                from .services.rag_engine import rag_engine

                await rag_engine.startup()
                embedder = rag_engine
            try:
                for case in dataset.cases:
                    top_k = _resolve_top_k(case, args, settings)
                    results.append(await _evaluate_e2e_case(client, case, top_k, embedder))
            finally:
                if embedder is not None:
                    await embedder.shutdown()
    except httpx.HTTPError as exc:
        raise EvalError(f"HTTP request to {args.base_url} failed: {exc}") from exc
    return results


async def _dispatch(
    args: argparse.Namespace, settings: Any, dataset: GoldenDataset
) -> tuple[list[CaseResult], bool]:
    """Run the requested mode and report whether answers were scored."""
    if args.mode == "e2e":
        return await _run_e2e(args, settings, dataset), True
    return await _run_retrieval(args, settings, dataset), False


def _build_meta(
    args: argparse.Namespace,
    settings: Any,
    dataset: GoldenDataset,
    *,
    duration_ms: float,
) -> dict[str, Any]:
    """Build the report metadata block (never includes secrets)."""
    meta: dict[str, Any] = {
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "mode": args.mode,
        "dataset": str(Path(args.dataset)),
        "dataset_version": dataset.version,
        "cases": len(dataset.cases),
        "corpus_files": len(dataset.corpus),
        "top_k_override": args.top_k,
        "semantic": bool(args.semantic),
        "duration_ms": round(duration_ms, 1),
        "embedding_provider": settings.EMBEDDING_PROVIDER,
        "embedding_model": settings.embedding_model,
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.llm_model,
    }
    if args.mode == "e2e":
        meta["base_url"] = args.base_url
    return meta


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    """Write the JSON report, creating parent directories as needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"cannot write the report to {path}: {exc}") from exc
    logger.info("Report written to %s", path)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser for ``python -m src.eval``."""
    parser = argparse.ArgumentParser(
        prog="python -m src.eval",
        description=(
            "Evaluate DocMind against a golden dataset. The retrieval mode needs a "
            "live pgvector database and the embedding provider; the e2e mode talks "
            "to a running API over HTTP and streams answers via SSE."
        ),
    )
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_DATASET),
        help=f"golden dataset JSON (default: {DEFAULT_DATASET})",
    )
    parser.add_argument(
        "--corpus-dir",
        default=str(DEFAULT_CORPUS_DIR),
        help=f"directory holding the corpus files (default: {DEFAULT_CORPUS_DIR})",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"JSON report path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--mode",
        choices=("retrieval", "e2e"),
        default="retrieval",
        help="retrieval scores only search; e2e streams answers from the API",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        dest="top_k",
        help="override the retrieval depth for every case",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        dest="base_url",
        help=f"API base URL used by e2e and --seed (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="upload the corpus through the upload API before running",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=DEFAULT_WAIT_TIMEOUT,
        dest="wait_timeout",
        help=f"seconds to wait for indexing after seeding (default: {DEFAULT_WAIT_TIMEOUT:.0f})",
    )
    parser.add_argument(
        "--fail-under-hit-rate",
        type=float,
        default=None,
        dest="fail_under_hit_rate",
        help="exit with status 1 when hit@k drops below this value",
    )
    parser.add_argument(
        "--semantic",
        action="store_true",
        help="enable embedding-based semantic faithfulness (needs the embedding provider)",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        dest="json_only",
        help="suppress the human-readable summary on stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the evaluation harness and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.top_k is not None and args.top_k <= 0:
        parser.error("--top-k must be a positive integer")

    try:
        settings = _load_settings()
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from .logging_config import configure_logging

    configure_logging(settings.LOG_LEVEL, json_output=settings.LOG_JSON)

    started = time.perf_counter()
    try:
        dataset = load_dataset(Path(args.dataset))
        case_results, with_answers = asyncio.run(_dispatch(args, settings, dataset))
        report = build_report(
            _build_meta(
                args,
                settings,
                dataset,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            ),
            case_results,
            with_answers=with_answers,
        )
        _write_report(Path(args.out), report)
    except (DatasetError, EvalError) as exc:
        logger.error("%s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logger.error("Evaluation interrupted.")
        return 1

    if not args.json_only:
        sys.stdout.write(render_summary(report) + "\n")

    summary = report["summary"]
    if summary["error_cases"]:
        logger.error("%d case(s) finished with an error; see %s.", summary["error_cases"], args.out)
        return 1
    threshold = args.fail_under_hit_rate
    if threshold is not None and float(summary["hit_at_k"]) < threshold:
        logger.error("hit@k %.4f is below the required %.4f.", summary["hit_at_k"], threshold)
        return 1
    logger.info(
        "Evaluation finished successfully in %.1f ms.",
        summary["latency_ms"]["mean"] or 0.0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
