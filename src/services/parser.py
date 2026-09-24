"""File upload validation and plain-text extraction."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

ALLOWED_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".csv", ".json", ".pdf"})


class UnsupportedFileTypeError(ValueError):
    """Raised when an uploaded file has an unsupported extension."""


def normalize_filename(filename: str | None) -> str:
    """Return a safe basename for an uploaded filename."""
    if not filename:
        raise UnsupportedFileTypeError("A filename is required.")
    return Path(filename).name


def validate_suffix(filename: str) -> str:
    """Validate and return the lower-cased file suffix."""
    suffix = Path(normalize_filename(filename)).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_SUFFIXES))
        raise UnsupportedFileTypeError(
            f"Unsupported file type '{suffix or 'unknown'}'. Allowed: {allowed}."
        )
    return suffix


def extract_text(path: Path) -> str:
    """Extract plain text from a stored document."""
    if not path.exists():
        raise FileNotFoundError(f"Document file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(page for page in pages if page).strip()

    return path.read_text(encoding="utf-8", errors="ignore").strip()
