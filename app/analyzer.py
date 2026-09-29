"""Comparison engine.

Three layers, cheapest first:

1. **Deterministic retrieval** - chunks are fetched by the exact
   ``(package_name, distro, source_type)`` metadata key, so the doc page and the
   build script are always the *matched pair* and never a cross-package
   coincidence.
2. **Rule engine** - set algebra over the extracted components (dependencies,
   env vars, build flags, s390x patches). This is reproducible, free and
   catches the mechanical drift; it is also injected into the LLM prompt as
   evidence rather than being replaced by it.
3. **LLM review** - Instructor forces the model into :class:`AnalysisReportCore`,
   which adds semantic findings the rules cannot see (a doc paragraph that
   contradicts a script comment, an undocumented s390x workaround, etc.).
   Findings are merged and de-duplicated on ``(category, component)``.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import Settings
from app.db import Repository
from app.llm import LLMError, StructuredLLM
from app.models import (
    _AUDIT_CATEGORY_BY_INTERNAL,
    AlignmentStats,
    AnalysisReport,
    AnalysisStatus,
    AuditCategory,
    AuditReportCore,
    ComponentSignature,
    Dependency,
    DetectedBy,
    Discrepancy,
    DiscrepancyCategory,
    Distro,
    IssueType,
    Severity,
    SourceRef,
    SourceType,
    audit_to_internal_discrepancy,
    match_key,
    normalize_distro,
    normalize_package_name,
    stable_id,
    verdict_for,
)
from app.parsers import parse_build_script, parse_markdown_doc
from app.vector_store import (
    Embedder,
    SearchHit,
    VectorStore,
    and_all,
    match_all,
    match_package,
)

logger = logging.getLogger(__name__)

SEVERITY_WEIGHT: dict[Severity, int] = {
    Severity.CRITICAL: 30,
    Severity.HIGH: 18,
    Severity.MEDIUM: 10,
    Severity.LOW: 5,
    Severity.INFO: 2,
}

#: Decay constant for the score curve: higher = more forgiving of many findings.
SCORE_DECAY = 90.0

#: Different package managers spell the same runtime/dev package differently.
_DEP_ALIASES: dict[str, str] = {
    "libssl-dev": "openssl-dev",
    "openssl-devel": "openssl-dev",
    "libssl-devel": "openssl-dev",
    "zlib1g-dev": "zlib-dev",
    "zlib-devel": "zlib-dev",
    "libcurl4-openssl-dev": "curl-dev",
    "libcurl-devel": "curl-dev",
    "libxml2-dev": "libxml2-dev",
    "libxml2-devel": "libxml2-dev",
    "g++": "gcc-c++",
    "gcc-c++": "gcc-c++",
    "make": "make",
    "python3-pip": "pip",
    "python3-dev": "python-dev",
    "python3-devel": "python-dev",
    "systemd-devel": "systemd-dev",
    "libsystemd-dev": "systemd-dev",
}

#: Packages whose absence is especially dangerous in an s390x build.
_S390X_CRITICAL_HINTS = ("openssl", "zlib", "curl", "libffi", "systemd", "gcc", "kernel", "jvm", "java")


class AnalysisError(RuntimeError):
    """Raised when an analysis cannot be produced."""


class NotIndexedError(AnalysisError):
    """Raised when the requested package/distro pair has no indexed content."""


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RetrievedPair:
    package_name: str
    distro: Distro
    doc_hits: list[SearchHit] = field(default_factory=list)
    script_hits: list[SearchHit] = field(default_factory=list)
    doc_signature: ComponentSignature | None = None
    script_signature: ComponentSignature | None = None
    #: Script version directory the comparison runs against.
    script_version: str | None = None
    #: Versions the documentation refers to that exist in the script tree.
    doc_versions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.doc_hits) and bool(self.script_hits)

    def source_ref(self, source_type: SourceType, hits: Sequence[SearchHit]) -> SourceRef | None:
        if not hits:
            return None
        first = hits[0].metadata
        paths = sorted({str(hit.metadata.get("source_path", "")) for hit in hits})
        distros = sorted({str(hit.metadata.get("distro", "unknown")) for hit in hits})
        counts = {"chunks": len(hits)}
        signature = self.doc_signature if source_type is SourceType.DOC else self.script_signature
        if signature is not None:
            counts.update(signature.counts())
        return SourceRef(
            source_type=source_type,
            path=paths[0] if len(paths) == 1 else ", ".join(paths[:3]),
            commit=str(first.get("commit")) if first.get("commit") else None,
            chunk_count=len(hits),
            distros=[normalize_distro(value) for value in distros],
            counts=counts,
        )


class Retriever:
    """Deterministic metadata retrieval + semantic re-ranking inside the pair."""

    def __init__(self, settings: Settings, store: VectorStore, embedder: Embedder) -> None:
        self.settings = settings
        self.store = store
        self.embedder = embedder

    def _fetch(self, package_name: str, distro: Distro, source_type: str) -> tuple[list[SearchHit], bool]:
        """Fetch the distro's chunks, merged with the shared (``unknown``) ones."""
        base = and_all(match_package(package_name), match_all(source_type=source_type))
        strict = self.store.fetch(
            and_all(base, match_all(distro=distro.value)), limit=2000
        )
        shared = self.store.fetch(
            and_all(base, match_all(distro=Distro.UNKNOWN.value)), limit=2000
        )
        seen: set[str] = set()
        hits: list[SearchHit] = []
        for hit in [*strict, *shared]:
            if hit.id in seen:
                continue
            seen.add(hit.id)
            hits.append(hit)
        return hits, (not strict and bool(shared))

    def retrieve(self, package_name: str, distro: Distro | str, top_k: int | None = None) -> RetrievedPair:
        resolved = normalize_distro(distro)
        pair = RetrievedPair(package_name=package_name, distro=resolved)

        doc_hits, doc_relaxed = self._fetch(package_name, resolved, SourceType.DOC.value)
        script_hits, script_relaxed = self._fetch(package_name, resolved, SourceType.SCRIPT.value)
        doc_hits.sort(key=_hit_order)
        script_hits.sort(key=_hit_order)
        pair.doc_hits = doc_hits
        if doc_relaxed:
            pair.warnings.append(
                f"no documentation section is tagged for {resolved.value}; used the shared sections"
            )

        # Compare against the version the documentation is about; a package can
        # ship dozens of version directories and only one of them is relevant.
        pair.script_version = pick_script_version(doc_hits, script_hits)
        if pair.script_version:
            filtered = [hit for hit in script_hits if hit.metadata.get("version") == pair.script_version]
            if filtered:
                script_hits = filtered
        pair.script_hits = script_hits
        if script_relaxed:
            pair.warnings.append(
                f"no build script is tagged for {resolved.value}; used the distro-agnostic script chunks"
            )

        if not pair.complete:
            missing = "documentation" if not pair.doc_hits else "build script"
            raise NotIndexedError(
                f"no indexed {missing} for {package_name!r} on {resolved.value}. "
                "Run POST /api/sync first, or check GET /api/packages for the indexed distros."
            )

        # Report the spelling the repository uses, not the caller's casing.
        source = pair.doc_hits or pair.script_hits
        pair.package_name = str(source[0].metadata.get("package_name") or package_name)

        pair.doc_signature = self._signature(pair, SourceType.DOC)
        pair.script_signature = self._signature(pair, SourceType.SCRIPT)
        pair.doc_versions = doc_version_candidates(
            " ".join(hit.text for hit in pair.doc_hits), pair.package_name
        )
        return pair

    def rank(
        self, query: str, package_name: str, distro: Distro, source_type: str, top_k: int
    ) -> list[SearchHit]:
        """Semantic top-k *within* the matched pair (used to prioritise excerpts)."""
        where = and_all(
            match_package(package_name),
            match_all(distro=distro.value, source_type=source_type),
        )
        try:
            embedding = self.embedder.embed([query])[0]
            hits = self.store.query(embedding, top_k, where)
        except Exception as exc:  # noqa: BLE001 - ranking is best-effort
            logger.warning("semantic ranking failed, falling back to deterministic order: %s", exc)
            return []
        return hits

    def _signature(self, pair: RetrievedPair, source_type: SourceType) -> ComponentSignature | None:
        hits = pair.doc_hits if source_type is SourceType.DOC else pair.script_hits
        if not hits:
            return None
        text = "\n\n".join(hit.text for hit in hits)
        path = str(hits[0].metadata.get("source_path", ""))
        commit = hits[0].metadata.get("commit")
        if source_type is SourceType.DOC:
            return parse_markdown_doc(text, pair.package_name, source_path=path, commit=commit, distro=pair.distro)
        return parse_build_script(text, pair.package_name, source_path=path, commit=commit, distro=pair.distro)


