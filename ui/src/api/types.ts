// Shared wire types for GET /api/v1/openapi.json (subset the UI uses).
// Field names mirror the FastAPI schemas 1:1 so the UI is a faithful
// superset of the CLI surface.

export type JobStatusValue = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface JobCreated {
  job_id: string;
  job_type: string;
  status: JobStatusValue;
  poll_url: string;
  chained_from_status?: JobStatusValue | null;
}

export interface JobStatus {
  job_id: string;
  job_type: string;
  status: JobStatusValue;
  params: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error?: string | null;
  exit_code?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface JobListOut {
  items: JobStatus[];
  total: number;
  limit: number;
  offset: number;
}

export interface JobConsoleOut {
  job_id: string;
  console: string;
  console_available: boolean;
}

export interface JobArtifactsOut {
  job_id: string;
  artifacts: string[];
  walked: number;
  total: number;
  truncated: boolean;
  limit: number;
  offset: number;
}

export interface ApiConnection {
  id: string;
  name: string;
  kind: 'source' | 'target';
  url: string;
  verify_ssl: boolean;
  timeout: number;
  created_at?: string;
  updated_at?: string;
}

export interface ConnectionListOut {
  connections: ApiConnection[];
  total: number;
  limit: number;
  offset: number;
}

export interface ActiveConfigOut {
  source?: ApiConnection | null;
  target?: ApiConnection | null;
}

export interface MigrationStatusOut {
  migration_id?: string | null;
  resource_stats: Record<string, unknown>;
  unreadable_types: string[];
  warning?: string | null;
}

export interface HealthOut {
  status: string;
  service: string;
  version: string;
  worker: string;
  queue_depth?: number | null;
  [key: string]: unknown;
}

export interface ApiError {
  detail: string;
}
