import { useState, useEffect, useRef, useReducer } from 'react';
import { createJobLogSocket, api } from '../api/client';
import type { Job } from '../types/resources';

const EVENT_WS_PREFIX = '\t';

export interface MigrationEvent {
  _event: string;
  [key: string]: unknown;
}

export interface MigrationStartEvent extends MigrationEvent {
  _event: 'migration_start';
  total_phases: number;
}

export interface PhaseStartEvent extends MigrationEvent {
  _event: 'phase_start';
  phase_num: number;
  total_phases: number;
  description: string;
  resource_type?: string;
}

export interface PhaseProgressEvent extends MigrationEvent {
  _event: 'phase_progress';
  phase_num: number;
  exported: number;
  created: number;
  skipped: number;
  failed: number;
  rate: string;
  elapsed: string;
}

export interface PhaseCompleteEvent extends MigrationEvent {
  _event: 'phase_complete';
  phase_num: number;
  description: string;
  created: number;
  updated: number;
  skipped: number;
  failed: number;
  exported: number;
  duration: string;
  warnings: Record<string, number>;
  warning_samples?: Record<string, string[]>;
}

export interface PhaseErrorEvent extends MigrationEvent {
  _event: 'phase_error';
  phase_num: number;
  error: string;
}

export interface ResourceResultEvent extends MigrationEvent {
  _event: 'resource_result';
  phase_num: number;
  name: string;
  resource_type: string;
  result: 'created' | 'updated' | 'skipped' | 'exists' | 'failed';
  detail: string;
}

export interface MigrationCompleteEvent extends MigrationEvent {
  _event: 'migration_complete';
  total_created: number;
  total_updated: number;
  total_skipped: number;
  total_failed: number;
}

export interface CredentialPauseEvent extends MigrationEvent {
  _event: 'credential_pause';
  credentials: Array<{
    name: string;
    credential_type: string;
    organization: string;
    source?: string;
    name_prefix?: string;
    used_by: Array<{ resource_type: string; resource_name: string }>;
  }>;
}

/* ------------------------------------------------------------------ */
/*  MigrationState — shared with MigrationProgressView                */
/* ------------------------------------------------------------------ */

export interface ResourceItem {
  name: string;
  resourceType: string;
  result: 'created' | 'updated' | 'skipped' | 'exists' | 'failed';
  detail: string;
}

export interface PhaseState {
  num: number;
  description: string;
  status: 'pending' | 'running' | 'complete' | 'failed';
  exported: number;
  created: number;
  updated: number;
  skipped: number;
  failed: number;
  rate: string;
  elapsed: string;
  duration: string;
  resources: ResourceItem[];
  error?: string;
}

export interface MigrationState {
  totalPhases: number;
  phases: PhaseState[];
  totalCreated: number;
  totalUpdated: number;
  totalSkipped: number;
  totalFailed: number;
  status: 'running' | 'complete' | 'failed';
  eventCount: number;
}

const INITIAL_MIGRATION_STATE: MigrationState = {
  totalPhases: 0,
  phases: [],
  totalCreated: 0,
  totalUpdated: 0,
  totalSkipped: 0,
  totalFailed: 0,
  status: 'running',
  eventCount: 0,
};

type MigrationAction =
  | { type: 'event'; event: MigrationEvent }
  | { type: 'bulk'; events: MigrationEvent[] }
  | { type: 'reset' };

function getOrCreatePhase(phases: Map<number, PhaseState>, num: number): PhaseState {
  let phase = phases.get(num);
  if (!phase) {
    phase = {
      num, description: '', status: 'pending', exported: 0,
      created: 0, updated: 0, skipped: 0, failed: 0,
      rate: '--/s', elapsed: '0s', duration: '', resources: [],
    };
    phases.set(num, phase);
  }
  return phase;
}

function clonePhase(phaseMap: Map<number, PhaseState>, num: number): PhaseState {
  const existing = phaseMap.get(num);
  if (!existing) return getOrCreatePhase(phaseMap, num);
  const cloned = { ...existing, resources: [...existing.resources] };
  phaseMap.set(num, cloned);
  return cloned;
}

