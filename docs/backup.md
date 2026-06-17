# Backup & Restore

TimeHub wird mit einer zweistufigen, automatisierten Backup-Pipeline gesichert.
Sie folgt der **3-2-1-Regel** (Live-Daten + lokale Kopie + Off-site-Kopie) und
dem **Großvater-Vater-Sohn-Prinzip** (täglich / wöchentlich / monatlich /
jährlich gestaffelte Aufbewahrung).

## Was wird gesichert?

| Datenbestand | Volume | Backup? |
|--------------|--------|---------|
| Postgres-Datenbank (User, Zeiteinträge, Import-Formate) | `timehub_db` | **ja** — das wichtigste Asset |
| Secrets / Config (`stack.env`) | Host `/opt/timehub` | ja (optional, in den Dump-Satz aufgenommen) |
| Hochgeladene CSVs | `timehub_uploads` | **nein** — nur einmalig beim Import relevant |

## Architektur

```
pg_dump -Fc            restic backup            restic copy
(konsistent,    ──►    lokales Repo      ──►    Hetzner S3 (off-site,
 kein Downtime)        /mnt/backup-hdd          verschlüsselt, dedupliziert)
                            │                         │
                       GFS-Pruning               GFS-Pruning (länger)
```

**Warum der `pg_dump`-Zwischenschritt?** restic allein würde das laufende
`timehub_db`-Volume auf Dateiebene kopieren — das kann mitten im Schreibbetrieb
ein **inkonsistentes** Backup erzeugen. `pg_dump` liefert dagegen einen
transaktionskonsistenten, versionsunabhängigen Dump ohne Downtime. restic
übernimmt danach nur noch Verschlüsselung, Transport, Dedup und Rotation.

- **Lokales Repo (HDD):** schneller Restore, kürzere Aufbewahrung.
- **S3-Repo (Hetzner):** off-site, schützt gegen Verlust der LXC / des Hosts,
  längere Aufbewahrung.

## Einrichtung (einmalig, auf dem LXC-Host)

1. **HDD mounten** unter `/mnt/backup-hdd` (Mountpoint dauerhaft in
   `/etc/fstab` eintragen).

2. **restic installieren:**
   ```bash
   apt-get install -y restic   # oder: das offizielle Binary von restic.net
   ```

3. **Skripte bereitstellen** — sie liegen im Repo unter `scripts/` und werden
   mit dem Compose-Stack nach `/opt/timehub` deployt. Falls nicht vorhanden,
   manuell kopieren.

4. **restic-Passwort erzeugen** (für beide Repos):
   ```bash
   mkdir -p /etc/timehub
   openssl rand -base64 32 > /etc/timehub/restic-password
   chmod 600 /etc/timehub/restic-password
   ```
   > ⚠️ Dieses Passwort **separat sichern** (z.B. Passwortmanager). Ohne das
   > Passwort ist das Off-site-Backup unwiederbringlich verschlüsselt.

5. **Config anlegen:**
   ```bash
   install -m 600 /opt/timehub/scripts/backup.env.example /etc/timehub/backup.env
   $EDITOR /etc/timehub/backup.env   # S3_REPO + AWS_* Credentials eintragen
   ```
   Die Hetzner-S3-Zugangsdaten erhältst du in der Hetzner Cloud Console unter
   *Object Storage*. `S3_REPO` hat die Form
   `s3:https://<location>.your-objectstorage.com/<bucket>/timehub`
   (`<location>` = `fsn1`, `nbg1` oder `hel1`).

6. **Erster Lauf manuell testen:**
   ```bash
   TIMEHUB_BACKUP_CONFIG=/etc/timehub/backup.env /opt/timehub/scripts/backup.sh
   restic -r /mnt/backup-hdd/restic-repo snapshots
   ```

7. **Timer aktivieren:**
   ```bash
   cp /opt/timehub/scripts/systemd/timehub-backup.* /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now timehub-backup.timer
   systemctl list-timers timehub-backup.timer   # nächste Laufzeit prüfen
   ```

## GFS-Retention

Eingestellt in `/etc/timehub/backup.env`. Standardwerte:

| Stufe | Lokal (HDD) | Off-site (S3) |
|-------|-------------|---------------|
| täglich (Sohn) | 7 | 14 |
| wöchentlich (Vater) | 4 | 8 |
| monatlich (Großvater) | 6 | 12 |
| jährlich | – | 3 |

restic setzt das nach jedem Lauf via `forget --prune` automatisch um.

## Restore

> Der Restore ist **destruktiv**: er überschreibt die aktuelle Datenbank. Die
> App wird dabei kurz gestoppt und danach wieder gestartet.

```bash
# Verfügbare Snapshots ansehen
restic -r /mnt/backup-hdd/restic-repo snapshots

# Neuesten Snapshot von der HDD zurückspielen
/opt/timehub/scripts/restore.sh --from local

# Bestimmten Snapshot von der HDD
/opt/timehub/scripts/restore.sh --from local --snapshot <id>

# Off-site aus S3 (z.B. wenn die HDD/LXC verloren ist)
/opt/timehub/scripts/restore.sh --from s3
```

Nach einem Komplettverlust der LXC: Stack neu deployen (Compose +
`stack.env`), dann `restore.sh --from s3` ausführen. Ist `stack.env` im Backup
enthalten (`INCLUDE_ENV_FILE=true`), liegt es im wiederhergestellten
Snapshot-Verzeichnis und kann von dort übernommen werden.

## Restore regelmäßig testen

Ein ungetestetes Backup ist kein Backup. Periodisch (z.B. quartalsweise) auf
einem Wegwerf-Stack einen Restore durchspielen und die Daten stichprobenartig
prüfen. Zusätzlich die Repo-Integrität kontrollieren:

```bash
restic -r /mnt/backup-hdd/restic-repo check
restic -r "$S3_REPO" check
```

## Monitoring

- `systemctl status timehub-backup.service` — Ergebnis des letzten Laufs
- `journalctl -u timehub-backup.service` — Log-Ausgabe
- Das Skript bricht mit Fehlercode ab, wenn der Dump leer ist, das restic-
  Passwort fehlt oder die HDD nicht gemountet ist — bestehende Backups werden
  in dem Fall **nicht** überschrieben.
