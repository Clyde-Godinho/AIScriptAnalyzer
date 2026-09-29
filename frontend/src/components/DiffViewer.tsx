"use client";

import { useMemo } from "react";
import type { AnalysisReport, Discrepancy } from "@/lib/api-types";
import DiffColumn from "./DiffColumn";
import type { DiffLine } from "./DiffColumn";

/**
 * Unified Diff Viewer: wiki documentation steps on the left, bash commands on
 * the right. Lines that a discrepancy cites on both sides are marked
 * "divergent" and highlighted; clicking one opens the discrepancy drawer.
 */
export default function DiffViewer({
  report,
  activeRef,
  onSelect,
}: {
  report: AnalysisReport;
  activeRef: number | null;
  onSelect: (index: number) => void;
}) {
  const { docLines, scriptLines } = useMemo(
    () => buildColumns(report),
    [report],
  );

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <DiffColumn
        title="📘 Wiki documentation"
        subtitle={report.doc?.path ?? report.doc_signature?.source_path ?? "documentation"}
        lines={docLines}
        divergentCount={docLines.filter((l) => l.kind === "divergent").length}
        tone="sky"
        activeRef={activeRef}
        onLineClick={onSelect}
      />
      <DiffColumn
        title="🖥️ Build script"
        subtitle={report.script?.path ?? report.script_signature?.source_path ?? "script"}
        lines={scriptLines}
        divergentCount={scriptLines.filter((l) => l.kind === "divergent").length}
        tone="violet"
        activeRef={activeRef}
        onLineClick={onSelect}
      />
    </div>
  );
}

function lineIsDivergent(disc: Discrepancy | undefined, evidence: string | null | undefined, lineText: string): boolean {
  if (!disc || !evidence || !lineText) return false;
  const ev = evidence.trim();
  if (ev.length < 6) return false;
  const hay = lineText.toLowerCase();
  const needle = ev.toLowerCase();
  // verbatim or token-overlap match between the cited evidence and this line
  if (hay.includes(needle) || needle.includes(hay)) return true;
  const tokens = needle.split(/[^a-z0-9_.-]+/).filter((t) => t.length > 3);
  if (tokens.length === 0) return false;
  const hits = tokens.filter((t) => hay.includes(t)).length;
  return hits / tokens.length >= 0.5;
}

function buildColumns(report: AnalysisReport): { docLines: DiffLine[]; scriptLines: DiffLine[] } {
  const docChunks = report.doc?.chunk_count ?? 0;
  const scriptChunks = report.script?.chunk_count ?? 0;

  // ---- documentation column: derive step lines from doc evidence ------------
  const docEvidence = report.discrepancies.map((d) => d.doc_evidence).filter((x): x is string => !!x);
  const docExtra = [
    ...report.documentations_to_update,
    ...(docChunks > 0 ? [`Retrieved ${docChunks} documentation chunk(s) for ${report.match_key}`] : []),
  ];
  const docLines: DiffLine[] = docEvidence.map((text, i) => ({
    n: i + 1,
    kind: "normal",
    text,
    refs: [],
  }));
  for (const extra of docExtra) {
    if (extra) docLines.push({ n: docLines.length + 1, kind: "normal", text: extra, refs: [] });
  }
  if (docLines.length === 0) {
    docLines.push({
      n: 1,
      kind: "normal",
      text: "No documentation excerpts were retrieved for this report.",
      refs: [],
    });
  }

  // ---- script column: derive command lines from script evidence -------------
  const scriptEvidence = report.discrepancies.map((d) => d.script_evidence).filter((x): x is string => !!x);
  const scriptExtra = [
    ...report.scripts_to_update,
    ...(scriptChunks > 0 ? [`Retrieved ${scriptChunks} script chunk(s) for ${report.match_key}`] : []),
  ];
  const scriptLines: DiffLine[] = scriptEvidence.map((text, i) => ({
    n: i + 1,
    kind: "normal",
    text,
    refs: [],
  }));
  for (const extra of scriptExtra) {
    if (extra) scriptLines.push({ n: scriptLines.length + 1, kind: "normal", text: extra, refs: [] });
  }
  if (scriptLines.length === 0) {
    scriptLines.push({
      n: 1,
      kind: "normal",
      text: "No script excerpts were retrieved for this report.",
      refs: [],
    });
  }

  // ---- attach discrepancy references and mark divergent pairs ---------------
  for (const [idx, d] of report.discrepancies.entries()) {
    let docHit = false;
    for (const line of docLines) {
      if (d.doc_evidence && lineIsDivergent(d, d.doc_evidence, line.text)) {
        line.refs.push(idx);
        docHit = true;
      }
    }
    if (!docHit && d.doc_evidence) {
      docLines.push({ n: docLines.length + 1, kind: "divergent", text: d.doc_evidence, refs: [idx] });
      docHit = true;
    }
    let scriptHit = false;
    for (const line of scriptLines) {
      if (d.script_evidence && lineIsDivergent(d, d.script_evidence, line.text)) {
        line.refs.push(idx);
        scriptHit = true;
      }
    }
    if (!scriptHit && d.script_evidence) {
      scriptLines.push({ n: scriptLines.length + 1, kind: "divergent", text: d.script_evidence, refs: [idx] });
    }
    void docHit;
    void scriptHit;
  }

  for (const line of docLines) if (line.refs.length) line.kind = "divergent";
  for (const line of scriptLines) if (line.refs.length) line.kind = "divergent";

  return { docLines, scriptLines };
}
