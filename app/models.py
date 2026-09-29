"""Pydantic schemas for the analyzer.

Two families of models live here:

* **Extraction models** - the deterministic, rule-based view of a doc page or a
  build script (env vars, dependencies, build flags, s390x patches).
* **LLM models** - the JSON contract enforced by Instructor on the comparison
  response. These use ``extra="forbid"`` so a drifting model cannot silently
  invent fields.

The deterministic metadata key used to pair documentation with scripts and to
filter the vector store is ``(package_name, distro)`` - see :func:`match_key`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Distro(str, Enum):
    UBUNTU = "ubuntu"
    RHEL = "rhel"
    CENTOS = "centos"
    FEDORA = "fedora"
    SLES = "sles"
    DEBIAN = "debian"
    UNKNOWN = "unknown"


DISTRO_ALIASES: dict[str, Distro] = {
    "ubuntu": Distro.UBUNTU,
    "ubu": Distro.UBUNTU,
    "rhel": Distro.RHEL,
    "redhat": Distro.RHEL,
    "red hat": Distro.RHEL,
    "rhel8": Distro.RHEL,
    "rhel9": Distro.RHEL,
    "centos": Distro.CENTOS,
    "centos7": Distro.CENTOS,
    "fedora": Distro.FEDORA,
    "sles": Distro.SLES,
    "sled": Distro.SLES,
    "suse": Distro.SLES,
    "sles15": Distro.SLES,
    "opensuse": Distro.SLES,
    "debian": Distro.DEBIAN,
}

#: Canonical ordering used when reporting which distros a package supports.
DISTRO_ORDER = list(Distro)


def normalize_distro(value: str | Distro | None) -> Distro:
    """Map free-form distro text (e.g. ``"RHEL 9.2"``) onto :class:`Distro`."""
    if isinstance(value, Distro):
        return value
    if not value:
        return Distro.UNKNOWN
    cleaned = " ".join(str(value).lower().replace("_", " ").replace("-", " ").split())
    if cleaned in DISTRO_ALIASES:
        return DISTRO_ALIASES[cleaned]
    for alias, distro in DISTRO_ALIASES.items():
        if cleaned.startswith(alias):
            return distro
    return Distro.UNKNOWN


class SourceType(str, Enum):
    DOC = "doc"
    SCRIPT = "script"


class PackageManager(str, Enum):
    APT = "apt"
    YUM = "yum"
    DNF = "dnf"
    ZYPPER = "zypper"
    PIP = "pip"
    OTHER = "other"


class ComponentKind(str, Enum):
    ENV_VAR = "env_var"
    DEPENDENCY = "dependency"
    BUILD_FLAG = "build_flag"
    S390X_PATCH = "s390x_patch"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DiscrepancyCategory(str, Enum):
    MISSING_DEPENDENCY = "missing_dependency"
    EXTRA_DEPENDENCY = "extra_dependency"
    VERSION_MISMATCH = "version_mismatch"
    MISSING_ENV_VAR = "missing_env_var"
    ENV_VAR_VALUE_MISMATCH = "env_var_value_mismatch"
    MISSING_BUILD_FLAG = "missing_build_flag"
    MISSING_S390X_PATCH = "missing_s390x_patch"
    DISTRO_MISMATCH = "distro_mismatch"
    UNDOCUMENTED_STEP = "undocumented_step"
    OTHER = "other"


class AnalysisStatus(str, Enum):
    UNINDEXED = "unindexed"
    PENDING = "pending"
    INDEXING = "indexing"
    INDEXED = "indexed"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    STALE = "stale"
    ERROR = "error"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DetectedBy(str, Enum):
    RULE = "rule"
    LLM = "llm"
    BOTH = "both"


# --------------------------------------------------------------------------- #
# Audit contract (bidirectional doc/script compliance audit)
# --------------------------------------------------------------------------- #


class IssueType(str, Enum):
    """Direction of the divergence between documentation and script."""

    UNDOCUMENTED_ACTION = "undocumented_action"
    OBSOLETE_DOCUMENTATION = "obsolete_documentation"
    PARAMETER_MISMATCH = "parameter_mismatch"


class AuditCategory(str, Enum):
    """Functional area a discrepancy belongs to."""

    PREREQUISITES = "prerequisites"
    ENVIRONMENT_VARIABLES = "environment_variables"
    BUILD_FLAGS = "build_flags"
    PATCHES = "patches"
    SOURCE_VERSION = "source_version"
    TESTING = "testing"


class AuditVerdict(str, Enum):
    MATCH = "MATCH"
    MINOR_DRIFT = "MINOR_DRIFT"
    BREAKING_DISCREPANCY = "BREAKING_DISCREPANCY"


class AuditDiscrepancy(BaseModel):
    """One finding in the strict audit schema (extra="forbid", DISC-nn ids)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable finding id, DISC-01, DISC-02, ...")
    category: AuditCategory
    severity: Severity
    issue_type: IssueType
    description: str = Field(description="Clear explanation of the divergence")
    wiki_reference: str | None = Field(
        default=None, description="Exact text from the wiki, or OMITTED when the wiki is silent"
    )
    script_reference: str | None = Field(
        default=None, description="Exact line from the script, or OMITTED when the script has no counterpart"
    )
    ibm_z_impact: str = Field(
        description="How this affects build stability or runtime performance on s390x"
    )
    suggested_fix: str = Field(description="Concrete action to align doc and script")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    detected_by: DetectedBy = DetectedBy.LLM

    @property
    def signature_key(self) -> str:
        """Identity used to de-duplicate rule and LLM findings."""
        base = (self.wiki_reference or self.script_reference or self.description).strip().lower()
        return f"{self.category.value}:{self.issue_type.value}:{base[:80]}"


class AlignmentStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_discrepancies: int = 0
    critical_count: int = 0
    missing_doc_steps: int = 0
    obsolete_doc_steps: int = 0


class AuditReportCore(BaseModel):
    """The exact JSON the audit LLM must return (no ids/timestamps - we add those)."""

    model_config = ConfigDict(extra="forbid")

    package: str
    target_os: str
    audit_verdict: AuditVerdict
    confidence_score: float = Field(ge=0.0, le=1.0, description="0 = untrustworthy, 1 = certain")
    summary: str = Field(description="High-level summary of findings")
    discrepancies: list[AuditDiscrepancy] = Field(default_factory=list)
    alignment_stats: AlignmentStats = Field(default_factory=AlignmentStats)
    documentations_to_update: list[str] = Field(default_factory=list)
    scripts_to_update: list[str] = Field(default_factory=list)
    notes: str | None = None

    def model_post_init(self, __context: Any) -> None:
        """Keep ids sequential and the stats block consistent with the findings."""
        for position, item in enumerate(self.discrepancies, start=1):
            item.id = f"DISC-{position:02d}"
        critical = sum(1 for item in self.discrepancies if item.severity is Severity.CRITICAL)
        self.alignment_stats = AlignmentStats(
            total_discrepancies=len(self.discrepancies),
            critical_count=critical,
            missing_doc_steps=sum(
                1 for item in self.discrepancies if item.issue_type is IssueType.UNDOCUMENTED_ACTION
            ),
            obsolete_doc_steps=sum(
                1 for item in self.discrepancies if item.issue_type is IssueType.OBSOLETE_DOCUMENTATION
            ),
        )


# --------------------------------------------------------------------------- #
# Deterministic extraction models
# --------------------------------------------------------------------------- #


