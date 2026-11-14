# invoice-agent

AI agent that checks synthetic B2B supplier invoices (PDF): structured extraction, RAG over purchase orders / contracts / vendor master data, tool-based checks, anomaly flags (IBAN change / BEC fraud, duplicates, PO mismatch, VAT/total errors, prompt injection), a proposed action (approve / clarify / block) and human-in-the-loop approval with a full audit log. Portfolio project; decisions live in `docs/decisions.md`.

Hard constraints: synthetic data only (no real invoices or personal data in the repo), minimal cost (capped LLM spend, no fixed paid services), public repo — never commit secrets.

## Bootstrap: load the shared skills repo (do this before any work)

This project relies on the private repo `alessiogarifo92/claude-skills` (skills, agents, global rules). A remote session (Claude Code on the web, a cloud container, a GitHub Action) does not have `~/.claude/`, so without this step none of those rules apply.

1. Attach it: `mcp__claude-code-remote__add_repo` with `owner=alessiogarifo92`, `repo=claude-skills` (access `read`), run the clone command it returns, then `register_repo_root`.
2. If the clone already existed (an earlier attach, a container reused), update it first: `git -C <clone> fetch origin main`, then fast-forward (`git -C <clone> merge --ff-only origin/main`, or rebase a working branch). The project's `.claude/session-start-hook.sh` does this on every session start and prints the skills commit and whether it is behind: read that line and act on it. If the project has no `.claude/session-start-hook.sh`, add it and its `.claude/settings.json` entry (copy `<clone>/templates/session-start-hook.sh`; the JSON is in its header) and commit them on the working branch without asking. State the skills commit in your first visible message. Run `bash <clone>/scripts/install.sh` (links every skill into `~/.claude/skills`, agents into `~/.claude/agents`, `CLAUDE.md` into `~/.claude/CLAUDE.md`), then check `ls ~/.claude/skills ~/.claude/agents`. If the running session does not pick the agents up, dispatch `general-purpose` with an explicit `model` and the agent file's body pasted into the brief, plus an instruction to load that agent's frontmatter `skills:` with the Skill tool first and to respect its read-only / no-git limits. If a named skill is still missing from the Skill list, Read `<clone>/<skill>/SKILL.md` directly and follow it. Read the clone's `README.md`.
3. New application or project: invoke `project-lifecycle` first, then `autonomous-workflow` to run its phases unattended. An existing backlog or multi-stage goal: `autonomous-workflow`. A single ticket: `product-ownership`.
   Claude Code detects agent files added to an existing `~/.claude/agents/` without a restart, but not when that directory did not exist at session start (typical in a fresh container). Then either restart the session once, or use the fallback above for this session. `templates/session-start-hook.sh` can install everything before the session starts (best effort).
4. If the attach fails, say so in visible text and ask the user how to proceed. Never continue silently without the rules.

Run / preview (manual gate):
- Local, whole stack: `docker compose up --build` (Postgres 16 on :5432, API on http://localhost:8000, Vite on http://localhost:5173). Check `http://localhost:8000/healthz` returns 200 and the page on :5173 loads.
- Migrations: `make migrate` (uses `OWNER_DATABASE_URL`; the API itself uses the restricted `DATABASE_URL`). Create the restricted role with `backend/scripts/create_app_role.py`.
- Gate before any push: `make verify` (ruff, mypy, pytest, eslint, vue-tsc, vitest). Needs uv and Node 22+.
- LLM + tracing smoke (T0): `cd backend && uv run python -m app.llm_ping` with GEMINI_API_KEY and the LANGFUSE_* keys set; on Render set `LLM_PING_ON_STARTUP=true` once, check the trace in Langfuse, then remove it.
- Per-PR preview: frontend only, Cloudflare Pages preview deploys (enabled once the Pages project is linked); the API has no per-PR preview.

Repo visibility: public (check: `gh api repos/<owner>/<repo> --jq .private`). Private means finite GitHub Actions minutes: `npm run verify` / `make verify` locally is the push gate, and a CI job that never starts follows `test-verification`'s billing fallback.

Needed environment variables / connectors for this project: `GEMINI_API_KEY` (primary LLM, free tier), `GROQ_API_KEY` (optional free fallback), `ANTHROPIC_API_KEY` (optional, off by default) — all CI and hosting secrets only, never in the repo (see the phase 2 amendment in `docs/decisions.md`); further variables are added by the tickets that introduce them. No `.mcp.json`.
