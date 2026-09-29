/**
 * API contract mirrored from `app/models.py` (Pydantic, extra="forbid").
 * Keep the two files in sync when the backend evolves.
 */

export type Distro =
  | "ubuntu"
  | "rhel"
  | "centos"
  | "fedora"
  | "sles"
  | "debian"
  | "unknown";

export type Severity = "info" | "low" | "medium" | "high" | "critical";

export type AnalysisStatus =
  | "unindexed"
  | "pending"
  | "indexing"
  | "indexed"
  | "analyzing"
  | "analyzed"
  | "stale"
  | "error";

export type JobState = "queued" | "running" | "succeeded" | "failed";

export type DetectedBy = "rule" | "llm" | "both";

export type IssueType =
  | "undocumented_action"
  | "obsolete_documentation"
  | "parameter_mismatch";

export type AuditCategory =
  | "prerequisites"
  | "environment_variables"
  | "build_flags"
  | "patches"
  | "source_version"
  | "testing";

export type AuditVerdict = "MATCH" | "MINOR_DRIFT" | "BREAKING_DISCREPANCY";

export type DiscrepancyCategory =
  | "missing_dependency"
  | "extra_dependency"
  | "version_mismatch"
  | "missing_env_var"
  | "env_var_value_mismatch"
  | "missing_build_flag"
  | "missing_s390x_patch"
  | "distro_mismatch"
  | "undocumented_step"
  | "other";

export interface Discrepancy {
  category: DiscrepancyCategory;
  severity: Severity;
  title: string;
  description: string;
  component?: string | null;
  doc_evidence?: string | null;
  script_evidence?: string | null;
  recommendation: string;
  confidence: number;
  detected_by: DetectedBy;
  audit_category?: AuditCategory | null;
  issue_type?: IssueType | null;
  ibm_z_impact?: string | null;
}

export interface SourceRef {
  source_type: "doc" | "script";
  path: string;
  commit?: string | null;
  chunk_count: number;
  distros: Distro[];
  counts: Record<string, number>;
}

export interface AnalysisReport {
  report_id: string;
  package_name: string;
  distro: Distro;
  match_key: string;
  status: AnalysisStatus;
  summary: string;
  consistency_score: number;
  discrepancies: Discrepancy[];
  documentations_to_update: string[];
  scripts_to_update: string[];
  notes?: string | null;
  doc?: SourceRef | null;
  script?: SourceRef | null;
  doc_signature?: { source_path: string } | null;
  script_signature?: { source_path: string } | null;
  model: string;
  duration_ms?: number | null;
  created_at: string;
  warnings: string[];
}

export interface PackageSummary {
  package_name: string;
  distro: Distro;
  match_key: string;
  status: AnalysisStatus;
  doc_path?: string | null;
  script_path?: string | null;
  versions: string[];
  chunk_count: number;
  doc_commit?: string | null;
  script_commit?: string | null;
  last_indexed_at?: string | null;
  last_analyzed_at?: string | null;
  consistency_score?: number | null;
  open_discrepancies: number;
  error?: string | null;
}

export interface PackageListResponse {
  total: number;
  index_commit?: string | null;
  indexed_at?: string | null;
  packages: PackageSummary[];
}

export interface SyncResponse {
  job_id: string;
  state: JobState;
  accepted: boolean;
  message: string;
  stats?: Record<string, unknown> | null;
}

export interface JobStatus {
  job_id: string;
  kind: "sync" | "analyze";
  state: JobState;
  submitted_at: string;
  finished_at?: string | null;
  result?: Record<string, unknown> | null;
  error?: string | null;
}

export interface AnalyzeResponse {
  job_id?: string | null;
  state?: JobState | null;
  report?: AnalysisReport | null;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  vector_backend: string;
  collection: string;
  embedding_provider: string;
  llm_provider: string;
  llm_model: string;
  indexed_chunks: number;
  packages: number;
  repositories: Record<string, string | null>;
}

export interface ListPackagesParams {
  status?: AnalysisStatus;
  search?: string;
  limit?: number;
}