def _hit_order(hit: SearchHit) -> tuple[str, int]:
    return (str(hit.metadata.get("source_path", "")), int(hit.metadata.get("chunk_index", 0)))


def _version_sort_key(version: str) -> tuple[int, ...]:
    base = version.split(":", 1)[0]
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[._-]", base))


def pick_script_version(doc_hits: Sequence[SearchHit], script_hits: Sequence[SearchHit]) -> str | None:
    """Choose which script version directory to compare against.

    Prefers the newest version the documentation actually mentions, so an
    out-of-date wiki page is compared against the release it describes; falls
    back to the newest version in the repository.
    """
    versions = sorted(
        {str(hit.metadata.get("version")) for hit in script_hits if hit.metadata.get("version")},
        key=_version_sort_key,
    )
    if not versions:
        return None
    doc_text = " ".join(hit.text for hit in doc_hits)
    mentioned = [
        version
        for version in versions
        if _version_sort_key(version) != (0,) and version.split(":", 1)[0] in doc_text
    ]
    return mentioned[-1] if mentioned else versions[-1]


def doc_version_candidates(doc_text: str, package_name: str) -> list[str]:
    """Versions the documentation pins for *this* package (``nginx-1.30.4``).

    Anchoring on the package name keeps unrelated version numbers (distro
    releases, dependency versions) out of the comparison.
    """
    names = {package_name.lower(), normalize_package_name(package_name)}
    pattern = "|".join(sorted((re.escape(name) for name in names if name), key=len, reverse=True))
    if not pattern:
        return []
    found = re.findall(rf"(?:{pattern})[-_/ ]v?(\d+\.\d+(?:\.\d+)?)", doc_text, re.IGNORECASE)
    return sorted(set(found), key=_version_sort_key)


