# TimeHub

Zentrale Zeiterfassungs-App für Consultants. Erfasst Zeiten granular
(Viertelstundentakt, mehrere Projekte/Tag) oder grob, dient als Drehscheibe
zwischen den Quell-Tools der Berater und den Zielsystemen
(Jira / Salesforce / BCS / Intern / CSV-Templates).

API-first, selbst gehostet, Docker-deploybar (Proxmox-LXC freundlich).

---

## 1. Tech-Stack & Begründung

| Schicht        | Wahl                                | Warum                                                                                  |
| -------------- | ----------------------------------- | -------------------------------------------------------------------------------------- |
| Sprache        | Python 3.12                         | pragmatisch, viele Integrations-Libs für Jira/Salesforce/BCS später                    |
| API            | FastAPI                             | API-first, OpenAPI-Doku automatisch, Pydantic-Validierung, async-fähig                 |
| ORM            | SQLAlchemy 2.x + Alembic            | reife, versionsstabile Migrationen, getestet, sowohl Postgres als auch SQLite          |
| DB             | PostgreSQL (prod) / SQLite (dev)    | Postgres im Container; SQLite ohne Setup für lokale Entwicklung                        |
| Frontend       | Jinja2 + Tailwind (CDN) + HTMX-ready| simpel, wartbar, kein Build-Step; UI bleibt dünner Wrapper um die API                  |
| Auth           | JWT (User) + API-Key (Tools)        | JWT für UI-Sessions, hash-basierte API-Keys für externe Tool-Intake                    |
| Container      | Single-Image, Multistage-Dockerfile | ein Image, Postgres als zweiter Compose-Service mit persistentem Volume                |

OAuth (Microsoft / Authentik) und echte API-Pushes zu Jira/Salesforce/BCS sind
vorbereitet (Sync-Target-Abstraktion, Metadata pro Projekt/Eintrag), aber
absichtlich nicht Teil von v1.

---

## 2. Projektstruktur

```
TimeHub/
├── app/
│   ├── main.py              # FastAPI-App, Middleware, Routen
│   ├── config.py            # Pydantic-Settings (.env)
│   ├── db.py                # Engine + SessionLocal
│   ├── deps.py              # Auth-Dependencies (JWT, API-Key, Session-Cookie)
│   ├── security.py          # Passwort-Hash, JWT, API-Keys
│   ├── models/              # SQLAlchemy-Modelle
│   ├── schemas/             # Pydantic-Schemas
│   ├── api/                 # REST-Router (/api/v1/...)
│   ├── services/            # Reporting, CSV-Import, Bootstrap
│   └── web/                 # Server-rendered UI (Templates)
├── alembic/                 # DB-Migrationen
├── tests/                   # Pytest-Smoke-Tests
├── scripts/entrypoint.sh    # alembic upgrade + uvicorn
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## 3. Datenbankschema (Kernentitäten)

```
users(id, email UNIQUE, full_name, hashed_password, is_admin, is_active, created_at)

api_keys(id, user_id → users, name, prefix, key_hash UNIQUE,
         last_used_at, revoked_at, created_at)

projects(id, name, code UNIQUE, customer, color, status,
         default_sync_target, sync_metadata JSON,
         created_at, updated_at)

time_entries(id, user_id → users, project_id → projects,
             entry_date, start_time?, end_time?, duration_minutes,
             description, tags JSON,
             sync_target_override?, sync_metadata_override JSON,
             sync_status, source, external_ref,
             created_at, updated_at)

csv_templates(id, name UNIQUE, columns JSON, separator, date_format,
              encoding, decimal_separator, created_at, updated_at)