class ExtractionModel(BaseModel):
    """Base for rule-based extraction: strict, but tolerant of missing fields."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class EnvVar(ExtractionModel):
    name: str
    value: str | None = None
    exported: bool = False
    evidence: str | None = Field(default=None, description="Source line the value came from")


class Dependency(ExtractionModel):
    name: str
    version: str | None = None
    manager: PackageManager = PackageManager.OTHER
    distro: Distro = Distro.UNKNOWN
    evidence: str | None = None


class BuildFlag(ExtractionModel):
    flag: str
    target: Literal["configure", "cmake", "make", "compiler", "other"] = "other"
    evidence: str | None = None


class S390xPatch(ExtractionModel):
    description: str
    method: Literal["sed", "patch", "git_apply", "conditional", "other"] = "other"
    target: str | None = Field(default=None, description="File, patch URL or patch directory")
    architecture_guard: str | None = Field(
        default=None, description="Guard such as `uname -m` == s390x"
    )
    evidence: str | None = None


class ComponentSignature(ExtractionModel):
    """Everything the parser could pull out of one source file."""

    package_name: str
    distro: Distro = Distro.UNKNOWN
    source_type: SourceType
    source_path: str = ""
    commit: str | None = None

    env_vars: list[EnvVar] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    build_flags: list[BuildFlag] = Field(default_factory=list)
    s390x_patches: list[S390xPatch] = Field(default_factory=list)
    configure_options: list[str] = Field(default_factory=list)
    distros_mentioned: list[Distro] = Field(default_factory=list)

    @property
    def match_key(self) -> str:
        return match_key(self.package_name, self.distro)

    def counts(self) -> dict[str, int]:
        return {
            "env_vars": len(self.env_vars),
            "dependencies": len(self.dependencies),
            "build_flags": len(self.build_flags),
            "s390x_patches": len(self.s390x_patches),
        }

    def dependency_index(self) -> dict[str, Dependency]:
        return {dep.name.lower(): dep for dep in self.dependencies}

    def env_var_index(self) -> dict[str, EnvVar]:
        return {var.name: var for var in self.env_vars}

    def flag_index(self) -> dict[str, BuildFlag]:
        return {flag.flag: flag for flag in self.build_flags}


# --------------------------------------------------------------------------- #
# LLM-enforced output contract
# --------------------------------------------------------------------------- #


class Discrepancy(BaseModel):
    """A single documentation/script inconsistency (persisted report schema)."""

    model_config = ConfigDict(extra="forbid")

    category: DiscrepancyCategory
    severity: Severity
    title: str = Field(max_length=160)
    description: str = Field(description="What disagrees and why it matters on s390x")
    component: str | None = Field(
        default=None, description="Package, env var, flag or patch the finding concerns"
    )
    doc_evidence: str | None = Field(default=None, description="Verbatim doc excerpt")
    script_evidence: str | None = Field(default=None, description="Verbatim script excerpt")
    recommendation: str = Field(description="Concrete fix for the doc or the script")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    detected_by: DetectedBy = DetectedBy.LLM
    # -- audit contract extensions ---------------------------------------- #
    audit_category: AuditCategory | None = Field(
        default=None, description="Audit taxonomy area (prerequisites, build_flags, ...)"
    )
    issue_type: IssueType | None = Field(
        default=None,
        description="Direction: undocumented_action, obsolete_documentation or parameter_mismatch",
    )
    ibm_z_impact: str | None = Field(
        default=None, description="How this affects build stability or runtime performance on s390x"
    )

    @property
    def signature_key(self) -> str:
        """Stable identity used to de-duplicate rule and LLM findings."""
        return f"{self.category.value}:{(self.component or self.title).strip().lower()}"


class AnalysisReportCore(BaseModel):
    """The exact JSON the LLM must return (no ids/timestamps - we add those)."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="2-4 sentence executive summary of the comparison")
    consistency_score: int = Field(
        ge=0, le=100, description="0 = doc and script fully contradict, 100 = fully consistent"
    )
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    documentations_to_update: list[str] = Field(
        default_factory=list, description="Doc paths/sections the maintainers should fix"
    )
    scripts_to_update: list[str] = Field(default_factory=list)
    notes: str | None = Field(default=None, description="Caveats, assumptions, or coverage limits")