# --------------------------------------------------------------------------- #
# Rule engine
# --------------------------------------------------------------------------- #


def canonical_dependency(name: str) -> str:
    cleaned = name.strip().lower()
    return _DEP_ALIASES.get(cleaned, cleaned)


def dependency_family(name: str) -> str:
    """Strip version tokens: ``openjdk-11-jdk``/``openjdk-8-jdk`` -> ``openjdk-jdk``.

    Documentation often lists every supported alternative JDK/Python while the
    script installs one of them; that is a version variant, not a missing
    prerequisite.
    """
    lowered = re.sub(r"\d+(?:\.\d+)*", "", canonical_dependency(name))
    return re.sub(r"[-_.]{2,}", "-", lowered).strip("-_")


def _ibm_z_impact(
    category: DiscrepancyCategory,
    component: str | None,
    severity: Severity,
) -> str:
    """s390x-specific impact sentence for a rule finding."""
    name = component or "this component"
    if category is DiscrepancyCategory.MISSING_S390X_PATCH:
        return (
            f"Skipping {name} builds a tree whose atomics/endianness assumptions differ from the "
            "supported configuration; the resulting binary can crash or corrupt data at runtime on Z."
        )
    if category is DiscrepancyCategory.MISSING_DEPENDENCY:
        if any(hint in name for hint in _S390X_CRITICAL_HINTS):
            return (
                f"{name} is a build prerequisite on s390x; without it the build picks a stale system "
                "library or fails at configure/link time, producing an unsupported binary."
            )
        return f"Missing {name} surfaces late in the build as a configure or link failure on Z."
    if category is DiscrepancyCategory.MISSING_BUILD_FLAG:
        return (
            f"Without `{name}` the build defaults diverge from the documented configuration; "
            "on s390x this commonly means missing -march=z13/z14 tuning or wrong ABI defaults."
        )
    if category in (DiscrepancyCategory.MISSING_ENV_VAR, DiscrepancyCategory.ENV_VAR_VALUE_MISMATCH):
        return (
            f"`{name}` steers paths/toolchain selection; a divergent value points the build at the "
            "wrong prefix or bitness (e.g. -m64 vs -m31) on Z."
        )
    if category is DiscrepancyCategory.VERSION_MISMATCH:
        return (
            f"Version skew on {name} means the documented reproduction steps do not match the "
            "tested s390x configuration."
        )
    return f"Following the documentation alone produces a divergent build on Z for {name}."


def _issue_type_for(category: DiscrepancyCategory) -> IssueType:
    """Direction of the divergence: doc-only, script-only, or value mismatch."""
    if category in (
        DiscrepancyCategory.MISSING_DEPENDENCY,
        DiscrepancyCategory.MISSING_ENV_VAR,
        DiscrepancyCategory.MISSING_BUILD_FLAG,
    ):
        return IssueType.OBSOLETE_DOCUMENTATION
    if category in (
        DiscrepancyCategory.EXTRA_DEPENDENCY,
        DiscrepancyCategory.UNDOCUMENTED_STEP,
        DiscrepancyCategory.MISSING_S390X_PATCH,
    ):
        return IssueType.UNDOCUMENTED_ACTION
    return IssueType.PARAMETER_MISMATCH


def _audit_fields_for_category(
    category: DiscrepancyCategory,
    component: str | None,
    severity: Severity,
) -> dict[str, str]:
    """Populate the audit fields on rule findings (impact, direction, taxonomy)."""
    return {
        "ibm_z_impact": _ibm_z_impact(category, component, severity),
        "issue_type": _issue_type_for(category).value,
        "audit_category": _AUDIT_CATEGORY_BY_INTERNAL.get(category, AuditCategory.PREREQUISITES).value,
    }


