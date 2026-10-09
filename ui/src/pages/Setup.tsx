import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
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
  PageSection,
  Spinner,
  Switch,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { CheckCircleIcon } from '@patternfly/react-icons';
import { api } from '../api/client';
import type { ActiveConfigOut, ApiConnection, ConnectionListOut } from '../api/types';

interface SideForm {
  name: string;
  url: string;
  token: string;
  verify_ssl: boolean;
  timeout: number;
}

interface SideState {
  form: SideForm;
  connectionId: string | null;
  testing: boolean;
  testResult: { reachable: boolean; version?: string; url: string } | null;
}

const defaultForm = (kind: 'source' | 'target'): SideForm => ({
  name: kind === 'source' ? 'prod-source' : 'prod-target',
  url: kind === 'source' ? 'https://' : 'https://',
  token: '',
  verify_ssl: false,
  timeout: 300,
});

const initialSide = (kind: 'source' | 'target'): SideState => ({
  form: defaultForm(kind),
  connectionId: null,
  testing: false,
  testResult: null,
});

/**
 * First-run setup wizard: enter the source and target AAP endpoints in the
 * browser so the server .env only ever holds AAP_BRIDGE_API_TOKEN.
 * Each side is saved as a stored connection (token encrypted at rest),
 * tested live, and set as the active pair on success.
 */
