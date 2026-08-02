import { useCallback, useMemo, useState } from "react";
import sampleJson from "./data/sample.json";
import greedyJson from "./data/sample_greedy_tuned.json";
import { BarChart } from "./components/BarChart";
import { Blotter } from "./components/Blotter";
import { LineChart } from "./components/LineChart";
import { RegimeStrip } from "./components/RegimeStrip";
import { GateTile, StatTile } from "./components/StatTile";
import { TradesTable } from "./components/TradesTable";
import { fmtMoney, fmtNum, fmtPct, rollingMean } from "./lib/format";
import { loadRunFromFiles } from "./lib/parse";
import type { RunData } from "./types";

/** Bundled runs, so the two strategies can be compared without loading files.
 * Drag & drop still overrides both. */
const BUNDLED: { label: string; run: RunData }[] = [
  { label: "positional", run: sampleJson as unknown as RunData },
  { label: "greedy", run: greedyJson as unknown as RunData },
];

function useThemeToggle(): () => void {
  return useCallback(() => {
    const cur = document.documentElement.dataset.theme;
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const effective = cur ?? (prefersDark ? "dark" : "light");
    document.documentElement.dataset.theme = effective === "dark" ? "light" : "dark";
  }, []);
}

export default function App() {
  // index, not identity: two bundled runs can share a name, and comparing by
  // name made every matching button render as active
  const [bundledIdx, setBundledIdx] = useState<number | null>(0);
  const [run, setRun] = useState<RunData>(BUNDLED[0].run);
  const [drag, setDrag] = useState(false);
  const toggleTheme = useThemeToggle();

  const onFiles = useCallback(async (files: File[]) => {
    if (files.length === 0) return;
    const loaded = await loadRunFromFiles(files);
    const n = files.map((f) => f.name).sort().join(", ");
    setBundledIdx(null);
    setRun({ ...loaded, name: `loaded: ${n.slice(0, 60)}${n.length > 60 ? "…" : ""}` });
  }, []);

  const s = run.summary;
  const equityDays = run.equity.map((p) => p.day);
  const equityValues: (number | null)[] = run.equity.map((p) => p.equity);

  // group the blotter by exit rule, biggest group first: which rule is doing
  // the trading is the first thing you want to see on a new strategy
  const blotterGroups = useMemo(() => {
    const by = new Map<string, typeof run.trades>();
    for (const t of run.trades) {
      const k = t.exit_reason || "(no rule)";
      if (!by.has(k)) by.set(k, []);
      by.get(k)!.push(t);
    }
    return [...by.entries()]
      .map(([rule, trades]) => ({ rule, trades }))
      .sort((a, b) => b.trades.length - a.trades.length);
  }, [run.trades]);

  const icDays = run.rankIc.map((p) => p.day);
  const icDaily: number[] = run.rankIc.map((p) => p.ic);
  const icRolling = useMemo(() => rollingMean(icDaily, 30), [icDaily]);

  const pnlByExit = useMemo(() => {
    const acc = new Map<string, number>();
    for (const t of run.trades) acc.set(t.exit_reason, (acc.get(t.exit_reason) ?? 0) + t.pnl);
    return [...acc.entries()]
      .map(([label, value]) => ({ label, value }))
      .sort((a, b) => b.value - a.value);
  }, [run.trades]);

  const deployedDays = run.cycles.map((c) => c.day);
  const deployedValues: (number | null)[] = run.cycles.map((c) =>
    Number.isFinite(c.deployed_pct) ? c.deployed_pct : null,
  );

  return (
    <div
      className={`app ${drag ? "dropzone" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDrag(false);
        void onFiles([...e.dataTransfer.files]);
      }}
    >
      <div className="topbar">
        <h1>System B — positional value/trend</h1>
        <span className="run-name">{run.name}</span>
        <span className="spacer" />
        <span className="run-picker">
          {BUNDLED.map((b, i) => (
            <button
              key={b.label}
              className={bundledIdx === i ? "active" : ""}
              onClick={() => {
                setBundledIdx(i);
                setRun(b.run);
              }}
            >
              {b.label}
            </button>
          ))}
        </span>
        <label className="load">
          load run files
          <input
            type="file"
            multiple
            hidden
            onChange={(e) => void onFiles([...(e.target.files ?? [])])}
          />
        </label>
        <button onClick={toggleTheme}>light / dark</button>
      </div>
      <p className="subtitle">
        Walk-forward paper results net of 1.5% fee, slippage, T+7 lock and T+7 settlement.
        Drop a run folder's files anywhere on this page to inspect another run.
      </p>

      <div className="kpis">
        <StatTile
          label="Final equity"
          value={fmtMoney(s.final_equity)}
          delta={{
            text: `${fmtPct(s.total_return, 2)} total`,
            good: (s.total_return ?? 0) >= 0,
          }}
        />
        <StatTile
          label="Annualized return"
          value={fmtPct(s.annualized_return, 2)}
          delta={{ text: `Sharpe ${fmtNum(s.sharpe, 2)}`, good: (s.annualized_return ?? 0) >= 0 }}
        />
        <StatTile label="Max drawdown" value={fmtPct(s.max_drawdown, 2)} />
        <StatTile
          label="Closed trades"
          value={String(s.n_trades_closed ?? "—")}
          delta={{
            text: `win rate ${fmtPct(s.win_rate)}`,
            good: (s.win_rate ?? 0) >= 0.5,
          }}
        />
        <StatTile
          label="Avg trade (net)"
          value={fmtPct(s.avg_trade_return_net, 2)}
          delta={{
            text: `median hold ${fmtNum(s.median_hold_days, 0)}d`,
            good: (s.avg_trade_return_net ?? 0) >= 0,
          }}
        />
        <StatTile
          label="Rank IC (mean)"
          value={fmtNum(s.rank_ic_mean, 3)}
          delta={{
            text: `${s.rank_ic_days ?? 0} days · ${s.n_refits ?? 0} refits`,
            good: (s.rank_ic_mean ?? 0) > 0,
          }}
        />
        <GateTile pass={s.go_live_gate_pass} />
      </div>

      <div className="grid">
        <div className="card wide">
          <h2>Equity curve</h2>
          <p className="sub">
            marked at exit-side bids net of fee · {s.start ?? "?"} → {s.end ?? "?"} · model: {s.model_type ?? "?"}
          </p>
          <LineChart
            days={equityDays}
            series={[{ name: "equity", color: "var(--accent)", values: equityValues }]}
            valueFmt={(v) => fmtMoney(v)}
            height={220}
            width={1120}
          />
        </div>

        <div className="card wide">
          <h2>Market regime & deployment</h2>
          <p className="sub">
            the regime gate sets the deployment ceiling (bull 80% · sideways 50% · bear 30% · weak 20%)
          </p>
          <LineChart
            days={deployedDays}
            series={[{ name: "deployed %", color: "var(--accent)", values: deployedValues }]}
            valueFmt={(v) => fmtPct(v, 1)}
            height={120}
            width={1120}
          />
          <RegimeStrip cycles={run.cycles} />
        </div>

        <div className="card">
          <h2>Walk-forward rank IC</h2>
          <p className="sub">daily Spearman IC of model rank vs realized forward return</p>
          <LineChart
            days={icDays}
            series={[
              { name: "daily IC", color: "var(--baseline)", values: icDaily },
              { name: "30d mean", color: "var(--accent)", values: icRolling },
            ]}
            valueFmt={(v) => fmtNum(v, 2)}
            referenceY={0}
            height={190}
          />
        </div>

        <div className="card">
          <h2>Net P&L by exit rule</h2>
          <p className="sub">realized CNY per exit reason, fees included</p>
          <BarChart rows={pnlByExit} valueFmt={(v) => fmtMoney(v)} diverging />
        </div>

        <div className="card wide">
          <h2>Model feature importances</h2>
          <p className="sub">latest walk-forward refit — which features carry the ranker</p>
          {run.importances.length > 0 ? (
            <BarChart
              rows={run.importances.slice(0, 12).map((r) => ({ label: r.feature, value: r.value }))}
              valueFmt={(v) => fmtNum(v, 3)}
              width={1120}
            />
          ) : (
            <p className="sub">No refits in this run (model never trained).</p>
          )}
        </div>

        <div className="card wide">
          <h2>Closed lots</h2>
          <p className="sub">the trade journal's attribution — every exit carries its rule</p>
          <TradesTable trades={run.trades} />
        </div>
      </div>

      <h2 className="section-head">Trade blotter</h2>
      <p className="section-sub">
        Every closed lot as a line: what was bought and sold, on which dates, at which prices,
        and the delta. <strong>Δ price</strong> is the gross move a chart would show you;{" "}
        <strong>Δ net</strong> is what the book actually received after the {fmtPct(0.015, 1)} BUFF
        sell fee and slippage. The gap between the two columns is the round-trip cost floor
        (~{fmtPct(0.0587, 1)} on real history), which is why a positive price move can still be a
        losing trade.
      </p>

      <div className="grid">
        <div className="card wide">
          <h2>All closed lots</h2>
          <p className="sub">oldest first</p>
          <Blotter trades={run.trades} />
        </div>

        {blotterGroups.map((g) => (
          <div className="card wide" key={g.rule}>
            <h2>{g.rule}</h2>
            <p className="sub">
              {g.trades.length} {g.trades.length === 1 ? "lot" : "lots"} exited by this rule
            </p>
            <Blotter trades={g.trades} />
          </div>
        ))}
      </div>

      <p className="footer-note">
        Reads the System B engine's run artifacts (equity.csv, rank_ic.csv, attribution.csv,
        feature_importances.csv, summary.json, journal.jsonl). Bundled sample is a synthetic-market
        backtest — not live trading results.
      </p>
    </div>
  );
}