def missing_dependency_discrepancy(
    *,
    name: str,
    documented: Dependency | None,
    installed: Dependency | None = None,
    variant: bool = False,
) -> Discrepancy:
    """Build the finding for a documented dependency the script does not install."""
    if variant and installed is not None:
        return Discrepancy(
            category=DiscrepancyCategory.VERSION_MISMATCH,
            severity=Severity.INFO,
            title=f"Documented dependency is a different version: {name} -> {installed.name}",
            description=(
                f"The documentation lists `{name}` while the build script installs `{installed.name}` "
                f"({installed.manager.value}). Same package family, different version - confirm the "
                "documented alternative is still supported."
            ),
            component=name,
            doc_evidence=documented.evidence if documented else None,
            script_evidence=installed.evidence,
            recommendation=(
                f"Trim the alternatives in the documentation, or add `{name}` to the script's "
                "installation step."
            ),
            confidence=0.5,
            detected_by=DetectedBy.RULE,
            **_audit_fields_for_category(DiscrepancyCategory.VERSION_MISMATCH, name, Severity.INFO),
        )
    severity = Severity.HIGH if any(hint in name for hint in _S390X_CRITICAL_HINTS) else Severity.MEDIUM
    return Discrepancy(
        category=DiscrepancyCategory.MISSING_DEPENDENCY,
        severity=severity,
        title=f"Dependency documented but never installed: {name}",
        description=(
            f"The wiki page lists `{name}` as a prerequisite, but no install command for it "
            "appears in the build script. On s390x this typically surfaces as a configure or "
            "link failure deep into the build."
        ),
        component=name,
        doc_evidence=documented.evidence if documented else None,
        recommendation=(
            f"Add `{name}` to the script's package installation step "
            "(or drop it from the documentation if it is obsolete)."
        ),
        confidence=0.85,
        detected_by=DetectedBy.RULE,
        **_audit_fields_for_category(DiscrepancyCategory.MISSING_DEPENDENCY, name, severity),
    )


