"""Tests for the LLM comparison & audit engine (Prompt 3 contract)."""

from __future__ import annotations

import asyncio

from app.analyzer import Analyzer, Retriever, RuleEngine, build_prompt, merge_findings
from app.config import Settings
from app.db import Repository
from app.llm import StructuredLLM
from app.models import (
    AlignmentStats,
    AnalysisReport,
    AnalysisStatus,
    AuditCategory,
    AuditReportCore,
    AuditVerdict,
    DetectedBy,
    Discrepancy,
    DiscrepancyCategory,
    Distro,
    IssueType,
    Severity,
    audit_to_internal_discrepancy,
    to_audit_report,
    verdict_for,
)
from app.vector_store import HashingEmbedder
from tests.fakes import InMemoryVectorStore
from tests.samples import SCRIPT_TEXT, WIKI_TEXT


def test_audit_output_schema_contract() -> None:
    """Every finding carries the strict audit fields; ids are sequential DISC-nn."""
    findings = [
        Discrepancy(
            category=DiscrepancyCategory.MISSING_S390X_PATCH,
            severity=Severity.HIGH,
            title="Undocumented s390x patch",
            description="script applies patch not in wiki",
            component="s390x-atomic-ops.patch",
            script_evidence="patch -p1 < s390x-atomic-ops.patch",
            recommendation="document the patch",
            confidence=0.7,
            detected_by=DetectedBy.RULE,
            ibm_z_impact="broken atomics on Z",
            issue_type=IssueType.UNDOCUMENTED_ACTION,
            audit_category=AuditCategory.PATCHES,
        ),
        Discrepancy(
            category=DiscrepancyCategory.ENV_VAR_VALUE_MISMATCH,
            severity=Severity.MEDIUM,
            title="Env value mismatch",
            description="EXAMPLE_ROOT differs",
            component="EXAMPLE_ROOT",
            doc_evidence="export EXAMPLE_ROOT=/usr/local/example",
            script_evidence="export EXAMPLE_ROOT=/opt/example",
            recommendation="reconcile",
            confidence=0.75,
            detected_by=DetectedBy.RULE,
        ),
    ]
    report = AnalysisReport(
        report_id="r1",
        package_name="example",
        distro=Distro.UBUNTU,
        match_key="example::ubuntu",
        status=AnalysisStatus.ANALYZED,
        summary="drift detected",
        consistency_score=42,
        discrepancies=findings,
        model="rules-only",
    )

    audit = to_audit_report(report)
    assert [item.id for item in audit.discrepancies] == ["DISC-01", "DISC-02"]
    assert audit.audit_verdict is AuditVerdict.BREAKING_DISCREPANCY  # score < 65
    assert audit.package == "example" and audit.target_os == "ubuntu"
    assert audit.alignment_stats.total_discrepancies == 2
    assert audit.alignment_stats.critical_count == 0
    assert audit.alignment_stats.missing_doc_steps == 1  # patch is script-only
    # The env-var mismatch is a PARAMETER_MISMATCH, not an obsolete doc step.
    assert audit.alignment_stats.obsolete_doc_steps == 0

    first = audit.discrepancies[0]
    assert first.category is AuditCategory.PATCHES
    assert first.issue_type is IssueType.UNDOCUMENTED_ACTION
    assert first.wiki_reference == "OMITTED"
    assert first.script_reference == "patch -p1 < s390x-atomic-ops.patch"
    assert first.ibm_z_impact == "broken atomics on Z"
    assert first.suggested_fix == "document the patch"

    # Round trip back into the storage schema keeps the audit metadata.
    back = audit_to_internal_discrepancy(first)
    assert back.issue_type is IssueType.UNDOCUMENTED_ACTION
    assert back.ibm_z_impact == "broken atomics on Z"
    assert back.doc_evidence is None and back.script_evidence is not None


def test_verdict_thresholds() -> None:
    assert verdict_for(100) is AuditVerdict.MATCH
    assert verdict_for(90) is AuditVerdict.MATCH
    assert verdict_for(89) is AuditVerdict.MINOR_DRIFT
    assert verdict_for(65) is AuditVerdict.MINOR_DRIFT
    assert verdict_for(64) is AuditVerdict.BREAKING_DISCREPANCY
    assert verdict_for(100, criticals=1) is AuditVerdict.BREAKING_DISCREPANCY
    assert verdict_for(95, highs=1) is AuditVerdict.MINOR_DRIFT
    assert verdict_for(None) is AuditVerdict.BREAKING_DISCREPANCY


