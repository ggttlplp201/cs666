# The exit cost floor — why tight ask-side triggers cannot be profitable

Measured 2026-07-30 on `system_b/var/panel_real` (18 items, 2,823 item-days,
2025-09-22 → 2026-05-20, real iflow/BUFF archive history).

Reproduce: `system_b/research/exit_cost_floor.py`.

## 1. The wedge

Bracket triggers are measured **ask-side** (`ret = ask / lot.buy_price - 1`),
because that is the side we bought on and the crash-course ±10-15% rules are
chart/list prices. But exits **fill bid-side**, and BUFF charges the fee on the
sell. So every round trip pays a fixed wedge before any trigger means anything:

| component                        |   cost |
| -------------------------------- | -----: |
| median ask→bid spread            | 3.37% |
| BUFF sell fee                    | 1.50% |
| slippage (entry + exit)          | 1.00% |
| **round-trip floor**             | **5.87%** |

Mean spread is 4.43% and p75 is 5.45%, so the floor is worse than 5.87% for
the thinner half of the universe.

## 2. What an ask-side trigger actually realizes

Entry pays `ask * (1 + slip)`; exit receives `bid * (1 - slip) * (1 - fee)`.

| ask-side trigger | realized net | rule |
| ---------------: | -----------: | ---- |
|  +15% |  +8.37% | `take_profit_full` |
|  +10% |  +3.65% | `take_profit_trim` (and only **half** the lot) |
|   −2% |  **−7.65%** | `bear_cut_ret` — Ivan's 1a51d72 value |
|   −5% |  −10.48% | `bear_cut_ret` — prior value, retained |
|  −10% |  −15.19% | `stop_loss_cut` |
|  −18% |  −22.73% | `stop_unconditional_liquidation` |

**A −2% trigger sits inside the 3.37% spread.** The ask ticking down 2% is not
a price move, it is quote noise — but the exit crystallizes a real ~7.6% loss,
on the whole lot.

## 3. Controlled sweep of `bear_cut_ret`

Same universe, same fill model, same +10% half-lot take-profit; only the cut
threshold moves. Entries on every item-day (not the live entry gate) so the
sample is large enough to read — this measures the *exit knob*, not the
strategy.

| `bear_cut_ret` | wins | losses | win rate | avg win (×0.5) | avg loss (×1.0) | net/round-trip |
| -------------: | ---: | -----: | -------: | -------------: | --------------: | -------------: |
| **−0.02** (Ivan) |  525 | 1515 | 26% | +28.01% | −9.87%  | **−0.12%** |
| −0.05 (retained) |  719 |  945 | 43% | +33.86% | −15.21% | **+5.99%** |
| −0.10            |  861 |  495 | 63% | +33.83% | −23.52% | **+12.89%** |

Tightening the cut raises the loss *count* far faster than it shrinks the loss
*size*, because the wedge dominates at small triggers. Ivan's ablation moved
this knob the wrong way; it looked right on n=18 trades.

## 4. Why the n=18 result did not catch it

`runs/backtest_iflow97_final` reports 18 trades / 78% win / +6.6% avg net over
2024-02 → 2026-05, with a pre/post-2025-09 split as the robustness check.

The window contains a **once-off repricing event**: the 2025-10-22 trade-up
announcement (the event class System A was built to detect) repriced trade-up
fuel by 10-25× in days. Verified against the raw archive with stable
`buff_id`s, so these are real moves, not data breaks:

| item | 2025-10-23 ask | next observation | `buff_id` |
| ---- | -------------: | ---------------: | --------: |
| MP9 \| Starlight Protector (FT) | 15.66 | **190.0** (10-24) | 886657 |
| AUG \| Chameleon (FT)           | absent | **195.0** (10-24) | 34002 |
| SSG 08 \| Dragonfire (FT)       | 129.5 | **3394.5** (11-30) | 36553 |

Median item return over the panel window is **+105%**; the equal-weight mean is
+514%. The pre/post-2025-09 split puts this entire event in the *post* half, so
"both halves positive" does not establish robustness — it establishes that one
half contained a boom.

## 5. The entry discount has no selection edge

`entry_limit_discount_pct` rests the buy below the ask; the order fills only if
the next day's ask drops to the limit. Ivan's sweep found avg net/trade
+2.9 / +3.8 / +6.6 / +2.5% at 0.5 / 1.0 / 1.5 / 2.0%.

Measured against the equal-weight panel over the identical calendar window
(medians; calendar-aligned, because coverage flickers and positional offsets
overstate horizons badly):

| discount | fills | fill rate | H5 raw | H5 vs bench | H10 raw | H10 vs bench | H20 raw | H20 vs bench |
| -------: | ----: | --------: | -----: | ----------: | ------: | -----------: | ------: | -----------: |
| 0.5% | 500 | 20.5% | +0.15% | **−2.11%** | +0.11% | **−2.52%** | +1.15% | **−5.18%** |
| 1.0% | 377 | 15.4% | +0.34% | **−2.26%** | +0.19% | **−3.40%** | +1.88% | **−5.38%** |
| 1.5% | 271 | 11.1% | +0.76% | **−1.61%** | +0.46% | **−2.18%** | +3.24% | **−2.97%** |
| 2.0% | 192 |  7.9% | +0.85% | **−1.57%** | +0.61% | **−3.01%** | +3.51% | **−2.89%** |

Raw forward returns are positive at every depth; **benchmark-relative returns
are negative at every depth.** Buying the dip underperformed simply holding the
panel. The sweep measured long exposure to the boom in §4, not entry skill.

The value is kept at 0.015 (Ivan's) because it is not *harmful* — it buys
slightly cheaper — but it must not be treated as a source of edge, and it
should not be re-tuned on this window.

## 6. The size asymmetry

`take_profit_trim` sells `lot.qty // 2`. In Ivan's version `bear_regime_cut`,
`distribution_shape_exit` and all stops fall through to the default
`sell_qty = lot.qty` — the **whole** lot. Half-size wins against full-size
losses means a high win rate still loses money: measured earlier on the real
panel, 9 winners averaging 167 CNY against 3 losers averaging 2,915 CNY at a
75% win rate.

`brackets.soft_exit_qty_pct` (retained, 0.5) scales the *discretionary*
shape/regime exits the same way take-profit is scaled. Hard stops are
deliberately exempt — a risk limit must still be able to liquidate.

## Rules of thumb

1. Never set a soft exit trigger tighter than the round-trip floor (~6%).
   Below that you are trading the spread, not the price.
2. Compare every candidate edge to the equal-weight panel over the same
   window. Raw returns in this dataset are dominated by market direction.
3. Do not tune on 2024-02 → 2026-05 alone without excluding, or at least
   isolating, the 2025-10 trade-up repricing.
4. Size losses the way you size wins, or measure net P&L rather than win rate.
