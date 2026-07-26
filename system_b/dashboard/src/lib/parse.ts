/** Parsers for the engine's raw artifact files (drag a run folder's files in). */

import type { CyclePoint, EquityPoint, IcPoint, ImportanceRow, RunData, Summary, Trade } from "../types";

/** Minimal CSV parser — the engine's artifacts are plain pandas to_csv output
 * (no embedded newlines; quotes only around fields containing commas). */
export function parseCsv(text: string): Record<string, string>[] {
  const lines = text.replace(/\r/g, "").split("\n").filter((l) => l.length > 0);
  if (lines.length < 2) return [];
  const header = splitCsvLine(lines[0]);
  return lines.slice(1).map((line) => {
    const cells = splitCsvLine(line);
    const row: Record<string, string> = {};
    header.forEach((h, i) => {
      row[h] = cells[i] ?? "";
    });
    return row;
  });
}

function splitCsvLine(line: string): string[] {
  const out: string[] = [];
  let cur = "";
  let inQ = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inQ) {
      if (c === '"' && line[i + 1] === '"') {
        cur += '"';
        i++;
      } else if (c === '"') {
        inQ = false;
      } else {
        cur += c;
      }
    } else if (c === '"') {
      inQ = true;
    } else if (c === ",") {
      out.push(cur);
      cur = "";
    } else {
      cur += c;
    }
  }
  out.push(cur);
  return out;
}

const num = (s: string | undefined): number => (s === undefined || s === "" ? NaN : Number(s));

export function parseEquityCsv(text: string): EquityPoint[] {
  return parseCsv(text)
    .map((r) => ({ day: (r["day"] ?? r[""] ?? "").slice(0, 10), equity: num(r["equity"]) }))
    .filter((p) => p.day && Number.isFinite(p.equity));
}

export function parseRankIcCsv(text: string): IcPoint[] {
  return parseCsv(text)
    .map((r) => ({ day: (r["day"] ?? r[""] ?? "").slice(0, 10), ic: num(r["spearman_ic"]) }))
    .filter((p) => p.day && Number.isFinite(p.ic));
}

export function parseAttributionCsv(text: string): Trade[] {
  return parseCsv(text).map((r) => ({
    item: r["item"] ?? "",
    entry_rule: r["entry_rule"] ?? "",
    exit_reason: r["exit_reason"] ?? "",
    hold_days: Number.isFinite(num(r["hold_days"])) ? num(r["hold_days"]) : null,
    pnl: num(r["pnl"]),
    ret_pct: Number.isFinite(num(r["ret_pct"])) ? num(r["ret_pct"]) : null,
  }));
}

/** feature_importances.csv: one row per refit (day index), one column per feature.
 * The dashboard shows the LATEST refit's importances. */
export function parseImportancesCsv(text: string): ImportanceRow[] {
  const rows = parseCsv(text);
  if (rows.length === 0) return [];
  const last = rows[rows.length - 1];
  return Object.entries(last)
    .filter(([k]) => k !== "" && k !== "day")
    .map(([feature, v]) => ({ feature, value: num(v) }))
    .filter((r) => Number.isFinite(r.value))
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
}

export function parseJournalJsonl(text: string): CyclePoint[] {
  const out: CyclePoint[] = [];
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    try {
      const rec = JSON.parse(t) as Record<string, unknown>;
      if (rec["kind"] !== "cycle") continue;
      out.push({
        day: String(rec["day"] ?? "").slice(0, 10),
        regime: String(rec["regime"] ?? "sideways"),
        equity: Number(rec["equity"] ?? NaN),
        cash: Number(rec["cash"] ?? NaN),
        deployed_pct: Number(rec["deployed_pct"] ?? NaN),
      });
    } catch {
      /* skip malformed journal lines */
    }
  }
  return out.filter((c) => c.day);
}

/** Assemble a RunData from a dropped set of files (any subset renders). */
export async function loadRunFromFiles(files: File[]): Promise<RunData> {
  const byName = new Map(files.map((f) => [f.name.toLowerCase(), f]));
  const read = async (name: string): Promise<string | null> => {
    const f = byName.get(name);
    return f ? f.text() : null;
  };
  const [summaryTxt, equityTxt, icTxt, attrTxt, impTxt, journalTxt] = await Promise.all([
    read("summary.json"),
    read("equity.csv"),
    read("rank_ic.csv"),
    read("attribution.csv"),
    read("feature_importances.csv"),
    read("journal.jsonl"),
  ]);
  return {
    name: "loaded run",
    summary: summaryTxt ? (JSON.parse(summaryTxt) as Summary) : {},
    equity: equityTxt ? parseEquityCsv(equityTxt) : [],
    rankIc: icTxt ? parseRankIcCsv(icTxt) : [],
    trades: attrTxt ? parseAttributionCsv(attrTxt) : [],
    importances: impTxt ? parseImportancesCsv(impTxt) : [],
    cycles: journalTxt ? parseJournalJsonl(journalTxt) : [],
  };
}
