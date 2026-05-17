export interface ResourceType {
  name: string;
  label: string;
  api_path: string;
}

export interface JobMetadata {
  events?: Record<string, unknown>[];
  [key: string]: unknown;
}

export interface Job {
  id: string;
  seq_id?: number;
  name?: string;
  type: string;
  connection_id?: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  started_at: string;
  finished_at?: string;
  error?: string;
  output?: string[];
  job_metadata?: JobMetadata;
}

export interface MigrationResource {
  source_id: number;
  name: string;
  type: string;
  action: string;
  dest_id?: number;
}

export interface MigrationPreviewData {
  source_id: string;
  destination_id: string;
  resources: Record<string, MigrationResource[]>;
  warnings: string[];
  host_counts?: Record<string, number>;
  group_counts?: Record<string, number>;
}

export interface DefaultExclusions {
  migration: Record<string, string[]>;
  cleanup: Record<string, Record<string, string[]>>;
}

// --- Migration Planner Types ---

export interface PlanPhaseOrg {
  id: string;
  source_id: string;
  org_id: number;
  org_name: string;
}

export interface PlanPhase {
  id: string;
  phase_number: number;
  name: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'completed_with_errors';
  update_mode: boolean;
  resource_types: string[];
  job_id?: string | null;
  orgs: PlanPhaseOrg[];
}

export interface ResourceTypeInfo {
  name: string;
  description: string;
  migration_order: number;
}

export interface PlanSource {
  id: string;
  connection_id: string;
  name_prefix?: string | null;
  analysis_job_id?: string | null;
}

export interface MigrationPlan {
  id: string;
  name: string;
  description: string;
  status: 'draft' | 'active' | 'completed' | 'failed';
  destination_id: string | null;
  created_at: string;
  updated_at: string;
  sources: PlanSource[];
  phases: PlanPhase[];
}

export interface MigrationPlanListItem {
  id: string;
  name: string;
  description: string;
  status: string;
  destination_id: string | null;
  created_at: string;
  updated_at: string;
  source_count: number;
  phase_count: number;
}
