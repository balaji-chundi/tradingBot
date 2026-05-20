.PHONY: install run test lint format typecheck kill report deploy clean help

# We use uv (https://docs.astral.sh/uv/) for env + package management.
# Install once: curl -LsSf https://astral.sh/uv/install.sh | sh
UV := uv

help:
	@echo "make install    - create venv and install deps (dev extras included)"
	@echo "make run        - start the FastAPI app on :8000 with reload"
	@echo "make test       - run pytest"
	@echo "make lint       - ruff check + format check"
	@echo "make format     - ruff format (writes)"
	@echo "make typecheck  - mypy --strict on app/"
	@echo "make kill       - run the kill-switch entrypoint (Phase 6)"
	@echo "make report     - generate end-of-day report (Phase 6)"
	@echo "make deploy     - rsync working tree to the Linode and uv sync there"
	@echo "make clean      - remove venv and caches"

install:
	@command -v $(UV) >/dev/null || { \
		echo "uv not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"; \
		exit 1; \
	}
	$(UV) sync --extra dev

run:
	$(UV) run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	$(UV) run pytest -v

lint:
	$(UV) run ruff check app tests
	$(UV) run ruff format --check app tests

format:
	$(UV) run ruff format app tests
	$(UV) run ruff check --fix app tests

typecheck:
	$(UV) run mypy

kill:
	$(UV) run python -m app.kill

report:
	$(UV) run python -m app.scripts.eod_report

deploy:
	./scripts/deploy.sh

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info uv.lock
	find . -type d -name __pycache__ -exec rm -rf {} +