export function Setup() {
  const [source, setSource] = useState<SideState>(() => initialSide('source'));
  const [target, setTarget] = useState<SideState>(() => initialSide('target'));
  const [active, setActive] = useState<ActiveConfigOut | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [list, act] = await Promise.all([
        api.get<ConnectionListOut>('/connections?limit=100&offset=0'),
        api.get<ActiveConfigOut>('/connections/active'),
      ]);
      const byId: Record<string, string> = {};
      for (const c of list.items ?? []) {
        byId[c.id] = c.name;
      }
      setNames(byId);
      setActive(act);
      // Pre-fill each side from its existing connection (first match by kind).
      const fill = (kind: 'source' | 'target', set: (s: SideState) => void) => {
        const existing = (list.items ?? []).find((c: ApiConnection) => c.kind === kind);
        if (existing) {
          set({
            form: {
              name: existing.name,
              url: existing.url,
              // Token is never returned: user re-enters it only when changing it.
              token: '',
              verify_ssl: existing.verify_ssl,
              timeout: existing.timeout,
            },
            connectionId: existing.id,
            testing: false,
            testResult: null,
          });
        }
      };
      fill('source', setSource);
      fill('target', setTarget);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load setup state');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const saveAndTest = async (
    kind: 'source' | 'target',
    side: SideState,
    set: (s: SideState) => void,
  ) => {
    set({ ...side, testing: true, testResult: null });
    setError(null);
    try {
      let id = side.connectionId;
      if (id && !side.form.token) {
        // No new token entered: keep the stored one, update the rest.
        await api.patch<ApiConnection>(`/connections/${id}`, {
          name: side.form.name,
          url: side.form.url,
          verify_ssl: side.form.verify_ssl,
          timeout: side.form.timeout,
        });
      } else if (id) {
        await api.put<ApiConnection>(`/connections/${id}`, {
          name: side.form.name,
          url: side.form.url,
          token: side.form.token,
          verify_ssl: side.form.verify_ssl,
          timeout: side.form.timeout,
        });
      } else {
        const created = await api.post<ApiConnection>('/connections', {
          name: side.form.name,
          kind,
          url: side.form.url,
          token: side.form.token,
          verify_ssl: side.form.verify_ssl,
          timeout: side.form.timeout,
        });
        id = created.id;
      }
      const result = await api.post<{ reachable: boolean; version?: string; url: string }>(
        `/connections/${id}/test`,
        {},
      );
      if (result.reachable && id) {
        // Test passed: make it the active side immediately.
        const updated = await api.post<ActiveConfigOut>(
          '/connections/active',
          kind === 'source' ? { source_id: id } : { target_id: id },
        );
        setActive(updated);
      }
      set({ ...side, connectionId: id, testing: false, testResult: result });
      const list = await api.get<ConnectionListOut>('/connections?limit=100&offset=0');
      const byId: Record<string, string> = {};
      for (const c of list.items ?? []) {
        byId[c.id] = c.name;
      }
      setNames(byId);
    } catch (err) {
      set({ ...side, testing: false });
      setError(err instanceof Error ? err.message : `Failed to save ${kind} connection`);
    }
  };

  const renderSide = (
    kind: 'source' | 'target',
    title: string,
    side: SideState,
    set: (s: SideState) => void,
    activeId: string | null,
  ) => {
    const isActive = side.connectionId !== null && side.connectionId === activeId;
    const setField = <K extends keyof SideForm>(key: K, value: SideForm[K]) =>
      set({ ...side, form: { ...side.form, [key]: value } });
    return (
      <Card isFullHeight>
        <CardTitle>
          {title}{' '}
          {isActive && (
            <span style={{ color: '#3f9c35', fontSize: 14 }}>
              <CheckCircleIcon /> Active
            </span>
          )}
        </CardTitle>
        <CardBody>
          <Form>
            <FormGroup label="Name" isRequired fieldId={`${kind}-name`}>
              <TextInput
                id={`${kind}-name`}
                value={side.form.name}
                onChange={(_e, v) => setField('name', v)}
              />
            </FormGroup>
            <FormGroup label="URL" isRequired fieldId={`${kind}-url`}>
              <TextInput
                id={`${kind}-url`}
                value={side.form.url}
                onChange={(_e, v) => setField('url', v)}
                placeholder={
                  kind === 'source'
                    ? 'https://aap24.example.com/api/v2'
                    : 'https://aap26.example.com/api/controller/v2'
                }
              />
            </FormGroup>
            <FormGroup
              label={side.connectionId ? 'Token (blank = keep stored)' : 'Token'}
              isRequired={!side.connectionId}
              fieldId={`${kind}-token`}
            >
              <TextInput
                id={`${kind}-token`}
                type="password"
                value={side.form.token}
                onChange={(_e, v) => setField('token', v)}
                placeholder={side.connectionId ? '•••••••• (stored)' : 'Paste AAP token (write scope)'}
              />
            </FormGroup>
            <FormGroup label="Verify SSL" fieldId={`${kind}-ssl`}>
              <Switch
                id={`${kind}-ssl`}
                isChecked={side.form.verify_ssl}
                onChange={(_e, c) => setField('verify_ssl', c)}
              />
            </FormGroup>
            <FormGroup label="Timeout (s)" fieldId={`${kind}-timeout`}>
              <TextInput
                id={`${kind}-timeout`}
                type="number"
                value={String(side.form.timeout)}
                onChange={(_e, v) => setField('timeout', Number(v) || 300)}
              />
            </FormGroup>
          </Form>
          <div style={{ marginTop: 12 }}>
            <Button
              variant="primary"
              isLoading={side.testing}
              isDisabled={!side.form.name || !side.form.url || (!side.connectionId && !side.form.token)}
              onClick={() => saveAndTest(kind, side, set)}
            >
              Save &amp; test
            </Button>
          </div>
          {side.testResult && (
            <Alert
              variant={side.testResult.reachable ? 'success' : 'danger'}
              title={
                side.testResult.reachable
                  ? `Reachable: ${side.testResult.url} (version ${side.testResult.version ?? 'unknown'}) — set as active ${kind}.`
                  : `Not reachable: ${side.testResult.url}`
              }
              style={{ marginTop: 12 }}
              isInline
            />
          )}
        </CardBody>
      </Card>
    );
  };

  const ready = active?.source_id !== null && active?.target_id !== null;

  return (
    <PageSection>
      <Title headingLevel="h1">Setup</Title>
      <p>
        Enter your source and target AAP endpoints here — tokens are encrypted at
        rest on the server, so the server <code>.env</code> only needs{' '}
        <code>AAP_BRIDGE_API_TOKEN</code>. AAP 2.6 targets use the Platform
        Gateway path <code>/api/controller/v2</code>.
      </p>
      {loading && <Spinner aria-label="Loading setup" />}
      {error && <Alert variant="danger" title={error} style={{ marginTop: 16 }} />}
      {!loading && (
        <>
          <Grid hasGutter style={{ marginTop: 16 }}>
            <GridItem span={12} md={6}>
              {renderSide('source', 'Source AAP (2.4 / 2.5)', source, setSource, active?.source_id ?? null)}
            </GridItem>
            <GridItem span={12} md={6}>
              {renderSide('target', 'Target AAP (2.5 / 2.6)', target, setTarget, active?.target_id ?? null)}
            </GridItem>
          </Grid>
          <Card style={{ marginTop: 16 }}>
            <CardBody>
              <p>
                Active source:{' '}
                <strong>
                  {active?.source_id ? (names[active.source_id] ?? active.source_id) : 'none'}
                </strong>{' '}
                · Active target:{' '}
                <strong>
                  {active?.target_id ? (names[active.target_id] ?? active.target_id) : 'none'}
                </strong>
              </p>
              {ready ? (
                <Alert variant="success" title="Pair configured — ready to migrate." isInline>
                  <Link to="/migrate">Go to Migrate</Link>
                </Alert>
              ) : (
                <Alert
                  variant="info"
                  title="Save & test both sides to activate the migration pair."
                  isInline
                />
              )}
            </CardBody>
          </Card>
        </>
      )}
    </PageSection>
  );
}
