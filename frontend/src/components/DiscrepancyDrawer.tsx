"use client";

import { useEffect, useState } from "react";
import type { AnalysisReport, Discrepancy } from "@/lib/api-types";
import { CATEGORY_LABEL, ISSUE_TYPE_LABEL, SEVERITY_META } from "@/lib/ui";

/**
 * Discrepancy Drawer: slides in from the right and renders the JSON finding —
 * severity tag, affected lines (evidence), IBM Z (s390x) impact and the
 * suggested fix — plus a collapsible raw-JSON view.
 */
export default function DiscrepancyDrawer({
  report,
  index,
  onClose,
}: {
  report: AnalysisReport | null;
  index: number | null;
  onClose: () => void;
}) {
  const open = index !== null && report !== null;
  const disc: Discrepancy | null =
    report !== null && index !== null ? (report.discrepancies[index] ?? null) : null;
  const [showJson, setShowJson] = useState(false);

  useEffect(() => {
    if (!open) setShowJson(false);
  }, [open]);

  // lock body scroll while the drawer is open
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <>
      {/* scrim */}
      <div
        onClick={onClose}
        className={`fixed inset-0 z-40 bg-slate-950/40 transition-opacity ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
        aria-hidden="true"
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Discrepancy detail"
        className={`fixed right-0 top-0 z-50 flex h-full w-full max-w-xl flex-col border-l border-slate-300 bg-white shadow-2xl transition-transform duration-200 ${
          open ? "translate-x-0" : "pointer-events-none translate-x-full"
        }`}
      >
        {report !== null && disc !== null && index !== null && (
          <DrawerBody
            disc={disc}
            report={report}
            index={index}
            showJson={showJson}
            onToggleJson={() => setShowJson((v) => !v)}
            onClose={onClose}
          />
        )}
      </aside>
    </>
  );
}

function DrawerBody({
  disc,
  report,
  index,
  showJson,
  onToggleJson,
  onClose,
}: {
  disc: Discrepancy;
  report: AnalysisReport;
  index: number;
  showJson: boolean;
  onToggleJson: () => void;
  onClose: () => void;
}) {
  const sev = SEVERITY_META[disc.severity];
  const docPath = report.doc?.path ?? report.doc_signature?.source_path ?? "documentation";
  const scriptPath = report.script?.path ?? report.script_signature?.source_path ?? "script";

  return (
    <>
      <header className={`border-b border-slate-200 px-5 py-4 ${sev.border} border-l-4`}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
              <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${sev.badge}`}>
                {sev.label}
              </span>
              <span className="rounded-full border border-slate-300 bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600">
                {CATEGORY_LABEL[disc.category] ?? disc.category}
              </span>
              {disc.issue_type && (
                <span className="rounded-full border border-violet-200 bg-violet-200 px-2 py-0.5 text-[10px] font-semibold text-violet-900">
                  {ISSUE_TYPE_LABEL[disc.issue_type] ?? disc.issue_type}
                </span>
              )}
              <span className="rounded-full border border-slate-300 bg-white px-2 py-0.5 text-[10px] font-medium text-slate-500">
                {disc.detected_by === "both" ? "rule + LLM" : `detected by ${disc.detected_by}`}
              </span>
              <span className="font-mono text-[10px] text-slate-400">#{index + 1}</span>
            </div>
            <h2 className="text-sm font-semibold leading-snug text-slate-900">{disc.title}</h2>
            <p className="mt-0.5 truncate font-mono text-[11px] text-slate-500">
              {report.package_name} · {report.distro} · report {report.report_id.slice(0, 12)}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close drawer"
            className="shrink-0 rounded p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
          >
            ✕
          </button>
        </div>
      </header>

      <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4">
        {/* confidence */}
        <section>
          <div className="mb-1 flex items-center justify-between text-[11px] font-medium text-slate-500">
            <span>Confidence</span>
            <span className="font-mono">{Math.round(disc.confidence * 100)}%</span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
            <div
              className={`h-full rounded-full ${
                disc.confidence >= 0.75 ? "bg-emerald-500" : disc.confidence >= 0.5 ? "bg-amber-400" : "bg-slate-400"
              }`}
              style={{ width: `${Math.round(disc.confidence * 100)}%` }}
            />
          </div>
        </section>

        {/* description + IBM Z impact */}
        <Section title="What disagrees">
          <p className="text-sm leading-relaxed text-slate-700">{disc.description}</p>
        </Section>

        <Section title="IBM Z (s390x) impact">
          <div className="rounded-md border border-sky-100 bg-sky-100 px-3 py-2 text-sm leading-relaxed text-sky-900">
            {disc.component ? (
              <span className="mb-1 block font-mono text-[11px] font-semibold">{disc.component}</span>
            ) : null}
            {disc.ibm_z_impact ?? disc.description}
          </div>
        </Section>

        {/* affected lines */}
        <Section title="Affected lines">
          <div className="space-y-2">
            <EvidenceBlock label="📘 Wiki doc" path={docPath} text={disc.doc_evidence} />
            <EvidenceBlock label="🖥️ Script" path={scriptPath} text={disc.script_evidence} />
          </div>
        </Section>

        {/* suggested fix */}
        <Section title="Suggested fix">
          <div className="rounded-md border border-emerald-200 bg-emerald-100 px-3 py-2 text-sm leading-relaxed text-emerald-800">
            {disc.recommendation}
          </div>
        </Section>

        {report.notes && (
          <Section title="Analyzer notes">
            <p className="text-xs leading-relaxed text-slate-500">{report.notes}</p>
          </Section>
        )}

        {/* raw JSON */}
        <section>
          <button
            type="button"
            onClick={onToggleJson}
            className="flex w-full items-center justify-between rounded-md border border-slate-300 px-3 py-2 text-xs font-medium text-slate-600 transition hover:border-slate-400"
          >
            <span>Raw JSON finding</span>
            <span aria-hidden="true">{showJson ? "▾" : "▸"}</span>
          </button>
          {showJson && (
            <pre className="mt-2 max-h-72 overflow-auto rounded-md bg-slate-900 p-3 font-mono text-[11px] leading-relaxed text-slate-300">
              {JSON.stringify(disc, null, 2)}
            </pre>
          )}
        </section>
      </div>

      <footer className="border-t border-slate-200 px-5 py-3 text-[11px] text-slate-400">
        model <span className="font-mono">{report.model}</span>
        {report.duration_ms != null && <> · {(report.duration_ms / 1000).toFixed(1)}s</>}
        {" · "}
        {new Date(report.created_at).toLocaleString()}
      </footer>
    </>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-1.5 text-[11px] font-bold uppercase tracking-wide text-slate-400">{title}</h3>
      {children}
    </section>
  );
}

function EvidenceBlock({ label, path, text }: { label: string; path: string; text?: string | null }) {
  if (!text) return null;
  return (
    <div className="overflow-hidden rounded-md border border-slate-300">
      <div className="flex items-center justify-between border-b border-slate-200 bg-slate-100 px-2.5 py-1">
        <span className="text-[10px] font-bold uppercase tracking-wide text-slate-500">{label}</span>
        <span className="truncate font-mono text-[10px] text-slate-400">{path}</span>
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words bg-white px-2.5 py-2 font-mono text-[11px] leading-relaxed text-slate-700">
        {text}
      </pre>
    </div>
  );
}
