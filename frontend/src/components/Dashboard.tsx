"use client";

import { useCallback, useEffect, useState } from "react";
import type { AnalysisReport } from "@/lib/api-types";
import { SYNC_DONE_EVENT } from "./TopBar";
import PackageMatrix from "./PackageMatrix";
import DiffViewer from "./DiffViewer";
import DiscrepancyDrawer from "./DiscrepancyDrawer";
import { AUDIT_VERDICT, SEVERITY_META } from "@/lib/ui";

/**
 * Client shell: owns the selected report + drawer state, listens for
 * sync-complete events from the TopBar, and renders the three main views.
 */
export default function Dashboard() {
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);

  const openReport = useCallback((r: AnalysisReport) => {
    setReport(r);
    setActiveIndex(r.discrepancies.length > 0 ? 0 : null);
  }, []);

  // reset the open report after a successful re-index (its commit may be stale)
  useEffect(() => {
    const onSynced = () => setReport(null);
    window.addEventListener(SYNC_DONE_EVENT, onSynced);
    return () => window.removeEventListener(SYNC_DONE_EVENT, onSynced);
  }, []);

  return (
    <div className="space-y-8 pt-6">
      <PackageMatrix onAnalyzed={openReport} />

      {report ? (
        <section aria-label="Comparison for the selected package" className="space-y-3">
          <header className="flex flex-wrap items-center gap-3">
            <h2 className="text-sm font-semibold text-white">
              {report.package_name} <span className="text-slate-500">· {report.distro}</span>
            </h2>
            <span
              className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${
                report.consistency_score >= 90
                  ? "border-emerald-200 bg-emerald-100 text-emerald-800"
                  : report.consistency_score >= 65
                    ? "border-amber-200 bg-amber-100 text-amber-800"
                    : "border-rose-200 bg-rose-100 text-rose-800"
              }`}
            >
              consistency {report.consistency_score}/100
            </span>
            {(() => {
              const criticals = report.discrepancies.filter((d) => d.severity === "critical").length;
              const highs = report.discrepancies.filter((d) => d.severity === "high").length;
              const verdict =
                criticals > 0 || report.consistency_score < 65
                  ? AUDIT_VERDICT.BREAKING_DISCREPANCY
                  : highs > 0 || report.consistency_score < 90
                    ? AUDIT_VERDICT.MINOR_DRIFT
                    : AUDIT_VERDICT.MATCH;
              return (
                <span className={`rounded-full border px-2 py-0.5 text-[11px] font-bold ${verdict.badge}`}>
                  {verdict.label}
                </span>
              );
            })()}
            <div className="ml-auto flex items-center gap-1.5">
              {report.discrepancies.slice(0, 6).map((d, i) => (
                <button
                  key={i}
                  type="button"
                  onClick={() => setActiveIndex(i)}
                  title={d.title}
                  className={`rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase ${SEVERITY_META[d.severity].badge}`}
                >
                  {d.severity}
                </button>
                ))}
            </div>
          </header>

          <p className="max-w-3xl text-xs leading-relaxed text-slate-400">{report.summary}</p>

          <DiffViewer report={report} activeRef={activeIndex} onSelect={setActiveIndex} />

          <ul className="space-y-1.5">
            {report.discrepancies.map((d, i) => (
              <li key={i}>
                <button
                  type="button"
                  onClick={() => setActiveIndex(i)}
                  className={`flex w-full items-start gap-2 rounded-md border border-slate-800 bg-slate-900 px-3 py-2 text-left transition hover:border-slate-600 ${SEVERITY_META[d.severity].border} border-l-4`}
                >
                  <span className={`mt-0.5 shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase ${SEVERITY_META[d.severity].badge}`}>
                    {d.severity}
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate text-xs font-medium text-slate-200">{d.title}</span>
                    <span className="block truncate text-[11px] text-slate-500">{d.description}</span>
                  </span>
                  <span className="ml-auto shrink-0 font-mono text-[10px] text-slate-500">#{i + 1}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : (
        <p className="rounded-lg border border-dashed border-slate-800 bg-slate-900/50 px-4 py-10 text-center text-sm text-slate-500">
          Select <span className="font-semibold text-slate-300">Analyze</span> on a package above to load its
          documentation ↔ script comparison.
        </p>
      )}

      <DiscrepancyDrawer
        report={report}
        index={activeIndex}
        onClose={() => setActiveIndex(null)}
      />
    </div>
  );
}
