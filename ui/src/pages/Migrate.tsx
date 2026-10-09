import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Form,
  FormGroup,
  Grid,
  GridItem,
  MenuToggle,
  PageSection,
  Select,
  SelectList,
  SelectOption,
  Switch,
  TextInput,
} from '@patternfly/react-core';
import { api } from '../api/client';
import type { ActiveConfigOut, ApiConnection, ConnectionListOut, JobCreated } from '../api/types';
import { PageHeader } from '../components/Layout';

// Full migration workflow: export -> transform -> import with job chaining.
// Each phase submits against the previous job_id so all phases share one
// working directory (exports/, xformed/, state DB), mirroring the CLI/TUI.
// Any stored source/target pair can be picked per submit; blank falls back
// to the active pair from Settings.
export function Migrate() {
  const navigate = useNavigate();
  const [resourceTypes, setResourceTypes] = useState('');
  const [skipPrep, setSkipPrep] = useState(true);
  const [dryRun, setDryRun] = useState(false);
  const [sources, setSources] = useState<ApiConnection[]>([]);
  const [targets, setTargets] = useState<ApiConnection[]>([]);
  const [sourceId, setSourceId] = useState('');
  const [targetId, setTargetId] = useState('');
  const [pairOpen, setPairOpen] = useState<'source' | 'target' | null>(null);
  const [submitting, setSubmitting] = useState<null | 'export' | 'migrate' | 'granular'>(null);
  const [error, setError] = useState<string | null>(null);
  const [chain, setChain] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.get<ConnectionListOut>('/connections?limit=100&offset=0'),
      api.get<ActiveConfigOut>('/connections/active'),
    ])
      .then(([list, act]) => {
        if (cancelled) {
          return;
        }
        const items = list.items ?? [];
        setSources(items.filter((c) => c.kind === 'source'));
        setTargets(items.filter((c) => c.kind === 'target'));
        setSourceId(act.source_id ?? '');
        setTargetId(act.target_id ?? '');
      })
      .catch(() => {
        // Pair selectors stay empty; submits fall back to the active pair.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const parsedTypes = resourceTypes
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

  const pairScope = {
    source_id: sourceId || null,
    target_id: targetId || null,
  };

  const submitAndGo = async (label: 'export' | 'migrate' | 'granular', path: string, body: object) => {
    setSubmitting(label);
    setError(null);
    try {
      const created = await api.post<JobCreated>(path, { ...pairScope, ...body });
      setChain((prev) => [...prev, `${label}: ${created.job_id}`]);
      navigate(`/jobs/${created.job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Submit failed');
    } finally {
      setSubmitting(null);
    }
  };

  const pairSelect = (
    which: 'source' | 'target',
    label: string,
    options: ApiConnection[],
    value: string,
    set: (v: string) => void,
  ) => (
    <FormGroup label={label} fieldId={`migrate-${which}`}>
      <Select
        id={`migrate-${which}`}
        isOpen={pairOpen === which}
        selected={value ? (options.find((c) => c.id === value)?.name ?? value) : 'Active pair'}
        onSelect={(_e, v) => {
          set(String(v));
          setPairOpen(null);
        }}
        onOpenChange={(open) => setPairOpen(open ? which : null)}
        toggle={(toggleRef) => (
          <MenuToggle
            ref={toggleRef}
            onClick={() => setPairOpen(pairOpen === which ? null : which)}
            isExpanded={pairOpen === which}
            style={{ minWidth: 240 }}
          >
            {value ? (options.find((c) => c.id === value)?.name ?? value) : 'Active pair'}
          </MenuToggle>
        )}
      >
        <SelectList>
          {options.map((c) => (
            <SelectOption key={c.id} value={c.id}>
              {c.name} ({c.url})
            </SelectOption>
          ))}
        </SelectList>
      </Select>
    </FormGroup>
  );

  return (
    <PageSection>
      <PageHeader
        title="Migrate"
        description={
          <>
            Submit ETL phases as background jobs. Phases chain via <code>job_id</code> so
            export → transform → import share one working directory.
          </>
        }
      />
      {error && <Alert variant="danger" title={error} style={{ marginTop: 16 }} />}
      {chain.length > 0 && (
        <Alert variant="info" title="Submitted" style={{ marginTop: 16 }}>
          {chain.join(' · ')}
        </Alert>
      )}
      {(sources.length > 0 || targets.length > 0) && (
        <Card style={{ marginTop: 16 }}>
          <CardTitle>Migration pair (defaults to the active pair from Settings)</CardTitle>
          <CardBody>
            <Form>
              <Grid hasGutter>
                <GridItem span={12} md={6}>
                  {pairSelect('source', 'Source', sources, sourceId, setSourceId)}
                </GridItem>
                <GridItem span={12} md={6}>
                  {pairSelect('target', 'Target', targets, targetId, setTargetId)}
                </GridItem>
              </Grid>
            </Form>
            <p style={{ marginTop: 8 }}>
              <Link to="/settings">Manage endpoints in Settings</Link>
            </p>
          </CardBody>
        </Card>
      )}
      <Card style={{ marginTop: 16 }}>
        <CardTitle>Phase scope</CardTitle>
        <CardBody>
          <Form>
            <FormGroup
              label="Resource types (comma-separated, blank = all)"
              fieldId="resource-types"
            >
              <TextInput
                id="resource-types"
                value={resourceTypes}
                onChange={(_e, v) => setResourceTypes(v)}
                placeholder="organizations, credentials, projects"
              />
            </FormGroup>
            <FormGroup label="Skip prep (schemas already discovered)" fieldId="skip-prep">
              <Switch id="skip-prep" isChecked={skipPrep} onChange={(_e, c) => setSkipPrep(c)} />
            </FormGroup>
            <FormGroup label="Dry run (import only)" fieldId="dry-run">
              <Switch id="dry-run" isChecked={dryRun} onChange={(_e, c) => setDryRun(c)} />
            </FormGroup>
          </Form>
        </CardBody>
      </Card>
      <div style={{ display: 'flex', gap: 12, marginTop: 16, flexWrap: 'wrap' }}>
        <Button
          variant="primary"
          isLoading={submitting === 'migrate'}
          onClick={() =>
            submitAndGo('migrate', '/migrations', {
              resource_types: parsedTypes.length ? parsedTypes : null,
              skip_prep: skipPrep,
              phase: 'all',
            })
          }
        >
          Run full migration
        </Button>
        <Button
          variant="secondary"
          isLoading={submitting === 'export'}
          onClick={() =>
            submitAndGo('export', '/exports', {
              resource_types: parsedTypes.length ? parsedTypes : null,
            })
          }
        >
          Export only
        </Button>
        <Button
          variant="secondary"
          isLoading={submitting === 'granular'}
          onClick={() =>
            submitAndGo('granular', '/imports/granular', {
              steps: parsedTypes.length ? parsedTypes : null,
              dry_run: dryRun,
            })
          }
        >
          Granular import
        </Button>
      </div>
      <p style={{ color: '#6a6e73', marginTop: 12 }}>
        Tip: run Export, then Transform and Import chained to the export job from the Job
        detail page for step-by-step control (mirrors the TUI granular import menu).
      </p>
    </PageSection>
  );
}
