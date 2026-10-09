/**
 * Minimal ANSI-to-HTML renderer in the spirit of ansible-ui's
 * `@ansible/common-ui/Ansi` (used by AWX JobOutputRow to colorize stdout).
 * Implemented here against the MIT-licensed `anser` package so the UI does
 * not depend on the ansible-ui monorepo. Escape keying mirrors AWX: one span
 * per styled run, whitespace preserved by CSS.
 */
import anser from 'anser';
import { useMemo } from 'react';

export function Ansi({ input }: { input: string }) {
  const runs = useMemo(() => anser.ansiToJson(input, { use_classes: false }), [input]);
  if (runs.length === 0) {
    return <>{input || ' '}</>;
  }
  return (
    <>
      {runs.map((run, i) => {
        const style: React.CSSProperties = {};
        if (run.fg) {
          style.color = `rgb(${run.fg})`;
        }
        if (run.bg) {
          style.backgroundColor = `rgb(${run.bg})`;
        }
        if (run.decorations?.includes('bold')) {
          style.fontWeight = 'bold';
        }
        if (run.decorations?.includes('italic')) {
          style.fontStyle = 'italic';
        }
        if (run.decorations?.includes('underline')) {
          style.textDecoration = 'underline';
        }
        return (
          <span key={i} style={Object.keys(style).length ? style : undefined}>
            {run.content || ' '}
          </span>
        );
      })}
    </>
  );
}
