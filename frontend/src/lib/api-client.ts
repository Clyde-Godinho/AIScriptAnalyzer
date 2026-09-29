"use client";

import type {
  AnalyzeResponse,
  AnalysisReport,
  Distro,
  HealthResponse,
  JobStatus,
  ListPackagesParams,
  PackageListResponse,
  SyncResponse,
} from "./api-types";

/**
 * The FastAPI backend is reached through a same-origin Next.js rewrite
 * (`next.config.mjs`), so `/api/...` works in the browser without CORS.
 * Set NEXT_PUBLIC_API_BASE_URL to bypass the proxy and hit the backend
 * directly (e.g. when the dashboard is served from a different origin).
 */
const BASE = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...(init?.headers as Record<string, string>) };
  if (init?.body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });

  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** GET /api/packages - searchable, optionally status-filtered catalog. */
export function listPackages(params: ListPackagesParams = {}, signal?: AbortSignal): Promise<PackageListResponse> {
  const qs = new URLSearchParams();
  if (params.status) qs.set("status", params.status);
  if (params.search) qs.set("search", params.search);
  if (params.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<PackageListResponse>(`/api/packages${suffix}`, { signal });
}

/** POST /api/sync?wait=true - re-index both repositories and await the counters. */
export function triggerSync(fullReindex = false, signal?: AbortSignal): Promise<SyncResponse> {
  return request<SyncResponse>(`/api/sync?wait=true`, {
    method: "POST",
    body: JSON.stringify({ full_reindex: fullReindex, prune: true }),
    signal,
  });
}

/** GET /api/jobs/{id} - poll a background sync/analyze job. */
export function getJob(jobId: string, signal?: AbortSignal): Promise<JobStatus> {
  return request<JobStatus>(`/api/jobs/${encodeURIComponent(jobId)}`, { signal });
}

/** POST /api/analyze - LLM comparison for one (package, distro) pair. */
export function analyzePackage(
  packageName: string,
  distro: Distro,
  opts: { force?: boolean; signal?: AbortSignal } = {},
): Promise<AnalyzeResponse> {
  return request<AnalyzeResponse>(`/api/analyze`, {
    method: "POST",
    body: JSON.stringify({ package_name: packageName, distro, force: opts.force ?? false }),
    signal: opts.signal,
  });
}

/** GET /api/results/{package}/latest - most recent stored report. */
export function latestResult(packageName: string, distro: Distro, signal?: AbortSignal): Promise<AnalysisReport> {
  return request<AnalysisReport>(
    `/api/results/${encodeURIComponent(packageName)}/latest?distro=${encodeURIComponent(distro)}`,
    { signal },
  );
}

/** GET /health - liveness + index statistics for the header pill. */
export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>(`/health`, { signal });
}
