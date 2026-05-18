import { fireEvent, render, screen } from '@testing-library/react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@patternfly/react-core', () => ({
  Page: ({
    children,
    header,
    sidebar,
  }: {
    children: ReactNode;
    header?: ReactNode;
    sidebar?: ReactNode;
  }) => (
    <div>
      {header}
      {sidebar}
      {children}
    </div>
  ),
  Masthead: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  MastheadMain: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  MastheadBrand: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  MastheadContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  MastheadToggle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  PageToggleButton: ({ children, onClick, onSidebarToggle, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { onSidebarToggle?: () => void }) => (
    <button onClick={onClick ?? onSidebarToggle} {...props}>
      {children}
    </button>
  ),
  Nav: ({ children }: { children: ReactNode }) => <nav>{children}</nav>,
  NavItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  NavList: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  NavExpandable: ({ title, isExpanded, onExpand, children }: { title: string; isExpanded: boolean; onExpand: () => void; children: ReactNode }) => (
    <div>
      <button onClick={onExpand}>{title}</button>
      {isExpanded ? children : null}
    </div>
  ),
  PageSidebar: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
  PageSidebarBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  PageSection: ({ children }: { children: ReactNode }) => <section>{children}</section>,
  Toolbar: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ToolbarContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ToolbarGroup: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ToolbarItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Alert: ({ title, children }: { title: string; children: ReactNode }) => (
    <div>
      <strong>{title}</strong>
      {children}
    </div>
  ),
  Button: ({ children, onClick }: ButtonHTMLAttributes<HTMLButtonElement>) => <button onClick={onClick}>{children}</button>,
}));

vi.mock('@patternfly/react-core/deprecated', () => ({
  Dropdown: ({ isOpen, toggle, dropdownItems }: { isOpen: boolean; toggle: ReactNode; dropdownItems: ReactNode[] }) => (
    <div>
      {toggle}
      {isOpen ? dropdownItems : null}
    </div>
  ),
  DropdownItem: ({ children, href }: { children: ReactNode; href?: string }) => <a href={href}>{children}</a>,
  KebabToggle: ({ children, onToggle }: { children: ReactNode; onToggle: (_e: unknown, open: boolean) => void }) => (
    <button aria-label="help" onClick={() => onToggle(undefined, true)}>
      {children}
    </button>
  ),
}));

vi.mock('@patternfly/react-icons/dist/esm/icons/bars-icon', () => ({ default: () => <span>bars</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/question-circle-icon', () => ({ default: () => <span>help</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/sun-icon', () => ({ default: () => <span>sun</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/moon-icon', () => ({ default: () => <span>moon</span> }));

vi.mock('./pages/Dashboard', () => ({ Dashboard: () => <div>Dashboard Page</div> }));
vi.mock('./pages/Operations', () => ({ Operations: () => <div>Operations Page</div> }));
vi.mock('./pages/Migrate', () => ({ Migrate: () => <div>Migrate Page</div> }));
vi.mock('./pages/ObjectBrowser', () => ({ ObjectBrowser: () => <div>Object Browser Page</div> }));
vi.mock('./pages/Jobs', () => ({ Jobs: () => <div>Jobs Page</div> }));
vi.mock('./pages/JobDetail', () => ({ JobDetail: () => <div>Job Detail Page</div> }));
vi.mock('./pages/Analysis', () => ({ Analysis: () => <div>Analysis Page</div> }));
vi.mock('./pages/Sizing', () => ({ Sizing: () => <div>Sizing Page</div> }));
vi.mock('./pages/Planner', () => ({ Planner: () => <div>Planner Page</div> }));
vi.mock('./pages/PlanDetail', () => ({ PlanDetail: () => <div>Plan Detail Page</div> }));

import { App } from './App';

describe('App', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.className = '';
    window.history.pushState({}, '', '/');
    vi.clearAllMocks();
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation(() => ({
        matches: false,
        media: '(prefers-color-scheme: dark)',
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
  });

  it('renders the shell, toggles theme, and opens help navigation', () => {
    render(<App />);

    expect(screen.getByText('AAP Bridge')).toBeInTheDocument();
    expect(screen.getByText('Jobs Page')).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Toggle dark mode'));
    expect(localStorage.getItem('theme')).toBe('dark');
    expect(document.documentElement.classList.contains('pf-v5-theme-dark')).toBe(true);

    fireEvent.click(screen.getByLabelText('help'));
    expect(screen.getByText('Documentation')).toBeInTheDocument();
    expect(screen.getByText('Source Code')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Planning'));
    fireEvent.click(screen.getByText('Migration'));
    expect(localStorage.getItem('nav-planning-open')).toBe('false');
    expect(localStorage.getItem('nav-migration-open')).toBe('false');
  });

  it('routes to settings', () => {
    window.history.pushState({}, '', '/settings');

    render(<App />);
    expect(screen.getByText('Dashboard Page')).toBeInTheDocument();
  });
});
