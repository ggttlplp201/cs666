"""System A operator dashboard, READ-ONLY by construction.

No trading controls, no config mutation, no secrets (never touches .env).
Reads: var/market.db, var/alerts.jsonl, var/lag_decay.json,
var/provenance_a.jsonl, config/*.yaml, and recomputes the event and spread
studies from the same modules the CLI uses (single source of truth, no stale
report files).

Written for an OPERATOR, not a developer. Four views answer four questions:

    NOW       is anything happening, and is the machine alive
    TIMELINE  what has happened, and what did it do to prices
    OUTLOOK   what to do when the next event fires, and where it may come from
    EVIDENCE  why we believe any of this

The developer-facing detail (per-rule scorecards, gap reports, raw decision
provenance) is kept under EVIDENCE rather than on the front page.

Launch:  make dashboard
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

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from theme import CSS, HIVIS, INK, MUTED, NEG, POS, STEEL, chart_style  # noqa: E402
from shared.configuration import Config                      # noqa: E402
from shared.store import SnapshotStore                       # noqa: E402
from system_a.event_study import run_event_study             # noqa: E402
from system_a.rules import RulesTable                        # noqa: E402
from system_a.spread_study import spread_stats               # noqa: E402

st.set_page_config(page_title="CS2 event desk", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown(CSS, unsafe_allow_html=True)


# ----------------------------------------------------------------- charts
def _magnitude_bar(df, cat, val, title, highlight=None, fmt="+%"):
    """Horizontal magnitude bars with direct value labels. The accent marks one
    bar and everything else is steel, so the eye lands where it should."""
    df = df.copy()
    df["_c"] = [HIVIS if (highlight and c == highlight) else STEEL for c in df[cat]]
    df["_lbl"] = df[val].map(
        lambda v: f"{v:+.0%}" if fmt == "+%" else f"{v:.2f}")
    pad = max(abs(df[val].max()), abs(df[val].min())) * 0.18 + 0.02
    base = alt.Chart(df).encode(
        y=alt.Y(f"{cat}:N", sort=None, title=None),
        x=alt.X(f"{val}:Q", title=title, axis=alt.Axis(format=fmt),
                scale=alt.Scale(domain=[min(0, df[val].min()) - pad,
                                        df[val].max() + pad])))
    bars = base.mark_bar(height=20).encode(
        color=alt.Color("_c:N", scale=None, legend=None),
        tooltip=[cat, alt.Tooltip(f"{val}:Q", format=".2f")])
    labels = base.mark_text(align="left", dx=6, fontSize=12,
                            font="JetBrains Mono", color=INK).encode(text="_lbl:N")
    return chart_style((bars + labels).properties(height=len(df) * 34 + 10))


def _signed_bar(df, cat, val, height=300):
    """Signed returns. The sign is carried by the value label as well as by
    hue, so the chart never relies on color alone."""
    df = df.copy()
    df["_c"] = [POS if v >= 0 else NEG for v in df[val]]
    base = alt.Chart(df).encode(
        x=alt.X(f"{cat}:N", sort="-y", title=None,
                axis=alt.Axis(labelAngle=-40, labelLimit=170)),
        y=alt.Y(f"{val}:Q", title="net return", axis=alt.Axis(format="+%")))
    bars = base.mark_bar(width=13).encode(
        color=alt.Color("_c:N", scale=None, legend=None),
        tooltip=[cat, alt.Tooltip(f"{val}:Q", format="+.1%")])
    zero = alt.Chart(df).mark_rule(color=STEEL, strokeWidth=1).encode(y=alt.datum(0))
    return chart_style((zero + bars).properties(height=height))


# ------------------------------------------------------------------- data
@st.cache_resource
def load_config() -> Config:
    return Config.load(REPO_ROOT, system="system_a")


def open_store(config: Config) -> SnapshotStore:
    return SnapshotStore(REPO_ROOT / config.require("data.snapshot_poller")["db_path"])


def live_source() -> str:
    return load_config().get("data.snapshot_poller", {}).get("live_source", "buff")


@st.cache_data(ttl=60)
def buff_frame() -> pd.DataFrame:
    store = open_store(load_config())
    return pd.read_sql_query(
        "SELECT market_hash_name, ts, lowest_sell, highest_buy, listing_count,"
        " buy_order_count FROM snapshots WHERE source=? ORDER BY ts",
        store.conn, params=(live_source(),))


@st.cache_data(ttl=300)
def study_results():
    config = load_config()
    store = open_store(config)
    rules = RulesTable.load(REPO_ROOT / config.require("system_a.rules_table_path"))
    seed = REPO_ROOT / config.require("data.steam_history")["items_file"]
    universe = sorted({l.strip() for l in seed.read_text().splitlines() if l.strip()})
    outcomes, scores, notes = run_event_study(
        rules, store, universe,
        lock_days=config.require("cooldown.trade_lock_days"),
        buff_fee_pct=config.require("costs.buff_fee_pct"),
        buff_fee_history=config.get("costs.fee_history", []),
        steam_fee_pct=config.require("costs.steam_fee_pct"))
    return rules, outcomes, scores, notes


@st.cache_data(ttl=30)
def watch_state() -> dict:
    """State of the standing trade-up watch (system_a.watch).

    The watch is the only thing standing between us and missing the roughly
    10-day window, so its liveness is front-page information, not a footnote."""
    alerts_path = REPO_ROOT / "var" / "alerts.jsonl"
    seen_path = REPO_ROOT / "var" / "watch_seen.json"
    alerts = []
    if alerts_path.exists():
        for line in alerts_path.read_text().splitlines():
            if line.strip():
                try:
                    alerts.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    alerts.sort(key=lambda a: a.get("detected_at", 0), reverse=True)
    return {"alerts": alerts,
            "last_run": seen_path.stat().st_mtime if seen_path.exists() else None,
            "ever_run": seen_path.exists()}


@st.cache_data(ttl=300)
def lag_curve() -> dict | None:
    """Entry-lag decay produced by `make lag-study`. Recomputing it scans about
    20 archive zips and takes minutes, far too slow for a page load, so the
    dashboard reads the artifact and shows its age. A stale curve is then
    visible as stale rather than silently wrong."""
    path = REPO_ROOT / "var" / "lag_decay.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    data["age_days"] = (time.time() - path.stat().st_mtime) / 86400
    return data


@st.cache_data(ttl=120)
def paper_desk() -> dict | None:
    """Result of the paper desk (system_a.paper_desk), which trades only on
    events that actually happened. Produced by `make desk`."""
    path = REPO_ROOT / "var" / "paper_desk.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    data["age_days"] = (time.time() - path.stat().st_mtime) / 86400
    return data


@st.cache_data(ttl=600)
def concentration_ranking():
    from system_a.concentration import _latest_snapshot, load_snapshot, rank_classes
    config = load_config()
    cache = REPO_ROOT / config.require("data.iflow_archive")["cache_dir"]
    snap = _latest_snapshot(cache)
    if snap is None:
        return None
    ranking = rank_classes(load_snapshot(snap))
    opened = set(config.get("system_a.concentration", {}).get("opened_classes", []))
    return ranking, snap.name, opened


@st.cache_data(ttl=300)
def trade_up_controls():
    """Event vs time-placebo for the 2025-10-22 trade-up event on iflow BUFF
    data. These are the negative controls that made trade-up the one surviving
    System A play. Returns None if iflow data is not loaded."""
    import random as _random
    config = load_config()
    store = open_store(config)
    if not store.counts_by_source().get("buff_iflow"):
        return None
    from system_a.collections import load_collection_map
    from system_a.event_study import DAY, _bar_after, _event_ts
    cmap = load_collection_map(REPO_ROOT / "config" / "trade_up_collections.yaml")
    seed = REPO_ROOT / config.require("data.steam_history")["items_file"]
    universe = sorted({l.strip() for l in seed.read_text().splitlines() if l.strip()})
    spreads = {s.item: s.median for s in spread_stats(store, source="buff_iflow")}
    med_spread = statistics.median(spreads.values()) if spreads else 0.04
    ev, fee = _event_ts("2025-10-22"), 0.025

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
    return {"n_reds": len(reds), "event_n": len(event), "event_med": med(event),
            "placebo_n": len(placebo), "placebo_med": med(placebo)}


def fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "never"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def ago(ts: float | None) -> str:
    if ts is None:
        return "never"
    mins = (time.time() - ts) / 60
    if mins < 90:
        return f"{mins:.0f} min ago"
    if mins < 2880:
        return f"{mins/60:.0f} h ago"
    return f"{mins/1440:.0f} d ago"


# ----------------------------------------------------------------- header
config = load_config()
frame = buff_frame()
gating = config.get("system_a.gating", {}) or {}
watch = watch_state()

paper = config.require("execution.paper_mode")
all_off = "weapon_balance_change" in gating.get("disabled_rules", [])
mode = "LOG-ONLY" if all_off else ("PAPER" if paper else "LIVE")
poller_alive = bool(subprocess.run(["pgrep", "-f", "system_a.runner --poll"],
                                   capture_output=True, text=True).stdout.strip())
watch_stale = watch["last_run"] is None or (time.time() - watch["last_run"]) > 86400

st.markdown('<h1 class="masthead">CS2 event desk</h1>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub">One event class in this market has survived its negative '
    'controls. This desk watches for the next one, and reports honestly on '
    'everything that did not survive.</p>', unsafe_allow_html=True)

st.markdown(
    f'<div class="status">'
    f'<span><span class="dot {"" if all_off else "dot--ok"}"></span>mode <b>{mode}</b></span>'
    f'<span>spent <b>$0</b></span>'
    f'<span><span class="dot {"dot--ok" if poller_alive else ""}"></span>'
    f'price feed <b>{"running" if poller_alive else "stopped"}</b></span>'
    f'<span><span class="dot {"" if watch_stale else "dot--ok"}"></span>'
    f'event watch <b>{ago(watch["last_run"])}</b></span>'
    f'<span>buff fee <b>{config.require("costs.buff_fee_pct"):.1%}</b></span>'
    f'<span>lock <b>T+{config.require("cooldown.trade_lock_days")}</b></span>'
    f'</div>', unsafe_allow_html=True)

view = st.segmented_control(
    "View", ["Now", "Simulation", "Timeline", "Outlook", "Evidence"],
    default="Now", label_visibility="collapsed")

# ============================================================== NOW
if view == "Now":
    alerts = watch["alerts"]
    if alerts:
        a = alerts[0]
        st.markdown(
            f'<div class="panel panel--alert"><h4>Trade-up event detected</h4>'
            f'<p class="mono" style="font-size:12px;color:{MUTED}">'
            f'{fmt_ts(a.get("posted_at"))} · {a.get("source", "")} · '
            f'confidence {a.get("confidence", "")}</p>'
            f'<p>{a.get("excerpt", "")[:300]}</p>'
            f'<p class="label">Open the outlook view for the entry window</p>'
            f'</div>', unsafe_allow_html=True)
    elif not watch["ever_run"]:
        st.warning("The event watch has never run, so nothing is looking for "
                   "the next trade-up change. Start it with `make watch`, then "
                   "put it on a cron schedule.")
    elif watch_stale:
        st.warning(f"The event watch last ran {ago(watch['last_run'])}. The "
                   "window after an event is about 10 days, so a watch this "
                   "stale can miss one. Re-run `make watch`.")
    else:
        st.markdown(
            '<div class="panel"><h4>No event detected</h4>'
            '<p>The watch is current and has seen nothing actionable. This is '
            'the expected state almost all the time: this event class has '
            'fired once, not weekly.</p></div>', unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)
    last_ts = frame.ts.max() if not frame.empty else None
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Alerts logged", len(alerts))
    c2.metric("Last price snapshot", ago(last_ts))
    c3.metric("Items tracked",
              int(frame.market_hash_name.nunique()) if not frame.empty else 0)
    c4.metric("Capital at risk", "$0", "log-only", delta_color="off")

    if not poller_alive:
        st.error("The price feed is not running, so the market series is going "
                 "stale right now. Start it with `make poll`.")

    if not frame.empty:
        st.markdown('<p class="label">Live market</p>', unsafe_allow_html=True)
        latest = (frame.sort_values("ts").groupby("market_hash_name").tail(1)
                  .set_index("market_hash_name"))
        show = latest[["lowest_sell", "highest_buy", "listing_count"]].copy()
        show.columns = ["ask", "bid", "listings"]
        if (show["bid"] > 0).any():
            show["spread"] = (show["ask"] - show["bid"]) / show["ask"]
        st.dataframe(show, use_container_width=True,
                     column_config={"spread": st.column_config.NumberColumn(
                         format="percent")})
        st.caption(
            "Live source is the Steam Market, free and keyless. Steam runs "
            "roughly 30 to 40 percent above BUFF and carries no real bid, so "
            "read it for liveness, not for BUFF trade economics."
            if live_source() == "steam_live" else f"Live source: {live_source()}.")

# ======================================================= SIMULATION
elif view == "Simulation":
    desk = paper_desk()
    if desk is None:
        st.info("Run `make desk` to trade the paper book on real detected events.")
    elif desk.get("status") == "no_events":
        st.markdown(
            '<div class="panel"><h4>Holding cash</h4>'
            '<p>No trade-up event has been detected, and none is labelled, so '
            'the desk has not traded. It will not invent a signal in order to '
            'have something to show.</p></div>', unsafe_allow_html=True)
    elif desk.get("status") == "no_market_data":
        st.warning("Events exist but no BUFF market data covers their windows. "
                   "Load history with `python -m shared.iflow_history`.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Paper book", f"{desk['capital']:,.0f} CNY")
        c2.metric("Total P&L", f"{desk['total_pnl']:+,.0f} CNY",
                  f"{desk['return_pct']:+.1%} on capital", delta_color="off")
        c3.metric("Positions opened", desk["positions_opened"])
        c4.metric("Events traded", len(desk["events"]))

        eq = pd.DataFrame(desk["equity"])
        eq["day"] = pd.to_datetime(eq["day"])
        line = alt.Chart(eq).mark_line(color=INK, strokeWidth=2).encode(
            x=alt.X("day:T", title=None),
            y=alt.Y("equity:Q", title="book value, CNY",
                    scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("day:T"), alt.Tooltip("equity:Q", format=",.0f"),
                     alt.Tooltip("open_lots:Q", title="open lots")])
        marks = pd.DataFrame([{"day": pd.to_datetime(e["day"]),
                               "label": e["origin"]} for e in desk["events"]])
        rules_layer = alt.Chart(marks).mark_rule(
            color=HIVIS, strokeWidth=2).encode(x="day:T",
                                               tooltip=["label:N"])
        st.altair_chart(chart_style((line + rules_layer).properties(height=300)),
                        use_container_width=True)
        st.caption(
            f"Paper book over {desk['days']} days, {desk['start']} to "
            f"{desk['end']}. The marked line is the real event the desk traded. "
            f"Prices are real BUFF, costs are the measured spread plus a "
            f"{desk['fee_pct']:.1%} fee, with a T+{desk['lock_days']} lock. "
            f"Run {desk['age_days']:.0f} days ago.")

        st.markdown('<p class="label">Events it traded, and where they came from</p>',
                    unsafe_allow_html=True)
        st.dataframe(
            pd.DataFrame([{"date": e["day"], "source": e["origin"],
                           "what happened": e["detail"]} for e in desk["events"]]
                         ).set_index("date"), use_container_width=True)

        st.markdown(
            '<div class="panel panel--alert"><h4>Read this before trusting the number</h4>'
            '<p>The rules this desk trades on, and the gold-case map it uses to '
            'pick items, were written after studying this exact event. The '
            'result is therefore <b>in-sample</b>: it shows the machine '
            'executing correctly on the event it was built around, which is not '
            'the same as evidence that it will work on the next one.</p>'
            '<p>There has been one event. A single trade is not a track record, '
            'however good the number looks. The genuine out-of-sample test is '
            'the next detection, which is the entire reason the watch runs.</p>'
            '</div>', unsafe_allow_html=True)

        if desk.get("trades"):
            with st.expander("Trade blotter"):
                st.dataframe(pd.DataFrame(desk["trades"]),
                             use_container_width=True, hide_index=True)
        if desk.get("decisions"):
            st.caption("Engine decisions: " + ", ".join(
                f"{k} {v}" for k, v in sorted(desk["decisions"].items())))

# ========================================================= TIMELINE
elif view == "Timeline":
    rules, outcomes, scores, notes = study_results()

    st.markdown('<p class="label">Labelled events</p>', unsafe_allow_html=True)
    rows = []
    for e in rules.historical_events:
        kind = e.get("type")
        kind = ",".join(kind) if isinstance(kind, list) else str(kind)
        rows.append({"date": str(e.get("date")), "type": kind,
                     "trade-up class": "trade_up" in kind,
                     "what changed": str(e.get("change", ""))[:130]})
    ev_df = pd.DataFrame(rows).sort_values("date", ascending=False)
    st.dataframe(ev_df.set_index("date"), use_container_width=True,
                 column_config={"trade-up class":
                                st.column_config.CheckboxColumn()})
    st.caption(
        "Eight labelled events, of which exactly one is a trade-up mechanic "
        "change. That is why the timing of the next one cannot be predicted "
        "from this history: one observation cannot fit or validate a model.")

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown('<p class="label">Measured reaction, out of sample</p>',
                unsafe_allow_html=True)
    traded = pd.DataFrame([
        {"label": f"{o.event_date}  {str(o.candidate.market_hash_name).split(' |')[0]}",
         "net": o.net_pnl_pct}
        for o in outcomes
        if o.net_pnl_pct is not None and o.sample_class == "out_of_sample"])
    if not traded.empty:
        st.altair_chart(_signed_bar(traded, "label", "net"),
                        use_container_width=True)
        st.caption(
            "Net return per balance-patch trade after costs. The picture is "
            "the point: this is what a class that does not work looks like, "
            "and it is why only the trade-up class is still live.")
    else:
        st.info("No scored outcomes yet. Load BUFF history with "
                "`python -m shared.iflow_history`.")

# ========================================================== OUTLOOK
elif view == "Outlook":
    st.markdown('<p class="label">If the watch fires, this is your window</p>',
                unsafe_allow_html=True)
    curve = lag_curve()
    if curve is None:
        st.info("Run `make lag-study` to compute the entry-lag decay curve.")
    else:
        cdf = pd.DataFrame(curve["curve"])
        same = cdf.loc[cdf.lag == 0, "excess"]
        week = cdf[(cdf.lag >= 1) & (cdf.lag <= 7)]["excess"]
        late = cdf[cdf.lag >= 14]["excess"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Acting same day", f"{same.iloc[0]:+.0%}" if len(same) else "n/a",
                  "excess over market", delta_color="off")
        c2.metric("Up to a week late", f"{week.median():+.0%}" if len(week) else "n/a",
                  "still worth acting", delta_color="off")
        c3.metric("Two weeks late", f"{late.median():+.0%}" if len(late) else "n/a",
                  "the move is gone", delta_color="off")

        line = alt.Chart(cdf).mark_line(color=INK, strokeWidth=2).encode(
            x=alt.X("lag:Q", title="days after the announcement"),
            y=alt.Y("excess:Q", title="excess over market",
                    axis=alt.Axis(format="+%")))
        pts = alt.Chart(cdf).mark_point(color=HIVIS, filled=True, size=70).encode(
            x="lag:Q", y="excess:Q",
            tooltip=[alt.Tooltip("lag:Q", title="days late"),
                     alt.Tooltip("excess:Q", format="+.0%"),
                     alt.Tooltip("n:Q", title="items measured")])
        st.altair_chart(chart_style((line + pts).properties(height=290)),
                        use_container_width=True)
        st.caption(
            f"Gold-case basket minus the rest of the market, {curve['hold_days']}-day "
            f"hold, net of BUFF spread and a {curve['fee']:.1%} fee, measured on the "
            f"{curve['event']} event. Curve computed {curve['age_days']:.0f} days ago.")

        st.markdown(
            '<div class="panel"><h4>What this does and does not say</h4>'
            '<p>It says you do not need to predict the date, and you do not '
            'need to beat bots to the trade. Arriving inside a week still '
            'captured most of the excess, and a watch checking every few hours '
            'is enough for that.</p>'
            '<p>It does not say the next event will behave this way. This is '
            'one event. Everything here rests on a single observation, and the '
            'honest job of the watch is to produce a second one.</p></div>',
            unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown('<p class="label">Where the next one might land</p>',
                unsafe_allow_html=True)
    rank = concentration_ranking()
    if rank is None:
        st.info("Load the BUFF archive with `python -m shared.iflow_history` "
                "to rank item classes by monopolization.")
    else:
        ranking, snap_name, opened = rank
        rdf = pd.DataFrame([{
            "class": c.cls + ("  (already opened)" if c.cls in opened else ""),
            "score": c.score} for c in ranking[:10]])
        top = rdf.iloc[0]["class"] if not rdf.empty else None
        st.altair_chart(
            _magnitude_bar(rdf, "class", "score", "monopolization score, 0 to 1",
                           highlight=top, fmt=".2f"),
            use_container_width=True)
        st.caption(
            f"A high score means a high price barrier on thin supply, which is "
            f"what knives looked like before they were opened. Snapshot "
            f"{snap_name[:10]}. Treat this as a prior worth watching, not a "
            f"forecast: no model has been validated against it.")

# ========================================================= EVIDENCE
elif view == "Evidence":
    st.markdown('<p class="label">Why the trade-up class is believed</p>',
                unsafe_allow_html=True)
    tu = trade_up_controls()
    if tu and tu["event_med"] is not None:
        # Labels stay short: the axis truncates anything longer, and a bar
        # whose label vanished is worse than no chart.
        chart_df = pd.DataFrame({
            "group": ["Trade-up event", "Random dates", "Broad market"],
            "net": [tu["event_med"], tu["placebo_med"] or 0, -0.07]})
        st.altair_chart(
            _magnitude_bar(chart_df, "group", "net", "median 60-day net return",
                           highlight="Trade-up event"),
            use_container_width=True)
        st.caption(
            "The middle bar is the control that matters: the same items on "
            "random dates. If the event bar did not tower over it, the effect "
            "would be nothing more than what these items always do.")
    else:
        st.info("Load BUFF history with `python -m shared.iflow_history` for "
                "the control chart.")

    st.markdown(
        '<div class="panel"><h4>What did not survive</h4>'
        '<p><b>Balance-patch trading.</b> Out of sample it loses money after '
        'BUFF costs, and passive limit entries do not rescue it.</p>'
        '<p><b>Picking the right trade-up fuel.</b> Non-fuel items from the '
        'same cases rose just as hard, so the effect is the whole gold-case '
        'ladder repricing, not fuel selection.</p>'
        '<p><b>System B, the positional engine.</b> Its ranker does have real '
        'predictive power on live data, but a BUFF round trip costs about 6 '
        'percent and the edge is smaller than the toll.</p></div>',
        unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)

    with st.expander("Rule scorecard, out of sample"):
        rules, outcomes, scores, notes = study_results()
        disabled = set(gating.get("disabled_rules", []))
        disabled_pairs = set(gating.get("disabled_pairs", []))

        def gate_status(rule: str) -> str:
            if rule.startswith("substitute_pair:"):
                if (rule.split(":", 1)[1] in disabled_pairs
                        or "weapon_balance_change" in disabled):
                    return "DO-NOT-TRADE"
            elif rule.split(".")[0] in disabled:
                return "DO-NOT-TRADE"
            return "enabled"

        st.dataframe(pd.DataFrame([
            {"rule": r, "events": len(s.events),
             "hit-rate": f"{s.hits}/{s.scoreable}" if s.scoreable else "n/a",
             "mean net": s.mean, "median": s.median, "n": s.n,
             "verdict": s.verdict, "gating": gate_status(r)}
            for r, s in sorted(scores.items())]).set_index("rule"),
            use_container_width=True,
            column_config={c: st.column_config.NumberColumn(format="percent")
                           for c in ["mean net", "median"]})
        st.caption("In-sample events are excluded. The 2022-11-18 break that "
                   "the rules were written from is quarantined out.")

    with st.expander("What trading costs on BUFF"):
        stats = spread_stats(open_store(config), source="buff_iflow")
        if stats:
            med = statistics.median([s.median for s in stats])
            fee = config.require("costs.buff_fee_pct")
            st.metric("Typical round trip", f"{med + fee:.1%}",
                      f"{med:.1%} spread plus {fee:.1%} fee", delta_color="off")
            st.dataframe(
                pd.DataFrame([{"item": s.item, "median spread": s.median}
                              for s in stats]).set_index("item"),
                use_container_width=True,
                column_config={"median spread": st.column_config.NumberColumn(
                    format="percent")})
            st.caption("This single number is what killed both the reactive "
                       "engine and System B. Any edge smaller than it loses.")
        else:
            st.info("No BUFF spread data loaded.")

    with st.expander("Data health"):
        store = open_store(config)
        gaps = store.gap_report(live_source(),
                               expected_seconds=config.require("data.refresh_seconds"))
        if gaps:
            st.error(f"{len(gaps)} gap(s) in the stored series. A holed series "
                     "must not be trusted.")
            st.dataframe(pd.DataFrame(
                [(fmt_ts(a), fmt_ts(b), f"{s/3600:.1f}h") for a, b, s in gaps],
                columns=["gap start", "gap end", "duration"]),
                use_container_width=True)
        else:
            st.success("No gaps beyond 2.5 times the cadence. The series is "
                       "continuous.")
        if not frame.empty:
            st.caption(f"coverage {fmt_ts(frame.ts.min())} to "
                       f"{fmt_ts(frame.ts.max())}")

    with st.expander("Decision log"):
        prov = REPO_ROOT / "var" / "provenance_a.jsonl"
        if not prov.exists():
            st.info("No decisions logged yet. `make demo` generates a sample run.")
        else:
            recs = [json.loads(l) for l in prov.read_text().splitlines() if l.strip()]
            st.dataframe(pd.DataFrame(recs[-300:]), use_container_width=True)
            st.caption(f"{len(recs)} decisions logged. Every one records which "
                       f"signals fired and which rule decided.")
