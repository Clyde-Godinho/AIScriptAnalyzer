"""Test doubles.

``InMemoryVectorStore`` implements :class:`app.vector_store.VectorStore` exactly
like the Chroma adapter does, including the filter semantics, so the ingestion
and analysis tests run without ChromaDB, Qdrant or an embedding API.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from app.vector_store import Chunk, SearchHit, VectorStore, Where, sanitize_metadata


def matches(metadata: dict[str, Any], where: Where | None) -> bool:
    """Evaluate a Chroma-style filter tree against a metadata dict."""
    if not where:
        return True
    for key, value in where.items():
        if key == "$and":
            if not all(matches(metadata, clause) for clause in value):
                return False
        elif key == "$or":
            if not any(matches(metadata, clause) for clause in value):
                return False
        elif key == "$not":
            if matches(metadata, value):
                return False
        elif isinstance(value, dict):
            actual = metadata.get(key)
            if "$eq" in value and actual != value["$eq"]:
                return False
            if "$in" in value and actual not in value["$in"]:
                return False
            if "$ne" in value and actual == value["$ne"]:
                return False
        elif metadata.get(key) != value:
            return False
    return True


class InMemoryVectorStore(VectorStore):
    backend = "memory"

    def __init__(self, collection: str = "test") -> None:
        super().__init__(collection)
        self.vectors: dict[str, list[float]] = {}
        self.documents: dict[str, str] = {}
        self.metadatas: dict[str, dict[str, Any]] = {}

    def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> int:
        for chunk, vector in zip(chunks, embeddings, strict=False):
            self.vectors[chunk.id] = list(vector)
            self.documents[chunk.id] = chunk.text
            self.metadatas[chunk.id] = sanitize_metadata(chunk.metadata)
        return len(chunks)

    def query(self, embedding: Sequence[float], top_k: int, where: Where | None = None) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for chunk_id, metadata in self.metadatas.items():
            if not matches(metadata, where):
                continue
            hits.append(
                SearchHit(
                    id=chunk_id,
                    text=self.documents[chunk_id],
                    metadata=metadata,
                    score=_cosine(embedding, self.vectors[chunk_id]),
                )
            )
        hits.sort(key=lambda hit: hit.score or 0.0, reverse=True)
        return hits[: max(1, top_k)]

    def fetch(self, where: Where | None = None, limit: int = 500) -> list[SearchHit]:
        hits = [
            SearchHit(id=chunk_id, text=self.documents[chunk_id], metadata=metadata)
            for chunk_id, metadata in self.metadatas.items()
            if matches(metadata, where)
        ]
        return hits[:limit]

    def delete(self, where: Where) -> int:
        if not where:
            raise ValueError("refusing to delete the whole collection; pass a filter")
        doomed = [chunk_id for chunk_id, meta in self.metadatas.items() if matches(meta, where)]
        for chunk_id in doomed:
            self.vectors.pop(chunk_id, None)
            self.documents.pop(chunk_id, None)
            self.metadatas.pop(chunk_id, None)
        return len(doomed)

    def count(self, where: Where | None = None) -> int:
        return sum(1 for meta in self.metadatas.values() if matches(meta, where))

    def reset(self) -> None:
        self.vectors.clear()
        self.documents.clear()
        self.metadatas.clear()


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return numerator / (left_norm * right_norm)
