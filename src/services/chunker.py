"""Token-aware text chunking with configurable overlap."""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


class TextChunker:
    """Split text into overlapping chunks.

    Chunk boundaries are measured in whitespace-delimited tokens (a close
    approximation of LLM tokens that avoids a heavy tokenizer dependency) and
    chunks never split a word.
    """

    def __init__(self, chunk_size: int = 500, overlap: int = 50) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if overlap < 0:
            raise ValueError("overlap must be non-negative")
        if overlap >= chunk_size:
            raise ValueError("overlap must be smaller than chunk_size")
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk_text(self, text: str) -> list[str]:
        """Return a list of non-empty chunks for ``text``."""
        if not text or not text.strip():
            return []

        words = _WHITESPACE_RE.split(text.strip())
        step = self.chunk_size - self.overlap
        chunks: list[str] = []
        for start in range(0, len(words), step):
            window = words[start : start + self.chunk_size]
            if not window:
                break
            chunk = " ".join(window).strip()
            if chunk:
                chunks.append(chunk)
            if start + self.chunk_size >= len(words):
                break
        return chunks
