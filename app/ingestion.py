"""Repository ingestion: clone/pull, discover doc-script pairs, chunk and index.

Matching is deterministic and explainable:

1. Wiki pages are named ``Building-<Package>`` (``docs/wiki/Building-Nginx.md``);
   build scripts live at ``scripts/<Package>/<Version>/build_<Package>.sh``.
2. Names are normalized (lower-case, non-alphanumerics stripped) and run through
   a small alias table so ``Golang`` pairs with ``go``.
3. Every chunk is tagged with the exact ``(package_name, distro)`` pair it
   belongs to, so analysis-time retrieval is a metadata filter, not a guess.

The pipeline is synchronous (subprocess + blocking embed calls); the API layer
runs it in a worker thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import difflib
import logging
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import Repository
from app.models import AnalysisStatus, Distro, PackageRef, SourceType, match_key
from app.parsers import (
    canonical_package_name,
    chunk_text,
    detect_distros,
    doc_package_from_path,
    guess_package_from_path,
    iter_code_blocks,
    iter_markdown_sections,
    normalize_package_name,
    version_from_path,
)
from app.vector_store import Chunk, Embedder, VectorStore, match_all, match_package

logger = logging.getLogger(__name__)

DOC_SUFFIXES = (".md", ".markdown")
WIKI_IGNORED_NAMES = {"home", "_sidebar", "_footer", "_header", "readme", "index"}
SCRIPT_NAME_RE = re.compile(r"^build[-_ ]?(?P<pkg>[\w.+-]*?)(?:[-_]s390x)?\.(?:sh|bash)$", re.IGNORECASE)


class GitError(RuntimeError):
    """Raised when a git operation fails."""


# --------------------------------------------------------------------------- #
# Repository checkouts
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RepoCheckout:
    name: str
    url: str
    path: Path
    head: str | None = None
    previous_head: str | None = None

    @property
    def changed(self) -> bool:
        return self.previous_head != self.head


@dataclass(slots=True)
class DiscoveredPackage:
    """A doc page paired with the build script(s) for the same package."""

    package_name: str
    normalized_name: str
    doc_path: Path | None = None
    script_paths: dict[str, Path] = field(default_factory=dict)
    distros: list[Distro] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: How the pair was established: exact | version-stripped | suffix | fuzzy | unmatched.
    match_rule: str = "exact"

    @property
    def versions(self) -> list[str]:
        return sorted(self.script_paths, reverse=True)

    @property
    def primary_script(self) -> Path | None:
        for version in self.versions:
            return self.script_paths[version]
        return None

    @property
    def match_keys(self) -> list[str]:
        return [match_key(self.package_name, distro) for distro in (self.distros or [Distro.UNKNOWN])]

    def to_ref(self, repo_root: Path | None = None) -> PackageRef:
        def rel(path: Path | None) -> str | None:
            if path is None:
                return None
            if repo_root is not None:
                try:
                    return path.relative_to(repo_root).as_posix()
                except ValueError:
                    pass
            return path.as_posix()

        return PackageRef(
            package_name=self.package_name,
            normalized_name=self.normalized_name,
            distros=self.distros or [Distro.UNKNOWN],
            doc_path=rel(self.doc_path),
            script_path=rel(self.primary_script),
            script_paths={version: rel(path) or "" for version, path in self.script_paths.items()},
            versions=self.versions,
            errors=list(self.warnings),
        )


class GitRepository:
    """Thin subprocess wrapper around the ``git`` CLI."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _run(self, args: Sequence[str], cwd: Path | None = None) -> str:
        if shutil.which("git") is None:  # pragma: no cover - environment issue
            raise GitError("the `git` executable was not found on PATH")
        try:
            completed = subprocess.run(  # noqa: S603 - args are built internally
                ["git", *args],
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=self.settings.git_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - slow network
            raise GitError(f"git {' '.join(args)} timed out") from exc
        if completed.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({completed.returncode}): {completed.stderr.strip()[:500]}"
            )
        return completed.stdout.strip()

    def head(self, path: Path) -> str | None:
        if not (path / ".git").exists():
            return None
        try:
            return self._run(["rev-parse", "HEAD"], cwd=path) or None
        except GitError:
            return None

    def sync(self, name: str, url: str, directory: Path, *, depth: int | None = None) -> RepoCheckout:
        """Clone when missing, otherwise fetch and hard-reset to the remote head."""
        depth = depth or self.settings.git_depth
        previous = self.head(directory) if directory.exists() else None

        if not (directory / ".git").exists():
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
            directory.parent.mkdir(parents=True, exist_ok=True)
            args = ["clone", "--quiet"]
            if depth > 0:
                args += ["--depth", str(depth)]
            args += [url, str(directory)]
            self._run(args)
        else:
            self._run(["remote", "set-url", "origin", url], cwd=directory)
            fetch = ["fetch", "--quiet", "--prune"]
            if depth > 0:
                fetch += ["--depth", str(depth)]
            self._run([*fetch, "origin"], cwd=directory)
            self._resolve_remote_head(directory)

        head = self.head(directory)
        logger.info("repository %s at %s (%s)", name, head, "updated" if head != previous else "unchanged")
        return RepoCheckout(name=name, url=url, path=directory, head=head, previous_head=previous)

    def _resolve_remote_head(self, directory: Path) -> None:
        """Reset onto whatever the remote's default branch now points at."""
        for candidate in ("origin/HEAD", "origin/main", "origin/master"):
            try:
                target = self._run(["rev-parse", candidate], cwd=directory)
            except GitError:
                continue
            if target:
                self._run(["reset", "--hard", "--quiet", target], cwd=directory)
                return
        self._run(["pull", "--quiet", "--ff-only"], cwd=directory)

    def changed_files(self, directory: Path, old: str | None, new: str | None) -> list[str] | None:
        """Files changed between two commits, or ``None`` when that is unknowable."""
        if not old or not new or old == new:
            return []
        try:
            output = self._run(["diff", "--name-only", old, new], cwd=directory)
        except GitError:
            return None  # shallow clone without the old object: treat as "reindex all"
        return [line for line in output.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _is_wiki_page(path: Path) -> bool:
    stem = path.stem.lower()
    return path.suffix.lower() in DOC_SUFFIXES and stem not in WIKI_IGNORED_NAMES


def discover_documents(wiki_dir: Path, extra_dirs: Iterable[Path] = ()) -> dict[str, Path]:
    """Map ``normalized package name -> doc path`` from the wiki (plus fallbacks)."""
    documents: dict[str, Path] = {}
    for directory in [wiki_dir, *extra_dirs]:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or not _is_wiki_page(path):
                continue
            name = doc_package_from_path(path.as_posix())
            if not name:
                continue
            normalized = normalize_package_name(name)
            documents.setdefault(normalized, path)
            documents.setdefault(canonical_package_name(name), path)
    return documents


def discover_scripts(scripts_dir: Path) -> dict[str, dict[str, Path]]:
    """Map ``normalized package name -> {version: build script path}``.

    In ``linux-on-ibm-z/scripts`` the **directory** is the package identity
    (``scripts/ApacheKafka/2.8.0/build_kafka_AdoptiumTemurin.sh``) and the file
    name only identifies the variant (Adoptium Temurin vs IBM Semeru JVM, for
    example). Variants of the same version are therefore stored as
    ``"<version>:<variant>"`` so none of them is silently dropped.
    """
    buckets: dict[str, dict[str, Path]] = {}
    if not scripts_dir.exists():
        return {}

    for path in sorted(scripts_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".sh", ".bash"}:
            continue
        matched = SCRIPT_NAME_RE.match(path.name)
        if not matched:
            continue
        relative = path.relative_to(scripts_dir)
        package_dir = relative.parts[0] if len(relative.parts) > 1 else ""
        variant = (matched.group("pkg") or "").strip()
        if not variant or variant.lower() in {"build", "install"}:
            variant = ""
        name = package_dir or variant or guess_package_from_path(relative.as_posix())
        if not name:
            continue

        version = version_from_path(relative.as_posix(), name) or "default"
        bucket = buckets.setdefault(normalize_package_name(name), {})
        key = version
        if key in bucket:  # same version built by several scripts
            key = f"{version}:{variant or path.stem}"
        bucket[key] = path

    scripts: dict[str, dict[str, Path]] = {}
    for normalized, bucket in buckets.items():
        scripts[normalized] = bucket
        alias = canonical_package_name(normalized)
        if alias != normalized:
            scripts.setdefault(alias, bucket)
    return scripts


def _strip_version_suffix(normalized_name: str) -> str:
    """``apachecassandra311`` -> ``apachecassandra``, ``antlr4`` -> ``antlr``.

    Wiki pages carry trailing version markers (``Building-ANTLR-4``,
    ``Building-Apache-Cassandra-3.11``) while the script tree usually does not.
    """
    stripped = re.sub(r"v?\d+(?:\.\d+)*[a-z]?$", "", normalized_name)
    return stripped if len(stripped) >= 4 else normalized_name


def match_leftover_script(
    doc_key: str, candidates: dict[str, dict[str, Path]]
) -> tuple[str, str] | None:
    """Deterministic fallback rules for names that did not match exactly.

    Ordered from safest to loosest, and every rule reports how it matched:

    1. ``version-stripped`` - the doc name minus its version suffix equals the script name
    2. ``suffix``           - one name is a suffix of the other (``Apache-ActiveMQ`` -> ``ActiveMQ``)
    3. ``fuzzy``            - high sequence similarity (typos, plural/singular)
    """
    stripped = _strip_version_suffix(doc_key)
    for script_key in sorted(candidates):
        if script_key == stripped:
            return script_key, "version-stripped"

    for script_key in sorted(candidates):
        short, long = sorted((doc_key, script_key), key=len)
        if len(short) >= 5 and long.endswith(short) and len(short) >= 0.5 * len(long):
            return script_key, "suffix"

    for script_key in sorted(candidates):
        if difflib.SequenceMatcher(None, doc_key, script_key).ratio() >= 0.88:
            return script_key, "fuzzy"
    return None


class PackageMatcher:
    """Deterministically pairs wiki pages with build scripts."""

    def __init__(self, documents: dict[str, Path], scripts: dict[str, dict[str, Path]]) -> None:
        self.documents = documents
        self.scripts = scripts

    def match(self) -> list[DiscoveredPackage]:
        claimed: set[str] = set()
        pairs: list[tuple[str, Path | None, dict[str, Path], str]] = []

        for doc_key in sorted(self.documents):
            doc_path = self.documents[doc_key]
            if doc_key in self.scripts:
                claimed.add(doc_key)
                pairs.append((doc_key, doc_path, dict(self.scripts[doc_key]), "exact"))
                continue
            leftovers = {
                key: value
                for key, value in self.scripts.items()
                if key not in self.documents and key not in claimed
            }
            found = match_leftover_script(doc_key, leftovers)
            if found is None:
                # A version-specific page (``Building-Apache-Cassandra-3.11``) shares
                # the script tree of its base page (``Building-Apache-Cassandra``).
                base = _strip_version_suffix(doc_key)
                if base != doc_key and base in self.scripts and base in self.documents:
                    pairs.append((doc_key, doc_path, dict(self.scripts[base]), "version-variant"))
                    continue
                pairs.append((doc_key, doc_path, {}, "unmatched"))
                continue
            script_key, rule = found
            claimed.add(script_key)
            pairs.append((doc_key, doc_path, dict(self.scripts[script_key]), rule))

        for script_key in sorted(self.scripts):
            if script_key in claimed or script_key in self.documents:
                continue
            pairs.append((script_key, None, dict(self.scripts[script_key]), "unmatched"))

        packages: list[DiscoveredPackage] = []
        for key, doc_path, script_paths, rule in pairs:
            name = (
                doc_package_from_path(doc_path.as_posix())
                if doc_path is not None
                else guess_package_from_path(next(iter(script_paths.values())).as_posix())
            )
            discovered = DiscoveredPackage(
                package_name=name,
                normalized_name=key,
                doc_path=doc_path,
                script_paths=script_paths,
                match_rule=rule,
            )
            if doc_path is None:
                discovered.warnings.append("no matching wiki page found for this build script")
            if not script_paths:
                discovered.warnings.append("no build script found for this wiki page")
            packages.append(discovered)
        return packages


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def _chunk_id(*parts: str) -> str:
    from app.models import stable_id

    return stable_id(*parts, length=32)


def build_doc_chunks(
    text: str,
    *,
    package_name: str,
    distros: Sequence[Distro],
    source_path: str,
    commit: str | None,
    repo: str,
    settings: Settings,
) -> list[Chunk]:
    """Chunk a wiki page, tagging each chunk with the distro it documents.

    Sections under a distro heading are tagged with that distro; sections
    without one are shared and tagged ``unknown`` (the retriever merges them
    back in for whichever distro is analysed). A page with no headings at all
    is replicated across the package's distros, because there is nothing to
    scope it with.
    """
    targets = [d.value for d in (distros or [Distro.UNKNOWN])]
    sections = iter_markdown_sections(text)
    chunks: list[Chunk] = []

    if not sections:
        pieces = chunk_text(text, settings.chunk_size, settings.chunk_overlap)
        index = 0
        for distro_value in targets:
            for piece in pieces:
                chunks.append(
                    _make_chunk(
                        text=piece,
                        package_name=package_name,
                        distro=distro_value,
                        source_type=SourceType.DOC,
                        source_path=source_path,
                        repo=repo,
                        commit=commit,
                        index=index,
                        heading=package_name,
                        extra={"doc_commands": ",".join(_command_kinds(piece)) or "none"},
                    )
                )
                index += 1
        return chunks

    index = 0
    for section in sections:
        body = section.text.strip()
        if not body:
            continue
        # Sections without a distro heading are shared: they are tagged
        # ``unknown`` and merged back in by the retriever, instead of being
        # replicated into every distro (which would blur a per-distro analysis).
        for piece in chunk_text(body, settings.chunk_size, settings.chunk_overlap):
            heading = section.heading_path
            chunks.append(
                _make_chunk(
                    text=f"{'#' * max(1, section.level)} {heading}\n\n{piece}",
                    package_name=package_name,
                    distro=section.distro.value,
                    source_type=SourceType.DOC,
                    source_path=source_path,
                    repo=repo,
                    commit=commit,
                    index=index,
                    heading=heading,
                    extra={
                        "section_level": section.level,
                        "section_topic": section.heading,
                        "doc_commands": ",".join(_command_kinds(piece)) or "none",
                    },
                )
            )
            index += 1
    return chunks


def build_script_chunks(
    text: str,
    *,
    package_name: str,
    distros: Sequence[Distro],
    source_path: str,
    commit: str | None,
    repo: str,
    version: str | None,
    settings: Settings,
) -> list[Chunk]:
    """Chunk a build script, tagging each chunk with the distros it branches for."""
    targets = [d.value for d in (distros or [Distro.UNKNOWN])]
    lines = text.splitlines()
    block_size = max(40, settings.chunk_size // 40)  # ~35 lines per chunk at default settings

    chunks: list[Chunk] = []
    for start in range(0, max(1, len(lines)), block_size):
        window = lines[start : start + block_size + 4]  # small forward overlap for context
        if not any(line.strip() for line in window):
            continue
        body = "\n".join(window).strip()
        index = start // block_size
        for distro_value in targets:
            chunks.append(
                _make_chunk(
                    text=body,
                    package_name=package_name,
                    distro=distro_value,
                    source_type=SourceType.SCRIPT,
                    source_path=source_path,
                    repo=repo,
                    commit=commit,
                    index=index,
                    heading=f"{source_path}:{start + 1}",
                    extra={
                        "line_start": start + 1,
                        "line_end": start + len(window),
                        "version": version or "default",
                    },
                )
            )
    return chunks


def _command_kinds(text: str) -> list[str]:
    kinds: list[str] = []
    for _, code, _ in iter_code_blocks(text):
        lowered = code.lower()
        for token in ("apt-get", "apt ", "yum", "dnf", "zypper", "configure", "cmake", "make", "patch"):
            if token in lowered and token.strip() not in kinds:
                kinds.append(token.strip())
    return kinds


def _make_chunk(
    *,
    text: str,
    package_name: str,
    distro: str,
    source_type: SourceType,
    source_path: str,
    repo: str,
    commit: str | None,
    index: int,
    heading: str,
    extra: dict[str, Any] | None = None,
) -> Chunk:
    metadata: dict[str, Any] = {
        "package_name": package_name,
        "normalized_name": normalize_package_name(package_name),
        "distro": distro,
        "match_key": match_key(package_name, distro),
        "source_type": source_type.value,
        "source_path": source_path,
        "repo": repo,
        "commit": commit or "unknown",
        "chunk_index": index,
        "heading": heading,
        "chars": len(text),
    }
    metadata.update(extra or {})
    return Chunk(
        id=_chunk_id(package_name, distro, source_type.value, source_path, str(index)),
        text=text,
        metadata=metadata,
    )


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class IngestionStats:
    repositories: dict[str, str | None] = field(default_factory=dict)
    discovered: int = 0
    matched_pairs: int = 0
    unmatched_docs: int = 0
    unmatched_scripts: int = 0
    packages_indexed: int = 0
    chunks_indexed: int = 0
    chunks_pruned: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    full_reindex: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "repositories": self.repositories,
            "discovered": self.discovered,
            "matched_pairs": self.matched_pairs,
            "unmatched_docs": self.unmatched_docs,
            "unmatched_scripts": self.unmatched_scripts,
            "packages_indexed": self.packages_indexed,
            "chunks_indexed": self.chunks_indexed,
            "chunks_pruned": self.chunks_pruned,
            "skipped_unchanged": self.skipped,
            "errors": self.errors,
            "full_reindex": self.full_reindex,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
        return payload


class IngestionPipeline:
    """End-to-end sync: repos -> discovery -> chunking -> embeddings -> store."""

    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        embedder: Embedder,
        vector_store: VectorStore,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.embedder = embedder
        self.store = vector_store
        self.git = GitRepository(settings)

    # -- paths ---------------------------------------------------------- #
    @property
    def wiki_dir(self) -> Path:
        return self.settings.work_dir / "docs.wiki"

    @property
    def docs_dir(self) -> Path:
        return self.settings.work_dir / "docs"

    @property
    def scripts_dir(self) -> Path:
        return self.settings.work_dir / "scripts"

    # -- repositories ---------------------------------------------------- #
    def sync_repositories(self) -> dict[str, RepoCheckout]:
        checkouts = {
            "docs_wiki": self.git.sync("docs_wiki", self.settings.docs_wiki_url, self.wiki_dir),
            "docs": self.git.sync("docs", self.settings.docs_repo_url, self.docs_dir),
            "scripts": self.git.sync("scripts", self.settings.scripts_repo_url, self.scripts_dir),
        }
        for name, checkout in checkouts.items():
            self.repository.set_repo_state(name, checkout.url, checkout.head)
        return checkouts

    # -- discovery ------------------------------------------------------- #
    def discover(self) -> list[DiscoveredPackage]:
        documents = discover_documents(self.wiki_dir, extra_dirs=[self.docs_dir])
        scripts = discover_scripts(self.scripts_dir)
        packages = PackageMatcher(documents, scripts).match()
        for package in packages:
            package.distros = self._resolve_distros(package)
        return packages

    def _resolve_distros(self, package: DiscoveredPackage) -> list[Distro]:
        found: list[Distro] = []
        if package.doc_path and package.doc_path.exists():
            text = _read_text(package.doc_path)
            for section in iter_markdown_sections(text):
                if section.distro is not Distro.UNKNOWN and section.distro not in found:
                    found.append(section.distro)
        for path in package.script_paths.values():
            for distro in detect_distros(_read_text(path)):
                if distro not in found:
                    found.append(distro)
        return found or [Distro.UNKNOWN]

    # -- indexing -------------------------------------------------------- #
    def index(
        self,
        packages: Sequence[DiscoveredPackage],
        *,
        full_reindex: bool = False,
        only: set[str] | None = None,
    ) -> IngestionStats:
        stats = IngestionStats(full_reindex=full_reindex)
        stats.discovered = len(packages)
        stats.matched_pairs = sum(1 for p in packages if p.doc_path and p.script_paths)
        stats.unmatched_docs = sum(1 for p in packages if p.doc_path and not p.script_paths)
        stats.unmatched_scripts = sum(1 for p in packages if p.script_paths and not p.doc_path)

        if full_reindex:
            logger.warning("full reindex requested: resetting the vector collection")
            self.store.reset()

        doc_commit = self.repository.repo_state().get("docs_wiki")
        script_commit = self.repository.repo_state().get("scripts")

        for package in packages:
            if only is not None:
                wanted = (
                    package.normalized_name in only
                    or normalize_package_name(package.package_name) in only
                )
                if not wanted:
                    continue
            try:
                chunks = self._chunks_for(package, doc_commit, script_commit)
                if not chunks and not full_reindex:
                    stats.skipped += 1
                    continue
                written = self._write(package, chunks, doc_commit, script_commit)
                stats.chunks_indexed += written
                stats.packages_indexed += 1
            except Exception as exc:  # noqa: BLE001 - one bad package must not stop the run
                logger.exception("failed indexing %s", package.package_name)
                stats.errors.append(f"{package.package_name}: {exc}")
                self.repository.set_package_status(
                    package.package_name,
                    package.distros[0] if package.distros else Distro.UNKNOWN,
                    AnalysisStatus.ERROR,
                    error=str(exc),
                )

        stats.finished_at = datetime.now(timezone.utc)
        return stats

    def _chunks_for(
        self, package: DiscoveredPackage, doc_commit: str | None, script_commit: str | None
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        if package.doc_path and package.doc_path.exists():
            relative = self._relative(package.doc_path, repo="docs_wiki")
            chunks.extend(
                build_doc_chunks(
                    _read_text(package.doc_path),
                    package_name=package.package_name,
                    distros=package.distros,
                    source_path=relative,
                    commit=doc_commit,
                    repo="docs_wiki",
                    settings=self.settings,
                )
            )
        for version, path in package.script_paths.items():
            relative = self._relative(path, repo="scripts")
            chunks.extend(
                build_script_chunks(
                    _read_text(path),
                    package_name=package.package_name,
                    distros=package.distros,
                    source_path=relative,
                    commit=script_commit,
                    repo="scripts",
                    version=version,
                    settings=self.settings,
                )
            )
        return chunks

    def _write(
        self,
        package: DiscoveredPackage,
        chunks: list[Chunk],
        doc_commit: str | None,
        script_commit: str | None,
    ) -> int:
        # Replace this package's vectors so re-indexing never leaves stale chunks.
        self.store.delete(match_package(package.package_name))
        written = 0
        batch_size = 64
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            embeddings = self.embedder.embed([chunk.text for chunk in batch])
            written += self.store.upsert(batch, embeddings)

        # Store repo-relative paths so the API, the vector metadata and the
        # prune step all speak the same language.
        doc_path = self._relative(package.doc_path, repo="docs_wiki") if package.doc_path else None
        script_paths = {
            version: self._relative(path, repo="scripts")
            for version, path in package.script_paths.items()
        }
        primary = script_paths[package.versions[0]] if package.versions else None

        for distro in package.distros or [Distro.UNKNOWN]:
            self.repository.upsert_package(
                package_name=package.package_name,
                normalized_name=package.normalized_name,
                distro=distro,
                match_key=match_key(package.package_name, distro),
                doc_path=doc_path,
                script_path=primary,
                versions=package.versions,
                script_paths=script_paths,
                doc_commit=doc_commit,
                script_commit=script_commit,
                status=AnalysisStatus.INDEXED.value,
                chunk_count=len(chunks),
                last_indexed_at=datetime.now(timezone.utc),
                error="; ".join(package.warnings) or None,
            )
        return written

    def prune(self, packages: Sequence[DiscoveredPackage]) -> int:
        """Drop vectors belonging to sources that no longer exist in the repos."""
        live_paths = {chunk_source for chunk_source in self._live_source_paths(packages)}
        removed = 0
        for path in self._indexed_source_paths() - live_paths:
            removed += self.store.delete(match_all(source_path=path))
        return removed

    def _live_source_paths(self, packages: Sequence[DiscoveredPackage]) -> set[str]:
        paths: set[str] = set()
        for package in packages:
            if package.doc_path:
                paths.add(self._relative(package.doc_path, repo="docs_wiki"))
            for path in package.script_paths.values():
                paths.add(self._relative(path, repo="scripts"))
        return paths

    def _indexed_source_paths(self) -> set[str]:
        """Repo-relative paths recorded for already-indexed packages."""
        paths: set[str] = set()
        for summary in self.repository.list_packages(limit=10_000):
            if summary.doc_path:
                paths.add(summary.doc_path)
            record = self.repository.get_package(summary.package_name, summary.distro)
            if record is None:
                continue
            for value in (record.script_path, *(record.script_paths or {}).values()):
                if value:
                    paths.add(value)
        return paths

    @staticmethod
    def _relative(path: Path, repo: str) -> str:
        """Path as it should appear in metadata (repo-root relative)."""
        parts = path.as_posix().split("/")
        for anchor in ("docs.wiki", "scripts", "docs"):
            if anchor in parts:
                index = parts.index(anchor)
                tail = "/".join(parts[index + 1 :])
                return f"{anchor}/{tail}" if anchor == "scripts" else f"docs/wiki/{tail}"
        return path.name if repo else path.as_posix()

    # -- entry point ----------------------------------------------------- #
    def run(
        self,
        *,
        full_reindex: bool = False,
        packages: Sequence[str] | None = None,
        prune: bool = True,
    ) -> dict[str, Any]:
        checkouts = self.sync_repositories()
        discovered = self.discover()
        only = {normalize_package_name(name) for name in packages} if packages else None
        stats = self.index(discovered, full_reindex=full_reindex, only=only)
        stats.repositories = {name: checkout.head for name, checkout in checkouts.items()}
        if prune and not only:
            stats.chunks_pruned = self.prune(discovered)
        logger.info(
            "ingestion complete: %s packages, %s chunks (%s pruned)",
            stats.packages_indexed,
            stats.chunks_indexed,
            stats.chunks_pruned,
        )
        return stats.to_dict()

    # -- helpers used by the analyzer ------------------------------------ #
    def load_signatures(self, package_name: str, distro: Distro) -> tuple[Any, Any]:
        """Re-derive doc/script signatures from the indexed chunks (used by analyze)."""
        from app.parsers import parse_build_script, parse_markdown_doc

        doc_chunks = self.store.fetch(
            match_all(package_name=package_name, distro=distro.value, source_type="doc"), limit=500
        )
        script_chunks = self.store.fetch(
            match_all(package_name=package_name, distro=distro.value, source_type="script"), limit=500
        )
        doc_text = "\n\n".join(hit.text for hit in sorted(doc_chunks, key=_chunk_order))
        script_text = "\n\n".join(hit.text for hit in sorted(script_chunks, key=_chunk_order))
        doc_signature = (
            parse_markdown_doc(
                doc_text,
                package_name,
                source_path=doc_chunks[0].metadata.get("source_path", ""),
                distro=distro,
            )
            if doc_chunks
            else None
        )
        script_signature = (
            parse_build_script(
                script_text,
                package_name,
                source_path=script_chunks[0].metadata.get("source_path", ""),
                distro=distro,
            )
            if script_chunks
            else None
        )
        return doc_signature, script_signature


def _chunk_order(hit: Any) -> tuple[str, int]:
    return (str(hit.metadata.get("source_path", "")), int(hit.metadata.get("chunk_index", 0)))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - unreadable file
        logger.warning("cannot read %s: %s", path, exc)
        return ""


__all__ = [
    "DiscoveredPackage",
    "GitRepository",
    "IngestionPipeline",
    "IngestionStats",
    "PackageMatcher",
    "RepoCheckout",
    "build_doc_chunks",
    "build_script_chunks",
    "discover_documents",
    "discover_scripts",
    "match_leftover_script",
]
