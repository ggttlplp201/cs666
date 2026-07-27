PY := $(CURDIR)/.venv/bin/python
PYTEST := $(CURDIR)/.venv/bin/pytest
A := $(CURDIR)/system_a
B := $(CURDIR)/system_b

.PHONY: test test-a test-b \
        demo dashboard poll gap-check event-study spread-study exit-study \
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

b-live:               ## one System B forward paper cycle on the live feed
	cd "$(B)" && PYTHONPATH=src "$(PY)" -m system_b.live_paper

b-dashboard:          ## React run-artifact dashboard (needs bun/npm)
	cd "$(B)/dashboard" && bun install && bun run dev
