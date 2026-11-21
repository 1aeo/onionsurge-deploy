PYTHON ?= python3

.PHONY: check
check:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
	node --check edge/worker.mjs
	node --test tests/*.mjs
