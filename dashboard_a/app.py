"""System A research dashboard — READ-ONLY by construction.

No trading controls, no config mutation, no secrets (never touches .env).
Reads: var/market.db, var/provenance_a.jsonl, config/*.yaml, and recomputes
the event/spread studies from the same modules the CLI uses (single source
of truth — no stale report files).

Launch:  make dashboard   (or: .venv/bin/streamlit run dashboard_a/app.py)
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# Muted, CVD-safe accents — highlight (teal) vs recessive (gray); signed P&L
# uses a diverging green/red with a neutral zero, always paired with value labels.
HILITE, MUTED = "#0d9488", "#94a3b8"
POS, NEG = "#15803d", "#b91c1c"


def _style(chart):
    """Shared clean styling: no chart junk, recessive axes, consistent type."""
    return (chart
            .configure_view(strokeWidth=0)
            .configure_axis(grid=False, labelColor="#64748b", titleColor="#64748b",
                            labelFontSize=12, titleFontSize=11, domainColor="#e2e8f0",
                            tickColor="#e2e8f0")
            .configure_axisY(domain=False, ticks=False))


def _magnitude_bar(df, cat, val, title, highlight=None):
    """Horizontal magnitude bars, direct value labels, headline bar highlighted."""
    df = df.copy()
    df["_c"] = [HILITE if (highlight and c == highlight) else MUTED for c in df[cat]]
    df["_lbl"] = df[val].map(lambda v: f"{v:+.0%}")
    pad = max(abs(df[val].max()), abs(df[val].min())) * 0.18 + 0.02
    base = alt.Chart(df).encode(
        y=alt.Y(f"{cat}:N", sort=None, title=None),
        x=alt.X(f"{val}:Q", title=title, axis=alt.Axis(format="+%"),
                scale=alt.Scale(domain=[min(0, df[val].min()) - pad,
                                        df[val].max() + pad])),
    )
    bars = base.mark_bar(height=20, cornerRadiusEnd=3).encode(
        color=alt.Color("_c:N", scale=None, legend=None),
        tooltip=[cat, alt.Tooltip(f"{val}:Q", format="+.1%")],
    )
    labels = base.mark_text(align="left", dx=5, fontSize=12, fontWeight=600,
                            color="#334155").encode(text="_lbl:N")
    return _style((bars + labels).properties(height=len(df) * 40 + 8))


def _signed_bar(df, cat, val):
    """Diverging signed-return bars (green up / red down), zero baseline."""
    df = df.copy()
    df["_c"] = [POS if v >= 0 else NEG for v in df[val]]
    base = alt.Chart(df).encode(
        x=alt.X(f"{cat}:N", sort="-y", title=None,
                axis=alt.Axis(labelAngle=-40, labelLimit=160)),
        y=alt.Y(f"{val}:Q", title="net return", axis=alt.Axis(format="+%")),
    )
    bars = base.mark_bar(width=14, cornerRadiusEnd=2, opacity=0.9).encode(
        color=alt.Color("_c:N", scale=None, legend=None),
        tooltip=[cat, alt.Tooltip(f"{val}:Q", format="+.1%")],
    )
    zero = alt.Chart(df).mark_rule(color="#cbd5e1", strokeWidth=1).encode(
        y=alt.datum(0))
    return _style((zero + bars).properties(height=300))

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from shared.configuration import Config                     # noqa: E402
from shared.store import SnapshotStore                      # noqa: E402
from system_a.event_study import run_event_study            # noqa: E402
from system_a.rules import RulesTable                       # noqa: E402
from system_a.spread_study import cross_spread_net, spread_stats  # noqa: E402

st.set_page_config(page_title="System A — research dashboard", layout="wide")


# ------------------------------------------------------------------ #
@st.cache_resource
def load_config() -> Config:
    return Config.load(REPO_ROOT, system="system_a")


def open_store(config: Config) -> SnapshotStore:
    return SnapshotStore(
        REPO_ROOT / config.require("data.snapshot_poller")["db_path"]
    )


@st.cache_data(ttl=60)
def buff_frame() -> pd.DataFrame:
    config = load_config()
    store = open_store(config)
    return pd.read_sql_query(
        "SELECT market_hash_name, ts, lowest_sell, highest_buy, listing_count,"
        " buy_order_count FROM snapshots WHERE source='buff' ORDER BY ts",
        store.conn,
    )


@st.cache_data(ttl=300)
def study_results():
    config = load_config()
    store = open_store(config)
    rules = RulesTable.load(REPO_ROOT / config.require("system_a.rules_table_path"))
    seed = REPO_ROOT / config.require("data.steam_history")["items_file"]
    universe = sorted(
        {l.strip() for l in seed.read_text().splitlines() if l.strip()}
    )
    outcomes, scores, notes = run_event_study(
        rules, store, universe,
        lock_days=config.require("cooldown.trade_lock_days"),
        buff_fee_pct=config.require("costs.buff_fee_pct"),
        buff_fee_history=config.get("costs.fee_history", []),
        steam_fee_pct=config.require("costs.steam_fee_pct"),
    )
    return rules, outcomes, scores, notes


def fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# ------------------------------------------------------------------ #
# STATUS BANNER — unmissable operating state on every page
# ------------------------------------------------------------------ #
config = load_config()
gating = config.get("system_a.rules_gating", {}) or {}
disabled_count = len(gating.get("disabled_rules", [])) + len(
    gating.get("disabled_pairs", [])
)
paper = config.require("execution.paper_mode")
all_directional_disabled = "weapon_balance_change" in gating.get("disabled_rules", [])
mode = "LOG-ONLY" if all_directional_disabled else ("PAPER" if paper else "LIVE")
banner_color = {"LOG-ONLY": "🟡", "PAPER": "🟠", "LIVE": "🔴"}[mode]
st.markdown(
    f"{banner_color} **{mode}** · $0 spent · {disabled_count} rules gated off · "
    f"read-only · BUFF fee {config.require('costs.buff_fee_pct'):.1%} · "
    f"T+{config.require('cooldown.trade_lock_days')} lock"
)

page = st.sidebar.radio(
    "Section",
    ["0 · Overview", "1 · Data health", "2 · Live market", "3 · Spread analysis",
     "4 · Rule scorecard", "5 · Event timeline", "6 · Prediction log",
     "7 · Trade-up class ★"],
)
frame = buff_frame()


@st.cache_data(ttl=300)
def trade_up_controls():
    """Event vs time-placebo vs broad-market for the 2025-10-22 trade-up event,
    from iflow BUFF data — the negative controls that made trade-up the one
    surviving System A play. Returns None if iflow data isn't loaded."""
    import statistics
    import random as _random
    config = load_config()
    store = open_store(config)
    if not store.counts_by_source().get("buff_iflow"):
        return None
    from system_a.collections import load_collection_map
    from system_a.event_study import _bar_after, _event_ts, DAY
    from system_a.spread_study import spread_stats
    cmap = load_collection_map(REPO_ROOT / "config" / "trade_up_collections.yaml")
    seed = REPO_ROOT / config.require("data.steam_history")["items_file"]
    universe = sorted({l.strip() for l in seed.read_text().splitlines() if l.strip()})
    spreads = {s.item: s.median for s in spread_stats(store, source="buff_iflow")}
    med_spread = statistics.median(spreads.values()) if spreads else 0.04
    ev = _event_ts("2025-10-22")
    fee = 0.025

    def held(series, ent, name):
        e = _bar_after(series, ent, max_delay_days=3.0)
        if not e:
            return None
        x = _bar_after(series, e[0] + 60 * DAY)
        if not x:
            return None
        s = spreads.get(name, med_spread)
        return x[1] * (1 - s / 2) * (1 - fee) / (e[1] * (1 + s / 2)) - 1

    reds = [i for i in universe if cmap.is_gold_case_covert(i)]
    event, placebo = [], []
    rng = _random.Random(11)
    for name in reds:
        series = store.series(name, source="buff_iflow")
        if not series:
            continue
        er = held(series, ev, name)
        if er is not None:
            event.append(er)
        lo, hi = series[0].ts, min(series[-1].ts - 65 * DAY, ev - 30 * DAY)
        for _ in range(8):
            if hi <= lo:
                break
            pr = held(series, rng.uniform(lo, hi), name)
            if pr is not None:
                placebo.append(pr)
    med = lambda x: statistics.median(x) if x else None
    return {
        "n_reds": len(reds), "map_verified": cmap.verified,
        "event_n": len(event), "event_med": med(event),
        "placebo_n": len(placebo), "placebo_med": med(placebo),
    }


