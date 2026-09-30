"use client";

import { useCallback, useEffect, useState } from "react";
import type { AnalysisReport, PackageSummary } from "@/lib/api-types";
import { analyzePackage, latestResult, seedDemo } from "@/lib/api-client";
import { DistroChip } from "./Chips";
import { SYNC_DONE_EVENT } from "./TopBar";
import Spinner from "./Spinner";

type RowState = { kind: "loading"; full?: boolean } | { kind: "error"; message: string } | null;

const LOAD_LIMIT = 500;
const SEARCH_DEBOUNCE_MS = 250;
const CURATED_PACKAGES = ["nginx", "redis", "example"];

export default function PackageMatrix({
  onAnalyzed,
  selectedReportKey,
}: {
  onAnalyzed?: (r: AnalysisReport) => void;
  selectedReportKey?: string | null;
}) {
  const [rows, setRows] = useState<PackageSummary[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [viewMode, setViewMode] = useState<"curated" | "all">("curated");
  const [tabFilter, setTabFilter] = useState<"all" | "issues" | "sync" | "pending">("all");
  const [rowState, setRowState] = useState<Record<string, RowState>>({});
  const [isSeeding, setIsSeeding] = useState(false);

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedSearch(search.trim()), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [search]);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (debouncedSearch) params.set("search", debouncedSearch);
        params.set("limit", String(LOAD_LIMIT));
        const qs = params.toString();
        const res = await fetch(`/api/packages${qs ? `?${qs}` : ""}`, {
          cache: "no-store",
          signal,
        });
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const data = (await res.json()) as {
          total: number;
          packages: PackageSummary[];
        };
        if (signal?.aborted) return;
        setRows(data.packages);
        setTotal(data.total);

        // Auto-select nginx or example if no report selected yet
        if (!selectedReportKey && data.packages.length > 0) {
          const defaultPkg =
            data.packages.find((p) => p.package_name === "nginx" && p.status === "analyzed") ||
            data.packages.find((p) => p.package_name === "example" && p.status === "analyzed") ||
            data.packages.find((p) => p.status === "analyzed");
          if (defaultPkg) {
            void latestResult(defaultPkg.package_name, defaultPkg.distro).then(onAnalyzed).catch(() => {});
          }
        }
      } catch (err) {
        if ((err as Error)?.name !== "AbortError" && !signal?.aborted) {
          setError(err instanceof Error ? err.message : "failed to load packages");
        }
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [debouncedSearch, selectedReportKey, onAnalyzed],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    const onSynced = () => void load();
    window.addEventListener(SYNC_DONE_EVENT, onSynced);
    return () => window.removeEventListener(SYNC_DONE_EVENT, onSynced);
  }, [load]);

  const handleQuickSeed = async () => {
    setIsSeeding(true);
    try {
      await seedDemo();
      window.dispatchEvent(new CustomEvent(SYNC_DONE_EVENT));
      await load();
    } catch {
      setError("Failed to seed demo packages");
    } finally {
      setIsSeeding(false);
    }
  };

  const openReport = async (pkg: PackageSummary) => {
    const key = `${pkg.package_name}::${pkg.distro}`;
    setRowState((s) => ({ ...s, [key]: { kind: "loading", full: false } }));
    try {
      const report = await latestResult(pkg.package_name, pkg.distro);
      onAnalyzed?.(report);
      setRowState((s) => ({ ...s, [key]: null }));
    } catch {
      void runAnalysis(pkg, false);
    }
  };

  const runAnalysis = useCallback(
    async (pkg: PackageSummary, force: boolean) => {
      const key = `${pkg.package_name}::${pkg.distro}`;
      setRowState((s) => ({ ...s, [key]: { kind: "loading", full: force } }));
      const controller = new AbortController();
      try {
        const res = await analyzePackage(pkg.package_name, pkg.distro, {
          force,
          signal: controller.signal,
        });
        if (res.report) {
          onAnalyzed?.(res.report);
          setRowState((s) => ({ ...s, [key]: null }));
          void load();
          return;
        }
      } catch (err) {
        if ((err as Error)?.name !== "AbortError") {
          setRowState((s) => ({
            ...s,
            [key]: {
              kind: "error",
              message: err instanceof Error ? err.message : "Analysis failed",
            },
          }));
        }
      }
    },
    [load, onAnalyzed],
  );

  // Curated showcase packages (NGINX, Redis, Example)
  const curatedRows = rows.filter((p) => CURATED_PACKAGES.includes(p.package_name.toLowerCase()));

  // Active list based on viewMode
  const activeDataset = viewMode === "curated" && curatedRows.length > 0 ? curatedRows : rows;

  const filteredRows = activeDataset.filter((pkg) => {
    if (tabFilter === "issues") {
      return (pkg.open_discrepancies ?? 0) > 0 || (pkg.consistency_score !== null && pkg.consistency_score < 90);
    }
    if (tabFilter === "sync") {
      return pkg.status === "analyzed" && (pkg.consistency_score ?? 0) >= 90;
    }
    if (tabFilter === "pending") {
      return pkg.status !== "analyzed";
    }
    return true;
  });

  return (
    <section aria-label="Package catalog" className="space-y-4">
      {/* Top Controls & View Switcher */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-2">
          {/* View Mode Toggle: Mentorship Showcase vs All Packages */}
          <div className="inline-flex rounded-xl border border-slate-800 bg-slate-900 p-1">
            <button
              type="button"
              onClick={() => setViewMode("curated")}
              className={`rounded-lg px-3 py-1.5 text-xs font-bold transition ${
                viewMode === "curated"
                  ? "bg-gradient-to-r from-sky-500 to-indigo-600 text-white shadow-md shadow-sky-500/20"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              ⭐ Curated Showcase ({curatedRows.length || 3})
            </button>
            <button
              type="button"
              onClick={() => setViewMode("all")}
              className={`rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                viewMode === "all"
                  ? "bg-slate-800 text-white"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              🌐 Full Catalog ({total ?? rows.length})
            </button>
          </div>

          <span className="text-xs text-slate-500 hidden md:inline">
            {viewMode === "curated"
              ? "Focused on key benchmarks for project evaluation"
              : "Raw upstream linux-on-ibm-z repository"}
          </span>
        </div>

        {/* Search & Refresh */}
        <div className="flex items-center gap-2">
          <div className="relative">
            <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500 text-xs">🔍</span>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search package..."
              aria-label="Search packages"
              className="w-48 rounded-lg border border-slate-700 bg-slate-900 py-1.5 pl-8 pr-3 text-xs text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none"
            />
          </div>
          <button
            type="button"
            onClick={() => void load()}
            title="Refresh"
            className="rounded-lg border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs text-slate-300 hover:border-slate-500 hover:text-white transition"
          >
            ↻
          </button>
        </div>
      </div>

      {/* Curated Showcase View (Large, intuitive cards) */}
      {viewMode === "curated" && (
        <div className="grid gap-4 md:grid-cols-3">
          {/* Card 1: NGINX */}
          <CuratedCard
            title="NGINX 1.24.0"
            distro="Ubuntu"
            role="Passing Benchmark"
            badge="✅ 100% In Sync"
            badgeColor="emerald"
            score={100}
            issues={0}
            description="Wiki documentation exactly reflects automated build flags, openssl module, and prerequisites on s390x."
            isSelected={selectedReportKey === "nginx::ubuntu"}
            onClick={() => {
              const pkg = rows.find((p) => p.package_name === "nginx" && p.distro === "ubuntu");
              if (pkg) void openReport(pkg);
            }}
          />

          {/* Card 2: Redis */}
          <CuratedCard
            title="Redis 7.2.4"
            distro="Ubuntu"
            role="Minor Drift Scenario"
            badge="🟡 90% Minor Drift"
            badgeColor="amber"
            score={90}
            issues={1}
            description="Documentation lists test package tcl-dev which script omits; script adds s390x allocator flag MALLOC=libc."
            isSelected={selectedReportKey === "redis::ubuntu"}
            onClick={() => {
              const pkg = rows.find((p) => p.package_name === "redis" && p.distro === "ubuntu");
              if (pkg) void openReport(pkg);
            }}
          />

          {/* Card 3: Example Package */}
          <CuratedCard
            title="Example Package"
            distro="Ubuntu"
            role="Breaking Discrepancy"
            badge="🔴 23% Critical Drift"
            badgeColor="rose"
            score={23}
            issues={11}
            description="Severe divergence: missing curl/git, wrong install paths (/usr/local vs /opt), and unapplied s390x patch."
            isSelected={selectedReportKey === "example::ubuntu"}
            onClick={() => {
              const pkg = rows.find((p) => p.package_name === "example" && p.distro === "ubuntu");
              if (pkg) void openReport(pkg);
            }}
          />
        </div>
      )}

      {/* Full Catalog View (or if user switches to 'all') */}
      {viewMode === "all" && (
        <div className="space-y-3">
          {/* Filter Tabs */}
          <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 pb-2">
            <button
              type="button"
              onClick={() => setTabFilter("all")}
              className={`rounded-lg px-3 py-1 text-xs font-medium transition ${
                tabFilter === "all"
                  ? "bg-sky-500/20 text-sky-300 border border-sky-500/40"
                  : "text-slate-400 hover:text-slate-200 border border-transparent"
              }`}
            >
              All Packages ({rows.length})
            </button>
            <button
              type="button"
              onClick={() => setTabFilter("issues")}
              className={`rounded-lg px-3 py-1 text-xs font-medium transition ${
                tabFilter === "issues"
                  ? "bg-rose-500/20 text-rose-300 border border-rose-500/40"
                  : "text-slate-400 hover:text-slate-200 border border-transparent"
              }`}
            >
              ⚠️ Discrepancies
            </button>
            <button
              type="button"
              onClick={() => setTabFilter("sync")}
              className={`rounded-lg px-3 py-1 text-xs font-medium transition ${
                tabFilter === "sync"
                  ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40"
                  : "text-slate-400 hover:text-slate-200 border border-transparent"
              }`}
            >
              ✅ In Sync
            </button>
            <button
              type="button"
              onClick={() => setTabFilter("pending")}
              className={`rounded-lg px-3 py-1 text-xs font-medium transition ${
                tabFilter === "pending"
                  ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                  : "text-slate-400 hover:text-slate-200 border border-transparent"
              }`}
            >
              ⏳ Ready to Audit
            </button>
          </div>

          {/* Table */}
          <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/60 shadow-md">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-slate-800 bg-slate-900/90 text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
                    <th className="py-3 px-4">Package</th>
                    <th className="py-3 px-4">Distro</th>
                    <th className="py-3 px-4">Consistency Status</th>
                    <th className="py-3 px-4 text-center">Score</th>
                    <th className="py-3 px-4 text-center">Mismatches</th>
                    <th className="py-3 px-4 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60">
                  {filteredRows.slice(0, 30).map((pkg) => {
                    const key = `${pkg.package_name}::${pkg.distro}`;
                    const isSelected = selectedReportKey === key;
                    const state = rowState[key] ?? null;
                    const isAnalyzed = pkg.status === "analyzed";
                    const score = pkg.consistency_score;
                    const discrepancies = pkg.open_discrepancies ?? 0;

                    return (
                      <tr
                        key={key}
                        onClick={() => (isAnalyzed ? void openReport(pkg) : void runAnalysis(pkg, false))}
                        className={`cursor-pointer transition hover:bg-slate-850/80 ${
                          isSelected ? "bg-sky-950/30 border-l-4 border-l-sky-500" : ""
                        }`}
                      >
                        <td className="py-3 px-4">
                          <div className="font-semibold text-slate-100">{pkg.package_name}</div>
                          <div className="text-[11px] text-slate-500 truncate max-w-xs">
                            {pkg.doc_path || "Building doc"}
                          </div>
                        </td>
                        <td className="py-3 px-4">
                          <DistroChip distro={pkg.distro} />
                        </td>
                        <td className="py-3 px-4">
                          {isAnalyzed ? (
                            score !== null && score >= 90 ? (
                              <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-0.5 text-[11px] font-semibold text-emerald-400">
                                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                                Fully In Sync
                              </span>
                            ) : (
                              <span className="inline-flex items-center gap-1.5 rounded-full border border-rose-500/30 bg-rose-500/10 px-2.5 py-0.5 text-[11px] font-semibold text-rose-400">
                                <span className="h-1.5 w-1.5 rounded-full bg-rose-400" />
                                Discrepancies Found
                              </span>
                            )
                          ) : (
                            <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-700 bg-slate-800 px-2.5 py-0.5 text-[11px] text-slate-400">
                              <span className="h-1.5 w-1.5 rounded-full bg-slate-500" />
                              Ready to Audit
                            </span>
                          )}
                        </td>
                        <td className="py-3 px-4 text-center font-mono font-bold">
                          {score !== null ? (
                            <span
                              className={
                                score >= 90
                                  ? "text-emerald-400 text-sm"
                                  : score >= 65
                                    ? "text-amber-400 text-sm"
                                    : "text-rose-400 text-sm"
                              }
                            >
                              {score}/100
                            </span>
                          ) : (
                            <span className="text-slate-600">—</span>
                          )}
                        </td>
                        <td className="py-3 px-4 text-center">
                          {isAnalyzed ? (
                            discrepancies > 0 ? (
                              <span className="rounded-full bg-rose-950/60 border border-rose-800/60 px-2 py-0.5 font-mono text-[11px] font-bold text-rose-400">
                                {discrepancies}
                              </span>
                            ) : (
                              <span className="text-slate-500">0</span>
                            )
                          ) : (
                            <span className="text-slate-600">—</span>
                          )}
                        </td>
                        <td className="py-3 px-4 text-right" onClick={(e) => e.stopPropagation()}>
                          <button
                            type="button"
                            onClick={() => (isAnalyzed ? void openReport(pkg) : void runAnalysis(pkg, false))}
                            disabled={state?.kind === "loading"}
                            className="rounded-lg bg-sky-600 px-3 py-1 text-[11px] font-semibold text-white hover:bg-sky-500 transition"
                          >
                            {isAnalyzed ? "View Report" : "▶ Run Audit"}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {filteredRows.length > 30 && (
              <div className="p-2.5 text-center text-xs text-slate-500 border-t border-slate-800">
                Showing top 30 of {filteredRows.length} packages. Use the search box above to find any package.
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

/** Card for Curated Mentorship Benchmarks */
function CuratedCard({
  title,
  distro,
  role,
  badge,
  badgeColor,
  score,
  issues,
  description,
  isSelected,
  onClick,
}: {
  title: string;
  distro: string;
  role: string;
  badge: string;
  badgeColor: "emerald" | "amber" | "rose";
  score: number;
  issues: number;
  description: string;
  isSelected: boolean;
  onClick: () => void;
}) {
  const borderClass = isSelected
    ? "border-sky-500 ring-2 ring-sky-500/50 bg-slate-850"
    : "border-slate-800 bg-slate-900/80 hover:border-slate-700 hover:bg-slate-850/80";

  return (
    <div
      onClick={onClick}
      className={`cursor-pointer rounded-2xl border p-4 shadow-lg transition flex flex-col justify-between ${borderClass}`}
    >
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="rounded bg-slate-800 px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-400 uppercase">
            {distro} · s390x
          </span>
          <span
            className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${
              badgeColor === "emerald"
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-400"
                : badgeColor === "amber"
                  ? "border-amber-500/40 bg-amber-500/10 text-amber-400"
                  : "border-rose-500/40 bg-rose-500/10 text-rose-400"
            }`}
          >
            {badge}
          </span>
        </div>

        <div>
          <h3 className="text-base font-bold text-white">{title}</h3>
          <div className="text-xs font-medium text-sky-400">{role}</div>
        </div>

        <p className="text-xs text-slate-400 leading-relaxed">{description}</p>
      </div>

      <div className="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between">
        <div className="text-xs font-mono">
          <span className="font-bold text-white text-sm">{score}/100</span>
          <span className="text-slate-500 ml-1.5">({issues} {issues === 1 ? "mismatch" : "mismatches"})</span>
        </div>
        <button
          type="button"
          className="rounded-lg bg-sky-500/20 border border-sky-500/40 px-3 py-1 text-xs font-bold text-sky-300 hover:bg-sky-500 hover:text-white transition"
        >
          {isSelected ? "Inspecting ✓" : "View Report →"}
        </button>
      </div>
    </div>
  );
}
