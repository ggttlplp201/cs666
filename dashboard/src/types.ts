/** Shapes of the System B engine's run artifacts (runs/<stamp>/ directory). */

export interface Summary {
  start?: string;
  end?: string;
  total_return?: number;
  annualized_return?: number;
  max_drawdown?: number;
  ann_vol?: number;
  sharpe?: number;
  n_trades_closed?: number;
  win_rate?: number;
  profit_factor?: number;
  realized_pnl?: number;
  final_equity?: number;
  avg_trade_return_net?: number | null;
  median_trade_return_net?: number | null;
  median_hold_days?: number | null;
  rank_ic_mean?: number | null;
  rank_ic_days?: number;
  n_refits?: number;
  model_type?: string;
  source?: string;
  go_live_gate_pass?: boolean;
  [key: string]: unknown;
}

export interface EquityPoint {
  day: string;
  equity: number;
}

export interface IcPoint {
  day: string;
  ic: number;
}

export interface Trade {
  item: string;
  entry_rule: string;
  exit_reason: string;
  hold_days: number | null;
  pnl: number;
  ret_pct: number | null;
}

export interface ImportanceRow {
  feature: string;
  value: number;
}

/** One "cycle" record from the decision journal (journal.jsonl). */
export interface CyclePoint {
  day: string;
  regime: string;
  equity: number;
  cash: number;
  deployed_pct: number;
}

export interface RunData {
  name: string;
  summary: Summary;
  equity: EquityPoint[];
  rankIc: IcPoint[];
  trades: Trade[];
  importances: ImportanceRow[];
  cycles: CyclePoint[];
}

export const REGIMES = ["bull", "sideways", "bear", "weak"] as const;
export type Regime = (typeof REGIMES)[number];
