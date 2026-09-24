"""Unit tests for the text chunker."""

from __future__ import annotations

import pytest

from src.services.chunker import TextChunker


def test_chunk_text_empty() -> None:
    assert TextChunker().chunk_text("") == []
    assert TextChunker().chunk_text("   ") == []


def test_chunk_text_single_chunk() -> None:
    text = " ".join(f"w{i}" for i in range(100))
    chunks = TextChunker(chunk_size=200, overlap=20).chunk_text(text)
    assert chunks == [text]


def test_chunk_text_overlap(sample_text: str) -> None:
    chunker = TextChunker(chunk_size=500, overlap=50)
    chunks = chunker.chunk_text(sample_text)
    assert len(chunks) > 1
    # Consecutive chunks must share the configured overlap of tokens.
    first_tail = chunks[0].split()[-50:]
    second_head = chunks[1].split()[:50]
    assert first_tail == second_head


def test_chunk_text_preserves_words(sample_text: str) -> None:
    chunks = TextChunker(chunk_size=100, overlap=10).chunk_text(sample_text)
    joined = " ".join(chunks)
    for word in sample_text.split():
        assert word in joined


@pytest.mark.parametrize(
    "chunk_size,overlap",
    [(0, 0), (100, -1), (50, 50), (50, 60)],
)
def test_invalid_parameters(chunk_size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        TextChunker(chunk_size=chunk_size, overlap=overlap)
