import { describe, expect, it, vi } from 'vitest';

const render = vi.fn();
const createRoot = vi.fn(() => ({ render }));

vi.mock('react-dom/client', () => ({
  default: { createRoot },
  createRoot,
}));

vi.mock('./App', () => ({
  App: () => <div>App Component</div>,
}));

describe('main', () => {
  it('mounts the App into the root element', async () => {
    document.body.innerHTML = '<div id="root"></div>';

    await import('./main');

    expect(createRoot).toHaveBeenCalledWith(document.getElementById('root'));
    expect(render).toHaveBeenCalledTimes(1);
  });
});
