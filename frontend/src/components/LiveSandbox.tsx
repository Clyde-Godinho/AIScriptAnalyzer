"use client";

import { useState } from "react";
import type { AnalysisReport } from "@/lib/api-types";
import { analyzeSandbox } from "@/lib/api-client";
import Spinner from "./Spinner";

const PRESETS = [
  {
    name: "1. Clean Match (100% In Sync)",
    description: "Documentation and script commands are completely aligned.",
    wiki: `# Building FastApp

Instructions for building FastApp on Linux on IBM Z (s390x).

## Ubuntu

### Prerequisites
\`\`\`bash
sudo apt-get update
sudo apt-get install -y gcc make libssl-dev zlib1g-dev
export APP_HOME=/opt/fastapp
\`\`\`

### Build and install
\`\`\`bash
./configure --prefix=/opt/fastapp --with-openssl
make -j$(nproc)
sudo make install
\`\`\`
`,
    script: `#!/bin/bash
set -e
sudo apt-get update
sudo apt-get install -y gcc make libssl-dev zlib1g-dev
export APP_HOME=/opt/fastapp
./configure --prefix=/opt/fastapp --with-openssl
make -j$(nproc)
sudo make install
`,
  },
  {
    name: "2. Port & Env Var Mismatch",
    description: "Wiki specifies port 8080 and dev environment; script uses port 9000 and prod.",
    wiki: `# Building WebService

Documentation for WebService on Linux on IBM Z.

## Ubuntu

### Configuration
\`\`\`bash
export SERVICE_PORT=8080
export ENV_MODE=development
export DB_HOST=localhost
\`\`\`

### Run
\`\`\`bash
./run_service.sh --port 8080 --mode dev
\`\`\`
`,
    script: `#!/bin/bash
export SERVICE_PORT=9000
export ENV_MODE=production
export DB_HOST=db.internal.s390x.net
./run_service.sh --port 9000 --mode prod
`,
  },
  {
    name: "3. Missing Dependencies & Flags (Drift)",
    description: "Wiki documents curl, git & --enable-shared, but automation never installs or uses them.",
    wiki: `# Building Example Service

Build guide on Linux on IBM Z (s390x).

## Ubuntu

### Prerequisites
\`\`\`bash
sudo apt-get install -y gcc make git curl libpcre3-dev
\`\`\`

### Build
\`\`\`bash
./configure --prefix=/usr/local/app --enable-shared --with-openssl
make -j$(nproc)
sudo make install
\`\`\`
`,
    script: `#!/bin/bash
set -e
sudo apt-get install -y gcc make
./configure --prefix=/opt/app --with-openssl
make -j$(nproc)
sudo make install
`,
  },
];

export default function LiveSandbox({ onAnalyzed }: { onAnalyzed: (r: AnalysisReport) => void }) {
  const [packageName, setPackageName] = useState("custom-app");
  const [distro, setDistro] = useState("ubuntu");
  const [wikiText, setWikiText] = useState(PRESETS[1].wiki);
  const [scriptText, setScriptText] = useState(PRESETS[1].script);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applyPreset = (idx: number) => {
    const p = PRESETS[idx];
    setWikiText(p.wiki);
    setScriptText(p.script);
    setError(null);
  };

  const handleRunAudit = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await analyzeSandbox({
        package_name: packageName.trim() || "custom-app",
        distro,
        wiki_content: wikiText,
        script_content: scriptText,
      });
      if (res.report) {
        onAnalyzed(res.report);
      } else {
        throw new Error("No report returned");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sandbox audit failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="rounded-2xl border border-indigo-900/60 bg-gradient-to-b from-slate-900 via-slate-900 to-indigo-950/20 p-5 shadow-xl space-y-4">
      {/* Header */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between border-b border-slate-800 pb-4">
        <div>
          <h3 className="text-base font-bold text-white flex items-center gap-2">
            <span>🧪 Interactive Live Sandbox</span>
            <span className="rounded bg-indigo-500/20 border border-indigo-500/30 px-2 py-0.5 font-mono text-[10px] text-indigo-300">
              Live Test Bench
            </span>
          </h3>
          <p className="text-xs text-slate-400">
            Paste or edit custom build instructions and shell scripts to test consistency checking in real-time.
          </p>
        </div>

        {/* Action Button */}
        <button
          type="button"
          onClick={handleRunAudit}
          disabled={loading}
          className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-sky-500 to-indigo-600 px-4 py-2 text-xs font-bold text-white shadow-lg shadow-sky-500/20 transition hover:from-sky-400 hover:to-indigo-500 active:scale-95 disabled:opacity-60"
        >
          {loading ? <Spinner className="h-4 w-4" /> : <span>▶</span>}
          Run Consistency Check
        </button>
      </div>

      {/* Preset Buttons */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
          Load Preset:
        </span>
        {PRESETS.map((p, i) => (
          <button
            key={i}
            type="button"
            onClick={() => applyPreset(i)}
            className="rounded-lg border border-slate-700 bg-slate-800/80 px-2.5 py-1 text-xs font-medium text-slate-300 transition hover:border-slate-500 hover:bg-slate-700 hover:text-white"
          >
            {p.name}
          </button>
        ))}
      </div>

      {/* Inputs for package & distro */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-1.5 text-xs text-slate-300">
          <label htmlFor="sb-pkg" className="text-slate-400">Package:</label>
          <input
            id="sb-pkg"
            value={packageName}
            onChange={(e) => setPackageName(e.target.value)}
            className="rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1 text-xs text-white focus:border-sky-500 focus:outline-none"
            placeholder="package-name"
          />
        </div>
        <div className="flex items-center gap-1.5 text-xs text-slate-300">
          <label htmlFor="sb-distro" className="text-slate-400">Distro:</label>
          <select
            id="sb-distro"
            value={distro}
            onChange={(e) => setDistro(e.target.value)}
            className="rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-white focus:border-sky-500 focus:outline-none"
          >
            <option value="ubuntu">Ubuntu</option>
            <option value="rhel">RHEL</option>
            <option value="sles">SLES</option>
          </select>
        </div>
      </div>

      {/* Error alert */}
      {error && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-2.5 text-xs text-rose-300">
          {error}
        </div>
      )}

      {/* Side-by-side Editors */}
      <div className="grid gap-4 md:grid-cols-2">
        {/* Left: Wiki Documentation */}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs font-semibold text-sky-400">
            <span className="flex items-center gap-1.5">
              <span>📘</span> Wiki Documentation (Markdown)
            </span>
            <span className="text-[10px] text-slate-500">Human Guide</span>
          </div>
          <textarea
            value={wikiText}
            onChange={(e) => setWikiText(e.target.value)}
            rows={12}
            className="w-full rounded-xl border border-slate-800 bg-slate-950 p-3 font-mono text-xs text-slate-200 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 leading-relaxed"
            placeholder="Paste wiki markdown here..."
          />
        </div>

        {/* Right: Build Automation Script */}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs font-semibold text-violet-400">
            <span className="flex items-center gap-1.5">
              <span>🖥️</span> Build Automation Script (Bash)
            </span>
            <span className="text-[10px] text-slate-500">Machine Script</span>
          </div>
          <textarea
            value={scriptText}
            onChange={(e) => setScriptText(e.target.value)}
            rows={12}
            className="w-full rounded-xl border border-slate-800 bg-slate-950 p-3 font-mono text-xs text-slate-200 focus:border-violet-500 focus:outline-none focus:ring-1 focus:ring-violet-500 leading-relaxed"
            placeholder="Paste bash script here..."
          />
        </div>
      </div>
    </section>
  );
}