# ------------------------------------------------------------------ #
if page == "0 · Overview":
    st.header("Overview")
    c1, c2, c3 = st.columns(3)
    c1.metric("Balance-patch trading", "not viable", "edge < spread",
              delta_color="inverse")
    c2.metric("Trade-up events", "viable", "+164% median · durable")
    c3.metric("Capital deployed", "$0", "paper / log-only")
    tu = trade_up_controls()
    if tu and tu["event_med"] is not None:
        chart_df = pd.DataFrame({
            "group": ["Trade-up event", "Random dates", "Broad market"],
            "net": [tu["event_med"], tu["placebo_med"] or 0, -0.07],
        })
        st.altair_chart(
            _magnitude_bar(chart_df, "group", "net", "median 60-day net return",
                           highlight="Trade-up event"),
            use_container_width=True,
        )
        st.caption("Median 60-day net return on BUFF (after spread + fee), "
                   "2025-10-22 trade-up event vs. two controls.")
    else:
        st.caption("Load iflow BUFF data (`python -m shared.iflow_history`) for "
                   "the trade-up chart.")

elif page == "1 · Data health":
    st.header("Data health")
    poller_ps = subprocess.run(
        ["pgrep", "-f", "system_a.runner --poll"], capture_output=True, text=True
    )
    pid = poller_ps.stdout.split()[0] if poller_ps.stdout.strip() else None
    last_ts = frame.ts.max() if not frame.empty else None
    age_min = (time.time() - last_ts) / 60 if last_ts else None
    refresh = config.require("data.refresh_seconds")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("launchd poller", f"alive (pid {pid})" if pid else "NOT RUNNING")
    c2.metric("last snapshot", f"{age_min:.0f} min ago" if age_min else "never")
    c3.metric("items tracked", frame.market_hash_name.nunique())
    c4.metric("total snapshots", len(frame))
    if not pid:
        st.error("Poller process not found — the series is going stale RIGHT NOW. "
                 "`launchctl print gui/$UID/com.leon.cs2quant.poller`")
    if age_min is not None and age_min * 60 > 2.5 * refresh:
        st.error(f"⚠ LAST SNAPSHOT IS {age_min:.0f} MINUTES OLD "
                 f"(cadence {refresh//60} min) — the series has a live gap.")

    store = open_store(config)
    gaps = store.gap_report("buff", expected_seconds=refresh)
    if gaps:
        st.error(f"⚠ {len(gaps)} GAP(S) IN THE STORED SERIES — a holed series "
                 "must not be trusted:")
        st.dataframe(pd.DataFrame(
            [(fmt_ts(a), fmt_ts(b), f"{s/3600:.1f}h") for a, b, s in gaps],
            columns=["gap start", "gap end", "duration"],
        ), use_container_width=True)
    else:
        st.success("No gaps > 2.5× cadence — series is continuous.")
    if not frame.empty:
        st.caption(f"coverage {fmt_ts(frame.ts.min())} → {fmt_ts(frame.ts.max())}")
        per_item = frame.groupby("market_hash_name").size().rename("snapshots")
        st.dataframe(per_item.to_frame(), use_container_width=True)

