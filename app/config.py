"""Runtime configuration.

Everything is env-driven so the same image can run against a local ChromaDB
directory or a remote Qdrant cluster, and against OpenAI/vLLM/Anthropic for the
LLM + embedding calls.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

VectorBackend = Literal["chroma", "qdrant"]
EmbeddingProvider = Literal["hash", "openai"]
LLMProvider = Literal["openai", "anthropic", "mock"]
ResponseMode = Literal["tools", "json", "md_json"]


class Settings(BaseSettings):
    """Application settings loaded from environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- repositories ------------------------------------------------------
    docs_wiki_url: str = "https://github.com/linux-on-ibm-z/docs.wiki.git"
    docs_repo_url: str = "https://github.com/linux-on-ibm-z/docs.git"
    scripts_repo_url: str = "https://github.com/linux-on-ibm-z/scripts.git"
    work_dir: Path = Path("data/repos")
    git_depth: int = 1
    git_timeout_seconds: int = 900

    # -- vector store ------------------------------------------------------
    vector_backend: VectorBackend = "chroma"
    chroma_dir: Path = Path("data/chroma")
    chroma_collection: str = "s390x_doc_script"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "s390x_doc_script"

    # -- embeddings --------------------------------------------------------
    embedding_provider: EmbeddingProvider = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 256

    # -- LLM ---------------------------------------------------------------
    llm_provider: LLMProvider = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_response_mode: ResponseMode = "tools"
    llm_temperature: float = 0.0
    llm_max_retries: int = 3
    llm_max_tokens: int = 4096
    llm_timeout_seconds: int = 180
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    anthropic_api_key: str | None = None

    # -- persistence -------------------------------------------------------
    database_url: str = "sqlite+pysqlite:///./data/analyzer.db"

    # -- RAG / chunking ----------------------------------------------------
    chunk_size: int = 1400
    chunk_overlap: int = 200
    retrieval_top_k: int = 8
    max_context_chars: int = 24_000

    # -- API ---------------------------------------------------------------
    api_token: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    log_level: str = "INFO"

    # ------------------------------------------------------------------ #
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            raw = value.strip()
            if raw.startswith("["):  # JSON list, parsed by pydantic-settings
                return value
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_lt_size(cls, value: int, info) -> int:
        size = info.data.get("chunk_size", 1400)
        if value >= size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return value

    # ------------------------------------------------------------------ #
    @property
    def data_dir(self) -> Path:
        return Path("data")

    def ensure_directories(self) -> None:
        """Create the writable directories the app expects."""
        for path in (self.work_dir, self.chroma_dir, self.data_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def llm_configured(self) -> bool:
        """True when the selected provider has usable credentials."""
        if self.llm_provider == "mock":
            return True
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        return bool(self.anthropic_api_key)

    @property
    def collection_name(self) -> str:
        return self.chroma_collection if self.vector_backend == "chroma" else self.qdrant_collection


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor used by FastAPI dependencies."""
    settings = Settings()
    return settings


def configure_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=(level or get_settings().log_level).upper(),
        format="%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
    )
