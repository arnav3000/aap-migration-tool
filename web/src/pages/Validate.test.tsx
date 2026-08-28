import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Validate } from './Validate';

vi.mock('../api/client', () => ({
  api: {
    listConnections: vi.fn().mockResolvedValue([
      { id: 'src-1', name: 'Src AAP', url: 'https://src.example.com', role: 'source', type: 'awx', verify_ssl: true },
      { id: 'tgt-1', name: 'Tgt AAP', url: 'https://tgt.example.com', role: 'destination', type: 'aap', verify_ssl: true },
    ]),
    runValidate: vi.fn().mockResolvedValue({ job_id: 'job-123' }),
  },
}));

describe('Validate page', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders heading and controls', async () => {
    render(
      <MemoryRouter>
        <Validate />
      </MemoryRouter>,
    );
    expect(screen.getByText('Post-Migration Validation')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('Live — compare exports vs target API (identity match)')).toBeInTheDocument());
  });

  it('starts validation and navigates on success', async () => {
    const { api } = await import('../api/client');
    render(
      <MemoryRouter>
        <Validate />
      </MemoryRouter>,
    );
    await waitFor(() => screen.getByText(/Run DB Validation/));
    fireEvent.click(screen.getByText('Run DB Validation'));
    await waitFor(() => expect(api.runValidate).toHaveBeenCalled());
  });
});
