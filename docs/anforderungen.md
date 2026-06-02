# TimeHub — Funktionale Anforderungen

Vollständige Verhaltensbeschreibung. Wer diesen Stand reproduzieren will,
findet hier jede Funktion und API. Sprache und UI sind deutsch.

Begriffe: **Nutzer** = eingeloggte Person (Rolle Admin oder Consultant).
**Eintrag** = Zeiteintrag. **Ziel** = Sync-Ziel. **Format** = Importprofil.

---

## 1. Grundprinzipien

- **FR-CORE-1** Zwei Rollen: *Admin* und *Consultant*. Admin verwaltet Nutzer,
  Projekte, globale Einstellungen, globale Formate.
- **FR-CORE-2** Single Source of Truth für die Dauer: `duration_minutes`. Start/
  Ende optional; sind beide gesetzt, wird die Dauer daraus abgeleitet.
- **FR-CORE-3** Sync zweistufig: Projekt hat ein Default-Ziel, Eintrag kann es
  + zielabhängige Felder überschreiben.
- **FR-CORE-4** API-first: jede Kernfunktion unter `/api/v1`; OpenAPI unter
  `/docs`, `/redoc`, `/openapi.json`.
- **FR-CORE-5** Lade-Indikator: Bei jedem Formular-Submit zeigt ein globales
  Overlay einen Spinner mit kontextpassendem Text („KI analysiert die CSV …",
  „Salesforce wird abgefragt …", „Import läuft …"). Forms können mit
  `data-no-loading="true"` opt-out.

---

## 2. Authentifizierung

- **FR-AUTH-1** Web-Login: `GET /login` (Formular), `POST /login` → Session-
  Cookie + Redirect `/`. Ungültige Credentials → 401-Seite. `POST /logout`
  räumt die Session.
- **FR-AUTH-2** API-Login: `POST /api/v1/auth/login` mit `{email, password}` →
  `{access_token, token_type=bearer}`.
- **FR-AUTH-3** Drei Auth-Wege geschützter Endpunkte (Reihenfolge):
  `Authorization: Bearer <jwt>`, `X-API-Key: <key>`, Session-Cookie. Inaktive
  Nutzer/falsche Credentials → 401.
- **FR-AUTH-4** JWT-Gültigkeit konfigurierbar (`ACCESS_TOKEN_EXPIRE_MINUTES`,
  Default 30 Tage).
- **FR-AUTH-5** API-Keys: `POST /api/v1/auth/api-keys {name}` → vollständigen
  Key **einmalig** (Hash + Prefix in DB), `GET` (Liste), `DELETE /{id}`
  (widerrufen). `last_used_at` bei Nutzung. `GET /api/v1/auth/me` → aktueller
  User.
- **FR-AUTH-6** Bootstrap-Admin: Existiert kein Nutzer beim Start, wird aus
  `INITIAL_ADMIN_EMAIL`/`_PASSWORD`/`_NAME` ein Admin angelegt.

---

## 3. Nutzer & Profil

- **FR-USER-1** `/users` (Admin): Liste + Anlegen (E-Mail, Name, Passwort,
  Admin-Flag); doppelte E-Mail → Fehlermeldung. Eigenen Account nicht
  deaktivierbar, eigene Adminrechte nicht entziehbar. `POST
  /users/{id}/toggle-active`, `POST /users/{id}/toggle-admin`.
- **FR-USER-2** API: `GET/POST /api/v1/users`, `GET/PATCH/DELETE
  /api/v1/users/{id}` (alle Admin). PATCH: full_name, is_admin, is_active,
  password. Doppelte E-Mail → 409.
- **FR-USER-3** Felder: id, email (uniq), full_name, is_admin, is_active,
  ai_hints? (persönliche KI-Vorgaben), created_at. Die alten
  `salesforce_user_id`/`salesforce_contact_id`-Felder bleiben in der DB
  (legacy, API-zugänglich), sind aber **nicht mehr in der UI** — Resource
  kommt aus der Salesforce-Assignment.
- **FR-PROF-1** `/profile` zeigt/ändert: Anzeigename, persönliche KI-Hinweise.
  E-Mail read-only. Rolle/Status nur Anzeige. `POST /profile` speichert,
  Flash „Profil gespeichert".

---

## 4. Globale Einstellungen (Admin)

