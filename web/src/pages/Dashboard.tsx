import { useState, useEffect, useCallback } from 'react';
import {
  Button,
  Card,
  CardBody,
  CardFooter,
  CardHeader,
  CardTitle,
  Title,
  TextContent,
  Text,
  Gallery,
  Grid,
  GridItem,
  Label,
  NumberInput,
  Split,
  SplitItem,
  DescriptionList,
  DescriptionListGroup,
  DescriptionListTerm,
  DescriptionListDescription,
  Alert,
  Divider,
} from '@patternfly/react-core';
import { Dropdown, DropdownItem, KebabToggle } from '@patternfly/react-core/deprecated';
import { api } from '../api/client';
import { ConnectionForm } from '../components/ConnectionForm';
import type { Connection } from '../types/connection';

export function Dashboard() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editConn, setEditConn] = useState<Connection | null>(null);
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [clearMsg, setClearMsg] = useState('');
  const [maxConcurrent, setMaxConcurrent] = useState(15);
  const [concurrencyMsg, setConcurrencyMsg] = useState('');

  const loadConnections = useCallback(async () => {
    try {
      const conns = await api.listConnections() as Connection[];
      setConnections(conns);
    } catch (err) {
      console.error('Failed to load connections:', err);
    }
  }, []);

  useEffect(() => { loadConnections(); }, [loadConnections]);

  useEffect(() => {
    api.getConcurrency().then(r => setMaxConcurrent(r.max_concurrent)).catch(() => {});
  }, []);

  const handleSave = async (conn: Omit<Connection, 'id'>) => {
    setSaveError(null);
    try {
      if (editConn) {
        await api.updateConnection(editConn.id, conn);
      } else {
        await api.createConnection(conn);
      }
      setShowForm(false);
      setEditConn(null);
      loadConnections();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to save connection';
      setSaveError(message);
    }
  };

  const handleDelete = async (id: string) => {
    setConnections(prev => prev.filter(c => c.id !== id));
    try {
      await api.deleteConnection(id);
    } catch { /* already removed from UI */ }
    loadConnections();
  };

  const handleTest = async (id: string) => {
    setTesting(id);
    setConnections(prev => prev.map(c =>
      c.id === id ? { ...c, ping_status: 'unknown', auth_status: 'unknown', ping_error: undefined, auth_error: undefined } : c
    ));
    try {
      await api.testConnection(id);
    } finally {
      setTesting(null);
      loadConnections();
    }
  };

  const dropdownItems = (conn: Connection) => [
    <DropdownItem key="edit" onClick={() => { setEditConn(conn); setShowForm(true); }}>Edit</DropdownItem>,
    <DropdownItem key="delete" onClick={() => handleDelete(conn.id)} style={{ color: '#c9190b' }}>Delete</DropdownItem>,
  ];

  const sources = connections.filter(c => c.role === 'source');
  const destinations = connections.filter(c => c.role === 'destination' || c.role === 'target');

  const pingLabel = (conn: Connection) => {
    switch (conn.ping_status) {
      case 'ok': return <Label color="green" isCompact>Ping OK</Label>;
      case 'error': return <Label color="red" isCompact>Unreachable</Label>;
      default: return <Label color="grey" isCompact>Ping ?</Label>;
    }
  };

  const authLabel = (conn: Connection) => {
    switch (conn.auth_status) {
      case 'ok': return <Label color="green" isCompact>Auth OK</Label>;
      case 'error': return <Label color="red" isCompact>Auth Failed</Label>;
      default: return <Label color="grey" isCompact>Auth ?</Label>;
    }
  };

  const renderCard = (conn: Connection) => {
    return (
      <Card key={conn.id}>
        <CardHeader
          actions={{
            actions: (
              <Dropdown
                isOpen={openMenu === conn.id}
                onSelect={() => setOpenMenu(null)}
                toggle={<KebabToggle onToggle={(_e, open) => setOpenMenu(open ? conn.id : null)} />}
                isPlain
                dropdownItems={dropdownItems(conn)}
                position="right"
              />
            ),
          }}
        >
          <CardTitle>
            <Split hasGutter>
              <SplitItem>{conn.name}</SplitItem>
              <SplitItem>
                <Label color={conn.type === 'awx' ? 'blue' : 'purple'}>
                  {conn.type.toUpperCase()}{conn.version ? ` v${conn.version}` : ''}
                </Label>
              </SplitItem>
              <SplitItem>{pingLabel(conn)}</SplitItem>
              <SplitItem>{authLabel(conn)}</SplitItem>
            </Split>
          </CardTitle>
        </CardHeader>
        <CardBody>
          <DescriptionList isHorizontal isCompact>
            <DescriptionListGroup>
              <DescriptionListTerm>URL</DescriptionListTerm>
              <DescriptionListDescription>{conn.url}</DescriptionListDescription>
            </DescriptionListGroup>
            <DescriptionListGroup>
              <DescriptionListTerm>Token</DescriptionListTerm>
              <DescriptionListDescription>{conn.token ? '********' : 'Not set'}</DescriptionListDescription>
            </DescriptionListGroup>
            <DescriptionListGroup>
              <DescriptionListTerm>SSL Verify</DescriptionListTerm>
              <DescriptionListDescription>{conn.verify_ssl ? 'On' : 'Off'}</DescriptionListDescription>
            </DescriptionListGroup>
          </DescriptionList>
          {conn.ping_status === 'error' && conn.ping_error && (
            <Alert variant="danger" isInline isPlain title={conn.ping_error} style={{ marginTop: 8 }} />
          )}
          {conn.auth_status === 'error' && conn.auth_error && (
            <Alert variant="danger" isInline isPlain title={conn.auth_error} style={{ marginTop: 8 }} />
          )}
        </CardBody>
        <CardFooter>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => handleTest(conn.id)}
            isLoading={testing === conn.id}
            isDisabled={testing === conn.id}
          >
            {testing === conn.id ? 'Testing...' : 'Test'}
          </Button>
        </CardFooter>
      </Card>
    );
  };

  return (
    <>
      <Title headingLevel="h1" size="2xl">Settings</Title>
      <TextContent style={{ marginBottom: 16 }}>
        <Text>Configure connections and application settings.</Text>
      </TextContent>

      <Grid hasGutter style={{ marginBottom: 24 }}>
        <GridItem span={6}>
          <Card isFullHeight>
            <CardHeader><CardTitle>Migration State</CardTitle></CardHeader>
            <CardBody>
              <TextContent style={{ marginBottom: 16 }}>
                <Text>Clear all stored ID mappings and progress records. This forces the next migration run to re-create all resources instead of skipping previously migrated ones.</Text>
              </TextContent>
              <Button
                variant="warning"
                onClick={async () => {
                  setClearMsg('');
                  try {
                    const result = await api.clearMigrationState();
                    setClearMsg(`Cleared ${result.cleared_progress} progress records and ${result.deleted_mappings} ID mappings`);
                  } catch (err) {
                    setClearMsg(`Error: ${err instanceof Error ? err.message : String(err)}`);
                  }
                }}
              >
                Clear Migration State
              </Button>
              {clearMsg && (
                <Alert
                  variant={clearMsg.startsWith('Error') ? 'danger' : 'success'}
                  isInline
                  title={clearMsg}
                  style={{ marginTop: 12 }}
                />
              )}
            </CardBody>
          </Card>
        </GridItem>
        <GridItem span={6}>
          <Card isFullHeight>
            <CardHeader><CardTitle>Concurrency</CardTitle></CardHeader>
            <CardBody>
              <TextContent style={{ marginBottom: 16 }}>
                <Text>Set how many objects to migrate concurrently. Higher values speed up migration but require more resources on both the source and destination systems.</Text>
              </TextContent>
              <NumberInput
                value={maxConcurrent}
                min={1}
                max={100}
                onMinus={() => setMaxConcurrent(prev => Math.max(1, prev - 1))}
                onPlus={() => setMaxConcurrent(prev => Math.min(100, prev + 1))}
                onChange={(event: React.FormEvent<HTMLInputElement>) => {
                  const val = parseInt((event.target as HTMLInputElement).value, 10);
                  if (!isNaN(val)) setMaxConcurrent(Math.max(1, Math.min(100, val)));
                }}
                widthChars={4}
              />
              <Button
                variant="primary"
                style={{ marginLeft: 16 }}
                onClick={async () => {
                  setConcurrencyMsg('');
                  try {
                    await api.updateConcurrency(maxConcurrent);
                    setConcurrencyMsg('Saved');
                  } catch (err) {
                    setConcurrencyMsg(`Error: ${err instanceof Error ? err.message : String(err)}`);
                  }
                }}
              >
                Save
              </Button>
              {concurrencyMsg && (
                <Alert
                  variant={concurrencyMsg.startsWith('Error') ? 'danger' : 'success'}
                  isInline
                  title={concurrencyMsg}
                  style={{ marginTop: 12 }}
                />
              )}
            </CardBody>
          </Card>
        </GridItem>
      </Grid>

      <Divider style={{ marginBottom: 24 }} />

      <Title headingLevel="h2" size="xl" style={{ marginBottom: 8 }}>Connections</Title>
      <Button variant="primary" onClick={() => { setEditConn(null); setShowForm(true); }} style={{ marginBottom: 16 }}>
        Add Connection
      </Button>

      {connections.length === 0 && (
        <Alert variant="info" isInline title="No connections yet. Click 'Add Connection' to get started." />
      )}

      {sources.length > 0 && (
        <>
          <Title headingLevel="h2" size="xl" style={{ marginTop: 16, marginBottom: 8 }}>Sources</Title>
          <Gallery hasGutter minWidths={{ default: '350px' }}>
            {sources.map(conn => renderCard(conn))}
          </Gallery>
        </>
      )}

      {destinations.length > 0 && (
        <>
          <Title headingLevel="h2" size="xl" style={{ marginTop: 24, marginBottom: 8 }}>Destinations</Title>
          <Gallery hasGutter minWidths={{ default: '350px' }}>
            {destinations.map(conn => renderCard(conn))}
          </Gallery>
        </>
      )}

      <ConnectionForm
        key={editConn?.id || 'new'}
        isOpen={showForm}
        initial={editConn || undefined}
        onSave={handleSave}
        onClose={() => { setShowForm(false); setEditConn(null); setSaveError(null); }}
        error={saveError}
      />
    </>
  );
}
