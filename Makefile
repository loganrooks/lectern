# Single verification entrypoint; CI runs exactly this.
.PHONY: verify lint type test public-check sync integration

sync:
	uv sync --extra dev

lint:
	uv run ruff check src tests tools
	uv run ruff format --check src tests tools

type:
	uv run pyright

test:
	uv run pytest

public-check:
	uv run python tools/public_safety_check.py

verify: lint type test public-check
	@echo "verify: OK"

integration:
	uv run pytest -m integration