class RuleEngine:
    """Set-algebra comparison of the extracted component signatures."""

    def diff(
        self,
        doc: ComponentSignature | None,
        script: ComponentSignature | None,
        doc_versions: Sequence[str] = (),
        script_version: str | None = None,
    ) -> list[Discrepancy]:
        if doc is None or script is None:
            return []
        findings: list[Discrepancy] = []
        findings.extend(self._dependencies(doc, script))
        findings.extend(self._env_vars(doc, script))
        findings.extend(self._flags(doc, script))
        findings.extend(self._patches(doc, script))
        findings.extend(self._versions(doc, script, tuple(doc_versions), script_version))
        return findings

    def _versions(
        self,
        doc: ComponentSignature,
        script: ComponentSignature,
        doc_versions: tuple[str, ...],
        script_version: str | None,
    ) -> list[Discrepancy]:
        if not script_version or not doc_versions:
            return []
        base = script_version.split(":", 1)[0]
        if base in doc_versions:
            return []
        return [
            Discrepancy(
                category=DiscrepancyCategory.VERSION_MISMATCH,
                severity=Severity.HIGH,
                title=(
                    f"Documentation targets {', '.join(doc_versions)} but the scripts build {base}"
                ),
                description=(
                    f"The wiki page pins version(s) {', '.join(doc_versions)} for this package, while the "
                    f"build scripts in the repository only provide {script_version}. Following the "
                    "documentation cannot reproduce the scripted build, and the scripted build is not "
                    "the configuration the documentation describes."
                ),
                component=base,
                doc_evidence=f"documented version(s): {', '.join(doc_versions)}",
                script_evidence=f"script path: {script.source_path}",
                recommendation=(
                    f"Update the wiki page to {base}, or add the missing {', '.join(doc_versions)} "
                    "build script directory."
                ),
                confidence=0.7,
                detected_by=DetectedBy.RULE,
                **_audit_fields_for_category(DiscrepancyCategory.VERSION_MISMATCH, base, Severity.HIGH),
            )
        ]

    # -- individual checks ---------------------------------------------- #
    def _dependencies(self, doc: ComponentSignature, script: ComponentSignature) -> list[Discrepancy]:
        documented = {canonical_dependency(dep.name): dep for dep in doc.dependencies}
        installed = {canonical_dependency(dep.name): dep for dep in script.dependencies}
        findings: list[Discrepancy] = []

        families = {dependency_family(name): dep for name, dep in installed.items()}
        for name, dep in documented.items():
            if name in installed:
                continue
            # `openjdk-8-jdk` documented, `openjdk-17-jdk` installed: same family.
            sibling = families.get(dependency_family(name))
            findings.append(
                missing_dependency_discrepancy(
                    name=dep.name,
                    documented=dep,
                    installed=sibling,
                    variant=sibling is not None,
                )
            )

        for name, dep in installed.items():
            if name in documented:
                continue
            findings.append(
                Discrepancy(
                    category=DiscrepancyCategory.EXTRA_DEPENDENCY,
                    severity=Severity.LOW,
                    title=f"Dependency installed but not documented: {dep.name}",
                    description=(
                        f"The build script installs `{dep.name}` via {dep.manager.value}, but the wiki page never "
                        f"mentions it, so a manual build following the docs would fail."
                    ),
                    component=dep.name,
                    script_evidence=dep.evidence,
                    recommendation=f"Document `{dep.name}` in the prerequisites section of the wiki page.",
                    confidence=0.7,
                    detected_by=DetectedBy.RULE,
                    **_audit_fields_for_category(DiscrepancyCategory.EXTRA_DEPENDENCY, dep.name, Severity.LOW),
                )
            )
        return findings

    def _env_vars(self, doc: ComponentSignature, script: ComponentSignature) -> list[Discrepancy]:
        findings: list[Discrepancy] = []
        documented = doc.env_var_index()
        scripted = script.env_var_index()

        for name, var in documented.items():
            if name not in scripted:
                findings.append(
                    Discrepancy(
                        category=DiscrepancyCategory.MISSING_ENV_VAR,
                        severity=Severity.MEDIUM,
                        title=f"Environment variable documented but unset in the script: {name}",
                        description=(
                            f"The documentation instructs building with `{name}`, but the script never exports it. "
                            "Builds will silently use defaults."
                        ),
                        component=name,
                        doc_evidence=var.evidence,
                        recommendation=f"Export `{name}` in the build script, or remove it from the documentation.",
                        confidence=0.8,
                        detected_by=DetectedBy.RULE,
                        **_audit_fields_for_category(DiscrepancyCategory.MISSING_ENV_VAR, name, Severity.MEDIUM),
                    )
                )
            elif (
                var.value
                and scripted[name].value
                and var.value.strip() != scripted[name].value.strip()
                # Values built from shell expansions (``$PWD``, ``$(nproc)``) are
                # computed at runtime, so a textual difference is not a finding.
                and not any(marker in var.value + scripted[name].value for marker in ("$", "`"))
            ):
                findings.append(
                    Discrepancy(
                        category=DiscrepancyCategory.ENV_VAR_VALUE_MISMATCH,
                        severity=Severity.MEDIUM,
                        title=f"Environment variable value differs for {name}",
                        description=f"Documented `{name}={var.value}` vs script `{name}={scripted[name].value}`.",
                        component=name,
                        doc_evidence=var.evidence,
                        script_evidence=scripted[name].evidence,
                        recommendation=f"Reconcile `{name}` so the documented and scripted values agree.",
                        confidence=0.75,
                        detected_by=DetectedBy.RULE,
                        **_audit_fields_for_category(
                            DiscrepancyCategory.ENV_VAR_VALUE_MISMATCH, name, Severity.MEDIUM
                        ),
                    )
                )
        return findings

    def _flags(self, doc: ComponentSignature, script: ComponentSignature) -> list[Discrepancy]:
        findings: list[Discrepancy] = []
        documented = doc.flag_index()
        scripted = script.flag_index()

        for flag, item in documented.items():
            if flag in scripted:
                continue
            flag_severity = Severity.HIGH if item.target == "configure" else Severity.MEDIUM
            findings.append(
                Discrepancy(
                    category=DiscrepancyCategory.MISSING_BUILD_FLAG,
                    severity=flag_severity,
                    title=f"Build flag documented but missing from the script: {flag}",
                    description=(
                        f"The wiki page documents `{flag}` for the {item.target} step, but the script never passes it."
                    ),
                    component=flag,
                    doc_evidence=item.evidence,
                    recommendation=f"Pass `{flag}` in the script's build step (or update the documentation).",
                    confidence=0.8,
                    detected_by=DetectedBy.RULE,
                    **_audit_fields_for_category(DiscrepancyCategory.MISSING_BUILD_FLAG, flag, flag_severity),
                )
            )

        for flag, item in scripted.items():
            if flag in documented:
                continue
            findings.append(
                Discrepancy(
                    category=DiscrepancyCategory.UNDOCUMENTED_STEP,
                    severity=Severity.LOW,
                    title=f"Build flag used in the script but undocumented: {flag}",
                    description=(
                        f"The script passes `{flag}` during the {item.target} step; "
                        "the docs do not mention it."
                    ),
                    component=flag,
                    script_evidence=item.evidence,
                    recommendation=f"Document `{flag}` in the build instructions.",
                    confidence=0.65,
                    detected_by=DetectedBy.RULE,
                    **_audit_fields_for_category(DiscrepancyCategory.UNDOCUMENTED_STEP, flag, Severity.LOW),
                )
            )
        return findings

    def _patches(self, doc: ComponentSignature, script: ComponentSignature) -> list[Discrepancy]:
        findings: list[Discrepancy] = []
        if not script.s390x_patches:
            return findings

        doc_patch_text = " ".join(
            f"{patch.description} {patch.target or ''}" for patch in doc.s390x_patches
        ).lower()
        for patch in script.s390x_patches:
            descriptor = (patch.target or patch.description or "").lower()
            if doc_patch_text and descriptor and descriptor.split("/")[-1] in doc_patch_text:
                continue
            patch_component = patch.target or patch.description[:80]
            findings.append(
                Discrepancy(
                    category=DiscrepancyCategory.MISSING_S390X_PATCH,
                    severity=Severity.HIGH,
                    title=f"Undocumented s390x patch: {patch.target or patch.description[:60]}",
                    description=(
                        "The build script applies an architecture-specific workaround "
                        f"({patch.method}) that the documentation does not describe. Anyone following the wiki "
                        "would build a subtly different (and likely broken) tree."
                    ),
                    component=patch_component,
                    script_evidence=patch.evidence,
                    recommendation=(
                        "Add a 's390x notes' section to the wiki page describing this patch, "
                        "or upstream the change so the patch can be dropped."
                    ),
                    confidence=0.7,
                    detected_by=DetectedBy.RULE,
                    **_audit_fields_for_category(
                        DiscrepancyCategory.MISSING_S390X_PATCH, patch_component, Severity.HIGH
                    ),
                )
            )
        return findings


