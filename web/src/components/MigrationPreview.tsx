import { useState, useEffect } from 'react';
import {
  Alert,
  Checkbox,
  Label,
  ExpandableSection,
} from '@patternfly/react-core';
import { api } from '../api/client';
import type { MigrationPreviewData, DefaultExclusions } from '../types/resources';

const resourceTypeLabels: Record<string, string> = {
  organizations: 'Organizations',
  teams: 'Teams',
  users: 'Users',
  credential_types: 'Credential Types',
  credentials: 'Credentials',
  projects: 'Projects',
  inventories: 'Inventories',
  hosts: 'Hosts',
  groups: 'Groups',
  job_templates: 'Job Templates',
  workflow_job_templates: 'Workflow Job Templates',
  schedules: 'Schedules',
};

const displayOrder = [
  'organizations', 'teams', 'users', 'credential_types', 'credentials',
  'projects', 'inventories', 'hosts', 'groups',
  'job_templates', 'workflow_job_templates', 'schedules',
];

interface Props {
  preview: MigrationPreviewData;
  exclude: Record<string, string[]>;
  onExcludeChange: (exclude: Record<string, string[]>) => void;
}

export function MigrationPreview({ preview, exclude, onExcludeChange }: Props) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [defaultExclusions, setDefaultExclusions] = useState<DefaultExclusions | null>(null);
  const [exclusionsExpanded, setExclusionsExpanded] = useState(false);

  useEffect(() => {
    api.getExclusions().then(data => setDefaultExclusions(data as DefaultExclusions)).catch(() => {});
  }, []);

  let createCount = 0;
  let skipCount = 0;
  let excludeCount = 0;
  for (const [type, items] of Object.entries(preview.resources)) {
    for (const item of items) {
      const excluded = exclude[type]?.includes(String(item.source_id));
      if (excluded) {
        excludeCount++;
      } else if (item.action === 'create') {
        createCount++;
      } else {
        skipCount++;
      }
    }
  }

  const toggleExpanded = (type: string) => {
    setExpanded(prev => ({ ...prev, [type]: !prev[type] }));
  };

  const isTypeFullyExcluded = (type: string): boolean => {
    const items = preview.resources[type];
    if (!items || items.length === 0) return false;
    const excludedIds = exclude[type] || [];
    return items.every(i => excludedIds.includes(String(i.source_id)));
  };

  const toggleTypeExclusion = (type: string) => {
    const items = preview.resources[type];
    if (!items) return;
    const newExclude = { ...exclude };
    if (isTypeFullyExcluded(type)) {
      delete newExclude[type];
    } else {
      newExclude[type] = items.map(i => String(i.source_id));
    }
    onExcludeChange(newExclude);
  };

  const toggleItemExclusion = (type: string, sourceId: number) => {
    const newExclude = { ...exclude };
    const current = newExclude[type] || [];
    const idStr = String(sourceId);
    if (current.includes(idStr)) {
      newExclude[type] = current.filter(id => id !== idStr);
      if (newExclude[type].length === 0) delete newExclude[type];
    } else {
      newExclude[type] = [...current, idStr];
    }
    onExcludeChange(newExclude);
  };

  const orderedTypes = displayOrder.filter(t => preview.resources[t]?.length > 0);

  return (
    <div>
      {preview.warnings?.map((w, i) => (
        <Alert key={i} variant="warning" isInline title={w} style={{ marginBottom: 8 }} />
      ))}

      <div style={{ margin: '16px 0', fontSize: '1.1em' }}>
        <strong>{createCount}</strong> to create, <strong>{skipCount}</strong> to skip (already exist)
        {excludeCount > 0 && <>, <strong>{excludeCount}</strong> excluded by user</>}
      </div>

      {defaultExclusions && (
        <ExpandableSection
          toggleText="Default Exclusions (always filtered during export)"
          isExpanded={exclusionsExpanded}
          onToggle={() => setExclusionsExpanded(!exclusionsExpanded)}
          style={{ marginBottom: 16 }}
        >
          <div style={{ fontSize: '0.9em', padding: '8px 16px', background: '#f0f0f0', borderRadius: 4 }}>
            {Object.entries(defaultExclusions.migration).map(([type, names]) => (
              <div key={type} style={{ marginBottom: 4 }}>
                <strong>{resourceTypeLabels[type] || type}:</strong>{' '}
                {names.map((n, i) => (
                  <Label key={i} isCompact color="grey" style={{ marginRight: 4 }}>{n}</Label>
                ))}
              </div>
            ))}
          </div>
        </ExpandableSection>
      )}

      {orderedTypes.map(type => {
        const items = preview.resources[type];
        const excludedIds = exclude[type] || [];
        const activeItems = items.filter(i => !excludedIds.includes(String(i.source_id)));
        const creates = activeItems.filter(i => i.action === 'create').length;
        const skips = activeItems.length - creates;
        const excluded = items.length - activeItems.length;
        const label = resourceTypeLabels[type] || type;
        const fullyExcluded = isTypeFullyExcluded(type);

        let hostInfo = '';
        if (type === 'inventories' && preview.host_counts) {
          const totalHosts = Object.values(preview.host_counts).reduce((a, b) => a + b, 0);
          const totalGroups = preview.group_counts ? Object.values(preview.group_counts).reduce((a, b) => a + b, 0) : 0;
          hostInfo = ` — ${totalHosts} hosts, ${totalGroups} groups total`;
        }

        return (
          <ExpandableSection
            key={type}
            toggleText={
              `${label} (${items.length})` +
              (excluded > 0 ? ` — ${creates} create, ${skips} skip, ${excluded} excluded` : ` — ${creates} create, ${skips} skip`) +
              hostInfo
            }
            isExpanded={expanded[type] || false}
            onToggle={() => toggleExpanded(type)}
          >
            <div style={{ marginBottom: 8 }}>
              <Checkbox
                id={`exclude-all-${type}`}
                label={`Exclude all ${label.toLowerCase()}`}
                isChecked={fullyExcluded}
                onChange={() => toggleTypeExclusion(type)}
              />
            </div>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.9em' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #d2d2d2', textAlign: 'left' }}>
                  <th style={{ padding: '4px 8px', width: 40 }}></th>
                  <th style={{ padding: '4px 8px' }}>Name</th>
                  <th style={{ padding: '4px 8px' }}>Action</th>
                  <th style={{ padding: '4px 8px' }}>Source ID</th>
                  {type === 'inventories' && preview.host_counts && (
                    <th style={{ padding: '4px 8px' }}>Hosts / Groups</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {items.map((item, i) => {
                  const isItemExcluded = excludedIds.includes(String(item.source_id));
                  return (
                    <tr key={i} style={{ borderBottom: '1px solid #eee', opacity: isItemExcluded ? 0.5 : 1 }}>
                      <td style={{ padding: '4px 8px' }}>
                        <Checkbox
                          id={`exclude-${type}-${i}`}
                          isChecked={isItemExcluded}
                          onChange={() => toggleItemExclusion(type, item.source_id)}
                          aria-label={`Exclude ${item.name}`}
                        />
                      </td>
                      <td style={{ padding: '4px 8px' }}>{item.name}</td>
                      <td style={{ padding: '4px 8px' }}>
                        {isItemExcluded ? (
                          <Label color="orange" isCompact>Excluded</Label>
                        ) : (
                          <Label
                            color={item.action === 'create' ? 'green' : 'grey'}
                            isCompact
                          >
                            {item.action === 'create' ? 'Create' : 'Skip (exists)'}
                          </Label>
                        )}
                      </td>
                      <td style={{ padding: '4px 8px' }}>{item.source_id}</td>
                      {type === 'inventories' && preview.host_counts && (
                        <td style={{ padding: '4px 8px' }}>
                          {preview.host_counts[item.name] ?? 0} / {preview.group_counts?.[item.name] ?? 0}
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </ExpandableSection>
        );
      })}
    </div>
  );
}
