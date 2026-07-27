"""Visual system for the operator dashboard.

Direction: industrial / engineered. The choice is derived from the subject
rather than taste: this system's entire output is measurements with units and
safety verdicts (DO-NOT-TRADE, gated rules, go-live gates). Machine-shop grey
with a hi-vis accent is the native vernacular of gated industrial equipment,
and it sidesteps the two reflex looks for a trading tool (crypto terminal dark,
fintech navy and gold).

Rules this file enforces, so pages cannot drift:
  * one accent (hi-vis orange) used only to mark the thing that needs a human
  * numbers are always mono; prose is never mono
  * no shadows, no mid-range radii, no second accent
  * background tinted, never pure white
"""

from __future__ import annotations

# --- tokens ---------------------------------------------------------------
BG = "#EFF0F1"        # cold light grey, machine-shop white
SURFACE = "#F7F8F8"
INK = "#18191B"
MUTED = "#6C7075"
STEEL = "#C3C4C8"
RULE = "#DEDFE1"
HIVIS = "#FE5B2A"     # the whole personality; marks what needs attention
HIVIS_DIM = "#FFE4D9"
POS = "#18191B"       # gains read as ink; the accent is reserved for caution
NEG = "#FE5B2A"

# Google Fonts is imported at runtime rather than self-hosted: this is a local
# research tool launched by `make dashboard`, never a deployed page.
CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

:root {{
  --bg: {BG}; --surface: {SURFACE}; --ink: {INK}; --muted: {MUTED};
  --steel: {STEEL}; --rule: {RULE}; --hivis: {HIVIS}; --hivis-dim: {HIVIS_DIM};
}}

.stApp, [data-testid="stAppViewContainer"] {{ background: var(--bg); }}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding-top: 2.2rem; max-width: 1400px; }}

html, body, [class*="css"], .stMarkdown, p, li, label {{
  font-family: 'Archivo', system-ui, sans-serif;
  color: var(--ink);
}}
p, li {{ font-size: 15px; line-height: 1.55; }}

/* Display: uppercase grotesk at weight 400. Weight 400 caps at scale reads
   more confident than 700, and caps take tracking back toward zero. */
/* Streamlit styles `h1` inside its heading wrapper, which outranks a bare
   class selector, so the parent is named here to win the cascade. */
[data-testid="stHeadingWithActionElements"] h1.masthead, h1.masthead {{
  font-family: 'Archivo', system-ui, sans-serif;
  font-size: clamp(2rem, 4.5vw, 3.4rem);
  font-weight: 400 !important; text-transform: uppercase;
  letter-spacing: -0.01em; line-height: 1.02;
  color: var(--ink); margin: 0 0 .35rem 0; padding: 0;
}}
.sub {{ color: var(--muted); font-size: 15px; max-width: 68ch; margin: 0; }}

/* Mono is the utility voice: every number, unit, timestamp and label. */
.mono, .stMetric [data-testid="stMetricValue"], code {{
  font-family: 'JetBrains Mono', ui-monospace, monospace !important;
}}
.label {{
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted);
}}

/* Metrics: plain, ruled, no card chrome. Density beats decoration here. */
[data-testid="stMetric"] {{
  background: transparent; border: 0; border-top: 1px solid var(--ink);
  border-radius: 0; padding: .7rem 0 0 0;
}}
[data-testid="stMetricLabel"] p {{
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 11px !important; text-transform: uppercase;
  letter-spacing: .09em; color: var(--muted) !important;
}}
[data-testid="stMetricValue"] {{
  font-size: 30px !important; font-weight: 500; letter-spacing: -0.02em;
  color: var(--ink) !important;
}}
[data-testid="stMetricDelta"] {{ font-size: 12px !important; }}

/* View toggle: square, hard-edged, the active view filled solid ink.
   Streamlit renders segmented_control as stButtonGroup and marks the active
   item with aria-checked, defaulting it to its own red tint. */
[data-testid="stButtonGroup"] button {{
  border-radius: 0 !important; border: 1px solid var(--steel) !important;
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 11px !important; text-transform: uppercase !important;
  letter-spacing: .09em !important; padding: .55rem 1.15rem !important;
  color: var(--muted) !important; background: transparent !important;
}}
[data-testid="stButtonGroup"] button p {{
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 11px !important; letter-spacing: .09em !important;
}}
[data-testid="stButtonGroup"] button[aria-checked="true"],
[data-testid="stButtonGroup"] button[aria-checked="true"] p {{
  background: var(--ink) !important; color: {BG} !important;
  border-color: var(--ink) !important;
}}
[data-testid="stButtonGroup"] button:hover {{ border-color: var(--ink) !important; }}

/* Panels: hairline rules, zero radius, no shadow. Depth from contrast. */
.panel {{
  border: 1px solid var(--rule); background: var(--surface);
  padding: 1.1rem 1.25rem; border-radius: 0;
}}
.panel--alert {{ border-color: var(--hivis); background: var(--hivis-dim); }}
.panel h4 {{ margin: 0 0 .5rem 0; font-size: 15px; font-weight: 600; }}

/* Status strip: the one place the accent is allowed to dominate. */
.status {{
  display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center;
  border-top: 2px solid var(--ink); border-bottom: 1px solid var(--rule);
  padding: .6rem 0; margin: 1rem 0 1.6rem 0;
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  text-transform: uppercase; letter-spacing: .09em; color: var(--muted);
}}
.status b {{ color: var(--ink); font-weight: 500; }}
.dot {{
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--hivis); margin-right: .45rem; vertical-align: middle;
}}
.dot--ok {{ background: var(--ink); }}

/* Verdict chips: state, never decoration. */
.chip {{
  display: inline-block; font-family: 'JetBrains Mono', monospace;
  font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
  padding: .2rem .55rem; border: 1px solid var(--steel); color: var(--muted);
}}
.chip--hot {{ border-color: var(--hivis); color: var(--hivis); }}
.chip--on {{ border-color: var(--ink); color: var(--ink); }}

hr {{ border: 0; border-top: 1px solid var(--rule); margin: 2rem 0; }}
[data-testid="stDataFrame"] {{ border: 1px solid var(--rule); border-radius: 0; }}
.stAlert {{ border-radius: 0; }}
</style>
"""


def chart_style(chart):
    """Recessive axes, no chart junk, mono tick labels."""
    return (chart
            .configure_view(strokeWidth=0)
            .configure_axis(grid=False, labelColor=MUTED, titleColor=MUTED,
                            labelFont="JetBrains Mono", labelFontSize=11,
                            titleFont="JetBrains Mono", titleFontSize=10,
                            domainColor=STEEL, tickColor=STEEL)
            .configure_axisY(domain=False, ticks=False))
