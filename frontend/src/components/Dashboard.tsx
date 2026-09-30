"use client";

import { useCallback, useEffect, useState } from "react";
import type { AnalysisReport, Discrepancy, PackageSummary } from "@/lib/api-types";
import { analyzePackage, latestResult, listPackages, reportJson } from "@/lib/api-client";
import { SYNC_DONE_EVENT } from "./TopBar";
import DiffViewer from "./DiffViewer";
import DiscrepancyDrawer from "./DiscrepancyDrawer";
import Spinner from "./Spinner";
import { AUDIT_VERDICT, CATEGORY_LABEL, SEVERITY_META } from "@/lib/ui";

const POPULAR_PACKAGES = [
  { id: "nginx", name: "NGINX", subtitle: "Web Server & Reverse Proxy", tag: "🟢 100% In Sync", defaultDistro: "ubuntu" },
  { id: "redis", name: "Redis", subtitle: "In-Memory Data Store", tag: "🟡 Minor Drift (90%)", defaultDistro: "ubuntu" },
  { id: "PostgreSQL", name: "PostgreSQL", subtitle: "Relational Database", tag: "🔴 Discrepancies (37%)", defaultDistro: "ubuntu" },
  { id: "Apache-HTTP-Server", name: "Apache HTTP Server", subtitle: "Web Server", tag: "🟡 Minor Drift (72%)", defaultDistro: "ubuntu" },
  { id: "Apache-Kafka", name: "Apache Kafka", subtitle: "Distributed Event Streamer", tag: "🔴 Discrepancies (15%)", defaultDistro: "ubuntu" },
  { id: "HAProxy", name: "HAProxy", subtitle: "TCP/HTTP Load Balancer", tag: "🟡 Discrepancies (50%)", defaultDistro: "ubuntu" },
  { id: "Elasticsearch", name: "Elasticsearch", subtitle: "Search & Analytics Engine", tag: "🔴 Discrepancies (24%)", defaultDistro: "ubuntu" },
  { id: "RabbitMQ", name: "RabbitMQ", subtitle: "AMQP Message Broker", tag: "🔴 Discrepancies (32%)", defaultDistro: "ubuntu" },
  { id: "CouchDB", name: "CouchDB", subtitle: "Document Database", tag: "🟡 Minor Drift (90%)", defaultDistro: "ubuntu" },
  { id: "example", name: "Example Benchmark", subtitle: "Reference Test Suite", tag: "🔴 Benchmark (23%)", defaultDistro: "ubuntu" },
];