- **FR-SET-1** Globale KI-Vorgaben auf `/users` (Sektion „Globale KI-Vorgaben"),
  `POST /settings/ai-hints`. Werden bei jedem KI-Aufruf zusätzlich zu den
  persönlichen Hinweisen verbindlich mitgegeben (als separater System-Block).
- **FR-SET-2** Theme-Auswahl (`POST /settings/theme`, Cookie 1 Jahr,
  SameSite=lax): `indigo` (Default), `mindsquare`, `dark`. Unbekannt → indigo.
  In Navigation Desktop & Mobil.
- **FR-SET-3** Salesforce-Zugangsdaten auf `/users` (Sektion „Salesforce-
  Integration"): Username, Passwort, Security-Token, Login-URL (Default
  `login.salesforce.com`), API-Version (Default `60.0`). `POST
  /settings/salesforce`. Passwort/Token nur überschrieben, wenn Wert
  eingegeben; nie zurückgespiegelt (nur „gesetzt"-Status). „Verbindung
  testen"-Button (`POST /settings/salesforce/test`) macht einen SOAP-
  Probelogin und meldet Erfolg/Fehler.

---

## 5. Projekte

- **FR-PROJ-1** Felder: name, code (uniq, stabiler Schlüssel), customer?, color
  (Hex, Default `#6366f1`), status (`active`/`inactive`), default_sync_target
  (`intern`/`jira`/`salesforce`/`bcs`/`none`), sync_metadata (zielabhängige
  Felder), created_at/updated_at.
- **FR-PROJ-2** Code optional / automatisch erzeugt: Beim Anlegen wird er aus
  dem Namen abgeleitet (`[^A-Za-z0-9]+` → `-`, Großbuchstaben, ≤60, am Rand
  getrimmt; leerer Rest → `PROJEKT`; bei Kollision `-2`/`-3` …). Beim
  Bearbeiten bleibt der bestehende Code, wenn das Feld leer bleibt; expliziter
  Code wird honoriert.
- **FR-PROJ-3** `display_label`: `"CODE – Name (Kunde)"`. Wenn Code und Name
  gleich oder Name leer → nur Code. Kunde-Suffix nur, wenn customer gesetzt.
  Wird in **allen** Projekt-Dropdowns angezeigt (Dashboard, Kalender, Edit, …).
  Pro Projekt-Eintrag (Liste/Block) zeigt ein Farbpunkt die `color`.
- **FR-PROJ-4** `/projects` (Admin) listet Projekte mit Code/Name/Kunde/Ziel/
  Status; Anlegen-Formular: Name (Pflicht), Code (optional), Kunde, Ziel,
  Farbe, plus zielabhängige Felder. Liste markiert `⚠ unvollständig`, wenn ein
  Projekt-Pflichtfeld des Ziels fehlt. Löschen blockiert, wenn Zeiteinträge
  existieren.
- **FR-PROJ-5** API: `GET /api/v1/projects` (optional `?status=`), `POST`
  (Admin), `GET/PATCH/DELETE /api/v1/projects/{id}` (PATCH/DELETE Admin).
  `sync_metadata` ist freier JSON-Dict. Doppelter Code → 409.

---

## 6. Sync-Ziele & zielabhängige Felder

- **FR-SYNC-1** Ziele: `jira`, `salesforce`, `bcs`, `intern`, `none`. `intern`
  und `none` brauchen nie einen Push (immer „sync-bereit").
- **FR-SYNC-2** Feld-Register (Code-definiert, erweiterbar):

  | Ziel | Feld | Ebene | Pflicht | Muster |
  | --- | --- | --- | --- | --- |
  | jira | default_issue (Standard-Ticket) | Projekt | nein | `[A-Z][A-Z0-9]+-\d+` |
  | jira | issue_key (Jira-Ticket) | Eintrag | **ja** | wie oben; erbt `default_issue` |
  | salesforce | assignment_id (Projektbesetzung) | Projekt | **ja** | 15-/18-stellige SF-Id; UI-Dropdown der aktiven PBs des Users (Live-SOQL) |
  | salesforce | remote (Remote / Vor Ort) | Eintrag | nein | Picklist `true`/`false`, Default „Remote"; per Import-Transform befüllbar |
  | bcs | subject | Eintrag | **ja** | — |
  | bcs | task | Eintrag | **ja** | — |
  | intern / none | — | — | — | — |

- **FR-SYNC-3** Speicherort: `projects.sync_metadata[ziel][key]` und
  `time_entries.sync_metadata_override[ziel][key]`.
- **FR-SYNC-4** Bedingte Anzeige: in Projekt-/Eintrag-Masken erscheinen **nur**
  die Felder des aktuell relevanten Ziels (Projekt-Default; am Eintrag das
  Override, sonst der Projekt-Default).
- **FR-SYNC-5** Vererbung: ein leeres Eintrag-Feld erbt den Projekt-Default
  (z. B. Jira `default_issue` → `issue_key`).
- **FR-SYNC-6** Validierung warnt, blockiert nicht: malformierte Werte werden
  gespeichert, aber als Format-Fehler gewertet. Pflichtfeld leer → nicht
  sync-bereit, aber Speichern erlaubt.
- **FR-SYNC-7** Sync-Bereitschaft je Eintrag: intern/none → immer bereit.
  Sonst: nicht bereit, wenn ein Pflichtfeld (Projekt- oder Eintrag-Ebene) leer
  ist oder ein gesetzter Wert das Muster verletzt; fehlende Labels werden
  gelistet.
- **FR-SYNC-8** Anzeige: Dashboard-Statusspalte, Kalender-Block + Untimed-Chip
  und Projektliste markieren fehlende Angaben mit `⚠`-Badge + Tooltip.

---

## 7. Zeiteinträge

- **FR-TE-1** Felder: user_id, project_id, entry_date, start_time?, end_time?,
  duration_minutes (autoritativ), description, tags (Liste),
  sync_target_override?, sync_metadata_override (JSON), sync_status
  (`pending`/`exported`/`synced`/`failed`/`skipped`), source
  (`manual`/`api`/`csv`), external_ref?, created_at/updated_at.
- **FR-TE-2** Dauer-Ableitung beim Speichern: Start+Ende → Differenz (Ende
  muss > Start sein); sonst gilt das Dauer-Feld; sonst Fehler. Dauer positiv.
- **FR-TE-3** 15-Min-Raster in der UI; minutengenaue Speicherung.
- **FR-TE-4** Eigentum: Nutzer sieht/ändert nur eigene Einträge; Admin alle.
  Admin kann via `user_id` für andere anlegen/filtern.
- **FR-TEAPI-1** `GET /api/v1/time-entries` mit Filtern date_from/date_to/
  project_id/user_id (Admin)/sync_target/tag, limit (Default 500, max 5000).
- **FR-TEAPI-2** `POST /api/v1/time-entries` (auch `/bulk`); `GET/PATCH/DELETE
  /api/v1/time-entries/{id}`; `POST /api/v1/intake/time-entries` (markiert
  `source=api`); `POST /api/v1/intake/csv` (multipart `file` + `mapping`-JSON
  mit column_map/separator/encoding/date_format/time_format).

---

## 8. Dashboard (`/`)

- **FR-DASH-1** Drei Kennzahl-Kacheln (Gefilterte Stunden + Anzahl, aktive
  Projekte, angemeldeter Nutzer + Rolle).
- **FR-DASH-2** Schnellerfassung: Datum (Default heute), Projekt, Von/Bis
  (Uhrzeit), Dauer (Min), Beschreibung. Wenn Projekt ein Ziel mit
  Eintrag-Feldern hat, erscheinen diese (JS) — Werte werden mitgespeichert.
- **FR-DASH-3** Filter (Von/Bis/Projekt). Default-Fenster = laufender Monat,
  wenn nichts gewählt.
- **FR-DASH-4** Eintragsliste gruppiert nach Tag mit Σ-Zeile (Anzahl + Σ h);
  Spalten: Datum, Projekt (Farbpunkt + Label inkl. Kunde), Dauer, Beschreibung,
  Ziel, Status (`⚠ <fehlende Felder>` statt sync_status, wenn nicht bereit).
- **FR-DASH-5** Aktionen je Eintrag: bearbeiten (`/entries/{id}/edit`), löschen
  (mit Confirm).
- **FR-DASH-6** CSV-Export: wenn Einträge + sichtbare Formate existieren →
  Formularauswahl + `GET /entries/export?format_id=…&…filter`.
- **FR-DASH-7** Salesforce-Auswahl: wenn SF-Credentials gesetzt und es
  sync-bereite SF-Einträge gibt, erscheint pro Eintrag eine Checkbox (HTML5
  `form="sf-sync-form"`) sowie der Button „Auswahl in Salesforce-Vorschau",
  POST → `/sync/salesforce/preview`.
- **FR-DASH-8** Mobile: Nav als Hamburger < md; Tabellen horizontal scrollbar;
  Formulare einspaltig.

---

## 9. Eintrag bearbeiten (`/entries/{id}/edit`)

- **FR-EDIT-1** Felder: Datum, Projekt, Von/Bis, Dauer, Beschreibung,
  **Sync-Ziel** (Override-Auswahl, leer = „Projekt-Standard") und die
  zielabhängigen Eintrag-Felder (JS richtet sie nach dem effektiven Ziel
  ein). Speichern via `POST /entries/{id}/edit`. Löschen-Button.

---

## 10. Kalender (`/calendar`)

- **FR-CAL-1** Umschaltbar 1/3/5/7 Tage; Navigation Zurück/Heute/Vor (verschiebt
  um die Tagesanzahl). 7-Tage-Ansicht startet montags. `start=` (ISO) +
  `days=` (1–7, geklemmt).
- **FR-CAL-2** 24-h-Zeitraster, Auto-Scroll auf 7:00; sticky Tages-Köpfe und
  Stunden-Gutter; feste Skala 44 px/h.
- **FR-CAL-3** Bestehende Zeiten: Einträge mit Start+Ende als positionierte
  Blöcke; Untimed-Einträge als Chip im Tageskopf. **Block-Hintergrund =
  getönte Projektfarbe** (`color-mix(in srgb, color 14%, white)`), linker
  Rand 3 px in voller Farbe. Bei nicht-sync-bereiten Einträgen
  `⚠ <fehlende Felder>`-Marker.
- **FR-CAL-4** Drag-Anlegen (nur Maus): Aufziehen auf dem Raster öffnet ein
  Popover (Projekt-Dropdown inkl. Kunde + Farbe-Hint, Von/Bis vorbefüllt,
  Beschreibung + zielabhängige Felder); Speichern via `POST
  /calendar/entries`.
- **FR-CAL-5** Verschieben (auch tagübergreifend) per Drag; untere Kante zieht
  die Dauer (15-Min-Raster). Persistiert via `POST /calendar/entries/{id}/move`.
- **FR-CAL-6** Klick auf einen Block (ohne nennenswerte Bewegung) öffnet
  `/entries/{id}/edit`.
- **FR-CAL-7** Touch-Geräte: Aufziehen nur mit Maus; Verschieben/Anpassen
  vorhandener Blöcke auch per Touch. Scrollposition über Reloads erhalten.
  401-Endpunkte liefern JSON-401.

---

## 11. Reporting (`/reports`)

- **FR-REP-1** Presets: wöchentlich detailliert (Default), wöchentlich pro
  Tag&Projekt, monatlich pro Projekt, pro Kunde&Projekt, pro Projekt
  detailliert.
- **FR-REP-2** Freie Gruppierung: Dimensionen Tag/Woche/Monat/Projekt/Kunde/
  Mitarbeiter beliebig verschachtelbar; Option „detailliert" hängt Einzel-
  einträge an. Zwischensummen je Ebene + Gesamtsumme.
- **FR-REP-3** Filter: Zeitraum, Projekt, Kunde, Mitarbeiter (Mitarbeiter-
  Filter nur Admin; Nicht-Admin sieht nur eigene Daten).

---

## 12. Importformate — Anlegen & KI-Assistent

- **FR-IMP-1** Sichtbarkeit/Eigentum: Formate gehören einem Nutzer; globale
  Formate (Flag `is_global`, nur Admin setzbar) sind für alle sichtbar.
  Bearbeiten/Löschen nur Eigentümer oder Admin. `POST /import-formats/{id}/
  promote` togglet `is_global`.
- **FR-IMP-2** Schritt 1 (`/import-formats/new`): Name + Beispiel-CSV hochladen.
  Ohne `ANTHROPIC_API_KEY` Hinweis, dass KI deaktiviert ist.
- **FR-IMP-3** KI-Vorschlag liefert: `source_hint`, Trenner, Encoding, Datums-/
  Zeitformat, `column_map` (vom Modell quell→ziel, vom Sanitizer auf ziel→
  quell invertiert — siehe FR-MAP-4), `transforms`, `target_rules`,
  `default_project_code`, kurze Notiz. Modell erhält System-Prompt (gecached)
  + admin-/user-Hinweise (zweiter, ungecachter System-Block).
- **FR-IMP-4** Review-Screen: links Mapping-Editor + erweiterte Optionen +
  Beispieldaten (sichtbar/editierbar) + KI-Nachschärf-Chat (bei aktiver KI);
  rechts Live-Vorschau „Quelle → Ziel" inkl. Transforms. Speichern via
  `POST /import-formats`.
- **FR-IMP-5** Nachschärf-Chat (Anlegen): Freitext-Anweisung → AI revidiert
  vorherigen Vorschlag unter Beibehaltung manueller Anpassungen
  (`POST /import-formats/refine`). Leere Anweisung → Hinweis; KI-Fehler →
  Meldung, Zwischenstand bleibt.
- **FR-IMP-6** Bearbeiten (`/import-formats/{id}/edit`): vollwertiger Editor
  mit Mapping, erweiterten Optionen, Beispieldaten, Live-Vorschau und
  Nachschärf-Chat (`POST /import-formats/{id}/refine`) — **bleibt auf der
  Edit-Seite**. Speichern via `POST /import-formats/{id}/edit`.
- **FR-IMP-7** Beim Bearbeiten stehen **alle** Quell-Spalten der gespeicherten
  Beispieldaten als Mapping-Quelle und als Transform-Quelle bereit — auch die
  als „— keine —" markierten; zusätzlich jede in einem Mapping referenzierte
  Quelle, die nicht in den Beispieldaten ist.
- **FR-IMP-8** KI-Vorgaben (global + persönlich) werden bei jedem Vorschlag/
  Nachschärfen verbindlich mitgegeben.
- **FR-IMP-9** Prompt-Härtungen: explizite Hinweise, dass `01:30:00` =
  HH:MM:SS = 90 Min bedeutet, und dass das `duration`-Ziel zu bevorzugen ist.

---

## 13. Importformate — Mapping & Editor

- **FR-MAP-1** Standard-Ziele: `entry_date`, `start_time`, `end_time`,
  `duration` (Auto, bevorzugt), `duration_minutes` (Minuten erzwingen),
  `duration_hours` (Stunden erzwingen), `project_code`, **`customer`**,
  `description`, `tags`, `sync_target`, `external_ref`.
- **FR-MAP-2** Sync-Feld-Ziele: alle Eintrag-Ebenen-Sync-Felder als Token
  `sync:<ziel>.<key>` (z. B. `sync:jira.issue_key`, `sync:bcs.subject`,
  `sync:bcs.task`).
- **FR-MAP-3** Lesbare Labels in der UI:
  - Datum / Startzeit / Endzeit / Dauer (automatisch | in Minuten | in Stunden)
    / Projekt (Code/Name) / Kunde / Beschreibung / Tags / Sync-Ziel (pro Zeile)
    / Externe Referenz
  - Sync: `<ziel>: <Feldlabel>`.
- **FR-MAP-4** `column_map`-Form ziel-orientiert: `{Zielfeld: Quellspalte}`.
  Je Zielfeld genau eine Quelle; **eine Quelle darf mehrere Ziele speisen**.
  Unbekannte Ziele beim Speichern verworfen. Die KI denkt quell→ziel; der
  Sanitizer invertiert. Migration 0007 dreht Altstand automatisch.
- **FR-MAP-5** Ziel-orientierter Editor: linke Spalte = Zielfelder (statisch,
  eine Zeile je Feld), rechte = Quell-Dropdown („— keine —" + Spalten der
  Beispieldaten). Reihenfolge: Datum, Startzeit, Endzeit, **Dauer** (eine
  Zeile mit Einheit-Auswahl auto/Min/Std → duration/duration_minutes/
  duration_hours), Projekt, Kunde, Beschreibung, Tags, Sync-Ziel, Externe
  Referenz. Danach Abschnitt **„Sync-Felder (zielabhängig)"**.

---

## 14. Importformate — Transforms (import-only)

- **FR-TF-1** Ein Transform leitet **einen** Zielwert aus einer Quellspalte ab
  und läuft nach dem einfachen column_map; der Export bleibt unberührt.
- **FR-TF-2** Operationen:
  - `copy` — Wert übernehmen.
  - `regex` — Teilstring via Muster + Capture-Gruppe (Default 1).
  - `date` — strptime mit `date_from`, strftime ins Format-Datumsformat.
  - `split` — Teilung an `sep`, Element `index` (0-basiert).
  - `constant` — fester Wert.
  - `duration` — `HH:MM:SS`/`HH:MM` → Minuten (bzw. Dezimalstunden, wenn Ziel
    `duration_hours`).
  - Jede Operation kennt `default` (Fallback bei leerem Ergebnis).
- **FR-TF-3** Sicherheit: Regex läuft gegen auf 2000 Zeichen gekappten Input;
  ungültige Muster werden abgefangen (kein Schutz gegen vorsätzliches ReDoS).
- **FR-TF-4** Editor: pro Regel Ziel/Quelle/Operation und passende Parameter;
  Änderungen lösen die Live-Vorschau aus.

---

## 15. Importer — Dauer-Auto

- **FR-DUR-1** Auto-Ziel `duration`: enthält `:` → Uhrzeit; enthält `.`/`,`
  → Dezimalstunden; reine Ganzzahl → Minuten. Ergebnis immer Minuten.
- **FR-DUR-2** Explizite Ziele `duration_minutes`/`duration_hours` erzwingen
  die Einheit; Uhrzeit-Werte (`:`) werden dennoch als Uhrzeit interpretiert.
- **FR-DUR-3** Auch direkt gemappte Dauer-Spalten parsen Uhrzeit-Formate
  (`01:30:00` → 90), unabhängig von Transformationen.
- **FR-DUR-4** Reihenfolge der Dauer-Ermittlung im Import: `duration` (auto)
  → `duration_minutes` → `duration_hours` → Start+Ende.

---

## 16. Importformate — Ziel-Regeln

- **FR-RULE-1** Regelarten:
  (a) Wenn Zielfeld befüllt: `{when, set_target}`,
  (b) Wenn Quellspalte regex-matcht: `{when_source, pattern, set_target}`.
  UI bietet die Feld-Variante (a) („Wenn `sync:jira.issue_key` befüllt → Jira").
- **FR-RULE-2** Anwendung nur, wenn beim Import die Checkbox „Ziel automatisch
  setzen" aktiv ist. Erste passende Regel gewinnt. Eine explizit gemappte
  `sync_target`-Spalte hat Vorrang. Unbekannte Ziele werden ignoriert.

---

## 17. Import durchführen (`/import`)

- **FR-RUN-1** Web: Format wählen, CSV hochladen, optional „Ziel automatisch
  setzen". `POST /import`.
- **FR-RUN-2** Ergebnisseite zeigt importierte/fehlgeschlagene Zeilen mit
  Zeilennummer, Fehlertext, Rohdaten; neu angelegte Projekte werden gelistet.
- **FR-RUN-3** Projekt-Auto-Anlage: unbekannte `project_code` werden (per
  Default) angelegt; Matching gegen normalisierten Code (Groß/Klein,
  Leerzeichen/`-`/`_` ignoriert). Wenn `customer` gemappt ist, wird er beim
  Anlegen direkt mit gepflegt; bei bestehenden Projekten ohne Kunde wird er
  ergänzt (vorhandener Kunde bleibt unverändert).
- **FR-RUN-4** Beispieldaten merken: hochgeladene CSV (gekürzt auf 30 Zeilen)
  wird am Format als `sample_data` gespeichert, nur wenn dort noch keine
  vorhanden ist. Spätere Importe überschreiben es nicht.
- **FR-RUN-5** API: `POST /api/v1/import-formats/{id}/run` (multipart `file`,
  optional `?apply_target_rules=true`). `POST /api/v1/import-formats/suggest`
  (multipart `file`) liefert einen KI-Vorschlag ohne zu speichern;
  503 wenn KI deaktiviert.

---

## 18. Importformate — Felder & API

- **FR-FMT-1** Felder: name, source_hint, separator, encoding, date_format,
  time_format, column_map (target→source), transforms, target_rules,
  sample_data?, default_project_code?, notes, owner_id?, is_global,
  created_at/updated_at.
- **FR-FMT-2** API: `GET/POST /api/v1/import-formats` (Scope
  `visible`/`mine`/`global`/`all`), `GET/PATCH/DELETE
  /api/v1/import-formats/{id}`. Schreiben nur Eigentümer/Admin;
  `is_global` nur Admin.
- **FR-FMT-3** Live-Vorschau-Endpoint: `POST /import-formats/preview`
  (Formular: sample_text, separator, date_format, time_format,
  column_map_json, transforms_json) rendert die Quelle→Ziel-Vorschau als
  HTML-Partial. 401, wenn nicht eingeloggt. Trigger: jeder `input`/`change`
  im Mapping-Formular (debounced 250 ms) + „↻ aktualisieren"-Button.

---

## 19. Export & Reports

- **FR-EXP-1** Timesheet-API: `GET /api/v1/reports/timesheet?format=json|csv|
  markdown|md` mit Filtern date_from/date_to/project_id/user_id/sync_target/
  tag und optional `csv_template_id`. Nicht-Admin nur eigene Daten.
- **FR-EXP-2** CSV-Standardspalten ohne Template: Datum, Start, Ende, Dauer (h),
  Projekt, Projektname, Kunde, Consultant, Beschreibung, Tags, SyncZiel,
  ExtRef (Trenner `;`, Dezimal `,`).
- **FR-EXP-3** Export über Importformat: `GET /entries/export` erzeugt CSV
  mit den Quell-Headern des Formats, gefüllt aus den Zielfeldern. Round-Trip
  Export → Re-Import mit gleichem Format → identische Einträge. Sync-Feld-
  Ziele (`sync:…`) und `duration` werden korrekt ausgegeben (`duration` =
  Minuten).
- **FR-TPL-1** CSV-Templates (Export-Profile): wiederverwendbare Profile
  (Spalten, Trenner, Datumsformat, Encoding, Dezimaltrenner) via
  `GET/POST /api/v1/csv-templates`, `GET/PATCH/DELETE /api/v1/csv-templates/{id}`
  (Schreiben Admin, Name eindeutig). Nutzbar via
  `reports/timesheet?format=csv&csv_template_id=…`.

---

## 20. Sync-Center (`/sync`)

- **FR-SYNCC-1** Hub für Stapel-Syncs (1×/Tag oder 1×/Woche). Pro Sync-Ziel
  eine Kachel mit Counts (sync-bereit / offen / synced); Salesforce ist aktiv
  (sofern Credentials), Jira/BCS als Platzhalter „geplant".
- **FR-SYNCC-2** Salesforce-Kachel: Button „Vorschau für alle (N)" mit
  vorbefüllten Hidden-Inputs (`entry_ids`) für alle sync-bereiten Einträge des
  Nutzers; POST → `/sync/salesforce/preview`. Ohne Credentials Hinweis auf
  Admin-Pflege.
- **FR-SYNCC-3** CSV-Export-Kachel mit Format-Dropdown (Default: erstes
  sichtbares Format) → `GET /entries/export?format_id=…`. Ohne Format Hinweis
  auf `/import-formats/new`.

---

## 21. Salesforce-Sync (Vorschau-Stand)

- **FR-SF-1** Zugangsdaten admin-pflegbar (siehe FR-SET-3). Auth: SOAP-Login
  an `/services/Soap/u/<api>` mit Username + (Passwort + Security-Token); die
  Session-Id dient als Bearer für REST.
- **FR-SF-2** Mapping-Anker: Pflichtfeld am Projekt ist
  `salesforce.assignment_id` (Id der Projektbesetzung__c). Daraus werden
  Projekt und Mitarbeiter beim Sync abgeleitet. Im Projekt-Edit-UI ist
  das ein **Dropdown** der aktiven Projektbesetzungen des aktuellen Users
  (Live-SOQL: `Mitarbeiter__r.Email = user.email` ODER
  `Externe_Projektbesetzung__r.Email = user.email`, `Geschlossen__c=false`).
  Ohne SF-Creds oder ohne Treffer → freies Text-Input als Fallback.
- **FR-SF-3** Auswahl im Dashboard (FR-DASH-7) oder zentral über Sync-Center.
- **FR-SF-4** Vorschau (read-only): `POST /sync/salesforce/preview` mit
  `entry_ids` rendert pro Eintrag eine `Zeiterfassung__c`-Payload, gruppiert
  nach (Projektbesetzung × Kontierungsmonat). Pro Eintrag wird per SOQL die
  Projektbesetzung aufgelöst und der Kontierungsmonat__c gesucht
  (`WHERE Projektbesetzung__c = '…' AND Monatsbeginn__c ≤ Tag AND
  Monatsende__c ≥ Tag`). Payload: `Kontierungsmonat__c`, `Tag__c`,
  `Arbeitszeit__c` / `Arbeitszeit_Minuten__c`,
  `Von_Stunde__c` / `Von_Minute__c` / `Bis_Stunde__c` / `Bis_Minute__c`
  (Default `0` bzw. Dauer-in-Stunden für Einträge ohne Uhrzeit; Minuten auf
  Viertelstunden-Picklist gesnappt), `Pause__c=0`,
  `Taetigkeitsbeschreibung__c` (auf 255 Zeichen gekappt) und `Remote__c`
  (aus dem Eintrag-Sync-Feld). Übersprungen wird, wenn: keine Assignment
  gepflegt / PB nicht in SF / PB geschlossen / Kontierungsmonat fehlt /
  Kontierungsmonat `Abgeschlossen__c=true` / `Status__c` ≠ `offen` (alles
  außer „offen" gilt als bereits eingereicht). **Es wird nichts geschrieben**
  — „Push noch nicht aktiv"-Hinweis am Ende.
- **FR-SF-5** Sichtbarkeit: SF-UI im Dashboard nur, wenn Credentials hinterlegt
  und mindestens ein Eintrag des Nutzers sync-bereit ist; im Sync-Center
  unabhängig sichtbar, der Button erscheint aber nur bei Sync-bereiten
  Einträgen.

---

## 22. System

- **FR-SYS-1** Health: `GET /healthz` (Liveness), `GET /readyz` (DB-Check);
  `GET /favicon.ico`; statisch unter `/static`.
- **FR-SYS-2** CORS: `CORS_ORIGINS` (Default `*`).
- **FR-SYS-3** Verhaltensrelevante Config: `ANTHROPIC_API_KEY` schaltet KI
  frei; `AI_MAPPING_MODEL` (Default `claude-sonnet-4-6`),
  `AI_MAPPING_MAX_SAMPLE_LINES` (Default 15); `ACCESS_TOKEN_EXPIRE_MINUTES`;
  `INITIAL_ADMIN_*`. Salesforce-Credentials werden NICHT aus Env gelesen,
  sondern admin-gepflegt (FR-SET-3).
- **FR-SYS-4** Migrationen 0001 (initial), 0002 (import_formats), 0003
  (user.salesforce_*), 0004 (format.transforms+target_rules), 0005
  (format.sample_data), 0006 (user.ai_hints + app_settings), 0007
  (column_map-Inversion).

---

## 23. Navigation (Web)

Eingeloggt: **Dashboard** (`/`), **Kalender** (`/calendar`), **Reports**
(`/reports`), **Sync** (`/sync`), **Projekte** (`/projects`), **Import**
(`/import`), **Formate** (`/import-formats`), **Nutzer** (`/users`, nur
Admin), **Profil** (`/profile`), **API** (`/docs`), Theme-Auswahl, Logout.
Nicht eingeloggt: Login + API. Mobil als Hamburger.

---

## 24. Abgrenzung

Nicht in diesem Stand:

- Echter Push nach Jira/Salesforce/BCS (nur Salesforce-**Vorschau** implementiert).
- OAuth/SSO (Microsoft/Authentik).
- Eigene Tag-/Kunden-Verwaltung (Tags = freie Liste; Kunde = Projektfeld,
  beim Import auto-pflegbar).

## 25. Backlog (geplant)

- **FR-FUTURE-1** Beim **CSV-Import** sollen Projekte, die in TimeHub noch
  nicht existieren, einen Vorschlag aus den aktiven Salesforce-
  Projektbesetzungen des Users bekommen (Match z. B. über
  Namens-/Bezeichnungsähnlichkeit), statt nur stumm angelegt zu werden.
- **FR-FUTURE-2** Gleiches beim **manuellen Projekt-Anlegen** im UI: Eingabe
  des Namens triggert Vorschläge passender SF-Projektbesetzungen samt
  Übernahme der `assignment_id`.
