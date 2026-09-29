"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getHealth, triggerSync, ApiError } from "@/lib/api-client";
import type { HealthResponse } from "@/lib/api-types";
import Spinner from "./Spinner";

type SyncState = "idle" | "syncing" | "done" | "error";

/** Window event the TopBar broadcasts after a successful re-index. */
export const SYNC_DONE_EVENT = "s390x:sync-done";

export default function TopBar() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [syncState, setSyncState] = useState<SyncState>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const mounted = useRef(true);

  const refreshHealth = useCallback(async (signal?: AbortSignal) => {
    try {
      const h = await getHealth(signal);
      if (mounted.current) setHealth(h);
    } catch {
      if (mounted.current) setHealth(null);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void refreshHealth(controller.signal);
    const id = window.setInterval(() => void refreshHealth(controller.signal), 30_000);
    return () => {
      mounted.current = false;
      controller.abort();
      window.clearInterval(id);
    };
  }, [refreshHealth]);

  async function handleSync(fullReindex: boolean) {
    setSyncState("syncing");
    setMessage(fullReindex ? "Full re-index running — pulling repos and re-embedding…" : "Syncing repositories…");
    try {
      const res = await triggerSync(fullReindex);
      const chunks =
        res.stats && typeof res.stats === "object" && "chunks_indexed" in res.stats
          ? ` — ${String(res.stats.chunks_indexed)} chunks indexed`
          : "";
      setSyncState("done");
      setMessage(`Sync OK${chunks}`);
      window.dispatchEvent(new CustomEvent(SYNC_DONE_EVENT, { detail: res }));
      void refreshHealth();
      window.setTimeout(() => setSyncState("idle"), 4_000);
    } catch (err) {
      setSyncState("error");
      setMessage(err instanceof ApiError ? `Sync failed: ${err.message}` : "Sync failed: backend unreachable");
      window.setTimeout(() => setSyncState("idle"), 6_000);
    }
  }

  const online = health !== null;
  const pill = online
    ? health.status === "ok"
      ? "bg-emerald-100 text-emerald-800 border-emerald-200"
      : "bg-amber-100 text-amber-800 border-amber-200"
    : "bg-rose-100 text-rose-800 border-rose-200";

  return (
    <header className="sticky top-0 z-40 border-b border-slate-800 bg-slate-900/95 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3">
        <div className="flex items-center gap-2">
          <span aria-hidden="true" className="text-lg">⚙️</span>
          <div>
            <h1 className="text-sm font-semibold leading-tight text-white">
              Documentation ↔ Script Consistency
            </h1>
            <p className="text-[11px] leading-tight text-slate-400">
              Linux on IBM Z (s390x) · wiki docs vs build scripts
            </p>
          </div>
        </div>

        <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${pill}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${online ? (health!.status === "ok" ? "bg-emerald-500" : "bg-amber-400") : "bg-rose-500"}`} />
          {online ? `backend ${health!.version}` : "backend offline"}
        </span>

        {online && (
          <span className="hidden text-xs text-slate-400 sm:inline">
            {health!.indexed_chunks.toLocaleString()} chunks · {health!.packages} packages · {health!.llm_model}
          </span>
        )}

        <div className="ml-auto flex items-center gap-2">
          {message && (
            <span
              className={`hidden max-w-xs truncate text-xs md:inline ${
                syncState === "error" ? "text-rose-500" : syncState === "done" ? "text-emerald-500" : "text-slate-400"
              }`}
              role="status"
            >
              {message}
            </span>
          )}
          <button
            type="button"
            onClick={() => void handleSync(false)}
            disabled={syncState === "syncing"}
            className="inline-flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-sky-500 disabled:opacity-60"
          >
            {syncState === "syncing" ? <Spinner className="h-3.5 w-3.5" /> : <span aria-hidden="true">⟳</span>}
            Re-index repos
          </button>
          <button
            type="button"
            onClick={() => void handleSync(true)}
            disabled={syncState === "syncing"}
            title="Drop the vector collection and re-embed everything"
            className="inline-flex items-center rounded-md border border-slate-600 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-slate-500 hover:text-white disabled:opacity-60"
          >
            Full re-index
          </button>
        </div>
      </div>
    </header>
  );
}
