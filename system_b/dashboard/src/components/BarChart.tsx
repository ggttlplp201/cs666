import { useMemo, useState } from "react";
import { Tooltip, type TooltipState } from "./Tooltip";

export interface BarRow {
  label: string;
  value: number;
}

interface Props {
  rows: BarRow[];
  valueFmt: (v: number) => string;
  /** diverging: negatives left in the cool/warm pair; else single sequential hue */
  diverging?: boolean;
  maxRows?: number;
  /** viewBox width — pass ~1120 inside full-width cards so text doesn't scale up */
  width?: number;
}

const BAR_H = 18; // <= 24px cap
const GAP = 8;

function barPath(x0: number, x1: number, yTop: number, h: number, roundRight: boolean): string {
  // 4px rounded DATA end, square at the baseline end
  const r = Math.min(4, Math.abs(x1 - x0));
  if (roundRight) {
    return `M${x0},${yTop} H${x1 - r} Q${x1},${yTop} ${x1},${yTop + r} V${yTop + h - r} Q${x1},${yTop + h} ${x1 - r},${yTop + h} H${x0} Z`;
  }
  return `M${x1},${yTop} H${x0 + r} Q${x0},${yTop} ${x0},${yTop + r} V${yTop + h - r} Q${x0},${yTop + h} ${x0 + r},${yTop + h} H${x1} Z`;
}

/** Horizontal bars: thin marks, rounded data-ends, value labels at the tips in
 * text ink, per-mark hover tooltip, single baseline. */
export function BarChart({ rows, valueFmt, diverging = false, maxRows = 12, width = 560 }: Props) {
  const W = width;
  const M = { top: 4, right: 64, bottom: 4, left: Math.min(220, W * 0.3) };
  const [tip, setTip] = useState<TooltipState | null>(null);
  const [hot, setHot] = useState<number | null>(null);
  const shown = rows.slice(0, maxRows);

  const H = M.top + M.bottom + shown.length * (BAR_H + GAP) - (shown.length ? GAP : 0);
  const iw = W - M.left - M.right;

  const { lo, hi } = useMemo(() => {
    const vals = shown.map((r) => r.value);
    const hi = Math.max(0, ...vals);
    const lo = Math.min(0, ...vals);
    return lo === hi ? { lo: 0, hi: 1 } : { lo, hi };
  }, [shown]);

  const x = (v: number) => M.left + ((v - lo) / (hi - lo)) * iw;
  const zero = x(0);

  const color = (v: number) =>
    diverging ? (v < 0 ? "var(--diverge-neg)" : "var(--diverge-pos)") : "var(--seq-450)";

  return (
    <>
      <svg viewBox={`0 0 ${W} ${Math.max(H, 24)}`} role="img" onPointerLeave={() => { setTip(null); setHot(null); }}>
        {/* single baseline at zero */}
        <line x1={zero} x2={zero} y1={0} y2={H} stroke="var(--baseline)" strokeWidth={1} />
        {shown.map((r, i) => {
          const yTop = M.top + i * (BAR_H + GAP);
          const neg = r.value < 0;
          const x0 = neg ? x(r.value) : zero;
          const x1 = neg ? zero : x(r.value);
          // negative rows: the area right of the zero baseline is free — put
          // the value there instead of colliding with the category labels
          const labelX = neg ? zero + 6 : x1 + 6;
          return (
            <g
              key={r.label}
              onPointerMove={(e) =>
                {
                  setHot(i);
                  setTip({ x: e.clientX, y: e.clientY, title: r.label, rows: [{ color: color(r.value), name: "", value: valueFmt(r.value) }] });
                }
              }
            >
              {/* oversized transparent hit target */}
              <rect x={0} y={yTop - GAP / 2} width={W} height={BAR_H + GAP} fill="transparent" />
              <path
                d={neg ? barPath(x0, x1, yTop, BAR_H, false) : barPath(x0, x1, yTop, BAR_H, true)}
                fill={color(r.value)}
                opacity={hot === null || hot === i ? 1 : 0.75}
              />
              <text
                x={M.left - 8}
                y={yTop + BAR_H / 2 + 3.5}
                textAnchor="end"
                fontSize={11}
                fill="var(--text-secondary)"
              >
                {r.label.length > 26 ? r.label.slice(0, 25) + "…" : r.label}
              </text>
              <text
                x={labelX}
                y={yTop + BAR_H / 2 + 3.5}
                textAnchor="start"
                fontSize={11}
                fill="var(--text-primary)"
              >
                {valueFmt(r.value)}
              </text>
            </g>
          );
        })}
      </svg>
      <Tooltip state={tip} />
    </>
  );
}