def test_audit_round_trip_omitted() -> None:
    item = Discrepancy(
        category=DiscrepancyCategory.OTHER,
        severity=Severity.LOW,
        title="t",
        description="d",
        recommendation="r",
    )
    audit_item = to_audit_report(
        AnalysisReport(
            report_id="r",
            package_name="p",
            distro=Distro.UBUNTU,
            match_key="p::ubuntu",
            summary="s",
            consistency_score=100,
            discrepancies=[item],
            model="m",
        )
    ).discrepancies[0]
    assert audit_item.wiki_reference == "OMITTED"
    assert audit_item.script_reference == "OMITTED"
    back = audit_to_internal_discrepancy(audit_item)
    assert back.doc_evidence is None and back.script_evidence is None


def test_rule_engine_findings_carry_audit_metadata(services_retriever) -> None:
    pair = services_retriever.retrieve("example", Distro.UBUNTU)
    findings = RuleEngine().diff(pair.doc_signature, pair.script_signature)
    assert findings, "the sample pair is deliberately inconsistent"
    for finding in findings:
        assert finding.issue_type is not None
        assert finding.audit_category is not None
        assert finding.ibm_z_impact, "every rule finding gets an s390x impact statement"
    # Audit category is derived at serialization time when not set explicitly.
    from app.models import to_audit_discrepancy

    audit_view = to_audit_discrepancy(findings[0], 1)
    assert audit_view.category in AuditCategory
    # A script-only s390x patch is an UNDOCUMENTED_ACTION in the PATCHES bucket.
    patch = next(f for f in findings if f.category is DiscrepancyCategory.MISSING_S390X_PATCH)
    assert patch.issue_type is IssueType.UNDOCUMENTED_ACTION
    assert patch.audit_category is AuditCategory.PATCHES
    # A documented-but-never-installed dep is OBSOLETE_DOCUMENTATION.
    dep = next(f for f in findings if f.category is DiscrepancyCategory.MISSING_DEPENDENCY)
    assert dep.issue_type is IssueType.OBSOLETE_DOCUMENTATION
    assert dep.audit_category is AuditCategory.PREREQUISITES


def test_prompt_uses_audit_input_markers(services_retriever) -> None:
    pair = services_retriever.retrieve("example", Distro.UBUNTU)
    findings = RuleEngine().diff(pair.doc_signature, pair.script_signature)
    prompt = build_prompt(pair, findings, max_chars=24_000, top_k=8)
    assert "[START OF WIKI DOCUMENTATION]" in prompt
    assert "[END OF WIKI DOCUMENTATION]" in prompt
    assert "[START OF BUILD SCRIPT]" in prompt
    assert "[END OF BUILD SCRIPT]" in prompt
    assert "Target Architecture: s390x" in prompt
    assert "Target Package: example" in prompt


def test_analyzer_report_is_audit_ready(services, corpus) -> None:
    analyzer: Analyzer = services.analyzer
    report = asyncio.run(analyzer.analyze("example", Distro.UBUNTU, force=True))
    audit = to_audit_report(report)
    assert audit.package == "example"
    assert audit.target_os == "ubuntu"
    assert audit.audit_verdict == verdict_for(
        report.consistency_score,
        criticals=sum(1 for d in report.discrepancies if d.severity is Severity.CRITICAL),
        highs=sum(1 for d in report.discrepancies if d.severity is Severity.HIGH),
    )
    assert audit.alignment_stats.total_discrepancies == len(report.discrepancies)
    for item in audit.discrepancies:
        assert item.id.startswith("DISC-")
        assert item.ibm_z_impact
        assert item.suggested_fix
        assert item.issue_type in IssueType