def compute_score(findings: Sequence[Discrepancy]) -> int:
    """Confidence-weighted consistency score: 100 = docs and script agree.

    The penalty decays exponentially so that a pair with ten small documentation
    nits does not score the same as a pair with ten missing s390x patches, while
    still spreading real reports across the whole 0-100 range.
    """
    penalty = sum(SEVERITY_WEIGHT.get(item.severity, 5) * (0.5 + item.confidence / 2) for item in findings)
    if penalty <= 0:
        return 100
    return max(0, min(100, int(round(100 * math.exp(-penalty / SCORE_DECAY)))))


def merge_findings(
    rule_findings: Sequence[Discrepancy], llm_findings: Sequence[Discrepancy]
) -> list[Discrepancy]:
    """De-duplicate on ``(category, component)``, preferring the rule evidence."""
    merged: dict[str, Discrepancy] = {}
    for finding in rule_findings:
        merged[finding.signature_key] = finding
    for finding in llm_findings:
        key = finding.signature_key
        existing = merged.get(key)
        if existing is None:
            merged[key] = finding
            continue
        # Keep the rule finding's hard evidence but adopt the model's richer prose.
        merged[key] = existing.model_copy(
            update={
                "description": finding.description or existing.description,
                "recommendation": finding.recommendation or existing.recommendation,
                "doc_evidence": existing.doc_evidence or finding.doc_evidence,
                "script_evidence": existing.script_evidence or finding.script_evidence,
                "severity": max(existing.severity, finding.severity, key=lambda s: SEVERITY_WEIGHT[s]),
                "detected_by": DetectedBy.BOTH,
                "confidence": max(existing.confidence, finding.confidence),
            }
        )
    ordered = sorted(merged.values(), key=lambda item: -SEVERITY_WEIGHT.get(item.severity, 0))
    return ordered


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You are a senior build systems engineer and static analysis auditor specializing
in Linux on IBM Z (s390x). You perform a strict, bidirectional compliance audit
between human-readable build instructions (Wiki) and the automated shell script
(Script) for a specific package. Identify whether the documentation accurately
reflects what the automation actually executes.

MANDATORY REASONING PROCESS:
1. Extraction - parse all commands from the Wiki (prerequisites, environment
   variables, git repos/tags, patches, build flags) and the same categories from
   the Shell Script, resolving conditional blocks for the specified target OS.
2. Normalization - treat equivalent package manager commands equally (apt-get vs
   apt, yum vs dnf), and differentiate benign differences (-j$(nproc) vs -j4)
   from functional discrepancies (missing flags, unapplied patches).
3. Categorization - classify every divergence as exactly one of:
   UNDOCUMENTED_ACTION (present in script, omitted in wiki),
   OBSOLETE_DOCUMENTATION (stated in wiki, absent in script), or
   PARAMETER_MISMATCH (mismatched versions, flags, or tags).