elif page == "2 · Live market":
    st.header("Live market")
    if frame.empty:
        st.warning("no poller data yet")
    else:
        latest = frame.sort_values("ts").groupby("market_hash_name").last()
        latest["spread_pct"] = (
            (latest.lowest_sell - latest.highest_buy) / latest.lowest_sell
        )
        latest["age_min"] = (time.time() - latest.ts) / 60
        refresh = config.require("data.refresh_seconds")
        latest["stale"] = latest.age_min * 60 > 2.5 * refresh
        show = latest[["lowest_sell", "highest_buy", "spread_pct",
                       "listing_count", "buy_order_count", "age_min", "stale"]]
        show.columns = ["ask ¥", "bid ¥", "spread", "listings", "bids",
                        "age (min)", "STALE"]
        st.dataframe(
            show.sort_values("spread"),
            use_container_width=True,
            column_config={"spread": st.column_config.NumberColumn(format="percent")},
        )
        if latest.stale.any():
            st.error(f"⚠ {int(latest.stale.sum())} item(s) stale")
        item = st.selectbox("history", sorted(frame.market_hash_name.unique()))
        history = frame[frame.market_hash_name == item].set_index(
            pd.to_datetime(frame[frame.market_hash_name == item].ts, unit="s")
        )
        c1, c2 = st.columns(2)
        c1.line_chart(history[["lowest_sell", "highest_buy"]])
        c2.line_chart(history[["listing_count", "buy_order_count"]])

