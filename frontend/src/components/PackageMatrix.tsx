"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AnalysisReport, PackageSummary } from "@/lib/api-types";
import { analyzePackage, latestResult } from "@/lib/api-client";
import { HEALTH_META, scoreColor } from "@/lib/ui";
import type { HealthLevel } from "@/lib/ui";
import HealthBadge from "./HealthBadge";
import { DistroChip, StatusBadge } from "./Chips";
import { SYNC_DONE_EVENT } from "./TopBar";
import Spinner from "./Spinner";

type RowState = { kind: "loading"; full?: boolean } | { kind: "error"; message: string } | null;

const LOAD_LIMIT = 500;
const SEARCH_DEBOUNCE_MS = 350;
const JOB_POLL_INTERVAL_MS = 1_500;
const JOB_POLL_TIMEOUT_MS = 5 * 60_000;

/** Computes the green/amber/red health level for a catalog row. */
export function healthFor(pkg: PackageSummary): HealthLevel {
  if (pkg.status === "error" || pkg.error) return "red";
  if (pkg.status !== "analyzed" && pkg.status !== "stale") return "grey";
  if (pkg.open_discrepancies > 0) return "red";
  const score = pkg.consistency_score ?? 0;
  if (score >= 90) return "green";
  if (score >= 65) return "amber";
  return "red";
}

