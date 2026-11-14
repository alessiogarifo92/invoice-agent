.PHONY: verify verify-backend verify-frontend up migrate

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
