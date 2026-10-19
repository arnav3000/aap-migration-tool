import { useEffect, useMemo, useState } from 'react';
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  SearchInput,
  Select,
  SelectOption,
  SelectList,
  MenuToggle,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
  Tooltip,
} from '@patternfly/react-core';
import DownloadIcon from '@patternfly/react-icons/dist/esm/icons/download-icon';
import ExpandIcon from '@patternfly/react-icons/dist/esm/icons/expand-icon';
import { api } from '../api/client';
import type { JobConsoleOut, JobStatusValue } from '../api/types';
import { JobOutputView } from './awx/JobOutputView';

// Event filter mirrors the AWX Job Output toolbar (Stdout/Event dropdown).
// Migration consoles are plain text, so filtering is client-side matching.
type EventFilter = 'all' | 'error' | 'failed' | 'unreachable' | 'skipped' | 'ok' | 'changed';

const EVENT_FILTERS: { value: EventFilter; label: string }[] = [
  { value: 'all', label: 'All events' },
  { value: 'error', label: 'Error' },
  { value: 'failed', label: 'Host failed' },
  { value: 'unreachable', label: 'Host unreachable' },
  { value: 'skipped', label: 'Skipped' },
  { value: 'ok', label: 'OK' },
  { value: 'changed', label: 'Changed' },
];

function matchesFilter(line: string, filter: EventFilter, search: string): boolean {
  const lower = line.toLowerCase();
  if (search && !lower.includes(search.toLowerCase())) {
    return false;
  }
  switch (filter) {
    case 'all':
      return true;
    case 'error':
      return lower.includes('error') || lower.includes('failed');
    case 'failed':
      return lower.includes('failed=');
    case 'unreachable':
      return lower.includes('unreachable=');
    case 'skipped':
      return lower.includes('skipped=');
    case 'ok':
      return lower.includes('ok=');
    case 'changed':
      return lower.includes('changed=');
    default:
      return true;
  }
}

interface JobOutputProps {
  jobId: string;
  jobStatus: JobStatusValue;
  pollIntervalMs?: number;
}

/**
 * AWX-style job output: status toolbar (search + event filter + Follow),
 * vendored output-grid rows, live polling while queued/running.
 */
export function JobOutput({ jobId, jobStatus, pollIntervalMs = 2000 }: JobOutputProps) {
  const [consoleText, setConsoleText] = useState('');
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<EventFilter>('all');
  const [filterOpen, setFilterOpen] = useState(false);
  const [follow, setFollow] = useState(jobStatus === 'queued' || jobStatus === 'running');
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const active = jobStatus === 'queued' || jobStatus === 'running';

  useEffect(() => {
    let cancelled = false;
    const fetchConsole = async () => {
      try {
        const data = await api.get<JobConsoleOut>(
          `/jobs/${jobId}/console?tail=5000`,
        );
        if (!cancelled) {
          setConsoleText(data.console || '');
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load console output');
        }
      }
    };
    void fetchConsole();
    if (!active) {
      return () => {
        cancelled = true;
      };
    }
    const timer = window.setInterval(fetchConsole, pollIntervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [jobId, active, pollIntervalMs]);

  const filteredText = useMemo(() => {
    if (filter === 'all' && !search) {
      return consoleText;
    }
    return consoleText
      .split('\n')
      .filter((line) => matchesFilter(line, filter, search))
      .join('\n');
  }, [consoleText, filter, search]);

  const download = () => {
    const blob = new Blob([consoleText], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `job-${jobId}-stdout.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const lineCount = consoleText ? consoleText.split('\n').length : 0;

  return (
    <Card isFullHeight isExpanded={expanded}>
      <CardHeader>
        <CardTitle>Output</CardTitle>
      </CardHeader>
      <CardBody>
        <Toolbar clearAllFilters={() => { setSearch(''); setFilter('all'); }}>
          <ToolbarContent>
            <ToolbarItem>
              <Select
                isOpen={filterOpen}
                selected={filter}
                onSelect={(_e, value) => {
                  setFilter(value as EventFilter);
                  setFilterOpen(false);
                }}
                onOpenChange={setFilterOpen}
                toggle={(toggleRef) => (
                  <MenuToggle
                    ref={toggleRef}
                    onClick={() => setFilterOpen(!filterOpen)}
                    isExpanded={filterOpen}
                  >
                    {EVENT_FILTERS.find((f) => f.value === filter)?.label ?? 'All events'}
                  </MenuToggle>
                )}
              >
                <SelectList>
                  {EVENT_FILTERS.map((f) => (
                    <SelectOption key={f.value} value={f.value}>
                      {f.label}
                    </SelectOption>
                  ))}
                </SelectList>
              </Select>
            </ToolbarItem>
            <ToolbarItem>
              <SearchInput
                placeholder="Search output"
                value={search}
                onChange={(_e, value) => setSearch(value)}
                onClear={() => setSearch('')}
                aria-label="Search job output"
              />
            </ToolbarItem>
            {active && (
              <ToolbarItem>
                <Button
                  variant={follow ? 'secondary' : 'primary'}
                  onClick={() => setFollow(!follow)}
                >
                  {follow ? 'Unfollow' : 'Follow'}
                </Button>
              </ToolbarItem>
            )}
            <ToolbarItem align={{ default: 'alignEnd' }}>
              <Tooltip content="Download stdout (.txt)">
                <Button variant="plain" onClick={download} aria-label="Download output">
                  <DownloadIcon />
                </Button>
              </Tooltip>
              <Tooltip content={expanded ? 'Collapse' : 'Expand'}>
                <Button
                  variant="plain"
                  onClick={() => setExpanded(!expanded)}
                  aria-label="Expand output"
                >
                  <ExpandIcon />
                </Button>
              </Tooltip>
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>

        {error && <p style={{ color: '#c9190b' }}>{error}</p>}
        <JobOutputView
          text={filteredText}
          follow={follow}
          onFollowChange={setFollow}
          height={expanded ? 700 : 500}
          waiting={active}
        />
        <p style={{ color: '#6a6e73', marginTop: 8 }}>
          {lineCount} lines{active ? ' · live — polling every 2s' : ''}
        </p>
      </CardBody>
    </Card>
  );
}