function applyEvent(state: MigrationState, phaseMap: Map<number, PhaseState>, evt: MigrationEvent): void {
  switch (evt._event) {
    case 'migration_start':
      state.totalPhases = evt.total_phases as number;
      break;

    case 'phase_start': {
      const e = evt as PhaseStartEvent;
      if (e.total_phases && e.total_phases > state.totalPhases) {
        state.totalPhases = e.total_phases;
      }
      const phase = clonePhase(phaseMap, e.phase_num);
      phase.description = e.description;
      phase.status = 'running';
      phase.exported = 0;
      phase.created = 0;
      phase.updated = 0;
      phase.skipped = 0;
      phase.failed = 0;
      phase.rate = '--/s';
      phase.elapsed = '0s';
      phase.duration = '';
      phase.resources = [];
      break;
    }

    case 'phase_progress': {
      const e = evt as PhaseProgressEvent;
      if (phaseMap.has(e.phase_num)) {
        const phase = clonePhase(phaseMap, e.phase_num);
        phase.exported = e.exported;
        phase.created = e.created;
        phase.skipped = e.skipped;
        phase.failed = e.failed;
        phase.rate = e.rate;
        phase.elapsed = e.elapsed;
      }
      break;
    }

    case 'resource_result': {
      const e = evt as ResourceResultEvent;
      if (phaseMap.has(e.phase_num)) {
        const phase = clonePhase(phaseMap, e.phase_num);
        phase.resources.push({
          name: e.name,
          resourceType: e.resource_type,
          result: e.result,
          detail: e.detail,
        });
        if (phase.resources.length > 200) {
          phase.resources = phase.resources.slice(-200);
        }
      }
      break;
    }

    case 'phase_complete': {
      const e = evt as PhaseCompleteEvent;
      if (phaseMap.has(e.phase_num)) {
        const phase = clonePhase(phaseMap, e.phase_num);
        phase.status = e.failed > 0 ? 'failed' : 'complete';
        phase.created = e.created;
        phase.updated = e.updated || 0;
        phase.skipped = e.skipped;
        phase.failed = e.failed;
        phase.exported = e.exported;
        phase.duration = e.duration;
      }
      break;
    }

    case 'phase_error': {
      const e = evt as PhaseErrorEvent;
      if (phaseMap.has(e.phase_num)) {
        const phase = clonePhase(phaseMap, e.phase_num);
        phase.status = 'failed';
        phase.error = e.error;
      }
      break;
    }

    case 'migration_complete': {
      state.totalCreated = evt.total_created as number;
      state.totalUpdated = (evt.total_updated as number) || 0;
      state.totalSkipped = evt.total_skipped as number;
      state.totalFailed = evt.total_failed as number;
      state.status = state.totalFailed > 0 ? 'failed' : 'complete';
      break;
    }
  }
}

function finalize(state: MigrationState, phaseMap: Map<number, PhaseState>, eventCount: number): MigrationState {
  const phases = Array.from(phaseMap.values()).sort((a, b) => a.num - b.num);

  if (state.status === 'running') {
    let created = 0, updated = 0, skipped = 0, failed = 0;
    for (const p of phases) {
      created += p.created;
      updated += p.updated;
      skipped += p.skipped;
      failed += p.failed;
    }
    state.totalCreated = created;
    state.totalUpdated = updated;
    state.totalSkipped = skipped;
    state.totalFailed = failed;
  }

  return { ...state, phases, eventCount };
}

function migrationReducer(prev: MigrationState, action: MigrationAction): MigrationState {
  if (action.type === 'reset') return INITIAL_MIGRATION_STATE;

  const events = action.type === 'bulk' ? action.events : [action.event];
  if (events.length === 0) return prev;

  const state = { ...prev };
  const phaseMap = new Map<number, PhaseState>(prev.phases.map(p => [p.num, p]));

  for (const evt of events) {
    applyEvent(state, phaseMap, evt);
  }

  return finalize(state, phaseMap, prev.eventCount + events.length);
}

/* ------------------------------------------------------------------ */
/*  Hook                                                              */
/* ------------------------------------------------------------------ */

function isEventMessage(line: string): boolean {
  return line.charAt(0) === EVENT_WS_PREFIX;
}

function parseEventMessage(line: string): MigrationEvent | null {
  try {
    return JSON.parse(line.slice(1)) as MigrationEvent;
  } catch {
    return null;
  }
}

export function useJobLogs(jobId: string) {
  const [textLines, setTextLines] = useState<string[]>([]);
  const [migration, dispatch] = useReducer(migrationReducer, INITIAL_MIGRATION_STATE);
  const [status, setStatus] = useState<string>('connecting');
  const wsReceivedRef = useRef(false);
  const rawEventsRef = useRef<MigrationEvent[]>([]);

  useEffect(() => {
    if (!jobId) {
      setStatus('empty');
      return;
    }

    setTextLines([]);
    dispatch({ type: 'reset' });
    rawEventsRef.current = [];
    setStatus('connecting');
    wsReceivedRef.current = false;
    let closed = false;

    const ws = createJobLogSocket(
      jobId,
      (line) => {
        wsReceivedRef.current = true;
        if (isEventMessage(line)) {
          const evt = parseEventMessage(line);
          if (evt) {
            rawEventsRef.current.push(evt);
            dispatch({ type: 'event', event: evt });
          }
        } else {
          setTextLines(prev => [...prev, line]);
        }
        setStatus('streaming');
      },
      (reason) => {
        const finalStatus = reason || 'closed';
        setStatus(finalStatus);
        if (!wsReceivedRef.current && !closed) {
          loadFromRest();
        }
      },
    );

    async function loadFromRest() {
      try {
        const job = (await api.getJob(jobId)) as Job;
        if (job.output && job.output.length > 0) {
          const text: string[] = [];
          const evts: MigrationEvent[] = [];
          for (const line of job.output) {
            if (isEventMessage(line)) {
              const evt = parseEventMessage(line);
              if (evt) evts.push(evt);
            } else {
              text.push(line);
            }
          }
          setTextLines(text);
          if (evts.length > 0) {
            rawEventsRef.current = evts;
            dispatch({ type: 'bulk', events: evts });
          }
        }
        if (rawEventsRef.current.length === 0) {
          const meta = job.job_metadata;
          if (meta && Array.isArray(meta.events)) {
            const metaEvents = meta.events as MigrationEvent[];
            rawEventsRef.current = metaEvents;
            dispatch({ type: 'bulk', events: metaEvents });
          }
        }
        setStatus(job.status || 'empty');
      } catch {
        setStatus('error');
      }
    }

    return () => {
      closed = true;
      ws.close();
    };
  }, [jobId]);

  return { textLines, migration, events: rawEventsRef.current, status };
}