elif page == "3 · Spread analysis":
    st.header("Spreads")
    store = open_store(config)
    stats = spread_stats(store)
    if not stats:
        st.warning("no poller data yet")
    else:
        fee = 0.025  # era fee for the studied OOS events (all pre-2026-04-14)
        table = pd.DataFrame(
            [{
                "item": s.item, "n": s.n, "spread median": s.median,
                "p25": s.p25, "p75": s.p75, "listings": s.median_listings,
                "bids": s.median_bids,
                "round-trip friction (spread + 2.5% fee)":
                    -cross_spread_net(0.0, s.median, fee),
            } for s in stats]
        ).set_index("item")
        medians = [s.median for s in stats]
        c1, c2, c3 = st.columns(3)
        c1.metric("median spread", f"{statistics.median(medians):.2%}")
        c2.metric("range", f"{min(medians):.2%} – {max(medians):.2%}")
        c3.metric("median round-trip friction",
                  f"{-cross_spread_net(0.0, statistics.median(medians), fee):.2%}")
        pct_cols = ["spread median", "p25", "p75",
                    "round-trip friction (spread + 2.5% fee)"]
        st.dataframe(
            table.sort_values("spread median"), use_container_width=True,
            column_config={c: st.column_config.NumberColumn(format="percent")
                           for c in pct_cols},
        )
        st.subheader("Spread vs. liquidity")
        scatter = pd.DataFrame(
            [{"listings": s.median_listings, "spread": s.median,
              "item": s.item.split(" (")[0]} for s in stats]
        )
        chart = alt.Chart(scatter).mark_circle(
            size=90, color=HILITE, opacity=0.7, stroke="white", strokeWidth=1
        ).encode(
            x=alt.X("listings:Q", title="listing count (liquidity)",
                    scale=alt.Scale(type="log")),
            y=alt.Y("spread:Q", title="spread", axis=alt.Axis(format="%")),
            tooltip=["item", alt.Tooltip("spread:Q", format=".1%"), "listings:Q"],
        ).properties(height=340)
        st.altair_chart(_style(chart), use_container_width=True)
        st.caption("More liquid items (right) trade tighter — the relationship "
                   "that makes reactive edges too small to clear costs.")

elif page == "4 · Rule scorecard":
    st.header("Rule scorecard")
    rules, outcomes, scores, notes = study_results()
    st.caption("Out-of-sample only; in-sample 2022-11-18 excluded.")
    disabled = set(gating.get("disabled_rules", []))
    disabled_pairs = set(gating.get("disabled_pairs", []))

    def gate_status(rule: str) -> str:
        if rule.startswith("substitute_pair:"):
            pair = rule.split(":", 1)[1]
            if pair in disabled_pairs or "weapon_balance_change" in disabled:
                return "DO-NOT-TRADE"
        elif rule.split(".")[0] in disabled:
            return "DO-NOT-TRADE"
        return "enabled"

    rows = []
    for rule, s in sorted(scores.items()):
        rows.append({
            "rule": rule, "confidence (yaml)": s.confidence,
            "events": len(s.events),
            "hit-rate": f"{s.hits}/{s.scoreable}" if s.scoreable else "—",
            "mean net (steam fee)": s.mean, "median": s.median,
            "n": s.n, "verdict": s.verdict, "gating": gate_status(rule),
        })
    st.dataframe(
        pd.DataFrame(rows).set_index("rule"), use_container_width=True,
        column_config={c: st.column_config.NumberColumn(format="percent")
                       for c in ["mean net (steam fee)", "median"]},
    )
    st.info("Headline verdicts: reactive first-order trading fails BUFF "
            "frictions (OOS mean −1.7% BUFF-costed); anticipatory limits do "
            "not fix it (best EV +0.2% pre-haircut). See git log for the "
            "full study reports.")

