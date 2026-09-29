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
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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


if __name__ == "__main__":  # pragma: no cover - convenience runner
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True, log_level=settings.log_level.lower())
