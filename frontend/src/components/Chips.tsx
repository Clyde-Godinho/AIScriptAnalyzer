import { STATUS_BADGE, DISTRO_LABEL } from "@/lib/ui";
import type { AnalysisStatus, Distro } from "@/lib/api-types";

export function StatusBadge({ status }: { status: AnalysisStatus }) {
  const meta = STATUS_BADGE[status] ?? { label: status, cls: "bg-slate-100 text-slate-500 border-slate-300" };
  return (
    <span className={`inline-flex rounded border px-1.5 py-0.5 font-mono text-[10px] ${meta.cls}`}>{meta.label}</span>
  );
}

export function DistroChip({ distro }: { distro: Distro }) {
  return (
    <span className="inline-flex rounded border border-sky-100 bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-900">
      {DISTRO_LABEL[distro] ?? distro}
    </span>
  );
}
