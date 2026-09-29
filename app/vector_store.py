"""Vector store abstraction.

The index is keyed by deterministic metadata - ``(package_name, distro)`` plus
``source_type`` - so retrieval for an analysis run is a *filtered* query rather
than a fuzzy top-k over the whole corpus. Semantic similarity is then only used
to pick the most relevant chunks *inside* the already-matched document/script
pair.

Filters are expressed in Chroma's syntax internally; :func:`to_qdrant_filter`
translates them for the Qdrant adapter.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings
from app.models import normalize_package_name

logger = logging.getLogger(__name__)

Where = dict[str, Any]
Metadata = dict[str, Any]

SCALAR_TYPES = (str, int, float, bool)


# --------------------------------------------------------------------------- #
# Chunks
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Chunk:
    """A single indexed unit with its deterministic metadata."""

    id: str
    text: str
    metadata: Metadata = field(default_factory=dict)

    @property
    def package_name(self) -> str:
        return str(self.metadata.get("package_name", ""))

    @property
    def distro(self) -> str:
        return str(self.metadata.get("distro", "unknown"))

    @property
    def source_path(self) -> str:
        return str(self.metadata.get("source_path", ""))

    @property
    def source_type(self) -> str:
        return str(self.metadata.get("source_type", "doc"))


@dataclass(slots=True)
class SearchHit:
    """A retrieval result."""

    id: str
    text: str
    metadata: Metadata
    score: float | None = None


def sanitize_metadata(metadata: Metadata) -> Metadata:
    """Chroma only accepts scalar metadata values, so coerce lists to CSV."""
    clean: Metadata = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, SCALAR_TYPES):
            clean[key] = value
        elif isinstance(value, (list, tuple, set)):
            clean[key] = ",".join(str(item.value if hasattr(item, "value") else item) for item in value) or "none"
        else:
            clean[key] = str(value)
    return clean


# --------------------------------------------------------------------------- #
# Filter helpers (Chroma syntax)
# --------------------------------------------------------------------------- #


def match_all(**equalities: Any) -> Where:
    """AND of equality clauses; ``None`` values are skipped."""
    clauses = [{key: {"$eq": value}} for key, value in equalities.items() if value is not None]
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def match_any(field_name: str, values: Sequence[Any]) -> Where:
    """OR of equality clauses for one field."""
    values = [value for value in values if value is not None]
    if not values:
        return {}
    if len(values) == 1:
        return {field_name: {"$eq": values[0]}}
    return {"$or": [{field_name: {"$eq": value}} for value in values]}


def match_package(package_name: str) -> Where:
    """Filter on the normalized package name.

    Chunks are tagged with the spelling the repository uses (``Nginx``,
    ``Apache-HTTP-Server``), but API callers type whatever they like, so every
    package-level lookup goes through the normalized form.
    """
    return {"normalized_name": {"$eq": normalize_package_name(package_name)}}


def and_all(*clauses: Where) -> Where:
    """Combine filters, dropping empties."""
    kept = [clause for clause in clauses if clause]
    if not kept:
        return {}
    if len(kept) == 1:
        return kept[0]
    return {"$and": kept}


def to_qdrant_filter(where: Where):
    """Translate a Chroma-style filter tree into a ``qdrant_client`` Filter."""
    from qdrant_client import models as qmodels

    def condition(key: str, value: Any):
        if isinstance(value, dict):
            if "$eq" in value:
                return qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value["$eq"]))
            if "$in" in value:
                return qmodels.FieldCondition(key=key, match=qmodels.MatchAny(any=list(value["$in"])))
            if "$ne" in value:
                return qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value["$ne"]))
            raise ValueError(f"unsupported qdrant operator in {value!r}")
        return qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value))

    def build(node: Where) -> qmodels.Filter:
        must: list[Any] = []
        should: list[Any] = []
        must_not: list[Any] = []
        for key, value in node.items():
            if key == "$and":
                must.extend(build(item) for item in value)
            elif key == "$or":
                should.extend(build(item) for item in value)
            elif key == "$not":
                must_not.append(build(value))
            elif key.startswith("$"):
                raise ValueError(f"unsupported operator {key}")
            else:
                condition_obj = condition(key, value)
                if isinstance(value, dict) and "$ne" in value:
                    must_not.append(condition_obj)
                else:
                    must.append(condition_obj)
        return qmodels.Filter(
            must=must or None,
            should=should or None,
            must_not=must_not or None,
        )

    if not where:
        return None
    return build(where)


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #


class Embedder(Protocol):
    provider: str
    model: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_./+-]{2,}", text.lower())


class HashingEmbedder:
    """Dependency-free, deterministic bag-of-words embedder.

    Used for offline development, CI and tests. It is intentionally *not* a
    semantic model - retrieval quality comes from the deterministic
    ``(package_name, distro)`` filter, with this only ranking within a pair.
    """

    provider = "hash"

    def __init__(self, dimension: int = 256, model: str = "hash-256") -> None:
        self.dimension = dimension
        self.model = model

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = _tokenize(text)
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


class OpenAIEmbedder:
    """Embeddings from any OpenAI-compatible ``/v1/embeddings`` endpoint."""

    provider = "openai"

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        base_url: str | None = None,
        dimension: int | None = None,
        batch_size: int = 96,
    ) -> None:
        from openai import OpenAI  # imported lazily: optional at import time

        self.model = model
        self.dimension = dimension or 1536
        self.batch_size = batch_size
        self._client = OpenAI(api_key=api_key, base_url=base_url or None)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            response = self._client.embeddings.create(model=self.model, input=batch)
            vectors.extend(item.embedding for item in response.data)
        if vectors:
            self.dimension = len(vectors[0])
        return vectors


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
        return OpenAIEmbedder(
            model=settings.embedding_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            dimension=settings.embedding_dim,
        )
    return HashingEmbedder(dimension=settings.embedding_dim)


# --------------------------------------------------------------------------- #
# Vector stores
# --------------------------------------------------------------------------- #


class VectorStore(ABC):
    """Minimal interface shared by the Chroma and Qdrant adapters."""

    backend: str = "base"

    def __init__(self, collection: str) -> None:
        self.collection = collection

    @abstractmethod
    def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> int: ...

    @abstractmethod
    def query(
        self,
        embedding: Sequence[float],
        top_k: int,
        where: Where | None = None,
    ) -> list[SearchHit]: ...

    @abstractmethod
    def fetch(self, where: Where | None = None, limit: int = 500) -> list[SearchHit]: ...

    @abstractmethod
    def delete(self, where: Where) -> int: ...

    @abstractmethod
    def count(self, where: Where | None = None) -> int: ...

    @abstractmethod
    def reset(self) -> None: ...

    def close(self) -> None:  # pragma: no cover - optional hook
        return None


class ChromaVectorStore(VectorStore):
    backend = "chroma"

    def __init__(self, collection: str, directory: str) -> None:
        super().__init__(collection)
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        self._client = chromadb.PersistentClient(
            path=directory,
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine", "description": "s390x doc/script consistency index"},
        )

    @staticmethod
    def _clean(where: Where | None) -> Where | None:
        return where or None

    def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> int:
        if not chunks:
            return 0
        self._collection.upsert(
            ids=[chunk.id for chunk in chunks],
            documents=[chunk.text for chunk in chunks],
            metadatas=[sanitize_metadata(chunk.metadata) for chunk in chunks],
            embeddings=[list(vector) for vector in embeddings],
        )
        return len(chunks)

    def query(
        self, embedding: Sequence[float], top_k: int, where: Where | None = None
    ) -> list[SearchHit]:
        response = self._collection.query(
            query_embeddings=[list(embedding)],
            n_results=max(1, top_k),
            where=self._clean(where),
            include=["documents", "metadatas", "distances"],
        )
        ids = (response.get("ids") or [[]])[0]
        documents = (response.get("documents") or [[]])[0]
        metadatas = (response.get("metadatas") or [[]])[0]
        distances = (response.get("distances") or [[]])[0]
        hits: list[SearchHit] = []
        for index, chunk_id in enumerate(ids):
            distance = distances[index] if index < len(distances) else None
            hits.append(
                SearchHit(
                    id=chunk_id,
                    text=documents[index] if index < len(documents) else "",
                    metadata=dict(metadatas[index] or {}),
                    score=1.0 - float(distance) if distance is not None else None,
                )
            )
        return hits

    def fetch(self, where: Where | None = None, limit: int = 500) -> list[SearchHit]:
        response = self._collection.get(
            where=self._clean(where), limit=limit, include=["documents", "metadatas"]
        )
        ids = response.get("ids") or []
        documents = response.get("documents") or []
        metadatas = response.get("metadatas") or []
        return [
            SearchHit(
                id=chunk_id,
                text=documents[index] if index < len(documents) else "",
                metadata=dict(metadatas[index] or {}),
            )
            for index, chunk_id in enumerate(ids)
        ]

    def delete(self, where: Where) -> int:
        if not where:
            raise ValueError("refusing to delete the whole collection; pass a filter")
        matching = self._collection.get(where=where, include=["metadatas"])
        ids = matching.get("ids") or []
        if ids:
            self._collection.delete(ids=ids)
        return len(ids)

    def count(self, where: Where | None = None) -> int:
        if where:
            return len(self._collection.get(where=where, include=["metadatas"]).get("ids") or [])
        return int(self._collection.count())

    def reset(self) -> None:
        self._client.delete_collection(self.collection)
        self._collection = self._client.get_or_create_collection(
            name=self.collection,
            metadata={"hnsw:space": "cosine", "description": "s390x doc/script consistency index"},
        )

    def distinct_packages(self) -> list[str]:
        records = self._collection.get(include=["metadatas"])
        names = {
            str(meta.get("package_name"))
            for meta in (records.get("metadatas") or [])
            if meta and meta.get("package_name")
        }
        return sorted(names)


class QdrantVectorStore(VectorStore):
    backend = "qdrant"

    def __init__(self, collection: str, url: str, api_key: str | None = None, dimension: int = 256) -> None:
        super().__init__(collection)
        from qdrant_client import QdrantClient

        self._client = QdrantClient(url=url, api_key=api_key or None, timeout=60)
        self._dimension = dimension
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        from qdrant_client import models as qmodels

        if self._client.collection_exists(self.collection):
            return
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=qmodels.VectorParams(size=self._dimension, distance=qmodels.Distance.COSINE),
        )
        for field_name, schema in (
            ("package_name", qmodels.PayloadSchemaType.KEYWORD),
            ("distro", qmodels.PayloadSchemaType.KEYWORD),
            ("source_type", qmodels.PayloadSchemaType.KEYWORD),
            ("source_path", qmodels.PayloadSchemaType.KEYWORD),
        ):
            self._client.create_payload_index(
                collection_name=self.collection, field_name=field_name, field_schema=schema
            )

    def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> int:
        from qdrant_client import models as qmodels

        if not chunks:
            return 0
        points = []
        for chunk, vector in zip(chunks, embeddings, strict=False):
            point_id = int(hashlib.blake2b(chunk.id.encode(), digest_size=8).hexdigest(), 16) >> 1
            points.append(
                qmodels.PointStruct(
                    id=point_id,
                    vector=list(vector),
                    payload={"chunk_id": chunk.id, "text": chunk.text, **sanitize_metadata(chunk.metadata)},
                )
            )
        self._client.upsert(collection_name=self.collection, points=points, wait=True)
        return len(points)

    def _to_hits(self, points: Iterable[Any]) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for point in points:
            payload = dict(point.payload or {})
            text = str(payload.pop("text", ""))
            chunk_id = str(payload.pop("chunk_id", point.id))
            hits.append(SearchHit(id=chunk_id, text=text, metadata=payload, score=getattr(point, "score", None)))
        return hits

    def query(
        self, embedding: Sequence[float], top_k: int, where: Where | None = None
    ) -> list[SearchHit]:
        query_filter = to_qdrant_filter(where or {})
        try:
            response = self._client.query_points(
                collection_name=self.collection,
                query=list(embedding),
                limit=max(1, top_k),
                query_filter=query_filter,
                with_payload=True,
            )
            points = response.points
        except AttributeError:  # pragma: no cover - older qdrant-client
            points = self._client.search(
                collection_name=self.collection,
                query_vector=list(embedding),
                limit=max(1, top_k),
                query_filter=query_filter,
                with_payload=True,
            )
        return self._to_hits(points)

    def fetch(self, where: Where | None = None, limit: int = 500) -> list[SearchHit]:
        records, _ = self._client.scroll(
            collection_name=self.collection,
            scroll_filter=to_qdrant_filter(where or {}),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return self._to_hits(records)

    def delete(self, where: Where) -> int:
        from qdrant_client import models as qmodels

        if not where:
            raise ValueError("refusing to delete the whole collection; pass a filter")
        before = self.count(where)
        self._client.delete(
            collection_name=self.collection,
            points_selector=qmodels.FilterSelector(filter=to_qdrant_filter(where)),
            wait=True,
        )
        return before

    def count(self, where: Where | None = None) -> int:
        if not where:
            return int(self._client.count(collection_name=self.collection, exact=True).count)
        return int(
            self._client.count(
                collection_name=self.collection,
                count_filter=to_qdrant_filter(where),
                exact=True,
            ).count
        )

    def reset(self) -> None:
        self._client.delete_collection(self.collection)
        self._ensure_collection()


def build_vector_store(settings: Settings, dimension: int | None = None) -> VectorStore:
    """Instantiate the configured backend."""
    if settings.vector_backend == "qdrant":
        return QdrantVectorStore(
            collection=settings.qdrant_collection,
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            dimension=dimension or settings.embedding_dim,
        )
    return ChromaVectorStore(
        collection=settings.chroma_collection,
        directory=str(settings.chroma_dir),
    )


__all__ = [
    "Chunk",
    "Embedder",
    "HashingEmbedder",
    "OpenAIEmbedder",
    "QdrantVectorStore",
    "ChromaVectorStore",
    "SearchHit",
    "VectorStore",
    "and_all",
    "build_embedder",
    "build_vector_store",
    "match_all",
    "match_any",
    "match_package",
    "sanitize_metadata",
    "to_qdrant_filter",
]