# --------------------------------------------------------------------------- #
# Persisted / returned report
# --------------------------------------------------------------------------- #


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    path: str
    commit: str | None = None
    chunk_count: int = 0
    distros: list[Distro] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class AnalysisReport(BaseModel):
    """Full report returned by ``POST /api/analyze`` and ``GET /api/results/{pkg}``."""

    model_config = ConfigDict(extra="forbid")

    report_id: str
    package_name: str
    distro: Distro
    match_key: str
    status: AnalysisStatus = AnalysisStatus.ANALYZED
    summary: str
    consistency_score: int = Field(ge=0, le=100)
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    documentations_to_update: list[str] = Field(default_factory=list)
    scripts_to_update: list[str] = Field(default_factory=list)
    notes: str | None = None

    doc: SourceRef | None = None
    script: SourceRef | None = None
    doc_signature: ComponentSignature | None = None
    script_signature: ComponentSignature | None = None

    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    warnings: list[str] = Field(default_factory=list)

    def severity_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.discrepancies:
            counts[item.severity.value] = counts.get(item.severity.value, 0) + 1
        return counts

    # -- audit contract ---------------------------------------------------- #
    @property
    def audit(self) -> AuditReportCore:
        """The strict audit JSON for this report (computed on demand)."""
        return to_audit_report(self)

    @property
    def audit_verdict(self) -> AuditVerdict:
        return verdict_for(
            self.consistency_score,
            criticals=sum(1 for d in self.discrepancies if d.severity is Severity.CRITICAL),
            highs=sum(1 for d in self.discrepancies if d.severity is Severity.HIGH),
        )


# --------------------------------------------------------------------------- #
# HTTP DTOs
# --------------------------------------------------------------------------- #


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_reindex: bool = Field(
        default=False, description="Drop the collection and re-embed everything"
    )
    packages: list[str] | None = Field(
        default=None, description="Restrict ingestion to these package names"
    )
    prune: bool = Field(default=True, description="Delete vectors for sources that disappeared")


class SyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    state: JobState
    accepted: bool = True
    message: str
    stats: dict[str, Any] | None = Field(
        default=None, description="Ingestion counters, present when the run is awaited (wait=true)"
    )


class JobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    kind: Literal["sync", "analyze"]
    state: JobState
    submitted_at: datetime
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class PackageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_name: str
    distro: Distro
    match_key: str
    status: AnalysisStatus
    doc_path: str | None = None
    script_path: str | None = None
    versions: list[str] = Field(default_factory=list)
    chunk_count: int = 0
    doc_commit: str | None = None
    script_commit: str | None = None
    last_indexed_at: datetime | None = None
    last_analyzed_at: datetime | None = None
    consistency_score: int | None = None
    open_discrepancies: int = 0
    error: str | None = None


class PackageListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    index_commit: str | None = None
    indexed_at: datetime | None = None
    packages: list[PackageSummary]


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_name: str = Field(min_length=1, description="e.g. `nginx`, `nodejs`")
    distro: Distro = Distro.UBUNTU
    force: bool = Field(default=False, description="Re-run even if a stored report matches HEAD")
    top_k: int | None = Field(default=None, ge=1, le=50, description="Override retrieval depth")


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str | None = None
    state: JobState | None = None
    report: AnalysisReport | None = None


class ResultListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_name: str
    total: int
    reports: list[AnalysisReport]


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    version: str
    vector_backend: str
    collection: str
    embedding_provider: str
    llm_provider: str
    llm_model: str
    indexed_chunks: int
    packages: int
    repositories: dict[str, str | None] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class PackageRef(BaseModel):
    """Lightweight record of a package discovered during repository traversal."""

    model_config = ConfigDict(extra="forbid")

    package_name: str
    normalized_name: str
    distros: list[Distro] = Field(default_factory=lambda: [Distro.UNKNOWN])
    doc_path: str | None = None
    script_path: str | None = None
    script_paths: dict[str, str] = Field(default_factory=dict, description="version -> path")
    versions: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def match_keys(self) -> list[str]:
        return [match_key(self.package_name, distro) for distro in self.distros]


