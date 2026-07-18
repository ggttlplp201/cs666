import { useMemo, useRef, useState } from "react";
import { fmtDay, niceTicks } from "../lib/format";
import { Tooltip, type TooltipState } from "./Tooltip";

export interface LineSeries {
  name: string;
  color: string; // CSS color (var(--...) ok)
  /** value per day-index; null = gap (not plotted) */
  values: (number | null)[];
}

interface Props {
  days: string[];
  series: LineSeries[];
  height?: number;
  /** viewBox width — pass ~1120 inside full-width cards so text doesn't scale up */
  width?: number;
  valueFmt: (v: number) => string;
  /** draw a reference hairline at this y-value (e.g. 0 for IC) */
  referenceY?: number;
}

const M = { top: 10, right: 14, bottom: 22, left: 52 };

/** 2px lines, hairline solid grid, >=8px end markers with a 2px surface ring,
 * crosshair snapped to the nearest day with a one-tooltip-all-series readout. */
export function LineChart({ days, series, height = 200, width = 560, valueFmt, referenceY }: Props) {
  const W = width;
  const [hover, setHover] = useState<number | null>(null);
  const [tip, setTip] = useState<TooltipState | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const H = height;
  const iw = W - M.left - M.right;
  const ih = H - M.top - M.bottom;

  const { lo, hi } = useMemo(() => {
    const all = series.flatMap((s) => s.values.filter((v): v is number => v !== null && Number.isFinite(v)));
    if (referenceY !== undefined) all.push(referenceY);
    if (all.length === 0) return { lo: 0, hi: 1 };
    let lo = Math.min(...all);
    let hi = Math.max(...all);
    if (lo === hi) {
      lo -= 1;
      hi += 1;
    }
    const pad = (hi - lo) * 0.06;
    return { lo: lo - pad, hi: hi + pad };
  }, [series, referenceY]);

  const x = (i: number) => M.left + (days.length <= 1 ? 0 : (i / (days.length - 1)) * iw);
  const y = (v: number) => M.top + ih - ((v - lo) / (hi - lo)) * ih;
  const ticks = niceTicks(lo, hi, 4);

  const path = (values: (number | null)[]) => {
    let d = "";
    let pen = false;
    values.forEach((v, i) => {
      if (v === null || !Number.isFinite(v)) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || days.length === 0) return;
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.round(((px - M.left) / iw) * (days.length - 1));
    const idx = Math.max(0, Math.min(days.length - 1, i));
    setHover(idx);
    setTip({
      x: e.clientX,
      y: e.clientY,
      title: fmtDay(days[idx]),
      rows: series
        .filter((s) => s.values[idx] !== null && Number.isFinite(s.values[idx] as number))
        .map((s) => ({ color: s.color, name: s.name, value: valueFmt(s.values[idx] as number) })),
    });
  };
  const onLeave = () => {
    setHover(null);
    setTip(null);
  };

  const lastIdx = (values: (number | null)[]) => {
    for (let i = values.length - 1; i >= 0; i--) {
      if (values[i] !== null && Number.isFinite(values[i] as number)) return i;
    }
    return -1;
  };

  return (
    <>
      {series.length >= 2 && (
        <div className="legend">
          {series.map((s) => (
            <span className="key" key={s.name}>
              <span className="line" style={{ borderTopColor: s.color }} />
              {s.name}
            </span>
          ))}
        </div>
      )}
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        onPointerMove={onMove}
        onPointerLeave={onLeave}
        role="img"
      >
        {/* hairline grid + clean ticks (text tokens, never series color) */}
        {ticks.map((t) => (
          <g key={t}>
            <line x1={M.left} x2={W - M.right} y1={y(t)} y2={y(t)} stroke="var(--grid)" strokeWidth={1} />
            <text x={M.left - 6} y={y(t) + 3.5} textAnchor="end" fontSize={10} fill="var(--text-muted)">
              {valueFmt(t)}
            </text>
          </g>
        ))}
        {referenceY !== undefined && (
          <line x1={M.left} x2={W - M.right} y1={y(referenceY)} y2={y(referenceY)} stroke="var(--baseline)" strokeWidth={1} />
        )}
        {/* x labels: first / middle / last (edge labels anchored inward so
            they never clip at the viewBox) */}
        {days.length > 0 &&
          [0, Math.floor((days.length - 1) / 2), days.length - 1]
            .filter((v, i, a) => a.indexOf(v) === i)
            .map((i) => (
              <text
                key={i}
                x={x(i)}
                y={H - 6}
                textAnchor={i === 0 ? "start" : i === days.length - 1 ? "end" : "middle"}
                fontSize={10}
                fill="var(--text-muted)"
              >
                {fmtDay(days[i])}
              </text>
            ))}
        {/* series: 2px round-joined lines; end marker r=4 with surface ring */}
        {series.map((s) => {
          const li = lastIdx(s.values);
          return (
            <g key={s.name}>
              <path d={path(s.values)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
              {li >= 0 && (
                <circle cx={x(li)} cy={y(s.values[li] as number)} r={4} fill={s.color} stroke="var(--surface-1)" strokeWidth={2} />
              )}
            </g>
          );
        })}
        {/* crosshair + snapped markers */}
        {hover !== null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={M.top} y2={M.top + ih} stroke="var(--baseline)" strokeWidth={1} />
            {series.map((s) =>
              s.values[hover] !== null && Number.isFinite(s.values[hover] as number) ? (
                <circle key={s.name} cx={x(hover)} cy={y(s.values[hover] as number)} r={4} fill={s.color} stroke="var(--surface-1)" strokeWidth={2} />
              ) : null,
            )}
          </g>
        )}
      </svg>
      <Tooltip state={tip} />
    </>
  );
}
