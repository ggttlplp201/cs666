PY := $(CURDIR)/.venv/bin/python
PYTEST := $(CURDIR)/.venv/bin/pytest
A := $(CURDIR)/system_a
B := $(CURDIR)/system_b

.PHONY: test test-a test-b \
        demo dashboard poll gap-check event-study spread-study exit-study \
        b-panel-archive b-greedy b-greedy-literal b-greedy-sweep b-cost-floor \
        b-panel b-backtest b-backtest-real b-dashboard lag-study watch desk b-live

# ---------------------------------------------------------------- tests
test:                 ## both systems
	"$(PYTEST)" -q

test-a:
	cd "$(A)" && "$(PYTEST)" -q

test-b:
	cd "$(B)" && "$(PYTEST)" -q

# ------------------------------------------------------ System A (event-driven)
demo:
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.runner --demo

dashboard:            ## read-only research dashboard on localhost
	cd "$(A)" && "$(CURDIR)/.venv/bin/streamlit" run dashboard/app.py

poll:
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.runner --poll

gap-check:
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.runner --gap-check

event-study:
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.event_study

spread-study:
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.spread_study

desk:                 ## paper-trade the real detected events (500k CNY book)
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.paper_desk

watch:                ## poll Steam news for a trade-up event (cron this)
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.watch

lag-study:
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.trade_up_lag

exit-study:
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.exit_study

# -------------------------------------------------------- System B (positional)
b-panel:              ## build the real BUFF panel from var/market.db
	cd "$(B)" && PYTHONPATH=src "$(PY)" -m shared_b.real_panel

b-backtest:           ## walk-forward on the synthetic simulator
	cd "$(B)" && PYTHONPATH=src "$(PY)" -m system_b.run_backtest --synthetic

b-backtest-real:      ## walk-forward on the real BUFF panel
	cd "$(B)" && PYTHONPATH=src "$(PY)" -m system_b.run_backtest --data-dir var/panel_real

b-panel-archive:      ## build the 97-item panel from the iflow.work archive
	cd "$(B)" && PYTHONPATH=src:../system_a/src "$(PY)" -m shared_b.vendors.iflow_archive \
	  --universe config/universe_b_draft.yaml --data-dir data/panel_iflow97

b-greedy:             ## greedy ratchet on the real panel (spread-aware defaults)
	cd "$(B)" && PYTHONPATH=src:../system_a/src "$(PY)" -m system_b.run_backtest \
	  --data-dir var/panel_real --strategy greedy

b-greedy-literal:     ## greedy ratchet with the spec's raw thresholds
	cd "$(B)" && PYTHONPATH=src:../system_a/src "$(PY)" -m system_b.run_backtest \
	  --data-dir var/panel_real --strategy greedy --greedy-literal

b-greedy-sweep:       ## sweep greedy thresholds over every item-day
	cd "$(B)" && PYTHONPATH=src:../system_a/src "$(PY)" research/greedy_sweep.py var/panel_real

b-cost-floor:         ## measure the round-trip cost floor + exit realizations
	cd "$(B)" && PYTHONPATH=src:../system_a/src "$(PY)" research/exit_cost_floor.py

b-live:               ## one System B forward paper cycle on the live feed
	cd "$(B)" && PYTHONPATH=src "$(PY)" -m system_b.live_paper

b-dashboard:          ## React run-artifact dashboard (needs bun/npm)
	cd "$(B)/dashboard" && bun install && bun run dev