def normalize_package_name(name: str) -> str:
    """Lower-case, strip separators - used for fuzzy doc/script pairing."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def match_key(package_name: str, distro: str | Distro) -> str:
    """The deterministic ``(package_name, distro)`` pairing key."""
    return f"{normalize_package_name(package_name)}::{normalize_distro(distro).value}"


def stable_id(*parts: str, length: int = 24) -> str:
    """Deterministic short id (used for chunk ids and report ids)."""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


# --------------------------------------------------------------------------- #
# Mapping between the persisted report schema and the audit contract
# --------------------------------------------------------------------------- #


#: Internal finding category -> audit schema category.
_AUDIT_CATEGORY_BY_INTERNAL: dict[DiscrepancyCategory, AuditCategory] = {
    DiscrepancyCategory.MISSING_DEPENDENCY: AuditCategory.PREREQUISITES,
    DiscrepancyCategory.EXTRA_DEPENDENCY: AuditCategory.PREREQUISITES,
    DiscrepancyCategory.VERSION_MISMATCH: AuditCategory.SOURCE_VERSION,
    DiscrepancyCategory.MISSING_ENV_VAR: AuditCategory.ENVIRONMENT_VARIABLES,
    DiscrepancyCategory.ENV_VAR_VALUE_MISMATCH: AuditCategory.ENVIRONMENT_VARIABLES,
    DiscrepancyCategory.MISSING_BUILD_FLAG: AuditCategory.BUILD_FLAGS,
    DiscrepancyCategory.MISSING_S390X_PATCH: AuditCategory.PATCHES,
    DiscrepancyCategory.DISTRO_MISMATCH: AuditCategory.SOURCE_VERSION,
    DiscrepancyCategory.UNDOCUMENTED_STEP: AuditCategory.BUILD_FLAGS,
    DiscrepancyCategory.OTHER: AuditCategory.BUILD_FLAGS,
}

#: Internal finding category -> audit issue type (direction of the divergence).
_AUDIT_ISSUE_BY_INTERNAL: dict[DiscrepancyCategory, IssueType] = {
    DiscrepancyCategory.MISSING_DEPENDENCY: IssueType.OBSOLETE_DOCUMENTATION,
    DiscrepancyCategory.EXTRA_DEPENDENCY: IssueType.UNDOCUMENTED_ACTION,
    DiscrepancyCategory.VERSION_MISMATCH: IssueType.PARAMETER_MISMATCH,
    DiscrepancyCategory.MISSING_ENV_VAR: IssueType.OBSOLETE_DOCUMENTATION,
    DiscrepancyCategory.ENV_VAR_VALUE_MISMATCH: IssueType.PARAMETER_MISMATCH,
    DiscrepancyCategory.MISSING_BUILD_FLAG: IssueType.OBSOLETE_DOCUMENTATION,
    DiscrepancyCategory.MISSING_S390X_PATCH: IssueType.UNDOCUMENTED_ACTION,
    DiscrepancyCategory.DISTRO_MISMATCH: IssueType.PARAMETER_MISMATCH,
    DiscrepancyCategory.UNDOCUMENTED_STEP: IssueType.UNDOCUMENTED_ACTION,
    DiscrepancyCategory.OTHER: IssueType.PARAMETER_MISMATCH,
}

#: Audit schema category -> internal finding category (LLM findings -> storage).
_INTERNAL_CATEGORY_BY_AUDIT: dict[AuditCategory, DiscrepancyCategory] = {
    value: key for key, value in _AUDIT_CATEGORY_BY_INTERNAL.items()
}


def audit_to_internal_discrepancy(item: AuditDiscrepancy) -> Discrepancy:
    """Convert an LLM audit finding into the persisted report schema."""
    title = item.description.strip().splitlines()[0][:160]
    return Discrepancy(
        category=_INTERNAL_CATEGORY_BY_AUDIT.get(item.category, DiscrepancyCategory.OTHER),
        severity=item.severity,
        title=title or item.category.value,
        description=item.description,
        component=None,
        doc_evidence=None if item.wiki_reference in (None, "OMITTED") else item.wiki_reference,
        script_evidence=None if item.script_reference in (None, "OMITTED") else item.script_reference,
        recommendation=item.suggested_fix,
        confidence=item.confidence,
        detected_by=item.detected_by if item.detected_by is not DetectedBy.BOTH else DetectedBy.LLM,
        audit_category=item.category,
        issue_type=item.issue_type,
        ibm_z_impact=item.ibm_z_impact,
    )


def verdict_for(score: int | None, *, criticals: int = 0, highs: int = 0) -> AuditVerdict:
    """Map a consistency score + finding mix onto the audit verdict enum."""
    if criticals > 0 or (score is None or score < 65):
        return AuditVerdict.BREAKING_DISCREPANCY
    if highs > 0 or score < 90:
        return AuditVerdict.MINOR_DRIFT
    return AuditVerdict.MATCH


def verdict_label(score: int | None, *, criticals: int = 0, highs: int = 0) -> str:
    """String label for the verdict, safe for JSON payloads."""
    return verdict_for(score, criticals=criticals, highs=highs).value


def to_audit_discrepancy(item: Discrepancy, position: int) -> AuditDiscrepancy:
    """Convert a persisted-report discrepancy into the strict audit schema."""
    return AuditDiscrepancy(
        id=f"DISC-{position:02d}",
        category=item.audit_category or _AUDIT_CATEGORY_BY_INTERNAL.get(item.category, AuditCategory.PREREQUISITES),
        severity=item.severity,
        issue_type=item.issue_type or _AUDIT_ISSUE_BY_INTERNAL.get(item.category, IssueType.PARAMETER_MISMATCH),
        description=item.description,
        wiki_reference=item.doc_evidence or "OMITTED",
        script_reference=item.script_evidence or "OMITTED",
        ibm_z_impact=item.ibm_z_impact or item.description,
        suggested_fix=item.recommendation,
        confidence=item.confidence,
        detected_by=item.detected_by,
    )


def to_audit_report(report: AnalysisReport) -> AuditReportCore:
    """Render the strict audit JSON for a full persisted report."""
    disc = [to_audit_discrepancy(item, position) for position, item in enumerate(report.discrepancies, 1)]
    criticals = sum(1 for d in report.discrepancies if d.severity is Severity.CRITICAL)
    highs = sum(1 for d in report.discrepancies if d.severity is Severity.HIGH)
    stats = AlignmentStats(
        total_discrepancies=len(disc),
        critical_count=criticals,
        missing_doc_steps=sum(1 for d in report.discrepancies if d.category is DiscrepancyCategory.UNDOCUMENTED_STEP),
        obsolete_doc_steps=sum(1 for d in report.discrepancies if d.category is DiscrepancyCategory.MISSING_DEPENDENCY),
    )
    return AuditReportCore(
        package=report.package_name,
        target_os=report.distro.value,
        audit_verdict=verdict_for(report.consistency_score, criticals=criticals, highs=highs),
        confidence_score=0.9 if report.model != "rules-only" else 0.6,
        summary=report.summary,
        discrepancies=disc,
        alignment_stats=stats,
        documentations_to_update=list(report.documentations_to_update),
        scripts_to_update=list(report.scripts_to_update),
        notes=report.notes,
    )


__all__ = [
    "AlignmentStats",
    "AnalysisReport",
    "AnalysisReportCore",
    "AnalysisStatus",
    "AnalyzeRequest",
    "AnalyzeResponse",
    "AuditCategory",
    "AuditDiscrepancy",
    "AuditReportCore",
    "AuditVerdict",
    "BuildFlag",
    "ComponentKind",
    "ComponentSignature",
    "Dependency",
    "DetectedBy",
    "Discrepancy",
    "DiscrepancyCategory",
    "Distro",
    "EnvVar",
    "HealthResponse",
    "IssueType",
    "JobState",
    "JobStatus",
    "PackageListResponse",
    "PackageManager",
    "PackageRef",
    "PackageSummary",
    "ResultListResponse",
    "S390xPatch",
    "Severity",
    "SourceRef",
    "SourceType",
    "SyncRequest",
    "SyncResponse",
    "match_key",
    "normalize_distro",
    "normalize_package_name",
    "stable_id",
    "to_audit_discrepancy",
    "audit_to_internal_discrepancy",
    "to_audit_report",
    "verdict_for",
    "verdict_label",
]
