"""FastAPI entry point.

    uvicorn app.main:app --reload

Endpoints
---------
``POST /api/sync``            pull latest commits and (re)index the repositories
``GET  /api/packages``        list packages and their indexing/analysis status
``POST /api/analyze``         compare the matched doc/script pair with the LLM
``GET  /api/results/{pkg}``   past discrepancy reports for a package
``GET  /api/jobs/{job_id}``   status of a background sync/analyze job
``GET  /health``              liveness + index statistics

Long-running work is dispatched to the in-process job registry and can also be
awaited inline (``?wait=true`` on sync, ``{"background": ...}``-style query flag
on analyze), which is what the tests and smoke scripts use.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import __version__
from app.analyzer import AnalysisError, Analyzer, NotIndexedError, Retriever
from app.config import Settings, configure_logging, get_settings
from app.db import Repository, init_db, make_engine
from app.ingestion import IngestionPipeline
from app.jobs import JobRegistry
from app.llm import LLMError, StructuredLLM
from app.models import (
    AnalysisReport,
    AnalysisStatus,
    AnalyzeRequest,
    AnalyzeResponse,
    AuditReportCore,
    Distro,
    HealthResponse,
    JobState,
    JobStatus,
    PackageListResponse,
    ResultListResponse,
    SyncRequest,
    SyncResponse,
    normalize_distro,
)
from app.vector_store import Embedder, VectorStore, build_embedder, build_vector_store

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


# --------------------------------------------------------------------------- #
# Service container
# --------------------------------------------------------------------------- #


@dataclass
class Services:
    settings: Settings
    repository: Repository
    embedder: Embedder
    store: VectorStore
    pipeline: IngestionPipeline
    retriever: Retriever
    llm: StructuredLLM
    analyzer: Analyzer
    jobs: JobRegistry


def build_services(settings: Settings) -> Services:
    """Wire the object graph. Kept separate so tests can reuse it."""
    repository = Repository(make_engine(settings.database_url))
    init_db(repository.engine)
    embedder = build_embedder(settings)
    store = build_vector_store(settings, dimension=embedder.dimension)
    pipeline = IngestionPipeline(settings, repository, embedder, store)
    retriever = Retriever(settings, store, embedder)
    llm = StructuredLLM(settings)
    analyzer = Analyzer(settings, repository, retriever, llm)
    return Services(
        settings=settings,
        repository=repository,
        embedder=embedder,
        store=store,
        pipeline=pipeline,
        retriever=retriever,
        llm=llm,
        analyzer=analyzer,
        jobs=JobRegistry(),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_directories()
    try:
        app.state.services = build_services(settings)
        app.state.startup_error = None
        logger.info(
            "ready: backend=%s collection=%s embeddings=%s llm=%s",
            settings.vector_backend,
            settings.collection_name,
            settings.embedding_provider,
            settings.llm_provider,
        )
    except Exception as exc:  # noqa: BLE001 - keep /health reachable for diagnosis
        logger.exception("failed to initialise services")
        app.state.services = None
        app.state.startup_error = str(exc)
    try:
        yield
    finally:
        services: Services | None = getattr(app.state, "services", None)
        if services is not None:
            await services.jobs.shutdown()
            services.repository.engine.dispose()


app = FastAPI(
    title="s390x Documentation-Script Consistency Analyzer",
    description=(
        "RAG pipeline and analysis API that compares Linux on IBM Z build documentation "
        "(linux-on-ibm-z/docs wiki) with the corresponding automated build scripts "
        "(linux-on-ibm-z/scripts)."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def get_services(request: Request) -> Services:
    services: Services | None = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"service unavailable: {getattr(request.app.state, 'startup_error', 'not initialised')}",
        )
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> None:
    services: Services | None = getattr(request.app.state, "services", None)
    settings: Settings = services.settings if services is not None else get_settings()
    if not settings.api_token:
        return
    if credentials is None or credentials.credentials != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


AuthDep = Annotated[None, Depends(require_token)]


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


@app.exception_handler(NotIndexedError)
async def _not_indexed_handler(_: Request, exc: NotIndexedError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(LLMError)
async def _llm_handler(_: Request, exc: LLMError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": str(exc)})


@app.exception_handler(AnalysisError)
async def _analysis_handler(_: Request, exc: AnalysisError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "name": "s390x doc/script consistency analyzer",
        "version": __version__,
        "docs": "/docs",
        "endpoints": ["/api/sync", "/api/packages", "/api/analyze", "/api/results/{package}", "/health"],
    }


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health(request: Request) -> HealthResponse:
    services: Services | None = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=getattr(request.app.state, "startup_error", "not initialised"),
        )
    try:
        indexed_chunks = services.store.count()
    except Exception as exc:  # noqa: BLE001 - report the backend as degraded
        logger.warning("vector store unreachable: %s", exc)
        indexed_chunks = -1
    stats = services.repository.stats()
    settings = services.settings
    return HealthResponse(
        status="ok" if indexed_chunks >= 0 else "degraded",
        version=__version__,
        vector_backend=settings.vector_backend,
        collection=settings.collection_name,
        embedding_provider=settings.embedding_provider,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        indexed_chunks=max(0, indexed_chunks),
        packages=stats["packages"],
        repositories=services.repository.repo_state(),
    )


@app.post(
    "/api/sync",
    response_model=SyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["ingestion"],
)
async def sync(
    payload: SyncRequest,
    services: ServicesDep,
    _: AuthDep,
    wait: Annotated[bool, Query(description="Run inline and return ingestion counters")] = False,
) -> SyncResponse:
    """Pull the latest commits of both repositories and (re)index the corpus."""
    running = services.jobs.running_of("sync")
    if running is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a sync job is already running: {running.job_id}",
        )

    async def run() -> dict[str, Any]:
        return await asyncio.to_thread(
            services.pipeline.run,
            full_reindex=payload.full_reindex,
            packages=payload.packages,
            prune=payload.prune,
        )

    if wait:
        stats = await run()
        return SyncResponse(
            job_id="",
            state=JobState.SUCCEEDED,
            message="sync completed synchronously",
            stats=stats,
        )

    job = services.jobs.submit("sync", run)
    return SyncResponse(
        job_id=job.job_id,
        state=job.state,
        message="sync accepted; poll GET /api/jobs/{job_id} for status",
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatus, tags=["system"])
async def job_status(job_id: str, services: ServicesDep, _: AuthDep) -> JobStatus:
    job = services.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job id")
    return job


@app.post("/api/demo/seed", tags=["demo"])
async def seed_demo(services: ServicesDep, _: AuthDep) -> dict[str, Any]:
    """Seed sample packages (example and nginx) into the database and vector store for instant demo."""
    from tests.samples import WIKI_PAGE_NAME, SCRIPT_NAME, WIKI_TEXT, SCRIPT_TEXT, WIKI_COMMIT, SCRIPT_COMMIT
    from app.ingestion import DiscoveredPackage
    from app.models import Distro

    wiki_dir = services.settings.work_dir / "docs.wiki"
    example_script_dir = services.settings.work_dir / "scripts" / "example" / "2.4.0"
    nginx_script_dir = services.settings.work_dir / "scripts" / "nginx" / "1.24.0"

    wiki_dir.mkdir(parents=True, exist_ok=True)
    example_script_dir.mkdir(parents=True, exist_ok=True)
    nginx_script_dir.mkdir(parents=True, exist_ok=True)

    # 1. Example package with known drift
    doc_path = wiki_dir / WIKI_PAGE_NAME
    script_path = example_script_dir / SCRIPT_NAME
    doc_path.write_text(WIKI_TEXT, encoding="utf-8")
    script_path.write_text(SCRIPT_TEXT, encoding="utf-8")

    # 2. NGINX package in sync
    nginx_wiki = wiki_dir / "Building-NGINX.md"
    nginx_script = nginx_script_dir / "build_nginx.sh"
    nginx_wiki.write_text(
        "# Building NGINX\n\nInstructions for building NGINX 1.24.0 on Linux on IBM Z (s390x).\n\n"
        "## Ubuntu\n\n### Prerequisites\n```bash\nsudo apt-get update\n"
        "sudo apt-get install -y gcc make libpcre3-dev zlib1g-dev libssl-dev\n```\n\n"
        "### Build and install\n```bash\n./configure --prefix=/usr/local/nginx --with-http_ssl_module\n"
        "make -j$(nproc)\nsudo make install\n```\n",
        encoding="utf-8",
    )
    nginx_script.write_text(
        "#!/bin/bash\nset -e\nsudo apt-get update\n"
        "sudo apt-get install -y gcc make libpcre3-dev zlib1g-dev libssl-dev\n"
        "./configure --prefix=/usr/local/nginx --with-http_ssl_module\n"
        "make -j$(nproc)\nsudo make install\n",
        encoding="utf-8",
    )

    # 3. Redis package with minor drift
    redis_script_dir = services.settings.work_dir / "scripts" / "redis" / "7.2.4"
    redis_script_dir.mkdir(parents=True, exist_ok=True)
    redis_wiki = wiki_dir / "Building-Redis.md"
    redis_script = redis_script_dir / "build_redis.sh"
    redis_wiki.write_text(
        "# Building Redis\n\nInstructions for building Redis 7.2.4 on Linux on IBM Z (s390x).\n\n"
        "## Ubuntu\n\n### Prerequisites\n```bash\nsudo apt-get update\n"
        "sudo apt-get install -y gcc make tcl-dev libssl-dev\n```\n\n"
        "### Build and install\n```bash\nmake -j$(nproc)\nsudo make install\n```\n",
        encoding="utf-8",
    )
    redis_script.write_text(
        "#!/bin/bash\nset -e\nsudo apt-get update\n"
        "sudo apt-get install -y gcc make libssl-dev\n"
        "make MALLOC=libc -j$(nproc)\nsudo make install\n",
        encoding="utf-8",
    )

    services.repository.set_repo_state("docs_wiki", "https://github.com/linux-on-ibm-z/docs.wiki.git", WIKI_COMMIT)
    services.repository.set_repo_state("scripts", "https://github.com/linux-on-ibm-z/scripts.git", SCRIPT_COMMIT)

    discovered = [
        DiscoveredPackage(
            package_name="example",
            normalized_name="example",
            doc_path=doc_path,
            script_paths={"2.4.0": script_path},
            distros=[Distro.UBUNTU, Distro.RHEL],
        ),
        DiscoveredPackage(
            package_name="nginx",
            normalized_name="nginx",
            doc_path=nginx_wiki,
            script_paths={"1.24.0": nginx_script},
            distros=[Distro.UBUNTU],
        ),
        DiscoveredPackage(
            package_name="redis",
            normalized_name="redis",
            doc_path=redis_wiki,
            script_paths={"7.2.4": redis_script},
            distros=[Distro.UBUNTU],
        ),
    ]
    stats = services.pipeline.index(discovered, full_reindex=False)
    # Pre-analyze all 10 popular packages in background tasks so the seed endpoint responds instantly
    POPULAR_10 = [
        "nginx", "redis", "example", "PostgreSQL", "Apache-HTTP-Server",
        "Apache-Kafka", "HAProxy", "Elasticsearch", "RabbitMQ", "CouchDB"
    ]
    for pkg in POPULAR_10:
        asyncio.create_task(services.analyzer.analyze(pkg, Distro.UBUNTU, force=False))

    return {
        "status": "ok",
        "message": "Top 10 popular packages seeded and indexed successfully!",
        "chunks_indexed": stats.chunks_indexed,
    }


class SandboxRequest(BaseModel):
    package_name: str = "sandbox-pkg"
    distro: str = "ubuntu"
    wiki_content: str
    script_content: str


@app.post("/api/analyze/sandbox", response_model=AnalyzeResponse, tags=["analysis"])
async def analyze_sandbox(
    payload: SandboxRequest,
    services: ServicesDep,
    _: AuthDep,
) -> AnalyzeResponse:
    """Run instant live consistency check on arbitrary wiki and script text."""
    import time
    from datetime import datetime, timezone
    from app.parsers import parse_markdown_doc, parse_build_script
    from app.analyzer import RuleEngine, compute_score
    from app.models import (
        AnalysisReport,
        AnalysisStatus,
        JobState,
        Severity,
        match_key,
        normalize_distro,
        stable_id,
        verdict_for,
    )

    distro = normalize_distro(payload.distro)
    doc_sig = parse_markdown_doc(payload.wiki_content, payload.package_name, distro)
    script_sig = parse_build_script(payload.script_content, payload.package_name, distro)
    rules = RuleEngine()
    discrepancies = rules.diff(doc_sig, script_sig)
    score = compute_score(discrepancies)
    criticals = sum(1 for d in discrepancies if d.severity is Severity.CRITICAL)
    highs = sum(1 for d in discrepancies if d.severity is Severity.HIGH)
    verdict = verdict_for(score, criticals=criticals, highs=highs)
    created_at = datetime.now(timezone.utc)
    report = AnalysisReport(
        report_id=stable_id("sandbox", payload.package_name, distro.value, str(time.time())),
        package_name=payload.package_name,
        distro=distro,
        match_key=match_key(payload.package_name, distro),
        status=AnalysisStatus.ANALYZED,
        summary=f"Sandbox Audit: {len(discrepancies)} discrepancies detected between custom documentation and script.",
        consistency_score=score,
        discrepancies=discrepancies,
        model="rules-only",
        warnings=[],
        doc_signature=doc_sig,
        script_signature=script_sig,
        created_at=created_at,
    )
    return AnalyzeResponse(job_id=None, state=JobState.SUCCEEDED, report=report)


@app.get("/api/packages", response_model=PackageListResponse, tags=["analysis"])
async def list_packages(
    services: ServicesDep,
    _: AuthDep,
    status_filter: Annotated[AnalysisStatus | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(description="Substring match on the package name")] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> PackageListResponse:
    """List every discovered package plus its indexing/analysis status."""
    packages = services.repository.list_packages(status=status_filter, search=search, limit=limit)
    repos = services.repository.repo_state()
    return PackageListResponse(
        total=len(packages),
        index_commit=repos.get("scripts"),
        indexed_at=next(
            (item.last_indexed_at for item in packages if item.last_indexed_at is not None), None
        ),
        packages=packages,
    )


@app.post("/api/analyze", response_model=AnalyzeResponse, tags=["analysis"])
async def analyze(
    payload: AnalyzeRequest,
    services: ServicesDep,
    _: AuthDep,
    background: Annotated[bool, Query(description="Queue the analysis instead of awaiting it")] = False,
) -> AnalyzeResponse:
    """Retrieve the matched doc/script pair and run the LLM comparison."""
    distro = normalize_distro(payload.distro)

    if background:

        async def run() -> dict[str, Any]:
            report = await services.analyzer.analyze(
                payload.package_name,
                distro,
                force=payload.force,
                top_k=payload.top_k,
            )
            return report.model_dump(mode="json")

        job = services.jobs.submit("analyze", run)
        return AnalyzeResponse(job_id=job.job_id, state=job.state)

    report: AnalysisReport = await services.analyzer.analyze(
        payload.package_name,
        distro,
        force=payload.force,
        top_k=payload.top_k,
    )
    return AnalyzeResponse(report=report, state=JobState.SUCCEEDED)


@app.get("/api/results/{package}", response_model=ResultListResponse, tags=["analysis"])
async def results(
    package: str,
    services: ServicesDep,
    _: AuthDep,
    distro: Annotated[Distro | None, Query(description="Optional distro filter")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResultListResponse:
    """Past discrepancy reports for a package, newest first."""
    reports = services.repository.list_reports(package, distro, limit=limit, offset=offset)
    if not reports:
        known = services.repository.list_packages(search=package, limit=1)
        if not known:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown package {package!r}; see GET /api/packages",
            )
    return ResultListResponse(package_name=package, total=len(reports), reports=reports)


@app.get("/api/results/{package}/latest", response_model=AnalysisReport, tags=["analysis"])
async def latest_result(
    package: str,
    services: ServicesDep,
    _: AuthDep,
    distro: Annotated[Distro, Query()] = Distro.UBUNTU,
) -> AnalysisReport:
    report = services.repository.latest_report(package, distro)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no stored report for {package!r} on {normalize_distro(distro).value}",
        )
    return report


@app.get("/api/results/{package}/latest/audit", response_model=AuditReportCore, tags=["analysis"])
async def latest_result_audit(
    package: str,
    services: ServicesDep,
    _: AuthDep,
    distro: Annotated[Distro, Query()] = Distro.UBUNTU,
) -> AuditReportCore:
    """The latest report rendered in the strict audit JSON schema."""
    report = services.repository.latest_report(package, distro)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no stored report for {package!r} on {normalize_distro(distro).value}",
        )
    return report.audit


def _json_response(payload: Any, filename: str, download: bool) -> Response:
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'} if download else {}
    return Response(content=body, media_type="application/json", headers=headers)


def _safe_filename_part(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name) or "package"


@app.get("/api/results/{package}/latest/json", tags=["analysis"])
async def latest_result_json(
    package: str,
    services: ServicesDep,
    _: AuthDep,
    distro: Annotated[Distro, Query()] = Distro.UBUNTU,
    download: Annotated[bool, Query(description="Send as a downloadable .json file")] = False,
) -> Response:
    """The latest full comparison report as formatted JSON."""
    report = services.repository.latest_report(package, distro)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no stored report for {package!r} on {normalize_distro(distro).value}",
        )
    stem = _safe_filename_part(package)
    return _json_response(
        report.model_dump(mode="json"),
        f"{stem}-{normalize_distro(distro).value}-comparison.json",
        download,
    )


@app.get("/api/results/{package}/latest/audit/json", tags=["analysis"])
async def latest_result_audit_json(
    package: str,
    services: ServicesDep,
    _: AuthDep,
    distro: Annotated[Distro, Query()] = Distro.UBUNTU,
    download: Annotated[bool, Query(description="Send as a downloadable .json file")] = False,
) -> Response:
    """The latest report in the strict audit JSON schema (DISC-xx findings)."""
    report = services.repository.latest_report(package, distro)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no stored report for {package!r} on {normalize_distro(distro).value}",
        )
    stem = _safe_filename_part(package)
    return _json_response(
        report.audit.model_dump(mode="json"),
        f"{stem}-{normalize_distro(distro).value}-audit.json",
        download,
    )


if __name__ == "__main__":  # pragma: no cover - convenience runner
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True, log_level=settings.log_level.lower())
