"""Persistence layer (SQLAlchemy 2.0).

Default is SQLite for zero-ops local runs; point ``DATABASE_URL`` at Postgres
for a shared deployment. Two tables carry the product state:

* ``packages`` - one row per ``(package_name, distro)`` pair, i.e. per
  deterministic match key, with its indexing/analysis status.
* ``reports``  - immutable analysis reports, stored as JSON next to the columns
  needed for querying (score, severity counts, commits).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from app.models import (
    AnalysisReport,
    AnalysisStatus,
    Distro,
    PackageSummary,
    match_key,
    normalize_distro,
)
from app.parsers import normalize_package_name

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RepositoryState(Base):
    """Tracks the commit currently indexed for each ingested repository."""

    __tablename__ = "repository_state"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(String(512))
    commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PackageRecord(Base):
    """One row per deterministic ``(package_name, distro)`` match."""

    __tablename__ = "packages"
    __table_args__ = (UniqueConstraint("match_key", name="uq_packages_match_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_key: Mapped[str] = mapped_column(String(200), index=True)
    package_name: Mapped[str] = mapped_column(String(120), index=True)
    normalized_name: Mapped[str] = mapped_column(String(120), index=True)
    distro: Mapped[str] = mapped_column(String(32), index=True, default=Distro.UNKNOWN.value)

    doc_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    script_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    versions: Mapped[list[str]] = mapped_column(JSON, default=list)
    script_paths: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    doc_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    script_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(24), default=AnalysisStatus.PENDING.value, index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consistency_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_discrepancies: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReportRecord(Base):
    """A stored analysis report."""

    __tablename__ = "reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    match_key: Mapped[str] = mapped_column(String(200), index=True)
    package_name: Mapped[str] = mapped_column(String(120), index=True)
    normalized_name: Mapped[str] = mapped_column(String(120), index=True)
    distro: Mapped[str] = mapped_column(String(32), index=True)
    summary: Mapped[str] = mapped_column(Text)
    consistency_score: Mapped[int] = mapped_column(Integer)
    discrepancy_count: Mapped[int] = mapped_column(Integer, default=0)
    severity_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    doc_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    script_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


def make_engine(database_url: str) -> Engine:
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    engine = create_engine(
        database_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    if database_url.startswith("sqlite"):
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


class Repository:
    """Thin data-access wrapper around a session factory."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Repository sync state
    # ------------------------------------------------------------------ #
    def set_repo_state(self, name: str, url: str, commit: str | None) -> None:
        with self.session() as session:
            record = session.get(RepositoryState, name)
            if record is None:
                record = RepositoryState(name=name, url=url, commit=commit)
                session.add(record)
            else:
                record.url = url
                record.commit = commit
                record.updated_at = utcnow()

    def repo_state(self) -> dict[str, str | None]:
        with self.session() as session:
            rows = session.scalars(select(RepositoryState)).all()
            return {row.name: row.commit for row in rows}

    # ------------------------------------------------------------------ #
    # Packages
    # ------------------------------------------------------------------ #
    def upsert_package(self, **fields: Any) -> None:
        key = fields.get("match_key") or match_key(fields["package_name"], fields["distro"])
        fields["match_key"] = key
        fields.setdefault("normalized_name", normalize_package_name(fields["package_name"]))
        fields["distro"] = normalize_distro(fields["distro"]).value
        with self.session() as session:
            record = session.scalar(select(PackageRecord).where(PackageRecord.match_key == key))
            if record is None:
                session.add(PackageRecord(**fields))
                return
            for name, value in fields.items():
                setattr(record, name, value)

    def set_package_status(
        self,
        package_name: str,
        distro: str | Distro,
        status: AnalysisStatus,
        *,
        error: str | None = None,
        chunk_count: int | None = None,
    ) -> None:
        key = match_key(package_name, distro)
        with self.session() as session:
            record = session.scalar(select(PackageRecord).where(PackageRecord.match_key == key))
            if record is None:
                record = PackageRecord(
                    match_key=key,
                    package_name=package_name,
                    normalized_name=normalize_package_name(package_name),
                    distro=normalize_distro(distro).value,
                )
                session.add(record)
            record.status = status.value
            record.error = error
            if chunk_count is not None:
                record.chunk_count = chunk_count

    def get_package(self, package_name: str, distro: str | Distro) -> PackageRecord | None:
        key = match_key(package_name, distro)
        with self.session() as session:
            return session.scalar(select(PackageRecord).where(PackageRecord.match_key == key))

    def list_packages(
        self, *, status: AnalysisStatus | None = None, search: str | None = None, limit: int = 500
    ) -> list[PackageSummary]:
        statement = select(PackageRecord).order_by(PackageRecord.package_name, PackageRecord.distro)
        if status is not None:
            statement = statement.where(PackageRecord.status == status.value)
        if search:
            statement = statement.where(
                PackageRecord.normalized_name.contains(normalize_package_name(search))
            )
        statement = statement.limit(limit)
        with self.session() as session:
            records = session.scalars(statement).all()
            return [self._to_summary(record) for record in records]

    def prune_packages(self, keep_match_keys: Sequence[str]) -> int:
        with self.session() as session:
            statement = delete(PackageRecord)
            if keep_match_keys:
                statement = statement.where(PackageRecord.match_key.notin_(list(keep_match_keys)))
            result = session.execute(statement)
            return int(result.rowcount or 0)

    @staticmethod
    def _to_summary(record: PackageRecord) -> PackageSummary:
        return PackageSummary(
            package_name=record.package_name,
            distro=normalize_distro(record.distro),
            match_key=record.match_key,
            status=AnalysisStatus(record.status),
            doc_path=record.doc_path,
            script_path=record.script_path,
            versions=list(record.versions or []),
            chunk_count=record.chunk_count,
            doc_commit=record.doc_commit,
            script_commit=record.script_commit,
            last_indexed_at=record.last_indexed_at,
            last_analyzed_at=record.last_analyzed_at,
            consistency_score=record.consistency_score,
            open_discrepancies=record.open_discrepancies,
            error=record.error,
        )

    # ------------------------------------------------------------------ #
    # Reports
    # ------------------------------------------------------------------ #
    def save_report(self, report: AnalysisReport) -> str:
        record = ReportRecord(
            report_id=report.report_id,
            match_key=report.match_key,
            package_name=report.package_name,
            normalized_name=normalize_package_name(report.package_name),
            distro=report.distro.value,
            summary=report.summary,
            consistency_score=report.consistency_score,
            discrepancy_count=len(report.discrepancies),
            severity_counts=report.severity_counts(),
            doc_commit=report.doc.commit if report.doc else None,
            script_commit=report.script.commit if report.script else None,
            model=report.model,
            payload=report.model_dump(mode="json"),
            created_at=report.created_at,
        )
        with self.session() as session:
            session.merge(record)
            package = session.scalar(
                select(PackageRecord).where(PackageRecord.match_key == report.match_key)
            )
            if package is not None:
                package.last_analyzed_at = report.created_at
                package.consistency_score = report.consistency_score
                package.open_discrepancies = len(report.discrepancies)
                package.status = AnalysisStatus.ANALYZED.value
        return report.report_id

    def get_report(self, report_id: str) -> AnalysisReport | None:
        with self.session() as session:
            record = session.get(ReportRecord, report_id)
            return AnalysisReport.model_validate(record.payload) if record else None

    def list_reports(
        self,
        package_name: str,
        distro: str | Distro | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[AnalysisReport]:
        statement = (
            select(ReportRecord)
            .where(ReportRecord.normalized_name == normalize_package_name(package_name))
            .order_by(ReportRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if distro is not None:
            statement = statement.where(ReportRecord.distro == normalize_distro(distro).value)
        with self.session() as session:
            records = session.scalars(statement).all()
            return [AnalysisReport.model_validate(record.payload) for record in records]

    def latest_report(self, package_name: str, distro: str | Distro) -> AnalysisReport | None:
        reports = self.list_reports(package_name, distro, limit=1)
        return reports[0] if reports else None

    def count_reports(self, package_name: str | None = None) -> int:
        statement = select(func.count()).select_from(ReportRecord)
        if package_name:
            statement = statement.where(
                ReportRecord.normalized_name == normalize_package_name(package_name)
            )
        with self.session() as session:
            return int(session.scalar(statement) or 0)

    def stats(self) -> dict[str, int]:
        with self.session() as session:
            return {
                "packages": int(session.scalar(select(func.count()).select_from(PackageRecord)) or 0),
                "reports": int(session.scalar(select(func.count()).select_from(ReportRecord)) or 0),
            }


__all__ = [
    "Base",
    "PackageRecord",
    "ReportRecord",
    "Repository",
    "RepositoryState",
    "init_db",
    "make_engine",
    "utcnow",
]
