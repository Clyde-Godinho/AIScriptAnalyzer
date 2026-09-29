from __future__ import annotations

import asyncio

import pytest

from app.analyzer import Analyzer, NotIndexedError, Retriever, RuleEngine, compute_score
from app.config import Settings
from app.db import Repository
from app.ingestion import DiscoveredPackage, IngestionPipeline, build_doc_chunks
from app.models import AnalysisStatus, Distro, Severity, SourceType
from app.vector_store import HashingEmbedder, match_all
from tests.fakes import InMemoryVectorStore


def test_index_tags_every_chunk_with_the_match_key(
    pipeline: IngestionPipeline, store: InMemoryVectorStore, corpus: dict[str, str]
) -> None:
    assert corpus["chunks"] > 0
    ubuntu_docs = store.fetch(match_all(package_name="example", distro="ubuntu", source_type="doc"))
    rhel_docs = store.fetch(match_all(package_name="example", distro="rhel", source_type="doc"))
    scripts = store.fetch(match_all(package_name="example", distro="ubuntu", source_type="script"))

    assert ubuntu_docs and rhel_docs and scripts
    assert all(hit.metadata["match_key"] == "example::ubuntu" for hit in ubuntu_docs)
    assert all(hit.metadata["source_type"] == "doc" for hit in ubuntu_docs)
    assert scripts[0].metadata["source_path"].endswith("build_example.sh")
    # Distro-specific sections stay scoped to their distro...
    rhel_text = " ".join(hit.text for hit in rhel_docs)
    assert "apt-get" not in rhel_text
    # ...while shared sections are tagged `unknown` and merged in at retrieval time.
    assert store.count(match_all(package_name="example", distro="unknown")) > 0


def test_package_rows_are_recorded_per_distro(
    pipeline: IngestionPipeline, repository: Repository, corpus: dict[str, str]
) -> None:
    packages = repository.list_packages()
    keys = {summary.match_key for summary in packages}
    assert keys == {"example::ubuntu", "example::rhel"}
    assert all(summary.status is AnalysisStatus.INDEXED for summary in packages)
    assert all(summary.doc_path and summary.doc_path.endswith("Building-Example.md") for summary in packages)


def test_doc_chunking_tags_sections_with_distro(settings: Settings) -> None:
    chunks = build_doc_chunks(
        "# Title\n\n## Ubuntu\n\nNo content here.\n\n## Ubuntu\n\n```bash\nsudo apt-get install -y a\n```\n",
        package_name="demo",
        distros=[Distro.UBUNTU],
        source_path="docs/wiki/Building-Demo.md",
        commit="c" * 40,
        repo="docs_wiki",
        settings=settings,
    )
    assert chunks
    assert all(chunk.metadata["distro"] == "ubuntu" for chunk in chunks)
    assert any("sudo apt-get install" in chunk.text for chunk in chunks)
    assert all(chunk.metadata["match_key"] == "demo::ubuntu" for chunk in chunks)


def test_retriever_returns_the_matched_pair(services_retriever) -> None:
    pair = services_retriever.retrieve("example", Distro.UBUNTU)
    assert pair.complete
    assert all(hit.metadata["source_type"] == "doc" for hit in pair.doc_hits)
    assert all(hit.metadata["source_type"] == "script" for hit in pair.script_hits)
    assert pair.doc_signature is not None and pair.script_signature is not None
    # The shared "Common prerequisites" section is merged back in.
    assert any(hit.metadata["distro"] == "unknown" for hit in pair.doc_hits)
    assert any("curl" in hit.text for hit in pair.doc_hits)
    assert pair.script_version == "2.4.0"


