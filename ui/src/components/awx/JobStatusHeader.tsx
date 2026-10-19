/**
 * AWX JobStatusBar header pattern, adapted from ansible-ui
 * (frontend/awx/views/jobs/JobOutput/JobStatusBar.tsx, Apache-2.0).
 *
 * Title + status on one baseline with a ticking elapsed clock while the job
 * is active. AWX shows play/task/host counts from the job record; migration
 * jobs carry exit codes and timestamps instead, shown as badges.
 */
import { Badge, Flex, FlexItem } from '@patternfly/react-core';
import { useEffect, useState } from 'react';
import type { JobStatus } from '../../api/types';
import { StatusBadge } from '../StatusBadge';
import './JobOutput.css';

function formatElapsed(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return [h, m, sec].map((n) => String(n).padStart(2, '0')).join(':');
}

export function JobStatusHeader({ job }: { job: JobStatus }) {
  const active = job.status === 'queued' || job.status === 'running';
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, job.job_id]);

  let elapsed = '—';
  if (job.created_at) {
    const end = !active && job.updated_at ? new Date(job.updated_at).getTime() : now;
    elapsed = formatElapsed((end - new Date(job.created_at).getTime()) / 1000);
  }

  return (
    <Flex alignItems={{ default: 'alignItemsCenter' }}>
      <div className="awx-header-title" data-testid="job-status-bar">
        <h1>
          {job.job_type} {job.job_id.slice(0, 8)}
        </h1>
        <StatusBadge status={job.status} />
      </div>
      <FlexItem align={{ default: 'alignRight' }}>
        <Flex alignItems={{ default: 'alignItemsCenter' }}>
          {job.exit_code !== null && job.exit_code !== undefined && (
            <FlexItem>
              Exit <Badge isRead>{job.exit_code}</Badge>
            </FlexItem>
          )}
          <FlexItem>
            Elapsed <Badge isRead>{elapsed}</Badge>
          </FlexItem>
        </Flex>
      </FlexItem>
    </Flex>
  );
}