def test_match_verdict_for_consistent_pair(
    pipeline, store: InMemoryVectorStore, repository: Repository, settings: Settings
) -> None:
    """A fully aligned doc/script pair yields MATCH and zero stats."""
    doc = (
        "# Building Aligned\n\n## Ubuntu\n\n```bash\n"
        "sudo apt-get install -y build-essential\n"
        "export ALIGNED_ROOT=/opt/aligned\n"
        "```\n\n```bash\n./configure --prefix=/opt/aligned\nmake -j$(nproc)\n```\n"
    )
    script = (
        "#!/bin/bash\nexport ALIGNED_ROOT=/opt/aligned\n"
        "sudo apt-get install -y build-essential\n"
        "./configure --prefix=/opt/aligned\nmake -j$(nproc)\n"
    )
    doc_path = settings.work_dir / "docs.wiki" / "Building-Aligned.md"
    script_path = settings.work_dir / "scripts" / "aligned" / "1.0" / "build_aligned.sh"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(doc, encoding="utf-8")
    script_path.write_text(script, encoding="utf-8")
    repository.set_repo_state("docs_wiki", "file:///fake", "d" * 40)
    repository.set_repo_state("scripts", "file:///fake", "e" * 40)

    from app.ingestion import DiscoveredPackage

    pipeline.index(
        [
            DiscoveredPackage(
                package_name="aligned",
                normalized_name="aligned",
                doc_path=doc_path,
                script_paths={"1.0": script_path},
                distros=[Distro.UBUNTU],
            )
        ],
        full_reindex=True,
    )
    retriever = Retriever(settings, store, HashingEmbedder(dimension=128))
    pair = retriever.retrieve("aligned", Distro.UBUNTU)
    findings = RuleEngine().diff(pair.doc_signature, pair.script_signature)
    assert findings == []

    llm = StructuredLLM(settings)
    analyzer = Analyzer(settings, repository, retriever, llm)
    report = asyncio.run(analyzer.analyze("aligned", Distro.UBUNTU, force=True))
    audit = to_audit_report(report)
    assert audit.audit_verdict is AuditVerdict.MATCH
    assert audit.alignment_stats.total_discrepancies == 0
    assert report.discrepancies == []


def test_merge_prefers_higher_severity() -> None:
    rule = Discrepancy(
        category=DiscrepancyCategory.MISSING_DEPENDENCY,
        severity=Severity.MEDIUM,
        title="dep",
        description="rule saw it",
        component="libz",
        recommendation="add it",
        detected_by=DetectedBy.RULE,
        issue_type=IssueType.OBSOLETE_DOCUMENTATION,
    )
    llm = Discrepancy(
        category=DiscrepancyCategory.MISSING_DEPENDENCY,
        severity=Severity.HIGH,
        title="dep",
        description="llm prose",
        component="libz",
        recommendation="add it",
        detected_by=DetectedBy.LLM,
    )
    merged = merge_findings([rule], [llm])
    assert len(merged) == 1
    assert merged[0].severity is Severity.HIGH
    assert merged[0].detected_by is DetectedBy.BOTH


def test_alignment_stats_recomputed_on_validation() -> None:
    core = AuditReportCore(
        package="p",
        target_os="ubuntu",
        audit_verdict=AuditVerdict.MINOR_DRIFT,
        confidence_score=0.8,
        summary="s",
        discrepancies=[
            {
                "id": "DISC-99",
                "category": "patches",
                "severity": "critical",
                "issue_type": "undocumented_action",
                "description": "d",
                "wiki_reference": "OMITTED",
                "script_reference": "patch -p1",
                "ibm_z_impact": "z",
                "suggested_fix": "f",
            }
        ],
        alignment_stats=AlignmentStats(),
    )
    assert core.discrepancies[0].id == "DISC-01"  # renumbered
    assert core.alignment_stats.critical_count == 1
    assert core.alignment_stats.missing_doc_steps == 1


def test_sample_texts_feed_the_audit_engine(services_retriever) -> None:
    """The shipped wiki/script sample pair is parseable by the audit pipeline."""
    assert "EXAMPLE_ROOT" in WIKI_TEXT
    assert "s390x-atomic-ops.patch" in SCRIPT_TEXT
    pair = services_retriever.retrieve("example", Distro.UBUNTU)
    assert pair.doc_signature is not None and pair.script_signature is not None