def test_retriever_picks_the_version_the_docs_mention(services, store, embedder, settings) -> None:
    """A package with many script versions is compared against the documented one."""
    from app.analyzer import doc_version_candidates, pick_script_version
    from app.vector_store import Chunk

    store.upsert(
        [
            Chunk(
                id=f"script-{version}",
                text="#!/bin/bash\n",
                metadata={
                    "normalized_name": "demo",
                    "package_name": "demo",
                    "match_key": "demo::ubuntu",
                    "distro": "ubuntu",
                    "source_type": "script",
                    "source_path": f"scripts/demo/{version}/build_demo.sh",
                    "version": version,
                    "chunk_index": 0,
                },
            )
            for version in ("1.2.0", "2.0.0", "3.1.4")
        ],
        [[0.1, 0.2], [0.2, 0.1], [0.3, 0.3]],
    )
    store.upsert(
        [
            Chunk(
                id="doc-0",
                text="Build demo-2.0.0 from source.",
                metadata={
                    "normalized_name": "demo",
                    "package_name": "demo",
                    "distro": "ubuntu",
                    "source_type": "doc",
                    "source_path": "docs/wiki/Building-Demo.md",
                    "chunk_index": 0,
                },
            )
        ],
        [[0.1, 0.1]],
    )

    pair = services.retriever.retrieve("demo", Distro.UBUNTU)
    assert pair.script_version == "2.0.0"
    assert doc_version_candidates("Build demo-2.0.0 and demo 1.2.0", "demo") == ["1.2.0", "2.0.0"]
    assert pick_script_version([], []) is None

    findings = RuleEngine().diff(
        pair.doc_signature, pair.script_signature, pair.doc_versions, "9.9.9"
    )
    assert [item.category.value for item in findings] == ["version_mismatch"]


def test_retriever_rejects_unknown_packages(services_retriever) -> None:
    with pytest.raises(NotIndexedError):
        services_retriever.retrieve("does-not-exist", Distro.UBUNTU)


def test_rule_engine_reports_the_expected_categories(
    services_retriever, corpus: dict[str, str]
) -> None:
    pair = services_retriever.retrieve("example", Distro.UBUNTU)
    findings = RuleEngine().diff(pair.doc_signature, pair.script_signature)
    categories = {finding.category.value for finding in findings}

    assert "missing_dependency" in categories  # libpcre3-dev, git, curl
    assert "env_var_value_mismatch" in categories  # EXAMPLE_ROOT
    assert "missing_build_flag" in categories  # --enable-shared, --prefix mismatch
    assert "missing_s390x_patch" in categories  # sed + patch never documented

    missing = {finding.component for finding in findings if finding.category.value == "missing_dependency"}
    assert {"libpcre3-dev", "git"} <= missing
    assert any(finding.severity is Severity.HIGH for finding in findings)
    assert all(finding.recommendation for finding in findings)
    assert 0 < compute_score(findings) < 100


def test_version_variant_dependencies_are_informational() -> None:
    """`openjdk-8-jdk` documented and `openjdk-17-jdk` installed is not a missing dep."""
    from app.parsers import parse_build_script, parse_markdown_doc

    doc = parse_markdown_doc("```bash\nsudo apt-get install -y openjdk-8-jdk\n```\n", "demo")
    script = parse_build_script("#!/bin/bash\nsudo apt-get install -y openjdk-17-jdk\n", "demo")

    findings = RuleEngine().diff(doc, script)
    by_category = {finding.category.value: finding.severity.value for finding in findings}
    assert by_category["version_mismatch"] == "info"
    assert "missing_dependency" not in by_category

    # A genuinely absent dependency stays a real finding.
    missing = parse_build_script("#!/bin/bash\nsudo apt-get install -y gcc\n", "demo")
    categories = {finding.category.value for finding in RuleEngine().diff(doc, missing)}
    assert "missing_dependency" in categories


def test_analyzer_persists_and_caches_reports(services, corpus: dict[str, str]) -> None:
    analyzer: Analyzer = services.analyzer

    report = asyncio.run(analyzer.analyze("example", Distro.UBUNTU))
    assert report.match_key == "example::ubuntu"
    assert report.discrepancies
    assert report.doc is not None and report.doc.source_type is SourceType.DOC
    assert report.script is not None
    assert report.summary
    # The mock provider keeps the rules authoritative, so the score is derived.
    assert report.model == "rules-only"
    assert any("mock" in warning for warning in report.warnings)

    cached = asyncio.run(analyzer.analyze("example", Distro.UBUNTU))
    assert cached.report_id == report.report_id

    stored = services.repository.list_reports("example")
    assert [item.report_id for item in stored] == [report.report_id]

    summary = next(item for item in services.repository.list_packages() if item.match_key == "example::ubuntu")
    assert summary.status is AnalysisStatus.ANALYZED
    assert summary.consistency_score == report.consistency_score
    assert summary.open_discrepancies == len(report.discrepancies)


