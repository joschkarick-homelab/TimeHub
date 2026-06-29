# TimeHub — Agent Hub Onboarding (DoD + Registration)

Branch: `feat/agent-hub-migration`. Status: code complete, full test suite green (379 passed), ruff clean.

> **Placeholder:** `joschka.rick` is a stand-in for the real mindcode owner/namespace.
> Replace it in the Dockerfile label, README, this file, and the Forgejo workflow before pushing.

## Definition of Done — Managed contract (§A.5)

| # | Requirement | State | Evidence |
|---|---|---|---|
| 1 | `EXPOSE <port>` in Dockerfile | ✅ | `EXPOSE 8000` |
| 2 | Image in an allowed registry | ⏳ open | CI pushes to `mindcode.mindsquare.de` (`.forgejo/workflows/build.yml`); first push is a user action (runbook) |
| 3 | README: package link **and** image-ref code block | ✅ | `README.md` → "## Container" |
| 4 | Hub-Waffle embedded | ✅ | `base.html`: `<script src="/embed/waffle.js" defer>` |
| 5 | `org.opencontainers.image.source` → repo | ✅ | Dockerfile LABEL (mindcode repo) |
| 6 | OCI core labels + `de.mindsquare.agenthub.category` | ✅ | Dockerfile LABEL (`category="productivity"`) |
| 7 | Startup diagnosable, fail-loud misconfig | ✅ (basic) | `config.py` `_guard_secret_key` raises in prod on a placeholder/empty key; DB URL resolved/validated at boot |
| 8 | LLM via gateway (if AI used) | ⏭ deferred | AI CSV-mapping is optional; leave disabled in the Hub, or wire the LiteLLM gateway later (plan Task 18) |
| 9 | `.env.example` copied **into** the image | ✅ | `COPY .env.example /app/.env.example` (WORKDIR `/app`) |
| 10 | `GET /health` → 200, no auth | ✅ | `app/main.py` `/health`; tested via `raw_client` (no identity) |
| 11 | Own login removed; identity from `X-MSQ-*` | ✅ | password + M365-SSO login removed; lazy resolver in `app/identity.py` |
| 12 | Local dev mode documented | ✅ | `AUTH_MODE=dev-bypass` (default outside prod); README "## Agent Hub" |
| 13 | Persistent state in named volumes; migrations forward-only + idempotent | ✅ | `VOLUME ["/app/data","/app/uploads"]`; Alembic single linear head |
| 14 | SPA/HTML `Cache-Control: no-cache, must-revalidate` | ✅ | pure-ASGI `HtmlNoCacheASGI`; tested |
| 15 | MCP `/mcp` via Hub identity (mcp-bearer) | ✅ | `HubIdentityAuthMiddleware` (pure-ASGI), no API-key/JWT |
| 16 | Onboarding form filled | ✅ | see Registration Summary below |
| 17 | Deployed + smoke test green | ⏳ open | container build/run smoke test runs in CI/Hub (no registry network locally) |
| — | Salesforce | ✅ documented | **app-managed unchanged** (service user + per-user OAuth + sync); Hub SF capability NOT used (no SF app permissions yet) |

**Self-service extras beyond the contract:** admin GUI **Datensicherung** (backup ZIP download + restore upload) — also the initial-data-import path, so no Hub-host/volume access is needed.

## Smoke test (run in CI / on the Hub host, §A.4)

```bash
# 1. App not reachable directly — only through the Hub
curl -I https://aiforge.msr2.de/timehub/            # → 401 or Hub login

# 2. Container up
docker ps --filter "label=msq.app=timehub"          # Status "Up"

# 3. Health from the hub container
docker exec <hub-container> wget -qO- http://app-timehub:8000/health   # → {"status":"ok"}

# 4. Browser end-to-end: https://aiforge.msr2.de/timehub/ → Hub login → app loads

# 5. HTML no-cache
docker exec <hub-container> wget -qO- --server-response \
  "http://app-timehub:8000/" 2>&1 | grep -i cache-control   # → no-cache, must-revalidate
```

## Registration Summary (Step F — paste into Hub admin)

```
Hub-Registrierung für TimeHub
═══════════════════════════════════════════
Anzeigename:    TimeHub
Slug:           timehub
Beschreibung:   Zentrale Zeiterfassung – Erfassung, Import, Export, Reporting
Icon:           ⏱️
Kategorie:      productivity

Image-Ref:      mindcode.mindsquare.de/<owner>/timehub:latest      # owner: confirm (placeholder joschka.rick)
Container-Port: 8000
Health-Pfad:    /health

Zugriff:        alle angemeldeten Benutzer
Sichtbarkeit:   public

Env-Variablen (Runtime-Tab):
  APP_ENV=production
  AUTH_MODE=hub
  ADMIN_EMAILS=rick@mindsquare.de
  BASE_PATH=/timehub
  MCP_ENABLED=true

Secret-Werte (Runtime-Tab):
  SECRET_KEY=<48-byte random: python -c "import secrets; print(secrets.token_urlsafe(48))">

Volumes (Runtime-Tab):
  appdata-timehub-data=/app/data
  appdata-timehub-uploads=/app/uploads

MCP / auth_mode:  mcp-bearer aktivieren (Hub übernimmt die M365-OAuth für /timehub/mcp)
Timeout:          30s (Default)
Body-Limit:       Default ok; höher setzen falls große CSV-/ZIP-Uploads (Restore)
Salesforce-Integration: nein (app-managed Service-User/OAuth bleibt intern)
```

## Open coordination items (before go-live)

1. **mindcode owner/namespace** — replace `joschka.rick` placeholder everywhere (Dockerfile, README, this file, runbook, Forgejo `runs-on`/secrets).
2. **Hub Git-Host credential** for `mindcode.mindsquare.de` so the Hub can pull the private package.
3. **`auth_mode=mcp-bearer`** set on the app in Hub admin.
4. **`SECRET_KEY`** generated and stored as a Hub secret.
5. **Initial data import:** run `scripts/migrate_pg_to_sqlite.py` against the old Postgres → zip as `db/timehub.sqlite` → upload via admin **Datensicherung → Wiederherstellen** (see `docs/mindcode-migration-runbook.md`).
