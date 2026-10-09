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
  Switch,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
  Tooltip,
} from '@patternfly/react-core';
import { LogViewer, LogViewerSearch } from '@patternfly/react-log-viewer';
import DownloadIcon from '@patternfly/react-icons/dist/esm/icons/download-icon';
import ExpandIcon from '@patternfly/react-icons/dist/esm/icons/expand-icon';
import { api } from '../api/client';
import type { JobConsoleOut, JobStatusValue } from '../api/types';

// Event filter options mirror the AWX Job Output toolbar (Stdout/Event
// dropdown): they filter the rendered console lines client-side.
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
 * AWX-style job output viewer.
 * Polls GET /jobs/{id}/console while the job is queued/running and renders
 * through PatternFly LogViewer (the same component family ansible-ui uses
 * for stdout), with search, event filter, autoscroll and download.
 */
export function JobOutput({ jobId, jobStatus, pollIntervalMs = 2000 }: JobOutputProps) {
  const [consoleText, setConsoleText] = useState('');
  const [available, setAvailable] = useState(false);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<EventFilter>('all');
  const [filterOpen, setFilterOpen] = useState(false);
  const [autoscroll, setAutoscroll] = useState(true);
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
          setAvailable(data.console_available);
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
  // PatternFly LogViewer has no autoscroll prop: pinning scrollToRow to the
  // last row while autoscroll is on reproduces the AWX follow behavior.

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
            <ToolbarItem>
              <Switch
                id={`autoscroll-${jobId}`}
                label="Autoscroll"
                isChecked={autoscroll}
                onChange={(_e, checked) => setAutoscroll(checked)}
              />
            </ToolbarItem>
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
        {!available && !consoleText && !error && (
          <p style={{ color: '#6a6e73' }}>
            No console output yet. Output appears here once the job starts producing logs.
          </p>
        )}
        {(available || consoleText) && (
          <div style={{ height: expanded ? 700 : 500 }}>
            <LogViewer
              data={filteredText}
              hasLineNumbers
              isTextWrapped={false}
              scrollToRow={autoscroll ? lineCount : undefined}
              theme="dark"
              toolbar={
                <LogViewerSearch
                  placeholder="Find in output"
                  minSearchChars={3}
                />
              }
            />
          </div>
        )}
        <p style={{ color: '#6a6e73', marginTop: 8 }}>
          {lineCount} lines{active ? ' · live — polling every 2s' : ''}
        </p>
      </CardBody>
    </Card>
  );
}
