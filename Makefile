PY := .venv/bin/python

.PHONY: test demo dashboard poll gap-check event-study spread-study exit-study

test:
	.venv/bin/pytest -q

demo:
	PYTHONPATH=src $(PY) -m system_a.runner --demo

dashboard:            ## read-only research dashboard on localhost
	.venv/bin/streamlit run dashboard_a/app.py

poll:
	PYTHONPATH=src $(PY) -m system_a.runner --poll

gap-check:
	PYTHONPATH=src $(PY) -m system_a.runner --gap-check

event-study:
	PYTHONPATH=src $(PY) -m system_a.event_study

spread-study:
	PYTHONPATH=src $(PY) -m system_a.spread_study

exit-study:
	PYTHONPATH=src $(PY) -m system_a.exit_study
