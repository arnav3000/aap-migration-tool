import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { IAM } from './IAM';

vi.mock('../api/client', () => ({
  api: {
    listConnections: vi.fn().mockResolvedValue([
      { id: 'src-1', name: 'Src', url: 'https://src.example.com', role: 'source', type: 'awx', verify_ssl: true },
      { id: 'tgt-1', name: 'Tgt', url: 'https://tgt.example.com', role: 'destination', type: 'aap', verify_ssl: true },
    ]),
    iamAudit: vi.fn().mockResolvedValue({ job_id: 'job-1' }),
    iamMigrate: vi.fn().mockResolvedValue({ job_id: 'job-2' }),
    iamBenchmark: vi.fn().mockResolvedValue({ output: 'benchmark ok' }),
  },
}));

describe('IAM page', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders tabs', async () => {
    render(
      <MemoryRouter>
        <IAM />
      </MemoryRouter>,
    );
    expect(screen.getByText('IAM Analysis & Migration')).toBeInTheDocument();
    expect(screen.getByText('Audit (read-only scan)')).toBeInTheDocument();
  });

  it('can switch tabs', async () => {
    render(
      <MemoryRouter>
        <IAM />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText('Migrate'));
    expect(screen.getByText('Run IAM Migration')).toBeInTheDocument();
  });
});
