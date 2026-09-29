import type { AnalysisStatus, Discrepancy, Distro, IssueType, Severity } from "./api-types";

/** green=In Sync, amber=Minor Drift, red=Breaking Discrepancy */
export type HealthLevel = "green" | "amber" | "red" | "grey";

const BREAKING: ReadonlySet<Severity> = new Set(["critical", "high"]);

export function healthLevelFor(
  pkg: {
    consistency_score?: number | null;
    open_discrepancies?: number;
    status?: AnalysisStatus;
    error?: string | null;
  },
  discrepancies?: Pick<Discrepancy, "severity">[],
): HealthLevel {
  if (pkg.status === "error" || pkg.error) return "red";
  if (pkg.status !== "analyzed" && pkg.status !== "stale") return "grey";
  if (discrepancies?.some((d) => BREAKING.has(d.severity))) return "red";
  const breaking = pkg.open_discrepancies ?? 0;
  if (breaking > 0) return "red";
  const score = pkg.consistency_score ?? 0;
  if (score >= 90) return "green";
  if (score >= 65) return "amber";
  return "red";
}

export const HEALTH_META: Record<HealthLevel, { label: string; badge: string; dot: string }> = {
  green: {
    label: "In Sync",
    badge: "bg-emerald-100 text-emerald-800 border-emerald-200",
    dot: "bg-emerald-500",
  },
  amber: {
    label: "Minor Drift",
    badge: "bg-amber-100 text-amber-800 border-amber-200",
    dot: "bg-amber-400",
  },
  red: {
    label: "Breaking Discrepancy",
    badge: "bg-rose-100 text-rose-800 border-rose-200",
    dot: "bg-rose-500",
  },
  grey: {
    label: "Not Analyzed",
    badge: "bg-slate-100 text-slate-600 border-slate-300",
    dot: "bg-slate-400",
  },
};

export const SEVERITY_META: Record<Severity, { label: string; badge: string; border: string }> = {
  critical: { label: "Critical", badge: "bg-rose-100 text-rose-800 border-rose-200", border: "border-l-rose-500" },
  high: { label: "High", badge: "bg-rose-100 text-rose-700 border-rose-200", border: "border-l-rose-500" },
  medium: { label: "Medium", badge: "bg-amber-100 text-amber-800 border-amber-200", border: "border-l-amber-400" },
  low: { label: "Low", badge: "bg-sky-100 text-sky-900 border-sky-100", border: "border-l-sky-400" },
  info: { label: "Info", badge: "bg-slate-100 text-slate-600 border-slate-300", border: "border-l-slate-400" },
};

export const STATUS_BADGE: Partial<Record<AnalysisStatus, { label: string; cls: string }>> = {
  unindexed: { label: "unindexed", cls: "bg-slate-100 text-slate-500 border-slate-300" },
  indexed: { label: "indexed", cls: "bg-sky-100 text-sky-900 border-sky-100" },
  analyzing: { label: "analyzing…", cls: "bg-violet-200 text-violet-900 border-violet-200" },
  analyzed: { label: "analyzed", cls: "bg-emerald-100 text-emerald-800 border-emerald-200" },
  stale: { label: "stale", cls: "bg-amber-100 text-amber-800 border-amber-200" },
  error: { label: "error", cls: "bg-rose-100 text-rose-800 border-rose-200" },
};

export const ISSUE_TYPE_LABEL: Record<IssueType, string> = {
  undocumented_action: "Undocumented Action",
  obsolete_documentation: "Obsolete Documentation",
  parameter_mismatch: "Parameter Mismatch",
};

export const AUDIT_VERDICT: Record<
  "MATCH" | "MINOR_DRIFT" | "BREAKING_DISCREPANCY",
  { label: string; badge: string }
> = {
  MATCH: { label: "MATCH", badge: "bg-emerald-100 text-emerald-800 border-emerald-200" },
  MINOR_DRIFT: { label: "MINOR DRIFT", badge: "bg-amber-100 text-amber-800 border-amber-200" },
  BREAKING_DISCREPANCY: { label: "BREAKING DISCREPANCY", badge: "bg-rose-100 text-rose-800 border-rose-200" },
};

export const DISTRO_LABEL: Partial<Record<Distro, string>> = {
  ubuntu: "Ubuntu",
  rhel: "RHEL",
  centos: "CentOS",
  fedora: "Fedora",
  sles: "SLES",
  debian: "Debian",
};

export const CATEGORY_LABEL: Record<string, string> = {
  missing_dependency: "Missing Dependency",
  extra_dependency: "Extra Dependency",
  version_mismatch: "Version Mismatch",
  missing_env_var: "Missing Env Var",
  env_var_value_mismatch: "Env Var Value Mismatch",
  missing_build_flag: "Missing Build Flag",
  missing_s390x_patch: "Missing s390x Patch",
  distro_mismatch: "Distro Mismatch",
  undocumented_step: "Undocumented Step",
  other: "Other",
};

export function scoreColor(score?: number | null): string {
  if (score == null) return "text-slate-500";
  if (score >= 90) return "text-emerald-500";
  if (score >= 65) return "text-amber-400";
  return "text-rose-500";
}

export function timeAgo(iso?: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
