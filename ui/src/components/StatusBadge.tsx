import {
  BriefcaseIcon,
  CheckCircleIcon,
  ClockIcon,
  ExclamationCircleIcon,
  InProgressIcon,
  MinusCircleIcon,
} from '@patternfly/react-icons';
import type { JobStatusValue } from '../api/types';

const STATUS_META: Record<JobStatusValue, { color: string; icon: () => JSX.Element; label: string }> = {
  queued: { color: '#63993d', icon: () => <ClockIcon />, label: 'Queued' },
  running: { color: '#0088ce', icon: () => <InProgressIcon />, label: 'Running' },
  succeeded: { color: '#3f9c35', icon: () => <CheckCircleIcon />, label: 'Successful' },
  failed: { color: '#c9190b', icon: () => <ExclamationCircleIcon />, label: 'Failed' },
  cancelled: { color: '#6a6e73', icon: () => <MinusCircleIcon />, label: 'Canceled' },
};

export function StatusBadge({ status }: { status: JobStatusValue }) {
  const meta = STATUS_META[status] ?? {
    color: '#6a6e73',
    icon: () => <BriefcaseIcon />,
    label: status,
  };
  const Icon = meta.icon;
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        color: meta.color,
        fontWeight: 600,
        textTransform: 'capitalize',
      }}
    >
      <Icon />
      {meta.label}
    </span>
  );
}

/** AWX-style status dot used in tables (mirrors ansible-ui job lists). */
export function StatusDot({ status }: { status: JobStatusValue }) {
  const meta = STATUS_META[status];
  return (
    <span
      title={meta?.label ?? status}
      style={{
        display: 'inline-block',
        width: 12,
        height: 12,
        borderRadius: '50%',
        backgroundColor: meta?.color ?? '#6a6e73',
        marginRight: 8,
        verticalAlign: 'middle',
      }}
    />
  );
}
