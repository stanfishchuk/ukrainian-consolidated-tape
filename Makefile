PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: help venv install test lint crawl extract aggregate pipeline dashboard docker-build up logs down clean

help:
	@echo "make install     — створити .venv і встановити залежності (з dashboard)"
	@echo "make test        — pytest"
	@echo "make pipeline    — crawl + extract + aggregate (2019–2025)"
	@echo "make dashboard   — Streamlit-дашборд на http://localhost:8501"
	@echo "make up          — docker compose up -d --build"
	@echo "make logs        — docker compose logs -f"

venv:
	python3 -m venv .venv

install: venv
	$(PIP) install -q -r requirements.txt
	$(PIP) install -q -e ".[dev,dashboard]"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check smida_tape tests dashboard.py

crawl:
	$(PY) -m smida_tape crawl --start 2019-01-01 --end 2025-12-31

extract:
	$(PY) -m smida_tape extract --retry-errors

aggregate:
	$(PY) -m smida_tape aggregate --year-from 2015 --year-to 2025

pipeline:
	$(PY) -m smida_tape pipeline --start 2019-01-01 --end 2025-12-31

dashboard:
	$(PY) -m streamlit run dashboard.py

docker-build:
	docker compose build

up:
	docker compose up -d --build

logs:
	docker compose logs -f

down:
	docker compose down

clean:
	rm -rf .pytest_cache **/__pycache__