```

Designentscheidungen:

- **`duration_minutes` ist authoritativ.** Start/Ende sind optional (für reine
  „8h auf ein Projekt"-Erfassung). Bei Angabe von Start+Ende wird die Dauer
  automatisch berechnet und auf Konsistenz geprüft.
- **`tags` als JSON-Array** statt eigener Tabelle – flexibel, Reporting filtert
  drüber. Eine Normalisierung lässt sich nachträglich einführen, ohne API zu
  brechen.
- **Sync-Konfiguration zweistufig:** Default am Projekt, optional pro Eintrag
  überschreibbar (`sync_target_override`, `sync_metadata_override`). Damit
  geht der häufige Mix „Projekt geht nach Jira, aber dieser eine Eintrag
  ist intern".
- **API-Key-Speicherung als SHA-256-Hash + Klartext-Prefix.** Volle Keys
  werden einmalig bei Erzeugung zurückgegeben.

---

## 4. API-Design (Übersicht)

Alle Routen unter `/api/v1`. Vollständige Doku unter `/docs` (Swagger UI)
bzw. `/redoc`. OpenAPI-JSON unter `/openapi.json`.

| Bereich         | Methode + Pfad                                        | Zweck                              |
| --------------- | ----------------------------------------------------- | ---------------------------------- |
| Auth            | `POST /auth/login`                                    | JWT holen                          |
|                 | `GET  /auth/me`                                       | aktuellen User abfragen            |
|                 | `POST /auth/api-keys`                                 | neuen API-Key erstellen (einmalig) |
|                 | `GET  /auth/api-keys`                                 | eigene Keys auflisten              |
|                 | `DELETE /auth/api-keys/{id}`                          | Key widerrufen                     |
| Users (admin)   | `GET/POST /users`, `GET/PATCH/DELETE /users/{id}`     | Benutzerverwaltung                 |
| Projects        | `GET/POST /projects`, `GET/PATCH/DELETE /projects/{id}` | Projekte                         |
| Time Entries    | `GET/POST /time-entries`                              | Liste/anlegen (Filter: from/to/project/user/sync_target/tag) |
|                 | `POST /time-entries/bulk`                             | Massenerfassung                    |
|                 | `GET/PATCH/DELETE /time-entries/{id}`                 | Detail                             |
| Intake          | `POST /intake/time-entries`                           | externes Tool drückt Einträge rein |
|                 | `POST /intake/csv` (multipart: file + mapping JSON)   | CSV-Import mit flexiblem Mapping   |
| Reporting       | `GET /reports/timesheet?format=json\|csv\|markdown`   | Filterbarer Timesheet-Export       |
| CSV-Templates   | `GET/POST /csv-templates`, `GET/PATCH/DELETE /csv-templates/{id}` | Wiederverwendbare CSV-Export-Profile |
| Import-Formate  | `GET/POST /import-formats`, `GET/PATCH/DELETE /import-formats/{id}` | Wiederverwendbare CSV-Input-Profile (Toggl, Clockify, …) |
|                 | `POST /import-formats/suggest` (multipart: file)      | One-Shot KI-Mapping über Claude    |
|                 | `POST /import-formats/{id}/run` (multipart: file)     | Gespeichertes Format auf CSV anwenden |
| System          | `GET /healthz`, `GET /readyz`                         | Liveness/Readiness                 |

Auth-Schemata, die alle geschützten Routen akzeptieren:

- **`Authorization: Bearer <jwt>`** – für UI und Skripte
- **`X-API-Key: thk_…`** – für externe Tool-Intake-Integrationen
- Session-Cookie als Fallback für die Web-UI

---

## 5. Setup

### Voraussetzungen
- Docker + Docker Compose (für das Deployment)
- alternativ Python 3.11+ für lokale Entwicklung

### Sicherheit & Pflicht-Konfiguration

- **`SECRET_KEY` ist in Produktion zwingend.** Derselbe Schlüssel signiert
  JWTs *und* Session-Cookies. Bei `APP_ENV=production` (Default) verweigert die
  App den Start, wenn `SECRET_KEY` fehlt oder noch den Platzhalter aus
  `.env.example` enthält. Einen starken Wert erzeugen:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
  Für lokale Entwicklung ohne eigenen Secret stattdessen `APP_ENV=dev` setzen.
- **CSRF-Schutz:** Die session-cookie-authentifizierte Web-UI prüft auf allen
  schreibenden Requests ein Session-Token (als `csrf_token`-Formularfeld bzw.
  `X-CSRF-Token`-Header). Die JSON-API unter `/api/*` authentifiziert
  ausschließlich über Bearer-Token oder API-Key (kein Session-Cookie).

### Lokal mit Docker (empfohlen)

```bash
cp .env.example .env
# SECRET_KEY (Pflicht!) und INITIAL_ADMIN_PASSWORD anpassen
docker compose up -d --build
docker compose logs -f app
```

Aufrufen:

- Web-UI:     http://localhost:8000/
- API-Doku:   http://localhost:8000/docs
- Health:     http://localhost:8000/healthz

Beim ersten Start legt die App den Admin gemäß `INITIAL_ADMIN_*` an,
sofern noch keine Nutzer existieren.

### Lokal ohne Docker (SQLite)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# in .env: DATABASE_URL=sqlite:///./data/timehub.sqlite
# in .env: APP_ENV=dev   (erlaubt den Start ohne produktiven SECRET_KEY)
mkdir -p data
alembic upgrade head
uvicorn app.main:app --reload
```

### Tests

```bash
pip install pytest
pytest -q
```

---

## 6. Deployment auf Proxmox-LXC

Ablauf:

1. Push auf `main` → **build**-Workflow baut das Image und pushed nach
   `ghcr.io/joschkarick-homelab/timehub` (Tags: `latest`, `sha-<commit>`).
2. Nach erfolgreichem Build feuert **deploy**-Workflow automatisch:
   verbindet sich über die [Tailscale GitHub Action](https://github.com/tailscale/github-action)
   ins Homelab, rendert eine `stack.env` aus den GitHub-Secrets, kopiert
   `docker-compose.prod.yml` + `stack.env` per Tailscale SSH auf den LXC
   und führt `docker compose pull && up -d` aus.

Compose-Dateien:

- `docker-compose.yml` — baut lokal, für Entwicklung
- `docker-compose.prod.yml` — pullt `ghcr.io/...:${TIMEHUB_TAG:-latest}`
  und liest seine Werte aus `stack.env`

### Einmalige Einrichtung

**LXC vorbereiten** (einmalig pro Host, eine einzige SSH-Session):

```bash
# 1. Docker
curl -fsSL https://get.docker.com | sh

# 2. Tailscale mit SSH, Host taggen
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh --advertise-tags=tag:server

# 3. Geteilter Deploy-User mit Docker-Rechten; bekommt /opt, damit der
#    Workflow per-App-Verzeichnisse ohne sudo anlegen darf
sudo useradd -m -G docker deploy
sudo chown deploy:deploy /opt
```

In der Tailscale-Admin-Console einmalig die ACL-Regel ergänzen:

```jsonc
{
  "ssh": [
    {"action": "accept", "src": ["tag:ci"], "dst": ["tag:server"], "users": ["deploy"]}
  ]
}
```

Damit ist der LXC fertig für *alle* zukünftigen Apps — jede neue App
braucht danach nur noch GitHub-Secrets, kein weiteres SSH.

**GHCR-Package nach dem ersten Build auf Public stellen**: auf
github.com unter dem Repo → Packages → `timehub` → Package settings →
Change visibility → Public. Spart den `docker login` auf jedem LXC; die
Image-Layer enthalten App-Code, aber keine Runtime-Secrets. Wenn du das
Package privat halten willst, brauchst du einmal pro LXC ein
`docker login ghcr.io` mit einem PAT (Scope `read:packages`).

**GitHub-Secrets** (alle im Repo unter Settings → Secrets and variables → Actions):

Infrastruktur (zwingend):

| Name | Inhalt |
| --- | --- |
| `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET` | Tailscale OAuth-Client mit `tag:ci` |
| `DEPLOY_USER` | SSH-User auf dem LXC, z.B. `deploy` (geteilt über alle Apps) |
| `DEPLOY_HOST` | Tailscale-DNS-Name oder IP des LXC |
| `DEPLOY_PATH` | Zielordner, z.B. `/opt/timehub` — wird vom Workflow angelegt |

App-Konfiguration (werden 1:1 in `stack.env` geschrieben — leere Secrets
werden übersprungen, die Defaults aus dem Compose greifen):

| Name | Pflicht? | Hinweis |
| --- | --- | --- |
| `SECRET_KEY` | ja | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `POSTGRES_PASSWORD` | ja | beliebige Zeichen erlaubt — die App baut die DB-URL aus diskreten Fields, nicht via String-Interpolation |
| `INITIAL_ADMIN_EMAIL` | ja | wird nur beim Erststart benutzt |
| `INITIAL_ADMIN_PASSWORD` | ja | s.o. |
| `INITIAL_ADMIN_NAME` | nein | Default: `Admin` |
| `APP_PORT` | nein | Default: `8000` |
| `TIMEHUB_TAG` | nein | Default: `latest` — z.B. auf `sha-abc1234` pinnen |
| `CORS_ORIGINS` | nein | Default: `*` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | nein | Default: 30 Tage |
| `ANTHROPIC_API_KEY` | nein | aktiviert KI-Mapping-Vorschläge |
| `AI_MAPPING_MODEL` | nein | Default: `claude-sonnet-4-6` |

### Erster Deploy

`workflow_dispatch` der `Deploy to Homelab`-Action in GitHub auslösen
(oder einen Dummy-Commit auf `main` machen). Danach läuft jedes spätere
Push auf `main` automatisch durch.

### Manueller Deploy auf dem LXC (Fallback)

Wenn der Tailscale-Pfad mal nicht geht, lassen sich die gleichen Dateien
manuell verteilen:

```bash
scp docker-compose.prod.yml stack.env timehub@lxc:/opt/timehub/
ssh timehub@lxc 'cd /opt/timehub && docker compose -f docker-compose.prod.yml --env-file stack.env pull && docker compose -f docker-compose.prod.yml --env-file stack.env up -d'
```

`stack.env` ist dann eine normale `KEY=value`-Datei mit den oben gelisteten
Variablen.

### Reverse-Proxy

Traefik / Caddy / Nginx vor `:${APP_PORT:-8000}` schalten, TLS dort
terminieren. Healthcheck-Endpoint: `/healthz`.

### Backup

Volumes `timehub_db` und `timehub_uploads` sichern. Beispiel für einen
nightly DB-Dump auf den Host:

```bash
docker compose -f docker-compose.prod.yml --env-file stack.env exec -T db \
  pg_dump -U timehub timehub | gzip > /backup/timehub-$(date +%F).sql.gz
```

---

## 7. KI-gestütztes Import-Mapping

Verschiedene Consultants bringen CSV-Exports aus unterschiedlichen Tools mit
(Toggl, Clockify, Excel-Templates eines Kunden, …). Statt für jedes Format
ein Mapping manuell zu bauen, legt TimeHub eine zentrale Bibliothek von
Importformaten an und nutzt Claude für den ersten Vorschlag.

**Flow:**

1. `/import-formats/new` → Name + Beispiel-CSV hochladen
2. App schickt die ersten ~15 Zeilen an Claude (`claude-sonnet-4-6`,
   zentraler `ANTHROPIC_API_KEY` aus `.env`, kein User-eigener Key nötig)
3. Modell liefert ein JSON-Mapping (Trennzeichen, Datumsformat, Spalten →
   TimeHub-Felder) → wird im UI vorgeblendet
4. Nutzer prüft/korrigiert per Dropdown und speichert
5. `/import` → Format aus Liste wählen + CSV hochladen → Einträge importiert

**Sichtbarkeit:**

- Standardmäßig privat (nur Ersteller sieht es)
- Admins können ein Format global schalten (für alle sichtbar)
- Globale Formate stehen oben in der Liste

**KI optional:** Ohne `ANTHROPIC_API_KEY` läuft alles weiter, nur der
„Vorschlag erzeugen"-Button meldet, dass die KI-Hilfe deaktiviert ist; das
Mapping kann manuell oder via API gepflegt werden.

---

## 8. Roadmap (bewusst out of scope für v1)

- OAuth (Microsoft Entra ID, Authentik) zusätzlich zur lokalen Auth
- Echte Push-Sync nach Jira (Worklogs), Salesforce, BCS
- Reaktive UI-Komponenten (HTMX-Detailbearbeitung, Tag-Autocomplete)
- Native Mobile-Apps
