interface Props {
  label: string;
  value: string;
  /** signed delta text; goodness decides the color (direction x whether up is good) */
  delta?: { text: string; good: boolean };
}

export function StatTile({ label, value, delta }: Props) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {delta && <div className={`delta ${delta.good ? "good" : "bad"}`}>{delta.text}</div>}
    </div>
  );
}

export function GateTile({ pass }: { pass: boolean | undefined }) {
  return (
    <div className="tile">
      <div className="label">Go-live gate</div>
      <div
        className="badge"
        style={{ color: pass ? "var(--status-good)" : "var(--status-critical)" }}
      >
        <span aria-hidden>{pass ? "✓" : "✕"}</span>
        {pass === undefined ? "—" : pass ? "PASS" : "HOLD"}
      </div>
      <div className="delta" style={{ color: "var(--text-muted)" }}>
        {pass ? "net edge cleared" : "paper mode only"}
      </div>
    </div>
  );
}
