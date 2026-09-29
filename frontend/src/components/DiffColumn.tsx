"use client";

import type { Discrepancy } from "@/lib/api-types";

/**
 * One side of the unified diff viewer: an ordered list of lines from either the
 * wiki page (steps) or the build script (commands), with semantically divergent
 * lines highlighted and clickable when a discrepancy points at them.
 */
export interface DiffLine {
  n: number; // 1-based line number in the source
  kind: "normal" | "divergent";
  text: string;
  /** discrepancies referencing this line (by index in the report) */
  refs: number[];
}

export interface DiffColumnProps {
  title: string;
  subtitle?: string;
  lines: DiffLine[];
  divergentCount: number;
  tone: "sky" | "violet";
  activeRef: number | null;
  onLineClick: (ref: number) => void;
}

const TONE = {
  sky: {
    header: "bg-sky-100 border-sky-100",
    title: "text-sky-900",
    chip: "bg-sky-600",
    divergent: "bg-amber-100 border-l-2 border-l-amber-400",
    active: "bg-amber-200",
  },
  violet: {
    header: "bg-violet-200 border-violet-200",
    title: "text-violet-900",
    chip: "bg-violet-800",
    divergent: "bg-amber-100 border-l-2 border-l-amber-400",
    active: "bg-amber-200",
  },
} as const;

export default function DiffColumn({
  title,
  subtitle,
  lines,
  divergentCount,
  tone,
  activeRef,
  onLineClick,
}: DiffColumnProps) {
  const t = TONE[tone];
  return (
    <div className="flex min-w-0 flex-col overflow-hidden rounded-lg border border-slate-300 bg-white">
      <div className={`flex items-center justify-between border-b px-3 py-2 ${t.header}`}>
        <div className="min-w-0">
          <h3 className={`truncate text-xs font-bold uppercase tracking-wide ${t.title}`}>{title}</h3>
          {subtitle && <p className="truncate text-[11px] text-slate-600">{subtitle}</p>}
        </div>
        <span className={`ml-2 shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold text-white ${t.chip}`}>
          {divergentCount} divergences
        </span>
      </div>
      <ol className="divide-y divide-slate-100 overflow-x-auto font-mono text-xs leading-relaxed">
        {lines.map((line) => {
          const isActive = activeRef !== null && line.refs.includes(activeRef);
          const highlight = line.kind === "divergent" || isActive;
          return (
            <li
              key={line.n}
              onClick={() => line.refs.length && onLineClick(line.refs[0])}
              className={`flex gap-2 px-3 py-1 ${
                isActive ? t.active : highlight ? t.divergent : ""
              } ${line.refs.length ? "cursor-pointer" : ""}`}
              title={line.refs.length ? "Click to open the discrepancy" : undefined}
            >
              <span className="w-8 shrink-0 select-none text-right text-slate-400">{line.n}</span>
              <span className={`whitespace-pre-wrap break-words ${highlight ? "text-slate-900" : "text-slate-600"}`}>
                {line.text || " "}
                {line.refs.length > 0 && (
                  <sup className="ml-1 rounded bg-amber-200 px-1 text-[9px] font-bold text-amber-950">
                    {line.refs.map((r) => `#${r + 1}`).join(" ")}
                  </sup>
                )}
              </span>
            </li>
          );
        })}
        {lines.length === 0 && <li className="px-3 py-6 text-center text-slate-400">No content</li>}
      </ol>
    </div>
  );
}
