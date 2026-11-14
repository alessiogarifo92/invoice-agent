.PHONY: verify verify-backend verify-frontend up migrate load-master

verify: verify-backend verify-frontend

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

up:
	docker compose up --build

migrate:
	cd backend && uv run alembic upgrade head

# Owner connection (OWNER_DATABASE_URL); optional MASTER=path, default data/seed/master.json.
load-master:
	cd backend && uv run python scripts/load_master_data.py $(MASTER)
