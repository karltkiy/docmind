"""Unit tests for upload validation and text extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.parser import (
    UnsupportedFileTypeError,
    extract_text,
    normalize_filename,
    validate_suffix,
)


def test_normalize_filename_strips_paths() -> None:
    assert normalize_filename("../../etc/passwd.txt") == "passwd.txt"


def test_validate_suffix_supported() -> None:
    assert validate_suffix("notes.TXT") == ".txt"
    assert validate_suffix("report.pdf") == ".pdf"


def test_validate_suffix_rejects_unknown() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        validate_suffix("malware.exe")


def test_normalize_filename_requires_value() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        normalize_filename(None)


def test_extract_text_from_txt(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("Hello DocMind", encoding="utf-8")
    assert extract_text(path) == "Hello DocMind"


def test_extract_text_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        extract_text(tmp_path / "missing.txt")
