"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getHealth, triggerSync, seedDemo, ApiError } from "@/lib/api-client";
import type { HealthResponse } from "@/lib/api-types";
import Spinner from "./Spinner";

type SyncState = "idle" | "syncing" | "seeding" | "done" | "error";

/** Window event the TopBar broadcasts after a successful re-index or seed. */
export const SYNC_DONE_EVENT = "s390x:sync-done";
/** Window event to toggle the explainer/how-it-works card. */
export const TOGGLE_GUIDE_EVENT = "s390x:toggle-guide";

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

  async function handleLoadDemo() {
    setSyncState("seeding");
    setMessage("Loading sample demo packages (NGINX & Example)…");
    try {
      const res = await seedDemo();
      setSyncState("done");
      setMessage("✓ Demo packages loaded & analyzed!");
      window.dispatchEvent(new CustomEvent(SYNC_DONE_EVENT, { detail: res }));
      void refreshHealth();
      window.setTimeout(() => setSyncState("idle"), 4_000);
    } catch (err) {
      setSyncState("error");
      setMessage(err instanceof ApiError ? `Demo load failed: ${err.message}` : "Backend unreachable");
      window.setTimeout(() => setSyncState("idle"), 5_000);
    }
  }

  async function handleSync(fullReindex = false) {
    setSyncState("syncing");
    setMessage(fullReindex ? "Re-indexing repositories…" : "Syncing latest commits…");
    try {
      const res = await triggerSync(fullReindex);
      const chunks =
        res.stats && typeof res.stats === "object" && "chunks_indexed" in res.stats
          ? ` (${String(res.stats.chunks_indexed)} chunks)`
          : "";
      setSyncState("done");
      setMessage(`✓ Synced successfully${chunks}`);
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
  const isBusy = syncState === "syncing" || syncState === "seeding";

  return (
    <header className="sticky top-0 z-40 border-b border-slate-800/80 bg-slate-950/90 backdrop-blur-md">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-4 py-3">
        {/* Brand identity */}
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-tr from-sky-500 to-indigo-600 font-mono text-sm font-black text-white shadow-md shadow-sky-500/20">
            Z
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-sm font-bold tracking-tight text-white">
                DocScript Analyzer
              </h1>
              <span className="rounded bg-sky-950/80 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-sky-400 border border-sky-800/50">
                Linux on IBM Z
              </span>
            </div>
            <p className="text-[11px] text-slate-400">
              Verify build docs against automation scripts
            </p>
          </div>
        </div>

        {/* Center status pill */}
        <div className="hidden items-center gap-2 md:flex">
          <div
            className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition ${
              online
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                : "border-rose-500/30 bg-rose-500/10 text-rose-400"
            }`}
          >
            <span
              className={`h-2 w-2 rounded-full ${
                online ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]" : "bg-rose-500"
              }`}
            />
            {online ? "Engine Active" : "Backend Offline"}
          </div>
          {online && health?.packages !== undefined && health.packages > 0 && (
            <span className="text-xs text-slate-400">
              · <strong className="font-semibold text-slate-200">{health.packages}</strong> packages tracked
            </span>
          )}
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2">
          {message && (
            <span
              className={`max-w-xs truncate text-xs font-medium ${
                syncState === "error"
                  ? "text-rose-400"
                  : syncState === "done"
                    ? "text-emerald-400"
                    : "text-sky-300"
              }`}
              role="status"
            >
              {message}
            </span>
          )}

          {/* Quick Demo Button */}
          <button
            type="button"
            onClick={handleLoadDemo}
            disabled={isBusy}
            title="Load ready-to-test sample packages (NGINX & Example with real IBM Z build steps)"
            className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-indigo-500 to-sky-500 px-3 py-1.5 text-xs font-semibold text-white shadow-md shadow-indigo-500/20 transition hover:from-indigo-400 hover:to-sky-400 active:scale-95 disabled:opacity-60"
          >
            {syncState === "seeding" ? <Spinner className="h-3.5 w-3.5" /> : <span>✨</span>}
            Load Demo Data
          </button>

          {/* Sync Repositories Button */}
          <button
            type="button"
            onClick={() => void handleSync(false)}
            disabled={isBusy}
            title="Pull and index the official linux-on-ibm-z/docs and scripts repositories"
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-850 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-slate-500 hover:bg-slate-800 hover:text-white active:scale-95 disabled:opacity-60"
          >
            {syncState === "syncing" ? <Spinner className="h-3.5 w-3.5" /> : <span>⟳</span>}
            Sync Repos
          </button>

          {/* How it works toggle */}
          <button
            type="button"
            onClick={() => window.dispatchEvent(new CustomEvent(TOGGLE_GUIDE_EVENT))}
            title="Toggle 'How It Works' guide"
            className="inline-flex items-center justify-center rounded-lg border border-slate-800 bg-slate-900 p-1.5 text-xs text-slate-400 transition hover:border-slate-700 hover:text-slate-200"
          >
            💡
          </button>
        </div>
      </div>
    </header>
  );
}
