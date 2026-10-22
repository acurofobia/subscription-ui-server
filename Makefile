PYTHON ?= python3

.PHONY: run check test

run:
	$(PYTHON) server.py

check:
	$(PYTHON) server.py --check

test:
	$(PYTHON) -m unittest discover -s tests -v
