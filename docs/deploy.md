# Deploy runbook (T11)

Fixed cost $0: Neon Free, Render Free, Cloudflare Pages, Langfuse Hobby, Gemini free tier.
Secrets live only in GitHub Actions secrets and the Render dashboard, never in the repo or a chat.

## 1. Database (Neon)

1. GitHub → Settings → Secrets and variables → Actions → New repository secret:
   `APP_ROLE_PASSWORD` = the `app` role password (the same one used in Render's `DATABASE_URL`).
2. GitHub → Actions → **db-setup** → Run workflow (branch `main`). It runs, in order:
   `alembic upgrade head`, `scripts/create_app_role.py` (role `app`, grants, password reset to
   the secret), `scripts/load_master_data.py` (synthetic vendors, POs, contracts). Every step is
   idempotent: run it again after any merge that adds a migration.

## 2. Demo token

The server stores only the sha256 of the token. Generate both on your own computer; keep the
token in a password manager, and put only the hash on Render.

PowerShell 7:

```powershell
$t = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24)).ToLower()
$h = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($t))).ToLower()
"TOKEN=$t"; "HASH=$h"
```

Python (any OS): see the command in `.env.example` next to `DEMO_TOKEN_HASHES`.

## 3. Backend (Render, service `invoice-agent-api`)

- Settings → Branch: `main`. Instance type: **Free** (Render defaults new services to a paid plan).
- Environment:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Neon connection string with user `app` and `APP_ROLE_PASSWORD` (`postgresql://app:…@…neon.tech/…?sslmode=require`) |
| `GEMINI_API_KEY` | the Gemini key |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Langfuse project keys |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` (EU) |
| `CORS_ORIGINS` | `https://invoice-agent-54f.pages.dev` (exact, no trailing `/`, no `*`) |
| `WORKER_ENABLED` | `true` |
| `DEMO_TOKEN_HASHES` | the HASH from step 2 (comma-separated for several tokens) |
| `LLM_PING_ON_STARTUP` | `true` only until the first Langfuse trace appears, then delete it |

A key copied from a Windows-Notepad file may start with an invisible BOM; the app strips it,
but saving the source file as plain "UTF-8" avoids the problem everywhere.

## 4. Frontend (Cloudflare Pages, project `invoice-agent`)

- Settings → Builds: production branch `main`, root `frontend`, build `npm ci && npm run build`,
  output `dist`.
- Variable `VITE_API_URL` = `https://invoice-agent-api-j413.onrender.com`. It is read at build
  time: redeploy after changing it.
- Deep links work without `_redirects` (no `404.html` in the build, so Pages serves `index.html`).

## 5. Smoke test on the live targets

1. `https://invoice-agent-api-j413.onrender.com/healthz` → `{"status":"ok",…}` (the first call can
   take ~1 min while Render wakes up).
2. Langfuse → project `invoice-agent` → a trace `llm.ping`. Then delete `LLM_PING_ON_STARTUP`.
3. `https://invoice-agent-54f.pages.dev`: the inbox loads (empty at first).
4. Submit an invoice with the demo token, e.g. `data/seed/invoices/INV-SEED-037.pdf` (IBAN
   swap). Within ~1 min it reaches "Waiting for a person" with action Block.
5. Record a decision; then `…/audit/verify` → `ok: true` and the event count went up.
6. Without the token, uploading must fail with "token required" (401).

## 6. Go / no-go

The owner decides. Rollback: Render → Events → redeploy the previous commit; Pages → Deployments
→ roll back. Migrations so far only add tables/columns, so the previous code runs on the new schema.
