import { fireEvent, render, screen } from '@testing-library/react';
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from 'react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@patternfly/react-core', () => ({
  Modal: ({ children, title, actions, isOpen }: { children: ReactNode; title: string; actions: ReactNode[]; isOpen: boolean }) =>
    isOpen ? (
      <div>
        <h1>{title}</h1>
        {children}
        <div>{actions}</div>
      </div>
    ) : null,
  ModalVariant: { medium: 'medium' },
  Form: ({ children }: { children: ReactNode }) => <form>{children}</form>,
  FormGroup: ({ children, label }: { children: ReactNode; label?: string }) => (
    <label>
      {label}
      {children}
    </label>
  ),
  FormHelperText: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  HelperText: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  HelperTextItem: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  TextInput: ({ id, value, onChange, ...props }: InputHTMLAttributes<HTMLInputElement> & { onChange: (_e: unknown, value: string) => void }) => (
    <input id={id} value={value as string} onChange={(event) => onChange(event, event.currentTarget.value)} {...props} />
  ),
  FormSelect: ({ id, value, onChange, isDisabled, children }: SelectHTMLAttributes<HTMLSelectElement> & { onChange: (_e: unknown, value: string) => void; isDisabled?: boolean }) => (
    <select
      id={id}
      aria-label={id}
      value={value as string}
      disabled={isDisabled}
      onChange={(event) => onChange(event, event.currentTarget.value)}
    >
      {children}
    </select>
  ),
  FormSelectOption: ({ value, label }: { value: string; label: string }) => <option value={value}>{label}</option>,
  Checkbox: ({ id, label, isChecked, onChange }: { id: string; label: string; isChecked?: boolean; onChange: (_e: unknown, checked: boolean) => void }) => (
    <label htmlFor={id}>
      {label}
      <input id={id} type="checkbox" checked={isChecked} onChange={(event) => onChange(event, event.currentTarget.checked)} />
    </label>
  ),
  Button: ({ children, onClick }: ButtonHTMLAttributes<HTMLButtonElement>) => <button onClick={onClick}>{children}</button>,
  Alert: ({ title, children }: { title: string; children: ReactNode }) => (
    <div>
      <strong>{title}</strong>
      {children}
    </div>
  ),
}));

import { ConnectionForm } from './ConnectionForm';

describe('ConnectionForm', () => {
  it('saves a new connection including null token when blank', () => {
    const onSave = vi.fn();

    render(<ConnectionForm isOpen onSave={onSave} onClose={vi.fn()} />);

    fireEvent.change(screen.getByPlaceholderText('My AAP Instance'), { target: { value: 'Source AAP' } });
    fireEvent.change(screen.getByPlaceholderText('https://aap.example.com'), { target: { value: 'https://source.example.com' } });
    fireEvent.click(screen.getByText('Save'));

    expect(onSave).toHaveBeenCalledWith({
      name: 'Source AAP',
      type: 'awx',
      role: 'source',
      url: 'https://source.example.com',
      verify_ssl: true,
      token: null,
    });
  });

  it('updates role and token behavior for edit and aap mode', () => {
    const onSave = vi.fn();
    const onClose = vi.fn();

    render(
      <ConnectionForm
        isOpen
        onSave={onSave}
        onClose={onClose}
        error="save failed"
        initial={{
          name: 'Existing',
          type: 'awx',
          role: 'source',
          url: 'https://old.example.com',
          verify_ssl: false,
        }}
      />
    );

    expect(screen.getByText('Edit Connection')).toBeInTheDocument();
    expect(screen.getByText('save failed')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('type'), { target: { value: 'aap' } });
    fireEvent.change(screen.getByLabelText('role'), { target: { value: 'destination' } });
    fireEvent.change(screen.getByPlaceholderText('Leave blank to keep current token'), { target: { value: 'new-token' } });
    fireEvent.click(screen.getByLabelText('Verify SSL certificate'));
    fireEvent.click(screen.getByText('Save'));
    fireEvent.click(screen.getByText('Cancel'));

    expect(onSave).toHaveBeenCalledWith({
      name: 'Existing',
      type: 'aap',
      role: 'destination',
      url: 'https://old.example.com',
      verify_ssl: true,
      token: 'new-token',
    });
    expect(onClose).toHaveBeenCalled();
  });
});
