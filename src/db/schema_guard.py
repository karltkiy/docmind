"""Fail-fast guard for the pgvector embedding dimension.

The vector width is baked into the database column by the initial Alembic
migration (``Vector(settings.embedding_dim)``). Switching ``EMBEDDING_PROVIDER``
or ``EMBEDDING_MODEL`` without an accompanying migration leaves the column and
the runtime provider out of sync; every insert then fails with an opaque
pgvector dimension error inside a background job.

This module compares the *configured* width against the width the database
actually declares, so a mismatch surfaces at startup with an actionable message
instead of during the first upload.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..config import settings

logger = logging.getLogger(__name__)

_TABLE = "document_chunks"
_COLUMN = "embedding"
_VECTOR_TYPE_RE = re.compile(r"vector\((\d+)\)")


class EmbeddingDimensionMismatch(RuntimeError):
    """Raised when the configured embedding dimension does not match the schema."""


async def _read_column_type(connection: AsyncConnection) -> str | None:
    """Return the PostgreSQL type of ``document_chunks.embedding``, if present."""
    result = await connection.execute(
        text(
            """
            SELECT format_type(atttypid, atttypmod) AS column_type
            FROM pg_attribute
            WHERE attrelid = to_regclass(:table)
              AND attname = :column
              AND NOT attisdropped
            """
        ),
        {"table": _TABLE, "column": _COLUMN},
    )
    row = result.first()
    return None if row is None else str(row[0])


async def verify_embedding_dimension(engine: AsyncEngine) -> None:
    """Ensure the schema vector width matches :attr:`Settings.embedding_dim`.

    Raises:
        EmbeddingDimensionMismatch: when the table/column is missing, is not a
            ``vector`` column, or its width differs from the configuration.
    """
    configured = settings.embedding_dim

    try:
        async with engine.connect() as connection:
            column_type = await _read_column_type(connection)
    except SQLAlchemyError as exc:
        raise EmbeddingDimensionMismatch(
            f"Unable to inspect {_TABLE}.{_COLUMN}: {exc}. "
            "Is PostgreSQL reachable and has 'alembic upgrade head' been run?"
        ) from exc

    if column_type is None:
        raise EmbeddingDimensionMismatch(
            f"Column {_TABLE}.{_COLUMN} was not found; run 'alembic upgrade head' first."
        )

    match = _VECTOR_TYPE_RE.search(column_type)
    if match is None:
        raise EmbeddingDimensionMismatch(
            f"Column {_TABLE}.{_COLUMN} has unexpected type '{column_type}' "
            "(expected vector(<dimension>))."
        )

    actual = int(match.group(1))
    if actual != configured:
        raise EmbeddingDimensionMismatch(
            "Embedding dimension mismatch: the database column is "
            f"vector({actual}) but the configuration resolves to {configured} "
            f"(provider={settings.EMBEDDING_PROVIDER}, "
            f"model={settings.embedding_model}). Align EMBEDDING_DIM with the "
            "model, regenerate the migration and re-index, or switch back to the "
            "provider that produced the existing vectors."
        )

    logger.info("Embedding dimension verified: vector(%d) matches configuration.", actual)
