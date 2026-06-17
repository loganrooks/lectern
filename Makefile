# Single verification entrypoint — CI runs exactly this (DESIGN §6).
.PHONY: verify lint type test sync

sync:
	uv sync --extra dev

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

type:
	uv run pyright

test:
	uv run pytest

verify: lint type test
	@echo "verify: OK"

integration:
	uv run pytest -m integration