RULES:
- Base the analysis ONLY on the provided texts. Never invent commands.
- Pay critical attention to IBM Z (s390x) specifics: endianness, memory
  settings, -march=z13/z14, and architecture patches.
- Report only discrepancies you can point at evidence for; quote the wiki and/or
  the script verbatim. When one side is silent, use the literal string OMITTED.
- Ignore cosmetic differences: wording, formatting, examples, version numbers of
  unrelated tooling.
- Severity: critical = breaks the build or produces a wrong binary; high = likely
  build/runtime failure; medium = behaviour diverges; low/info = documentation
  quality.
- If wiki and script genuinely agree, return an empty `discrepancies` list, the
  MATCH verdict, and alignment_stats with all zeros. Never invent findings.
"""


def build_prompt(
    pair: RetrievedPair,
    rule_findings: Sequence[Discrepancy],
    *,
    max_chars: int,
    top_k: int,
) -> str:
    doc = pair.doc_signature
    script = pair.script_signature
    input_header = (
        f"# INPUT DATA\nTarget Package: {pair.package_name}\n"
        f"Target OS / Distro: {pair.distro.value}\nTarget Architecture: s390x"
    )
    sections: list[str] = [input_header]
    if doc is not None:
        sections.append(
            "## Documentation signature (extracted, for reference)\n"
            f"source: {doc.source_path}\n```json\n{doc.model_dump_json(indent=2, exclude_none=True)}\n```"
        )
    if script is not None:
        sections.append(
            "## Build script signature (extracted, for reference)\n"
            f"source: {script.source_path}\n```json\n{script.model_dump_json(indent=2, exclude_none=True)}\n```"
        )

    if rule_findings:
        rendered = "\n".join(
            f"- [{item.severity.value}] "
            f"{item.issue_type.value if item.issue_type else item.category.value} :: {item.title}"
            + (f" (component: {item.component})" if item.component else "")
            for item in rule_findings
        )
        sections.append(
            "## Deterministic pre-checks (already confirmed by set comparison)\n"
            f"{rendered}\n\nDo not repeat these verbatim; add semantic findings and, if a pre-check is "
            "clearly a false positive, explain why in `notes`."
        )

    excerpt_budget = max(2000, max_chars // 2)
    wiki_block = "[START OF WIKI DOCUMENTATION]\n" + _render_excerpts(pair.doc_hits, excerpt_budget)
    script_block = "[START OF BUILD SCRIPT]\n" + _render_excerpts(pair.script_hits, excerpt_budget)
    sections.append(wiki_block + "\n[END OF WIKI DOCUMENTATION]")
    sections.append(script_block + "\n[END OF BUILD SCRIPT]")

    sections.append(
        "## Task\nPerform the bidirectional audit following the MANDATORY REASONING PROCESS. Return findings "
        "a maintainer can act on: verbatim wiki/script references (OMITTED when one side is silent), an "
        "s390x-specific `ibm_z_impact`, and a concrete `suggested_fix`. Populate `documentations_to_update`, "
        "`scripts_to_update`, and `notes` where useful."
    )
    prompt = "\n\n".join(sections)
    return prompt[:max_chars]


def _render_excerpts(hits: Sequence[SearchHit], budget: int) -> str:
    if not hits:
        return "(none)"
    per_chunk = max(400, budget // max(1, min(len(hits), 10)))
    parts: list[str] = []
    used = 0
    for hit in hits[:12]:
        text = hit.text[:per_chunk]
        header = f"[{hit.metadata.get('source_path', '?')}#{hit.metadata.get('chunk_index', 0)}]"
        block = f"{header}\n{text}"
        if used + len(block) > budget:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts) or "(none)"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


class Analyzer:
    """Runs one comparison end-to-end and persists the report."""

    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        retriever: Retriever,
        llm: StructuredLLM,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.retriever = retriever
        self.llm = llm
        self.rules = RuleEngine()

    async def analyze(
        self,
        package_name: str,
        distro: Distro | str,
        *,
        force: bool = False,
        top_k: int | None = None,
        use_llm: bool = True,
    ) -> AnalysisReport:
        started = time.perf_counter()
        resolved = normalize_distro(distro)

        if not force:
            cached = self._fresh_report(package_name, resolved)
            if cached is not None:
                logger.info("returning cached report for %s/%s", package_name, resolved.value)
                return cached

        pair = self.retriever.retrieve(package_name, resolved, top_k=top_k)
        rule_findings = self.rules.diff(
            pair.doc_signature,
            pair.script_signature,
            doc_versions=pair.doc_versions,
            script_version=pair.script_version,
        )

        warnings = list(pair.warnings)
        rule_score = compute_score(rule_findings)
        llm_core = AuditReportCore(
            package=package_name,
            target_os=resolved.value,
            audit_verdict=verdict_for(rule_score, criticals=0, highs=0),
            confidence_score=0.6,
            summary="Deterministic rule comparison only.",
            discrepancies=[],
            alignment_stats=AlignmentStats(),
            documentations_to_update=[],
            scripts_to_update=[],
            notes=None,
        )
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        model_name = "rules-only"

        if use_llm and self.llm.available:
            try:
                prompt = build_prompt(
                    pair,
                    rule_findings,
                    max_chars=self.settings.max_context_chars,
                    top_k=top_k or self.settings.retrieval_top_k,
                )
                result = await self.llm.extract(prompt, AuditReportCore, system=SYSTEM_PROMPT)
                llm_core = AuditReportCore.model_validate(result.data)
                prompt_tokens = result.prompt_tokens
                completion_tokens = result.completion_tokens
                model_name = result.model or self.settings.llm_model
            except LLMError as exc:
                logger.warning("LLM review unavailable, falling back to rules: %s", exc)
                warnings.append(f"LLM review skipped: {exc}")
        elif use_llm:
            warnings.append(
                f"LLM provider '{self.settings.llm_provider}' is not configured; report contains rule findings only"
            )
        if self.settings.llm_provider == "mock":
            warnings.append(
                "LLM_PROVIDER=mock: the semantic review is stubbed; this report reflects deterministic checks only"
            )

        # LLM audit findings arrive in the strict audit schema; convert them into
        # the persisted report schema before merging with the rule findings.
        llm_discrepancies = [audit_to_internal_discrepancy(item) for item in llm_core.discrepancies]
        discrepancies = merge_findings(rule_findings, llm_discrepancies)
        rule_score = compute_score(discrepancies)
        created_at = datetime.now(timezone.utc)
        audit_verdict = verdict_for(
            rule_score,
            criticals=sum(1 for d in discrepancies if d.severity is Severity.CRITICAL),
            highs=sum(1 for d in discrepancies if d.severity is Severity.HIGH),
        )
        report = AnalysisReport(
            report_id=stable_id(
                "report",
                package_name,
                resolved.value,
                pair.source_ref(SourceType.DOC, pair.doc_hits).commit if pair.doc_hits else "",
                pair.source_ref(SourceType.SCRIPT, pair.script_hits).commit if pair.script_hits else "",
                created_at.isoformat(),
            ),
            package_name=package_name,
            distro=resolved,
            match_key=match_key(package_name, resolved),
            status=AnalysisStatus.ANALYZED,
            summary=llm_core.summary,
            # Two independent signals: keep the more conservative score. The LLM's
            # own confidence modulates nothing here - audit verdicts are derived
            # from the merged findings below via `audit_verdict`.
            consistency_score=(
                rule_score
                if model_name == "rules-only"
                else min(int(llm_core.confidence_score * 100), rule_score)
            ),
            discrepancies=discrepancies,
            documentations_to_update=llm_core.documentations_to_update,
            scripts_to_update=llm_core.scripts_to_update,
            notes=llm_core.notes,
            doc=pair.source_ref(SourceType.DOC, pair.doc_hits),
            script=pair.source_ref(SourceType.SCRIPT, pair.script_hits),
            doc_signature=pair.doc_signature,
            script_signature=pair.script_signature,
            model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=int((time.perf_counter() - started) * 1000),
            created_at=created_at,
            warnings=warnings,
        )

        self.repository.save_report(report)
        logger.info(
            "analyzed %s/%s: score=%s verdict=%s findings=%s in %sms",
            package_name,
            resolved.value,
            report.consistency_score,
            audit_verdict.value,
            len(report.discrepancies),
            report.duration_ms,
        )
        return report

    def _fresh_report(self, package_name: str, distro: Distro) -> AnalysisReport | None:
        """Reuse a stored report when the doc/script commits have not moved."""
        stored = self.repository.latest_report(package_name, distro)
        if stored is None:
            return None
        record = self.repository.get_package(package_name, distro)
        if record is None:
            return stored
        if (stored.doc.commit if stored.doc else None) != record.doc_commit:
            return None
        if (stored.script.commit if stored.script else None) != record.script_commit:
            return None
        return stored


__all__ = [
    "AnalysisError",
    "Analyzer",
    "NotIndexedError",
    "RetrievedPair",
    "Retriever",
    "RuleEngine",
    "SYSTEM_PROMPT",
    "build_prompt",
    "canonical_dependency",
    "compute_score",
    "dependency_family",
    "doc_version_candidates",
    "merge_findings",
    "missing_dependency_discrepancy",
    "pick_script_version",
]