export default function Dashboard() {
  const [packages, setPackages] = useState<PackageSummary[]>([]);
  const [selectedPkgName, setSelectedPkgName] = useState("nginx");
  const [selectedDistro, setSelectedDistro] = useState("ubuntu");
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"findings" | "diff">("findings");

  // Load package list
  const loadPackages = useCallback(async () => {
    try {
      const data = await listPackages({ limit: 500 });
      setPackages(data.packages);

      // Auto-load initial report (nginx or first popular package)
      const initialName = "nginx";
      const initialDistro = "ubuntu";
      setSelectedPkgName(initialName);
      setSelectedDistro(initialDistro);

      try {
        const r = await latestResult(initialName, initialDistro);
        setReport(r);
      } catch {
        // If not analyzed yet, run it
        void runAudit(initialName, initialDistro);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load package list");
    }
  }, []);

  useEffect(() => {
    void loadPackages();
  }, [loadPackages]);

  useEffect(() => {
    window.addEventListener(SYNC_DONE_EVENT, loadPackages);
    return () => window.removeEventListener(SYNC_DONE_EVENT, loadPackages);
  }, [loadPackages]);

  // Restrict dropdown strictly to the 10 popular packages
  const popularList = POPULAR_PACKAGES.filter((pop) => {
    if (packages.length === 0) return true;
    return packages.some((p) => p.package_name.toLowerCase() === pop.id.toLowerCase());
  });

  // Available distros for the selected package
  const availableDistros = Array.from(
    new Set(
      packages
        .filter((p) => p.package_name.toLowerCase() === selectedPkgName.toLowerCase())
        .map((p) => p.distro)
    )
  );
  if (availableDistros.length === 0) availableDistros.push("ubuntu");

  // Load or analyze the selected package
  const handleSelectPackage = async (pkgName: string, distro: string) => {
    // If target distro is not available for this package, fall back to first available or ubuntu
    const pkgDistros = packages
      .filter((p) => p.package_name.toLowerCase() === pkgName.toLowerCase())
      .map((p) => p.distro);
    const targetDistro = pkgDistros.includes(distro as any) ? distro : (pkgDistros[0] || "ubuntu");

    setSelectedPkgName(pkgName);
    setSelectedDistro(targetDistro);
    setLoading(true);
    setError(null);
    try {
      const r = await latestResult(pkgName, targetDistro);
      setReport(r);
    } catch {
      // If no stored report, run analysis
      await runAudit(pkgName, targetDistro);
    } finally {
      setLoading(false);
    }
  };

  const runAudit = async (pkgName: string, distro: string) => {
    setAnalyzing(true);
    setError(null);
    try {
      const res = await analyzePackage(pkgName, distro as any, { force: true });
      if (res.report) {
        setReport(res.report);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Analysis failed");
    } finally {
      setAnalyzing(false);
    }
  };

  return (
    <div className="space-y-6 pt-6 max-w-5xl mx-auto">
      {/* Super Simple Package Selector Card */}
      <section className="rounded-2xl border border-slate-800 bg-slate-900/90 p-5 shadow-xl space-y-4">
        <div>
          <h2 className="text-base font-bold text-white flex items-center gap-2">
            <span>📦 Select Package to Check</span>
            <span className="rounded bg-sky-950 border border-sky-800/60 px-2 py-0.5 font-mono text-[10px] text-sky-400">
              Linux on IBM Z (s390x)
            </span>
            <span className="rounded bg-emerald-950 border border-emerald-800/60 px-2 py-0.5 font-mono text-[10px] text-emerald-400">
              Top 10 Popular Packages
            </span>
          </h2>
          <p className="text-xs text-slate-400 mt-0.5">
            Pick any of the 10 popular packages below to check whether its build wiki matches its automation script.
          </p>
        </div>

        {/* Dropdowns Row */}
        <div className="grid gap-3 sm:grid-cols-12 items-end">
          {/* Package Dropdown */}
          <div className="sm:col-span-6 space-y-1">
            <label htmlFor="package-select" className="block text-xs font-semibold text-slate-300">
              Package:
            </label>
            <select
              id="package-select"
              value={selectedPkgName}
              onChange={(e) => void handleSelectPackage(e.target.value, selectedDistro)}
              className="w-full rounded-xl border border-slate-700 bg-slate-950 px-3.5 py-2.5 text-xs font-medium text-white focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 transition"
            >
              {popularList.map((pkg) => (
                <option key={pkg.id} value={pkg.id}>
                  {pkg.name} — {pkg.subtitle} ({pkg.tag})
                </option>
              ))}
            </select>
          </div>

          {/* Distro Dropdown */}
          <div className="sm:col-span-3 space-y-1">
            <label htmlFor="distro-select" className="block text-xs font-semibold text-slate-300">
              Target Distro:
            </label>
            <select
              id="distro-select"
              value={selectedDistro}
              onChange={(e) => void handleSelectPackage(selectedPkgName, e.target.value)}
              className="w-full rounded-xl border border-slate-700 bg-slate-950 px-3.5 py-2.5 text-xs font-medium text-white focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 transition uppercase"
            >
              {availableDistros.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </div>

          {/* Analyze Button */}
          <div className="sm:col-span-3">
            <button
              type="button"
              onClick={() => void runAudit(selectedPkgName, selectedDistro)}
              disabled={analyzing || loading}
              className="w-full inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-sky-500 to-indigo-600 px-4 py-2.5 text-xs font-bold text-white shadow-lg shadow-sky-500/20 transition hover:from-sky-400 hover:to-indigo-500 active:scale-95 disabled:opacity-60"
            >
              {analyzing ? <Spinner className="h-4 w-4" /> : <span>▶</span>}
              {analyzing ? "Auditing…" : "Check Consistency"}
            </button>
          </div>
        </div>

        {/* 1-Click Quick Benchmark Shortcuts */}
        <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-slate-800/80">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
            Quick Select:
          </span>
          {popularList.map((b) => (
            <button
              key={b.id}
              type="button"
              onClick={() => void handleSelectPackage(b.id, b.defaultDistro)}
              className={`rounded-lg border px-2.5 py-1 text-xs font-medium transition ${
                selectedPkgName.toLowerCase() === b.id.toLowerCase()
                  ? "border-sky-500 bg-sky-500/20 text-sky-200 font-semibold"
                  : "border-slate-800 bg-slate-950/60 text-slate-400 hover:border-slate-700 hover:text-slate-200"
              }`}
            >
              {b.name}
            </button>
          ))}
        </div>
      </section>

      {/* Error Message */}
      {error && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-xs text-rose-300">
          {error}
        </div>
      )}

      {/* Loading State */}
      {(loading || analyzing) && (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/40 p-12 text-center text-xs text-slate-400 space-y-3">
          <Spinner className="mx-auto h-7 w-7 text-sky-400" />
          <p className="font-semibold text-slate-200">
            Auditing {selectedPkgName} on Linux on IBM Z ({selectedDistro})…
          </p>
          <p className="text-[11px] text-slate-500">
            Extracting dependencies, build flags, env vars, and architecture patches
          </p>
        </div>
      )}

      {/* Results View */}
      {!loading && !analyzing && report && (
        <section className="rounded-2xl border border-slate-800 bg-slate-900/90 p-5 shadow-xl space-y-5">
          {/* Score & Verdict Card */}
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between border-b border-slate-800 pb-5">
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-xl font-bold tracking-tight text-white capitalize">
                  {report.package_name}
                </h3>
                <span className="rounded-md border border-slate-700 bg-slate-800 px-2 py-0.5 font-mono text-xs font-semibold uppercase text-slate-300">
                  {report.distro}
                </span>
              </div>
              <p className="max-w-xl text-xs text-slate-400 leading-relaxed mt-1">
                {report.summary}
              </p>
            </div>

            {/* Big Score Gauge */}
            <div
              className={`flex items-center gap-3.5 rounded-2xl border px-4 py-2.5 ${
                report.consistency_score >= 90
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                  : report.consistency_score >= 65
                    ? "border-amber-500/30 bg-amber-500/10 text-amber-300"
                    : "border-rose-500/30 bg-rose-500/10 text-rose-300"
              }`}
            >
              <div>
                <div className="text-[10px] font-bold uppercase tracking-wider opacity-80">
                  Consistency Score
                </div>
                <div className="text-3xl font-black font-mono leading-none">
                  {report.consistency_score}
                  <span className="text-xs font-normal opacity-70"> / 100</span>
                </div>
              </div>

              <div className="h-9 w-px bg-current opacity-20" />

              <div>
                {(() => {
                  const criticals = report.discrepancies.filter((d) => d.severity === "critical").length;
                  const highs = report.discrepancies.filter((d) => d.severity === "high").length;
                  const verdict =
                    criticals > 0 || report.consistency_score < 65
                      ? AUDIT_VERDICT.BREAKING_DISCREPANCY
                      : highs > 0 || report.consistency_score < 90
                        ? AUDIT_VERDICT.MINOR_DRIFT
                        : AUDIT_VERDICT.MATCH;
                  return <span className="text-xs font-bold block">{verdict.label}</span>;
                })()}
                <span className="text-[11px] opacity-80 block">
                  {report.discrepancies.length === 0
                    ? "Docs and scripts agree"
                    : `${report.discrepancies.length} mismatches found`}
                </span>
              </div>
            </div>
          </div>

          {/* Simple Tab Switcher: Findings List vs Diff */}
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 pb-2">
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setViewMode("findings")}
                className={`rounded-lg px-3.5 py-1.5 text-xs font-semibold transition ${
                  viewMode === "findings"
                    ? "bg-sky-500 text-white shadow-md shadow-sky-500/20"
                    : "text-slate-400 hover:text-slate-200 bg-slate-800/60"
                }`}
              >
                Discrepancies ({report.discrepancies.length})
              </button>
              <button
                type="button"
                onClick={() => setViewMode("diff")}
                className={`rounded-lg px-3.5 py-1.5 text-xs font-semibold transition ${
                  viewMode === "diff"
                    ? "bg-sky-500 text-white shadow-md shadow-sky-500/20"
                    : "text-slate-400 hover:text-slate-200 bg-slate-800/60"
                }`}
              >
                Side-by-Side Diff
              </button>
            </div>

            <ExportButtons report={report} />
          </div>

          {/* View 1: Discrepancy Cards */}
          {viewMode === "findings" && (
            <div className="space-y-3">
              {report.discrepancies.length === 0 ? (
                <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-8 text-center space-y-1">
                  <span className="text-3xl">🎉</span>
                  <h4 className="text-sm font-bold text-emerald-300">
                    Documentation is 100% In Sync!
                  </h4>
                  <p className="text-xs text-emerald-400/80">
                    The build documentation and automation scripts match completely. No drift detected.
                  </p>
                </div>
              ) : (
                <div className="grid gap-3">
                  {report.discrepancies.map((d, i) => (
                    <FindingCard
                      key={i}
                      discrepancy={d}
                      index={i}
                      isActive={activeIndex === i}
                      onSelect={() => setActiveIndex(i)}
                    />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* View 2: Side-by-Side Unified Diff */}
          {viewMode === "diff" && (
            <div className="space-y-2">
              <p className="text-xs text-slate-400">
                Compare wiki instructions (left) with automation shell commands (right).
              </p>
              <DiffViewer report={report} activeRef={activeIndex} onSelect={setActiveIndex} />
            </div>
          )}
        </section>
      )}

      {/* Discrepancy Detail Drawer */}
      <DiscrepancyDrawer
        report={report}
        index={activeIndex}
        onClose={() => setActiveIndex(null)}
      />
    </div>
  );
}

/** Clean Finding Card with Documented vs Script comparison */
function FindingCard({
  discrepancy: d,
  index,
  isActive,
  onSelect,
}: {
  discrepancy: Discrepancy;
  index: number;
  isActive: boolean;
  onSelect: () => void;
}) {
  const meta = SEVERITY_META[d.severity] ?? SEVERITY_META.medium;

  return (
    <div
      onClick={onSelect}
      className={`cursor-pointer rounded-xl border p-4 transition ${
        isActive
          ? "border-sky-500 bg-slate-850 shadow-md ring-1 ring-sky-500"
          : "border-slate-800 bg-slate-900/90 hover:border-slate-700 hover:bg-slate-850/80"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2">
          <span className={`rounded-md border px-2 py-0.5 text-[10px] font-bold uppercase ${meta.badge}`}>
            {d.severity}
          </span>
          <span className="rounded bg-slate-800 px-2 py-0.5 font-mono text-[10px] text-slate-400">
            {CATEGORY_LABEL[d.category] ?? d.category}
          </span>
          <h4 className="text-xs font-bold text-slate-200">{d.title}</h4>
        </div>
        <span className="font-mono text-[10px] text-slate-500">#{index + 1}</span>
      </div>

      <p className="text-xs text-slate-400 leading-relaxed mb-3">{d.description}</p>

      {/* Comparison boxes */}
      <div className="grid gap-2 sm:grid-cols-2 text-xs font-mono">
        <div className="rounded-lg border border-sky-900/40 bg-sky-950/20 p-2.5">
          <div className="text-[10px] font-bold text-sky-400 uppercase tracking-wider mb-1 flex items-center gap-1">
            <span>📘</span> Stated in Wiki Docs
          </div>
          <div className="text-slate-300 break-words text-[11px]">
            {d.doc_evidence || <span className="italic text-slate-600">None / Omitted</span>}
          </div>
        </div>

        <div className="rounded-lg border border-violet-900/40 bg-violet-950/20 p-2.5">
          <div className="text-[10px] font-bold text-violet-400 uppercase tracking-wider mb-1 flex items-center gap-1">
            <span>🖥️</span> Actual in Build Script
          </div>
          <div className="text-slate-300 break-words text-[11px]">
            {d.script_evidence || <span className="italic text-slate-600">None / Omitted</span>}
          </div>
        </div>
      </div>

      {/* Suggested Fix */}
      {d.recommendation && (
        <div className="mt-3 flex items-start gap-2 rounded-lg border border-emerald-500/20 bg-emerald-950/20 p-2.5 text-xs text-emerald-300">
          <span className="font-bold shrink-0">💡 Fix:</span>
          <span className="text-[11px] leading-relaxed text-emerald-200">{d.recommendation}</span>
        </div>
      )}
    </div>
  );
}

/** Quick export actions */
function ExportButtons({ report }: { report: AnalysisReport }) {
  const [copied, setCopied] = useState(false);

  const copySummary = async () => {
    try {
      const summaryText =
        `Consistency Report for ${report.package_name} (${report.distro})\n` +
        `Score: ${report.consistency_score}/100\n` +
        `Summary: ${report.summary}\n` +
        `Discrepancies (${report.discrepancies.length}):\n` +
        report.discrepancies
          .map((d, i) => `${i + 1}. [${d.severity.toUpperCase()}] ${d.title}\n   Fix: ${d.recommendation}`)
          .join("\n");
      await navigator.clipboard.writeText(summaryText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  const downloadJson = async () => {
    const raw = await reportJson(report.package_name, report.distro);
    const blob = new Blob([raw], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${report.package_name}-${report.distro}-consistency.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={copySummary}
        className="rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs font-medium text-slate-300 hover:border-slate-500 hover:text-white transition"
      >
        {copied ? "✓ Copied Summary" : "📋 Copy Summary"}
      </button>
      <button
        type="button"
        onClick={downloadJson}
        className="rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs font-medium text-slate-300 hover:border-slate-500 hover:text-white transition"
      >
        ⬇ Download JSON
      </button>
    </div>
  );
}
