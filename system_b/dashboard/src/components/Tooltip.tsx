import type { ReactNode } from "react";

export interface TooltipRow {
  color?: string;
  name: string;
  value: string;
}

export interface TooltipState {
  x: number;
  y: number;
  title: string;
  rows: TooltipRow[];
}

/** Floating readout. Values lead (strong), names follow; series keyed by a
 * short line of the series color. Text is set via React text nodes only —
 * labels come from CSV/journal files and are untrusted. */
export function Tooltip({ state }: { state: TooltipState | null }): ReactNode {
  if (!state) return null;
  const pad = 14;
  const style: React.CSSProperties = {
    left: Math.min(state.x + pad, window.innerWidth - 190),
    top: Math.min(state.y + pad, window.innerHeight - 30 - state.rows.length * 20),
  };
  return (
    <div className="tooltip" style={style} role="status">
      <div className="t-title">{state.title}</div>
      {state.rows.map((r, i) => (
        <div className="t-row" key={i}>
          {r.color ? <span className="t-key" style={{ borderTopColor: r.color }} /> : null}
          <span className="t-val">{r.value}</span>
          <span className="t-name">{r.name}</span>
        </div>
      ))}
    </div>
  );
}