elif page == "5 · Event timeline":
    st.header("Event timeline")
    rules, outcomes, scores, notes = study_results()
    rows = []
    for o in outcomes:
        rows.append({
            "event": o.event_date,
            "item": o.candidate.market_hash_name,
            "rule": o.candidate.rule,
            "sample": o.sample_class,
            "predicted": o.candidate.direction.value,
            "direction": ("HIT" if o.direction_hit else "miss")
                         if o.direction_hit is not None else "no data",
            "gross": o.gross_pct,
            "net (steam fee)": o.net_pnl_pct,
            "entry": fmt_ts(o.entry_ts)[:10] if o.entry_ts else "—",
            "exit": fmt_ts(o.exit_ts)[:10] if o.exit_ts else "—",
        })
    traded = pd.DataFrame(
        [r for r in rows if r["net (steam fee)"] is not None
         and r["sample"] == "out_of_sample"]
    )
    if not traded.empty:
        traded["label"] = traded["event"] + " · " + traded["item"].str.split(" |").str[0]
        st.subheader("Out-of-sample net return per trade")
        st.altair_chart(_signed_bar(traded, "label", "net (steam fee)"),
                        use_container_width=True)
        st.caption("Balance-patch trades net-of-cost, out-of-sample.")
    st.dataframe(
        pd.DataFrame(rows), use_container_width=True, height=420,
        column_config={c: st.column_config.NumberColumn(format="percent")
                       for c in ["gross", "net (steam fee)"]},
    )
    live = [e for e in rules.historical_events
            if "ACTIVE" in str(e.get("status", ""))]
    if live:
        st.subheader("Live forward tests")
        for event in live:
            ts = datetime.strptime(str(event["date"]), "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
            st.caption(f"{event['date']} · tracking {(time.time()-ts)/86400:.0f} days")

elif page == "6 · Prediction log":
    st.header("Decision log")
    provenance_path = REPO_ROOT / "var" / "provenance_a.jsonl"
    if not provenance_path.exists():
        st.warning("no provenance log yet (var/provenance_a.jsonl) — run a "
                   "paper cycle or the demo")
    else:
        records = [json.loads(l) for l in
                   provenance_path.read_text().splitlines() if l.strip()]
        table = pd.DataFrame(records)
        c1, c2, c3 = st.columns(3)
        rule_filter = c1.multiselect("rule", sorted(table.rule.dropna().unique()))
        item_filter = c2.multiselect("item", sorted(table["item"].dropna().unique()))
        action_filter = c3.multiselect("action", sorted(table.action.unique()))
        view = table
        if rule_filter:
            view = view[view.rule.isin(rule_filter)]
        if item_filter:
            view = view[view["item"].isin(item_filter)]
        if action_filter:
            view = view[view.action.isin(action_filter)]
        st.caption(f"{len(view)} decisions (of {len(table)})")
        st.dataframe(
            view[["ts", "action", "item", "rule", "regime", "score"]],
            use_container_width=True, height=380,
        )
        idx = st.number_input("inspect row (index)", min_value=0,
                              max_value=max(len(view) - 1, 0), value=0)
        if len(view):
            st.json(view.iloc[int(idx)].to_dict())

elif page == "7 · Trade-up class ★":
    st.header("Trade-up class")
    from system_a.collections import load_collection_map
    cmap = load_collection_map(REPO_ROOT / "config" / "trade_up_collections.yaml")
    tu = trade_up_controls()
    if tu is None:
        st.caption("No iflow BUFF data — run `python -m shared.iflow_history`.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Event (fuel reds)",
                  f"{tu['event_med']:+.0%}" if tu['event_med'] is not None else "—",
                  help=f"median 60d net, n={tu['event_n']}")
        c2.metric("Random dates",
                  f"{tu['placebo_med']:+.0%}" if tu['placebo_med'] is not None else "—",
                  help=f"time placebo, n={tu['placebo_n']}")
        c3.metric("Broad market", "−7%", help="all iflow items, same window")
        chart_df = pd.DataFrame({
            "group": ["Trade-up event", "Random dates", "Broad market"],
            "net": [tu['event_med'], tu['placebo_med'] or 0, -0.07],
        })
        st.altair_chart(
            _magnitude_bar(chart_df, "group", "net",
                           "median 60-day net return (after BUFF frictions)",
                           highlight="Trade-up event"),
            use_container_width=True,
        )
        st.caption("2025-10-22 trade-up event, 60-day hold. Event vs. two "
                   "controls (`python -m system_a.trade_up_control` for detail).")
    st.divider()
    c1, c2, c3 = st.columns(3)
    c1.metric("Gold-case coverts mapped", len(cmap.covert_to_case))
    c2.metric("Map corroborated", "yes" if cmap.verified else "no")
    paper = REPO_ROOT / "var" / "trade_up_paper.jsonl"
    if paper.exists():
        buys = sum(1 for l in paper.read_text().splitlines()
                   if l.strip() and json.loads(l)["action"] == "buy_placed")
        c3.metric("Last paper run — positions", buys, help="trade_up_paper.py")
