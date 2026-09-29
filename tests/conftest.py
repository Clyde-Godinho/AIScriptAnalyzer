from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.analyzer import Analyzer, Retriever
from app.config import Settings
from app.db import Repository, init_db, make_engine
from app.ingestion import IngestionPipeline
from app.jobs import JobRegistry
from app.llm import StructuredLLM
from app.main import Services
from app.vector_store import HashingEmbedder
from tests.fakes import InMemoryVectorStore
from tests.samples import (
    SCRIPT_COMMIT,
    SCRIPT_NAME,
    SCRIPT_TEXT,
    WIKI_COMMIT,
    WIKI_PAGE_NAME,
    WIKI_TEXT,
)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    resolved = Settings(
        _env_file=None,  # ignore any developer .env for deterministic tests
        work_dir=tmp_path / "repos",
        chroma_dir=tmp_path / "chroma",
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'analyzer.db').as_posix()}",
        embedding_provider="hash",
        embedding_dim=128,
        llm_provider="mock",
        chunk_size=600,
        chunk_overlap=80,
        api_token=None,
    )
    resolved.ensure_directories()
    return resolved


@pytest.fixture()
def repository(settings: Settings) -> Iterator[Repository]:
    repo = Repository(make_engine(settings.database_url))
    init_db(repo.engine)
    yield repo
    repo.engine.dispose()


@pytest.fixture()
def store() -> InMemoryVectorStore:
    return InMemoryVectorStore()


@pytest.fixture()
def embedder() -> HashingEmbedder:
    return HashingEmbedder(dimension=128)


@pytest.fixture()
def pipeline(
    settings: Settings,
    repository: Repository,
    store: InMemoryVectorStore,
    embedder: HashingEmbedder,
) -> IngestionPipeline:
    return IngestionPipeline(settings, repository, embedder, store)


@pytest.fixture()
def corpus(pipeline: IngestionPipeline, settings: Settings, repository: Repository) -> dict[str, str]:
    """Write a fake wiki page and build script to disk and index them."""
    wiki_dir = settings.work_dir / "docs.wiki"
    scripts_dir = settings.work_dir / "scripts" / "example" / "2.4.0"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    doc_path = wiki_dir / WIKI_PAGE_NAME
    script_path = scripts_dir / SCRIPT_NAME
    doc_path.write_text(WIKI_TEXT, encoding="utf-8")
    script_path.write_text(SCRIPT_TEXT, encoding="utf-8")

    repository.set_repo_state("docs_wiki", "file:///fake/docs.wiki", WIKI_COMMIT)
    repository.set_repo_state("scripts", "file:///fake/scripts", SCRIPT_COMMIT)

    from app.ingestion import DiscoveredPackage
    from app.models import Distro

    discovered = DiscoveredPackage(
        package_name="example",
        normalized_name="example",
        doc_path=doc_path,
        script_paths={"2.4.0": script_path},
        distros=[Distro.UBUNTU, Distro.RHEL],
    )
    stats = pipeline.index([discovered], full_reindex=True)
    assert stats.errors == []
    return {"doc_path": str(doc_path), "script_path": str(script_path), "chunks": stats.chunks_indexed}


@pytest.fixture()
def services_retriever(services: Services, corpus: dict[str, str]) -> Retriever:
    """Retriever wired to a corpus that has already been indexed."""
    return services.retriever


@pytest.fixture()
def services(
    settings: Settings,
    repository: Repository,
    store: InMemoryVectorStore,
    embedder: HashingEmbedder,
    pipeline: IngestionPipeline,
) -> Services:
    retriever = Retriever(settings, store, embedder)
    llm = StructuredLLM(settings)
    return Services(
        settings=settings,
        repository=repository,
        embedder=embedder,
        store=store,
        pipeline=pipeline,
        retriever=retriever,
        analyzer=Analyzer(settings, repository, retriever, llm),
        llm=llm,
        jobs=JobRegistry(),
    )
