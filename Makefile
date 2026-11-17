.PHONY: verify verify-backend verify-frontend verify-synth eval-l1 eval-l3 up migrate seed load-master

SEED ?= 20261110

verify: verify-backend verify-frontend verify-synth eval-l1

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

# Layer-1 eval ($0, no DB): rules + matching on the seed's ground truth. Exit 1 if a fraud or
# injection case is approved or recall falls below evals/baseline.json; refresh the baseline
# deliberately with: cd backend && uv run python -m app.evals.layer1 ... --write-baseline
eval-l1:
	cd backend && uv run python -m app.evals.layer1 --seed-dir ../data/seed --baseline ../evals/baseline.json

# Layer-3 eval (live LLM, on demand, spends free-tier quota): the full pipeline on the seed's
# flagged cases in a throwaway database it creates and drops. Needs EVAL_ADMIN_DATABASE_URL (a
# Postgres URL allowed to CREATE DATABASE) and the LLM_PROVIDER's key, e.g. GEMINI_API_KEY.
# Exit 1 if a fraud or injection case is approved; exhausted quota passes with a warning.
eval-l3:
	cd backend && uv run python -m app.evals.layer3 --seed-dir ../data/seed --report ../evals/out/layer3-report.json

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
