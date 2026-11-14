.PHONY: verify verify-backend verify-frontend verify-synth up migrate seed load-master

SEED ?= 20261110

verify: verify-backend verify-frontend verify-synth

verify-backend:
	cd backend && uv sync --frozen
	cd backend && uv run ruff check .
	cd backend && uv run ruff format --check .
	cd backend && uv run mypy
	cd backend && uv run pytest

verify-frontend:
	cd frontend && npm ci
	cd frontend && npm run lint
	cd frontend && npm run typecheck
	cd frontend && npm test

verify-synth:
	cd synth && uv sync --frozen
	cd synth && uv run ruff check .
	cd synth && uv run ruff format --check .
	cd synth && uv run mypy
	cd synth && uv run pytest

# Regenerates the committed synthetic dataset; the same SEED gives byte-identical files.
seed:
	cd synth && uv run python -m synth --seed $(SEED) --out ../data/seed --force

up:
	docker compose up --build

migrate:
	cd backend && uv run alembic upgrade head

# Owner connection (OWNER_DATABASE_URL); optional MASTER=path, default data/seed/master.json.
load-master:
	cd backend && uv run python scripts/load_master_data.py $(MASTER)
