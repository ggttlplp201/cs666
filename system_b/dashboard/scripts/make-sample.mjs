#!/usr/bin/env node
/**
 * Bundle a System B run directory (runs/<stamp>/) into src/data/sample.json.
 *
 *   node scripts/make-sample.mjs <run-dir> [name]
 *
 * The dashboard also parses these raw files directly via drag & drop; this
 * script just refreshes the bundled out-of-the-box sample.
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const runDir = process.argv[2];
const name = process.argv[3] ?? "sample backtest";
if (!runDir) {
  console.error("usage: node scripts/make-sample.mjs <run-dir> [name]");
  process.exit(1);
}

const read = (f) => (existsSync(join(runDir, f)) ? readFileSync(join(runDir, f), "utf8") : null);

const csv = (text) => {
  const lines = text.replace(/\r/g, "").split("\n").filter(Boolean);
  const header = lines[0].split(",");
  return lines.slice(1).map((l) => {
    // naive split is fine for our artifacts except quoted item names
    const cells = l.match(/("([^"]|"")*"|[^,]*)(,|$)/g).map((c) =>
      c.replace(/,$/, "").replace(/^"|"$/g, "").replace(/""/g, '"'),
    );
    return Object.fromEntries(header.map((h, i) => [h, cells[i] ?? ""]));
  });
};

const summaryTxt = read("summary.json");
const equityTxt = read("equity.csv");
const icTxt = read("rank_ic.csv");
const attrTxt = read("attribution.csv");
const impTxt = read("feature_importances.csv");
const journalTxt = read("journal.jsonl");

const num = (s) => (s === "" || s === undefined ? null : Number(s));

const equity = equityTxt
  ? csv(equityTxt).map((r) => ({ day: (r.day ?? r[""]).slice(0, 10), equity: Number(r.equity) }))
  : [];
const rankIc = icTxt
  ? csv(icTxt).map((r) => ({ day: (r.day ?? r[""]).slice(0, 10), ic: Number(r.spearman_ic) }))
  : [];
const trades = attrTxt
  ? csv(attrTxt).map((r) => ({
      item: r.item,
      entry_rule: r.entry_rule,
      exit_reason: r.exit_reason,
      hold_days: num(r.hold_days),
      pnl: Number(r.pnl),
      ret_pct: num(r.ret_pct),
    }))
  : [];
let importances = [];
if (impTxt) {
  const rows = csv(impTxt);
  const last = rows[rows.length - 1] ?? {};
  importances = Object.entries(last)
    .filter(([k]) => k && k !== "day" && k !== "")
    .map(([feature, v]) => ({ feature, value: Number(v) }))
    .filter((r) => Number.isFinite(r.value))
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
}
const cycles = [];
if (journalTxt) {
  for (const line of journalTxt.split("\n")) {
    if (!line.trim()) continue;
    try {
      const rec = JSON.parse(line);
      if (rec.kind !== "cycle") continue;
      cycles.push({
        day: String(rec.day).slice(0, 10),
        regime: rec.regime,
        equity: rec.equity,
        cash: rec.cash,
        deployed_pct: rec.deployed_pct,
      });
    } catch {
      /* skip */
    }
  }
}

const out = {
  name,
  summary: summaryTxt ? JSON.parse(summaryTxt) : {},
  equity,
  rankIc,
  trades,
  importances,
  cycles,
};

const here = dirname(fileURLToPath(import.meta.url));
const dest = join(here, "..", "src", "data", "sample.json");
writeFileSync(dest, JSON.stringify(out));
console.log(
  `wrote ${dest}: ${equity.length} equity pts, ${cycles.length} cycles, ${trades.length} trades, ${rankIc.length} IC pts`,
);