def test_analyzer_honours_force(
    services, corpus: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    analyzer: Analyzer = services.analyzer
    asyncio.run(analyzer.analyze("example", Distro.UBUNTU))

    calls = {"count": 0}
    original = analyzer.llm.extract

    async def counting_extract(*args, **kwargs):
        calls["count"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(analyzer.llm, "extract", counting_extract)
    asyncio.run(analyzer.analyze("example", Distro.UBUNTU, force=True))
    assert calls["count"] == 1


def test_consistent_pair_scores_100(
    pipeline: IngestionPipeline,
    store: InMemoryVectorStore,
    repository: Repository,
    settings: Settings,
) -> None:
    doc = """\
# Building Clean

## Ubuntu

### Prerequisites

```bash
sudo apt-get install -y build-essential zlib1g-dev
export CLEAN_ROOT=/usr/local/clean
```

### Build

```bash
./configure --prefix=/usr/local/clean
make -j$(nproc)
```
"""
    script = """\
#!/bin/bash
export CLEAN_ROOT=/usr/local/clean
sudo apt-get install -y build-essential zlib1g-dev
./configure --prefix=/usr/local/clean
make -j$(nproc)
"""
    doc_path = settings.work_dir / "docs.wiki" / "Building-Clean.md"
    script_path = settings.work_dir / "scripts" / "clean" / "1.0" / "build_clean.sh"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(doc, encoding="utf-8")
    script_path.write_text(script, encoding="utf-8")
    repository.set_repo_state("docs_wiki", "file:///fake", "d" * 40)
    repository.set_repo_state("scripts", "file:///fake", "e" * 40)

    pipeline.index(
        [
            DiscoveredPackage(
                package_name="clean",
                normalized_name="clean",
                doc_path=doc_path,
                script_paths={"1.0": script_path},
                distros=[Distro.UBUNTU],
            )
        ],
        full_reindex=True,
    )

    retriever = Retriever(settings, store, HashingEmbedder(dimension=128))
    pair = retriever.retrieve("clean", Distro.UBUNTU)
    findings = RuleEngine().diff(pair.doc_signature, pair.script_signature)
    assert findings == []
    assert compute_score(findings) == 100


def test_prune_drops_vectors_for_removed_sources(
    pipeline: IngestionPipeline, store: InMemoryVectorStore, repository: Repository
) -> None:
    from tests.samples import SCRIPT_TEXT, WIKI_TEXT

    doc_path = pipeline.settings.work_dir / "docs.wiki" / "Building-Gone.md"
    script_path = pipeline.settings.work_dir / "scripts" / "gone" / "1.0" / "build_gone.sh"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(WIKI_TEXT.replace("Example", "Gone"), encoding="utf-8")
    script_path.write_text(SCRIPT_TEXT, encoding="utf-8")

    package = DiscoveredPackage(
        package_name="gone",
        normalized_name="gone",
        doc_path=doc_path,
        script_paths={"1.0": script_path},
        distros=[Distro.UBUNTU],
    )
    pipeline.index([package], full_reindex=True)
    assert store.count(match_all(package_name="gone")) > 0

    removed = pipeline.prune([])  # nothing is live any more
    assert removed > 0
    assert store.count(match_all(package_name="gone")) == 0


def test_discovery_pairs_wiki_pages_with_scripts(settings: Settings) -> None:
    wiki = settings.work_dir / "docs.wiki"
    scripts = settings.work_dir / "scripts"
    (wiki).mkdir(parents=True, exist_ok=True)
    (scripts / "nginx" / "1.24.0").mkdir(parents=True, exist_ok=True)
    (scripts / "orphan" / "3.0").mkdir(parents=True, exist_ok=True)
    (wiki / "Building-Nginx.md").write_text("# Building Nginx\n", encoding="utf-8")
    (wiki / "Building-Only-Docs.md").write_text("# Building Only Docs\n", encoding="utf-8")
    (wiki / "Home.md").write_text("# Home\n", encoding="utf-8")
    (scripts / "nginx" / "1.24.0" / "build_nginx.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts / "orphan" / "3.0" / "build_orphan.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    from app.ingestion import PackageMatcher, discover_documents, discover_scripts

    documents = discover_documents(wiki)
    scripts_map = discover_scripts(scripts)
    assert "nginx" in documents and "home" not in documents

    matched = {package.normalized_name: package for package in PackageMatcher(documents, scripts_map).match()}
    assert matched["nginx"].doc_path is not None
    assert "1.24.0" in matched["nginx"].versions
    assert matched["nginx"].script_paths["1.24.0"].name == "build_nginx.sh"
    assert matched["orphan"].doc_path is None
    assert "no matching wiki page" in matched["orphan"].warnings[0]
    assert matched["onlydocs"].script_paths == {}
