"use client";

import { useCallback, useEffect, useState } from "react";
import type { AnalysisReport } from "@/lib/api-types";
import { auditJson, reportJson } from "@/lib/api-client";
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
      <ExplainerCard />
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

          <JsonExportBar report={report} />

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

const COPY_JSON_URL = "/api/results/{pkg}/latest/json?distro={distro}";

/** One-screen answer to "what is this?" — the two sources and the core question. */
function ExplainerCard() {
  return (
    <section
      aria-label="What this tool does"
      className="rounded-lg border border-slate-800 bg-slate-900/60 px-4 py-3"
    >
      <h2 className="text-sm font-semibold text-white">What this tool checks</h2>
      <p className="mt-1 max-w-4xl text-xs leading-relaxed text-slate-400">
        For each package it compares two sources and answers one question:
        <span className="text-slate-200"> “Does the build documentation accurately describe what the automation scripts actually do?”</span>
      </p>
      <div className="mt-2.5 grid gap-2 md:grid-cols-2">
        <div className="rounded-md border border-sky-900/60 bg-sky-950/40 px-3 py-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-sky-200">
            <span aria-hidden="true">📘</span> 1 · Build documentation (wiki)
          </div>
          <a
            href="https://github.com/linux-on-ibm-z/docs/wiki/"
            target="_blank"
            rel="noreferrer"
            className="mt-0.5 block truncate font-mono text-[11px] text-sky-400 underline decoration-dotted hover:text-sky-300"
          >
            github.com/linux-on-ibm-z/docs/wiki
          </a>
          <p className="mt-0.5 text-[11px] leading-relaxed text-slate-400">
            Manual steps: prerequisites to install, env vars, build commands, patches.
          </p>
        </div>
        <div className="rounded-md border border-violet-900/60 bg-violet-950/40 px-3 py-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-violet-200">
            <span aria-hidden="true">🖥️</span> 2 · Automated build scripts
          </div>
          <a
            href="https://github.com/linux-on-ibm-z/scripts"
            target="_blank"
            rel="noreferrer"
            className="mt-0.5 block truncate font-mono text-[11px] text-violet-400 underline decoration-dotted hover:text-violet-300"
          >
            github.com/linux-on-ibm-z/scripts
          </a>
          <p className="mt-0.5 text-[11px] leading-relaxed text-slate-400">
            What the automation actually installs, exports and configures on s390x.
          </p>
        </div>
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
        Every mismatch is reported with severity, evidence from both sides, a suggested fix, and can be
        exported as JSON. Health colours: <span className="font-semibold text-emerald-400">green = in sync</span>,{" "}
        <span className="font-semibold text-amber-400">amber = minor drift</span>,{" "}
        <span className="font-semibold text-rose-400">red = breaking discrepancy</span>,{" "}
        <span className="font-semibold text-slate-300">grey = not analyzed yet</span>.
      </p>
    </section>
  );
}

/** Copy / download the comparison as JSON (both raw report and audit schema). */
function JsonExportBar({ report }: { report: AnalysisReport }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  const saveFile = (name: string, text: string) => {
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  };

  const copy = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      window.setTimeout(() => setCopied(null), 2000);
    } catch {
      setCopied("error");
      window.setTimeout(() => setCopied(null), 2000);
    }
  };

  const fetchJson = (kind: "report" | "audit") =>
    kind === "report" ? reportJson(report.package_name, report.distro) : auditJson(report.package_name, report.distro);

  const btn =
    "rounded-md border border-slate-700 px-2.5 py-1 text-[11px] font-medium text-slate-300 transition hover:border-slate-500 hover:text-white disabled:opacity-50";

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Export</span>
        <button
          type="button"
          className={btn}
          onClick={async () => copy("report", await fetchJson("report"))}
          title="Copy the full comparison JSON to the clipboard"
        >
          {copied === "report" ? "Copied ✓" : "Copy JSON"}
        </button>
        <button
          type="button"
          className={btn}
          onClick={async () =>
            saveFile(
              `${report.package_name}-${report.distro}-comparison.json`,
              await fetchJson("report"),
            )
          }
        >
          Download comparison.json
        </button>
        <button type="button" className={btn} onClick={() => setOpen((v) => !v)}>
          {open ? "Hide audit JSON" : "View audit JSON"}
        </button>
        <a
          className="ml-auto text-[11px] text-slate-500 underline decoration-dotted hover:text-slate-300"
          href={COPY_JSON_URL.replace("{pkg}", encodeURIComponent(report.package_name)).replace(
            "{distro}",
            report.distro,
          )}
          target="_blank"
          rel="noreferrer"
          title="Open the raw JSON endpoint"
        >
          raw endpoint ↗
        </a>
      </div>
      {open && <AuditJsonPanel report={report} />}
      {copied === "error" && (
        <p className="mt-1 text-[10px] text-rose-500">Clipboard blocked by the browser — use Download instead.</p>
      )}
    </div>
  );
}

/** Lazily loads and pretty-prints the strict audit-schema JSON. */
function AuditJsonPanel({ report }: { report: AnalysisReport }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    auditJson(report.package_name, report.distro, controller.signal)
      .then(setText)
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setError(err instanceof Error ? err.message : "failed");
      });
    return () => controller.abort();
  }, [report.package_name, report.distro]);

  if (error) return <p className="mt-2 text-[11px] text-rose-500">{error}</p>;
  return (
    <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-300">
      {text ?? "Loading…"}
    </pre>
  );
}
