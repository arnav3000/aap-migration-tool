import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Ansi } from './Ansi';

describe('Ansi', () => {
  it('renders plain text unchanged', () => {
    render(<Ansi input="plain output" />);

    expect(screen.getByText('plain output')).toBeInTheDocument();
  });

  it('renders ANSI color and decoration classes', () => {
    render(<Ansi input={'\u001b[31mred text\u001b[0m \u001b[1mbold text\u001b[0m'} />);

    expect(screen.getByText('red text')).toHaveClass('ansi-red-fg');
    expect(screen.getByText('bold text')).toHaveClass('ansi-bold');
  });
});
