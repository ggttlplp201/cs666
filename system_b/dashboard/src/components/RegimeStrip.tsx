import { useState } from "react";
import { fmtDay, fmtPct } from "../lib/format";
import type { CyclePoint } from "../types";
import { Tooltip, type TooltipState } from "./Tooltip";

/** Regimes are market STATES, so they wear the status palette (with the
 * mandated icon/label pairing via legend + tooltip — never color alone):
 * bull=good, weak=warning, bear=critical, sideways=neutral gray. */
const REGIME_COLOR: Record<string, string> = {
  bull: "var(--status-good)",
  sideways: "var(--baseline)",
  bear: "var(--status-critical)",
  weak: "var(--status-warning)",
};
const REGIME_LABEL: Record<string, string> = {
  bull: "Bull",
  sideways: "Sideways",
  bear: "Bear",
  weak: "Weak",
};

const W = 560;
const H = 26;

export function RegimeStrip({ cycles }: { cycles: CyclePoint[] }) {
  const [tip, setTip] = useState<TooltipState | null>(null);
  if (cycles.length === 0) return null;

  // merge consecutive same-regime days into segments
  const segs: { regime: string; from: number; to: number }[] = [];
  cycles.forEach((c, i) => {
    const last = segs[segs.length - 1];
    if (last && last.regime === c.regime) last.to = i;
    else segs.push({ regime: c.regime, from: i, to: i });
  });
  const x = (i: number) => (i / cycles.length) * W;

  return (
    <>
      <div className="legend">
        {Object.entries(REGIME_LABEL).map(([k, label]) => (
          <span className="key" key={k}>
            <span
              className="rect"
              style={{
                background:
                  k === "bear"
                    ? `repeating-linear-gradient(135deg, ${"rgba(0,0,0,0.45)"} 0 1px, transparent 1px 4px), var(--status-critical)`
                    : REGIME_COLOR[k],
              }}
            />
            {label}
          </span>
        ))}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" onPointerLeave={() => setTip(null)}>
        {/* bear/bull are a red-green pair — deutan-indistinguishable by hue, so
            bear additionally carries a 135° tone-on-tone hatch (the texture
            channel), and legend + tooltip always name the state */}
        <defs>
          <pattern id="bear-hatch" width={5} height={5} patternUnits="userSpaceOnUse" patternTransform="rotate(135)">
            <line x1={0} y1={0} x2={0} y2={5} stroke="rgba(0,0,0,0.45)" strokeWidth={1.4} />
          </pattern>
        </defs>
        {segs.map((s, i) => (
          <rect
            key={i}
            x={x(s.from)}
            y={4}
            width={Math.max(x(s.to + 1) - x(s.from) - 1, 1)}
            height={H - 8}
            rx={2}
            fill={REGIME_COLOR[s.regime] ?? "var(--baseline)"}
            onPointerMove={(e) => {
              const c0 = cycles[s.from];
              const c1 = cycles[s.to];
              setTip({
                x: e.clientX,
                y: e.clientY,
                title: `${fmtDay(c0.day)} – ${fmtDay(c1.day)}`,
                rows: [
                  { color: REGIME_COLOR[s.regime], name: "regime", value: REGIME_LABEL[s.regime] ?? s.regime },
                  { name: "deployed at end", value: fmtPct(c1.deployed_pct) },
                ],
              });
            }}
          />
        ))}
        {segs
          .filter((s) => s.regime === "bear")
          .map((s, i) => (
            <rect
              key={`h${i}`}
              x={x(s.from)}
              y={4}
              width={Math.max(x(s.to + 1) - x(s.from) - 1, 1)}
              height={H - 8}
              rx={2}
              fill="url(#bear-hatch)"
              pointerEvents="none"
            />
          ))}
      </svg>
      <Tooltip state={tip} />
    </>
  );
}
