/**
 * AWX JobOutput presentation pattern, adapted from ansible-ui
 * (frontend/awx/views/jobs/JobOutput/JobOutputEvents.tsx + JobOutputRow.tsx,
 * Apache-2.0 — see ui/NOTICE).
 *
 * AWX renders one row per job event with a sticky line-number gutter and
 * ANSI-colored stdout. Migration jobs have no Ansible events, so rows here
 * are plain console lines; the grid, gutter, follow-mode and loading-row
 * behavior mirror AWX. Virtualization is unnecessary: the console endpoint
 * caps tails at 5000 lines.
 */
import { useEffect, useRef } from 'react';
import { Ansi } from './Ansi';
import './JobOutput.css';

interface JobOutputViewProps {
  text: string;
  follow: boolean;
  onFollowChange: (follow: boolean) => void;
  height?: number;
  waiting: boolean;
}

export function JobOutputView({ text, follow, onFollowChange, height = 500, waiting }: JobOutputViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const lines = text ? text.split('\n') : [];
  const digits = String(lines.length).length;

  // Follow mode: pin to the bottom on new output (AWX follow behavior).
  useEffect(() => {
    const el = containerRef.current;
    if (el && follow) {
      el.scrollTop = el.scrollHeight;
    }
  }, [text, follow]);

  const handleScroll = () => {
    const el = containerRef.current;
    if (!el || !follow) {
      return;
    }
    // User scrolled up: drop out of follow mode like AWX does.
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom > 40) {
      onFollowChange(false);
    }
  };

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="output-grid"
      style={{ height, overflowY: 'auto', ['--output-line-chars' as string]: digits }}
      role="log"
      aria-label="Job output"
    >
      {lines.length === 0 && (
        <div className="output-grid-row">
          <div style={{ padding: '8px 16px', color: 'var(--pf-t--global--text--color--subtle)' }}>
            {waiting ? 'Waiting for output — lines appear here once the job starts producing logs.' : 'No output.'}
          </div>
        </div>
      )}
      {lines.map((line, i) => (
        <div key={i} className="output-grid-row">
          <div
            style={{
              position: 'sticky',
              left: 0,
              display: 'flex',
              gap: 8,
              padding: '2px 8px',
              borderRight: '1px solid var(--pf-t--global--border--color--default)',
              zIndex: 1,
              backgroundColor: 'var(--pf-t--global--background--color--secondary--default)',
            }}
          >
            <div style={{ flex: 1, textAlign: 'right' }}>{i + 1}</div>
          </div>
          <div style={{ padding: '2px 16px' }} className="awx-stdout">
            <Ansi input={line} />
          </div>
        </div>
      ))}
    </div>
  );
}