export default function PackageMatrix({ onAnalyzed }: { onAnalyzed?: (r: AnalysisReport) => void }) {
  const [rows, setRows] = useState<PackageSummary[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [indexCommit, setIndexCommit] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [rowState, setRowState] = useState<Record<string, RowState>>({});

  // Keep search text and the value actually sent to the API apart: typing stays
  // instant, requests are debounced.
  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedSearch(search.trim()), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [search]);

  // An aborted request is always immediately replaced by a new one that manages
  // the `loading` flag itself, so the flag can never get stuck.
  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (debouncedSearch) params.set("search", debouncedSearch);
        if (statusFilter) params.set("status", statusFilter);
        params.set("limit", String(LOAD_LIMIT));
        const qs = params.toString();
        const res = await fetch(`/api/packages${qs ? `?${qs}` : ""}`, {
          cache: "no-store",
          signal,
        });
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const data = (await res.json()) as {
          total: number;
          index_commit?: string | null;
          packages: PackageSummary[];
        };
        if (signal?.aborted) return; // a newer request is in charge now
        setRows(data.packages);
        setTotal(data.total);
        setIndexCommit(data.index_commit ?? null);
      } catch (err) {
        if ((err as Error)?.name !== "AbortError" && !signal?.aborted) {
          setError(err instanceof Error ? err.message : "failed to load packages");
        }
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [debouncedSearch, statusFilter],
  );

  // Initial load + reload whenever filters change. Each new run aborts the
  // previous request, so stale responses can never overwrite fresh ones
  // (including React StrictMode's double-invoke in dev).
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // Refresh the table after a successful re-index (TopBar broadcast).
  useEffect(() => {
    const onSynced = () => void load();
    window.addEventListener(SYNC_DONE_EVENT, onSynced);
    return () => window.removeEventListener(SYNC_DONE_EVENT, onSynced);
  }, [load]);

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
        if (res.job_id) {
          // background job: poll until it finishes, with a hard timeout
          const started = Date.now();
          for (;;) {
            if (Date.now() - started > JOB_POLL_TIMEOUT_MS) {
              throw new Error("analysis is taking too long — check GET /api/jobs later");
            }
            await new Promise((r) => setTimeout(r, JOB_POLL_INTERVAL_MS));
            const jobRes = await fetch(`/api/jobs/${encodeURIComponent(res.job_id)}`, {
              cache: "no-store",
            });
            if (!jobRes.ok) throw new Error(`job poll failed: ${jobRes.status}`);
            const job = (await jobRes.json()) as { state: string; error?: string | null };
            if (job.state === "succeeded") {
              const report = await latestResult(pkg.package_name, pkg.distro);
              onAnalyzed?.(report);
              setRowState((s) => ({ ...s, [key]: null }));
              void load();
              break;
            }
            if (job.state === "failed") throw new Error(job.error ?? "analysis job failed");
          }
          return;
        }
        throw new Error("unexpected analyze response");
      } catch (err) {
        if ((err as Error)?.name !== "AbortError") {
          setRowState((s) => ({
            ...s,
            [key]: {
              kind: "error",
              message: err instanceof Error ? err.message : "analysis failed",
            },
          }));
        }
      }
    },
    [load, onAnalyzed],
  );

  return (
    <section aria-label="Package matrix">
      {/* toolbar */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-white">Package matrix</h2>
        <span className="rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 text-[11px] text-slate-400">
          {total ?? "…"} packages
        </span>
        {indexCommit && (
          <span
            className="font-mono text-[11px] text-slate-500"
            title="scripts repo commit at index time"
          >
            @{indexCommit.slice(0, 9)}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search packages…"
            aria-label="Search packages"
            className="w-48 rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs text-slate-200 placeholder:text-slate-500 focus:border-sky-600 focus:outline-none"
          />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            aria-label="Filter by status"
            className="rounded-md border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-300 focus:border-sky-600 focus:outline-none"
          >
            <option value="">All statuses</option>
            <option value="analyzed">analyzed</option>
            <option value="indexed">indexed</option>
            <option value="stale">stale</option>
            <option value="error">error</option>
            <option value="unindexed">unindexed</option>
          </select>
          <button
            type="button"
            onClick={() => void load()}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-slate-500 hover:text-white"
          >
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <p
          className="mb-3 rounded-md border border-rose-200 bg-rose-100 px-3 py-2 text-xs text-rose-800"
          role="alert"
        >
          Failed to load packages: {error}
        </p>
      )}

      {/* table */}
      <div className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-900">
        <table className="min-w-full text-left text-xs">
          <thead>
            <tr className="border-b border-slate-800 text-[10px] uppercase tracking-wide text-slate-500">
              <th className="px-3 py-2 font-semibold">Package</th>
              <th className="px-3 py-2 font-semibold">Distro</th>
              <th className="px-3 py-2 font-semibold">Health</th>
              <th className="px-3 py-2 font-semibold">Score</th>
              <th className="px-3 py-2 font-semibold">Findings</th>
              <th className="px-3 py-2 font-semibold">Status</th>
              <th className="px-3 py-2 font-semibold">Last analyzed</th>
              <th className="px-3 py-2 text-right font-semibold">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {loading && rows.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-10 text-center text-slate-400">
                  <Spinner className="mx-auto h-5 w-5" />
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-10 text-center text-slate-400">
                  No packages match. Run <span className="font-mono text-slate-300">Re-index repos</span> first.
                </td>
              </tr>
            )}
            {rows.map((pkg) => {
              const key = `${pkg.package_name}::${pkg.distro}`;
              const state = rowState[key] ?? null;
              const level = healthFor(pkg);
              return (
                <tr key={key} className="transition hover:bg-slate-800/60">
                  <td className="px-3 py-2">
                    <div className="font-medium text-slate-200">{pkg.package_name}</div>
                    {pkg.versions.length > 0 && (
                      <div className="font-mono text-[10px] text-slate-500">
                        {pkg.versions.slice(0, 3).join(", ")}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <DistroChip distro={pkg.distro} />
                  </td>
                  <td className="px-3 py-2">
                    <HealthBadge level={level} score={null} />
                  </td>
                  <td className={`px-3 py-2 font-mono ${scoreColor(pkg.consistency_score)}`}>
                    {pkg.consistency_score ?? "—"}
                  </td>
                  <td className="px-3 py-2">
                    {pkg.open_discrepancies > 0 ? (
                      <span className="font-mono text-rose-500">{pkg.open_discrepancies}</span>
                    ) : (
                      <span className="text-slate-500">0</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={pkg.status} />
                    {pkg.error && (
                      <div
                        className="mt-0.5 max-w-[180px] truncate text-[10px] text-rose-500"
                        title={pkg.error}
                      >
                        {pkg.error}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-slate-400">{timeAgo(pkg.last_analyzed_at)}</td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-1.5">
                      <button
                        type="button"
                        onClick={() => void runAnalysis(pkg, false)}
                        disabled={state?.kind === "loading"}
                        className="rounded bg-sky-600 px-2.5 py-1 text-[11px] font-semibold text-white transition hover:bg-sky-500 disabled:opacity-60"
                      >
                        {state?.kind === "loading" && !state.full ? "Analyzing…" : "Analyze"}
                      </button>
                      <button
                        type="button"
                        onClick={() => void runAnalysis(pkg, true)}
                        disabled={state?.kind === "loading"}
                        title="Re-run even if a stored report matches the indexed commit"
                        className="rounded border border-slate-600 px-2.5 py-1 text-[11px] text-slate-300 transition hover:border-slate-500 hover:text-white disabled:opacity-60"
                      >
                        {state?.kind === "loading" && state.full ? "Re-analyzing…" : "Force"}
                      </button>
                    </div>
                    {state?.kind === "error" && (
                      <p className="mt-1 max-w-[220px] text-right text-[10px] text-rose-500" role="alert">
                        {state.message}
                      </p>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function timeAgo(iso?: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
