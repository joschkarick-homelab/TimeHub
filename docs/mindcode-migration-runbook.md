# mindcode.mindsquare.de — Repo & Image Move Runbook

> Owner/namespace placeholder `joschka.rick` is used throughout — replace with the
> real mindcode owner before running. (TODO: confirm with Forgejo/Hub admin.)

## 1. Create the repo on mindcode
Create `timehub` under the chosen owner on https://mindcode.mindsquare.de.

## 2. Move the git remote
```bash
git remote rename origin github-old          # keep a reference (optional)
git remote add origin https://mindcode.mindsquare.de/<owner>/timehub.git
git push -u origin main
git push origin --tags
```

## 3. Forgejo Actions secrets
In the mindcode repo settings → Actions → Secrets, set:
- `MINDCODE_USER` / `MINDCODE_TOKEN` — a user/token with push access to the repo's container registry.
Adjust `runs-on` in `.forgejo/workflows/build.yml` to a label your Forgejo runners advertise (e.g. `docker`).

## 4. First image build
Push to `main` (or tag `vX.Y.Z`) → the `build` workflow runs tests, then builds and pushes
`mindcode.mindsquare.de/<owner>/timehub:latest` (+ a sha tag). Confirm the package appears under the repo's Packages tab.

## 5. Hub onboarding
- Ensure the Hub operator has a Git-Host credential for `mindcode.mindsquare.de` so the Hub can pull the (private) package.
- Register the app in Hub admin using the values from the Registration Summary (see the migration plan, Task 20). Set `auth_mode=mcp-bearer` for the /mcp endpoint.

## 6. Initial data import
After the first deploy, log in as an admin (email in `ADMIN_EMAILS`), go to the admin
**Datensicherung → Wiederherstellen** page, and upload the migrated SQLite as a ZIP
(`db/timehub.sqlite`). Produce that file by running, against the old Postgres:
```bash
SOURCE_URL=postgresql+psycopg://timehub:***@<old-host>:5432/timehub \
TARGET_URL=sqlite:////tmp/timehub.sqlite \
python -m scripts.migrate_pg_to_sqlite
DATABASE_URL=sqlite:////tmp/timehub.sqlite alembic stamp head
# then: zip -j timehub-backup.zip /tmp/timehub.sqlite  (place it inside the zip as db/timehub.sqlite)
```
(Zip layout must be `db/timehub.sqlite`.)
