PY := $(CURDIR)/.venv/bin/python
PYTEST := $(CURDIR)/.venv/bin/pytest
A := $(CURDIR)/system_a
B := $(CURDIR)/system_b

.PHONY: test test-a test-b \
        demo dashboard poll gap-check event-study spread-study exit-study \
        b-panel b-backtest b-backtest-real b-dashboard

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

exit-study:
	cd "$(A)" && PYTHONPATH=src "$(PY)" -m system_a.exit_study

# -------------------------------------------------------- System B (positional)
b-panel:              ## build the real BUFF panel from var/market.db
	cd "$(B)" && PYTHONPATH=src "$(PY)" -m shared_b.real_panel

b-backtest:           ## walk-forward on the synthetic simulator
	cd "$(B)" && PYTHONPATH=src "$(PY)" -m system_b.run_backtest --synthetic

b-backtest-real:      ## walk-forward on the real BUFF panel
	cd "$(B)" && PYTHONPATH=src "$(PY)" -m system_b.run_backtest --data-dir var/panel_real

b-dashboard:          ## React run-artifact dashboard (needs bun/npm)
	cd "$(B)/dashboard" && bun install && bun run dev
