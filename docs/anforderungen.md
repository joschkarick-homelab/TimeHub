# TimeHub — Funktionale Anforderungen (Stand: aktueller Entwicklungsstand)

Dieses Dokument beschreibt **funktional** und vollständig, was TimeHub können
muss, sodass sich der aktuelle Stand allein anhand dieser Beschreibung 1:1
wiederherstellen lässt. Es beschreibt **Verhalten, Regeln, Felder und API** —
nicht die technische Umsetzung (Framework, ORM, Dateistruktur).

Begriffe: **Nutzer** = eingeloggte Person (Rolle *Admin* oder *Consultant*).
**Eintrag** = Zeiteintrag. **Ziel** = Sync-Ziel (wohin eine Zeit später
gebucht wird). **Format** = Importformat (wiederverwendbares CSV-Eingabeprofil).

---

## 0. Rollen & Grundprinzipien

- **FR-CORE-1 Rollen:** Es gibt genau zwei Rollen: *Admin* und *Consultant*
  (Nicht-Admin). Admin darf Nutzer, Projekte, globale Einstellungen und globale
  Formate verwalten; Consultant verwaltet eigene Zeiten, eigenes Profil und
  eigene (private) Formate.
- **FR-CORE-2 Sprache:** Die gesamte Oberfläche ist deutsch.
- **FR-CORE-3 Single Source of Truth Dauer:** Eine Zeit hat genau eine
  autoritative Dauer in **Minuten**. Start/Ende sind optionaler Kontext. Es
  werden niemals zwei Dauer-Werte redundant gespeichert.
- **FR-CORE-4 Zweistufige Sync-Konfiguration:** Jedes Projekt hat ein
  Default-Ziel; jeder Eintrag kann das Ziel und zielabhängige Felder
  überschreiben.
- **FR-CORE-5 API-first:** Jede Kernfunktion ist über eine REST-API unter
  `/api/v1` nutzbar; die Web-UI ist ein dünner Aufsatz darauf. OpenAPI unter
  `/docs`, `/redoc`, `/openapi.json`.

---

## 1. Authentifizierung & Sitzungen

- **FR-AUTH-1 Login (Web):** `/login` zeigt ein Formular (E-Mail + Passwort).
  Bei Erfolg wird ein Token in der Server-Session (Cookie) hinterlegt und auf
  `/` weitergeleitet. Bei Fehlschlag erscheint „Ungültige Zugangsdaten" (HTTP 401-Seite).
- **FR-AUTH-2 Logout:** `POST /logout` leert die Session, leitet auf `/login`.
- **FR-AUTH-3 Login (API):** `POST /api/v1/auth/login` mit `{email, password}`
  liefert `{access_token, token_type=bearer}` (JWT). Inaktive Nutzer und
  falsche Credentials → 401.
- **FR-AUTH-4 Drei Auth-Wege** für geschützte Endpunkte, in dieser Reihenfolge
  geprüft: (1) `Authorization: Bearer <jwt>`, (2) `X-API-Key: <key>`,
  (3) Session-Cookie (nur Web). Schlägt alles fehl oder ist der Nutzer inaktiv
  → 401.
- **FR-AUTH-5 Token-Gültigkeit:** JWT läuft nach `ACCESS_TOKEN_EXPIRE_MINUTES`
  ab (Default 30 Tage).
- **FR-AUTH-6 API-Keys:** Nutzer können eigene API-Keys erstellen
  (`POST /api/v1/auth/api-keys` mit `{name}` → liefert den **vollständigen Key
  genau einmal** plus Prefix), auflisten (`GET`), widerrufen
  (`DELETE /api/v1/auth/api-keys/{id}`). Gespeichert wird nur ein Hash + Prefix.
  Bei Nutzung wird `last_used_at` aktualisiert; widerrufene Keys gelten nicht mehr.
- **FR-AUTH-7 Self:** `GET /api/v1/auth/me` liefert den aktuellen Nutzer.
- **FR-AUTH-8 Bootstrap-Admin:** Existiert beim Start kein Nutzer, wird aus
  `INITIAL_ADMIN_EMAIL`/`INITIAL_ADMIN_PASSWORD`/`INITIAL_ADMIN_NAME` ein
  Admin angelegt.

---

## 2. Nutzerverwaltung (Admin)

- **FR-USER-1 Web-Liste:** `/users` (nur Admin) listet alle Nutzer.
  Nicht-Admins, die `/users` aufrufen, erhalten 403; Nicht-eingeloggte → `/login`.
- **FR-USER-2 Anlegen (Web):** Formular mit E-Mail, Name, Passwort,
  Admin-Flag. Doppelte E-Mail → Fehlermeldung „E-Mail bereits vergeben".
- **FR-USER-3 Aktiv umschalten:** `POST /users/{id}/toggle-active`. Der eigene
  Account kann nicht deaktiviert werden („Eigenen Account nicht deaktivieren").
- **FR-USER-4 Admin umschalten:** `POST /users/{id}/toggle-admin`. Die eigenen
  Adminrechte können nicht entzogen werden.
- **FR-USER-5 API:** `GET/POST /api/v1/users`, `GET/PATCH/DELETE
  /api/v1/users/{id}` (alle Admin-pflichtig). PATCH kann full_name, is_admin,
  is_active, password setzen. Doppelte E-Mail → 409.
- **FR-USER-6 Felder eines Nutzers:** id, email (eindeutig), full_name,
  is_admin, is_active, salesforce_user_id?, salesforce_contact_id?, ai_hints?,
  created_at.

---

## 3. Profil (jeder Nutzer)

- **FR-PROF-1 `/profile`** zeigt/ändert: Anzeigename; E-Mail (read-only);
  **Salesforce-Anbindung** (Salesforce-User-ID + Salesforce-Contact-ID, beide
  optional, Muster alphanumerisch ≤18); **persönliche KI-Hinweise** (Freitext).
- **FR-PROF-2 Speichern** via `POST /profile`; Flash „Profil gespeichert".
- **FR-PROF-3 Rolle/Status** werden im Profil nur angezeigt, nicht editiert.

---

## 4. Globale Einstellungen (Admin)

- **FR-SET-1 Globale KI-Vorgaben:** Auf `/users` gibt es ein Admin-Feld
  „Globale KI-Vorgaben" (Freitext), gespeichert via `POST /settings/ai-hints`.
  Diese Vorgaben gelten für **alle** Nutzer beim KI-Import-Assistenten.
- **FR-SET-2 Theme-Auswahl:** `POST /settings/theme` setzt ein Cookie (1 Jahr,
  SameSite=lax). Themes: `indigo` (Default), `mindsquare`, `dark`. Ungültige
  Werte fallen auf `indigo` zurück. Auswahl ist in der Navigation (Desktop &
  Mobil) verfügbar.

---

## 5. Projekte

- **FR-PROJ-1 Felder:** name, code (eindeutiger, stabiler Schlüssel),
  customer?, color (Hex, Default `#6366f1`), status (`active`/`inactive`),
  default_sync_target (`intern`/`jira`/`salesforce`/`bcs`/`none`),
  sync_metadata (zielabhängige Felder, siehe §6), created_at/updated_at.
- **FR-PROJ-2 Code optional/automatisch:** Beim Anlegen ist der Code optional.
  Bleibt er leer, wird er aus dem Namen erzeugt: Buchstaben/Ziffern bleiben,
  alles andere wird zu `-`, Großbuchstaben, am Rand getrimmt, ≤60 Zeichen;
  leerer Rest → `PROJEKT`. Kollidiert er, wird `-2`, `-3`, … angehängt. Der
  Anwender muss sich also keinen Schlüssel ausdenken.
  - Die Auto-Code-Normalisierung ist mit dem Import-Matching kompatibel
    (z. B. „Acme Website" → `ACME-WEBSITE`, matcht beim Import den Klartext „Acme Website").
- **FR-PROJ-3 Code beim Bearbeiten:** optional; bleibt das Feld leer, wird der
  bestehende Code beibehalten (nie verloren). Power-User können ihn anpassen
  (relevant fürs Import-Matching).
- **FR-PROJ-4 Verwaltung (Web, Admin):** `/projects` listet alle Projekte mit
  Farbpunkt, Code, Name, Kunde, Ziel, Status. Anlegen/Bearbeiten/Löschen nur
  Admin. Anlege-Formular enthält Name, Code (optional), Kunde, Ziel, Farbe +
  zielabhängige Felder (§6).
- **FR-PROJ-5 Löschschutz:** Ein Projekt mit Zeiteinträgen kann nicht gelöscht
  werden → Fehlermeldung. (Web zeigt sie an; API über Integritätsfehler.)
- **FR-PROJ-6 Sync-Bereitschaft in der Liste:** Fehlen Projekt-Pflichtfelder
  des gewählten Ziels (§6), zeigt die Zeile `⚠ unvollständig` mit Tooltip der
  fehlenden Felder.
- **FR-PROJ-7 API:** `GET /api/v1/projects` (optional `?status=`),
  `POST` (Admin), `GET/PATCH/DELETE /api/v1/projects/{id}` (PATCH/DELETE Admin).
  `sync_metadata` ist frei setzbar. Doppelter Code → 409.
- **FR-PROJ-8 Dropdown-Label:** Wo Projekte zur Auswahl stehen, ist das Label
  „CODE – Name"; sind Code und Name gleich, nur der Code.

---

## 6. Sync-Ziele & zielabhängige Felder

- **FR-SYNC-1 Ziele:** `jira`, `salesforce`, `bcs`, `intern`, `none`. `intern`
  und `none` brauchen nie einen Push (immer „sync-bereit").
- **FR-SYNC-2 Feld-Register (fest definiert, erweiterbar):** Pro Ziel sind
  Zusatzfelder mit Ebene (Projekt/Eintrag), Pflicht-Flag, optionalem
  Validierungsmuster, Platzhalter und Hilfetext definiert:

  | Ziel | Feld (key) | Ebene | Pflicht | Muster/Hinweis |
  | --- | --- | --- | --- | --- |
  | jira | default_issue (Standard-Ticket) | Projekt | nein | `[A-Z][A-Z0-9]+-\d+`, z. B. `ABC-1` |
  | jira | issue_key (Jira-Ticket) | Eintrag | **ja** | `[A-Z][A-Z0-9]+-\d+`; erbt `default_issue` |
  | salesforce | project_id (Salesforce Projekt-ID) | Projekt | **ja** | z. B. `a0X…` |
  | bcs | subject (BCS Subject) | Eintrag | **ja** | — |
  | bcs | task (BCS Task) | Eintrag | **ja** | — |
  | intern / none | — | — | — | — |

- **FR-SYNC-3 Speicherort:** Projekt-Feldwerte liegen in
  `projects.sync_metadata[ziel][key]`, Eintrag-Feldwerte in
  `time_entries.sync_metadata_override[ziel][key]`.
- **FR-SYNC-4 Bedingte Anzeige:** In allen Erfassungs-/Bearbeitungsmasken
  werden **nur** die Felder des aktuell relevanten Ziels angezeigt — sonst
  keine. Das relevante Ziel ist: am Projekt das gewählte Default-Ziel; am
  Eintrag das Eintrag-Override, sonst das Projekt-Default.
- **FR-SYNC-5 Vererbung:** Ein leeres Eintrag-Feld erbt den Projekt-Default
  (z. B. Jira `default_issue` → `issue_key`).
- **FR-SYNC-6 Validierung (warnen, nicht blockieren):** Ein gesetzter Wert, der
  dem Muster widerspricht, wird gespeichert, aber als Formatfehler gewertet.
  Fehlende Pflichtfelder blockieren das Speichern **nicht**.
- **FR-SYNC-7 Sync-Bereitschaft eines Eintrags:** Effektives Ziel bestimmen;
  bei `intern`/`none` immer bereit. Sonst: nicht bereit, wenn ein Pflichtfeld
  (Projekt- oder Eintrag-Ebene) leer ist oder ein gesetzter Wert das Muster
  verletzt; die fehlenden/fehlerhaften Feld-Labels werden ausgewiesen.
- **FR-SYNC-8 Anzeige der Bereitschaft:** Dashboard-Liste, Kalender (Block &
  „ohne Uhrzeit"-Chip) und Projektliste markieren fehlende Angaben mit `⚠` und
  nennen die fehlenden Felder.

---

## 7. Zeiteinträge — Datenmodell & Regeln

- **FR-TE-1 Felder:** user_id, project_id, entry_date, start_time?, end_time?,
  duration_minutes (autoritativ), description, tags (Liste),
  sync_target_override?, sync_metadata_override (JSON), sync_status
  (`pending`/`exported`/`synced`/`failed`/`skipped`), source
  (`manual`/`api`/`csv`), external_ref?, created_at/updated_at.
- **FR-TE-2 Dauer-Ableitung:** Sind Start und Ende gesetzt, wird die Dauer
  daraus berechnet (Ende muss nach Start liegen, sonst Fehler). Sonst gilt die
  explizit angegebene Dauer (Minuten). Ist beides leer/0 → Fehler „Dauer
  angeben oder Start + Ende ausfüllen". Dauer muss positiv sein.
- **FR-TE-3 15-Minuten-Raster:** Die UI nutzt 15-Minuten-Schritte; die
  Speicherung bleibt minutengenau.
- **FR-TE-4 Eigentum:** Ein Nutzer sieht/ändert nur eigene Einträge; Admin darf
  alle. (API: Admin darf via `user_id` für andere anlegen/filtern.)

---

## 8. Zeiteinträge — API

- **FR-TEAPI-1 Liste:** `GET /api/v1/time-entries` mit Filtern `date_from`,
  `date_to`, `project_id`, `user_id` (nur Admin wirksam), `sync_target`, `tag`,
  `limit` (Default 500, max 5000). Nicht-Admin sieht nur eigene.
- **FR-TEAPI-2 Anlegen:** `POST /api/v1/time-entries`. Pflicht: project_id,
  entry_date, und entweder duration_minutes oder start+end. Optional: tags,
  description, sync_target_override, sync_metadata_override, external_ref,
  user_id (Admin). Unbekanntes Projekt → 400; fremder user_id ohne Adminrecht → 403.
- **FR-TEAPI-3 Bulk:** `POST /api/v1/time-entries/bulk` mit `{entries:[…]}`;
  liefert `{created, failed, errors[], ids[]}`, einzelne Fehler brechen den
  Rest nicht ab.
- **FR-TEAPI-4 Detail/Ändern/Löschen:** `GET/PATCH/DELETE
  /api/v1/time-entries/{id}` (nur eigene oder Admin). PATCH setzt nur
  übergebene Felder; fehlt die Dauer, aber Start+Ende sind da, wird sie
  nachberechnet.
- **FR-TEAPI-5 Intake:** `POST /api/v1/intake/time-entries` (wie Bulk, markiert
  `source=api`) und `POST /api/v1/intake/csv` (multipart: `file` + `mapping`
  als JSON mit column_map/default_project_code/separator/encoding/date_format/
  time_format).

---

## 9. Dashboard (`/`)

- **FR-DASH-1 Kennzahlen:** drei Kacheln — gefilterte Stunden + Eintragsanzahl,
  Anzahl aktiver Projekte, angemeldeter Nutzer/Rolle.
- **FR-DASH-2 Schnellerfassung:** Formular mit Datum (Default heute), Projekt,
  **Von/Bis** (Uhrzeit), **Dauer (Min)**, Beschreibung, Speichern. Regel: Von+Bis
  ⇒ Dauer berechnet; sonst gilt das Dauer-Feld (Hinweistext erklärt das).
- **FR-DASH-3 Zielabhängige Felder:** Wechselt das gewählte Projekt, erscheinen
  (per JS) die Eintrag-Felder des Projekt-Ziels (z. B. Jira-Ticket). Werte
  werden mitgespeichert.
- **FR-DASH-4 Filter:** Von/Bis/Projekt; Standard-Fenster = laufender Monat,
  wenn nichts gewählt. „Zurücksetzen" leert die Filter.
- **FR-DASH-5 Liste gruppiert nach Tag:** Tabelle mit Datum, Projekt
  (Farbpunkt+Label), Dauer (h), Beschreibung, Ziel, Status; je Tag eine
  Σ-Zeile (Anzahl + Summe h). Status-Spalte zeigt `⚠ <fehlende Felder>` statt
  des sync_status, wenn der Eintrag nicht sync-bereit ist.
- **FR-DASH-6 Bearbeiten/Löschen:** Links je Zeile zu `/entries/{id}/edit` bzw.
  Löschen (mit Bestätigung).
- **FR-DASH-7 Export:** Wenn Einträge + sichtbare Formate vorhanden sind, kann
  die aktuelle Filtermenge über ein Importformat als CSV exportiert werden
  (`GET /entries/export?format_id=…&date_from=…&date_to=…&project_id=…`).
- **FR-DASH-8 Mobile:** Navigation als Hamburger-Menü < md; Tabellen horizontal
  scrollbar; Formulare brechen einspaltig um.
- **FR-EDIT-1 Eintrag bearbeiten:** `/entries/{id}/edit` — Datum, Projekt,
  Von/Bis, Dauer, Beschreibung, **Sync-Ziel** (Override-Auswahl,
  „Projekt-Standard" = leer) und die zielabhängigen Eintrag-Felder (per JS nach
  effektivem Ziel). Speichern via `POST /entries/{id}/edit`. Löschen-Button.

---

## 10. Kalender-Ansicht (`/calendar`) — Toggl/Clockify-Stil

- **FR-CAL-1 Zeitraum:** Umschaltbar 1/3/5/7 Tage; Navigation Zurück/Heute/Vor
  (verschiebt um die Tagesanzahl). 7-Tage-Ansicht startet montags; kürzere
  starten heute. Querparameter `start` (ISO) und `days` (1–7, geklemmt).
- **FR-CAL-2 Raster:** vertikales 24-h-Zeitraster, scrollbar (Standard-Scroll
  auf 7:00), Stundenlinien, sticky Tages-Köpfe und Stunden-Gutter. Skala fix
  (44 px/Stunde).
- **FR-CAL-3 Bestehende Zeiten:** Einträge mit Start+Ende werden als farbige
  Blöcke positioniert (Projektfarbe, Label, Zeit, ggf. `⚠`-Hinweis,
  Beschreibung). Einträge **ohne** Uhrzeit erscheinen als Chips im Tageskopf
  („ohne Uhrzeit"), klickbar zur Bearbeitung.
- **FR-CAL-4 Drag-Anlegen:** Aufziehen auf dem Raster (nur Maus) öffnet ein
  schlankes Popover (Projekt, Von/Bis vorbefüllt, Beschreibung **und** die
  zielabhängigen Felder des Projekt-Ziels). Speichern via
  `POST /calendar/entries` (JSON). Klick ohne Ziehen = 1-Stunden-Vorgabe.
- **FR-CAL-5 Verschieben/Größe:** Block ziehen verschiebt ihn (auch auf einen
  anderen Tag); untere Kante zieht die Dauer; 15-Minuten-Raster. Persistiert via
  `POST /calendar/entries/{id}/move` (JSON: entry_date/start/end).
- **FR-CAL-6 Klick = Bearbeiten:** Klick auf einen Block (ohne nennenswerte
  Bewegung) öffnet die Eintrag-Bearbeitung.
- **FR-CAL-7 Touch:** Aufziehen-zum-Anlegen ist bewusst nur mit Maus aktiv
  (Touch scrollt); Verschieben/Anpassen vorhandener Blöcke geht auch per Touch.
- **FR-CAL-8 Robustheit:** Scrollposition wird über Reloads erhalten; bei
  fehlendem Projekt ist das Anlegen deaktiviert. Auth-Endpunkte liefern 401-JSON
  bei abgelaufener Sitzung.

---

## 11. Reporting (`/reports`)

- **FR-REP-1 Presets:** wöchentlich detailliert (Default), wöchentlich pro
  Tag&Projekt, monatlich pro Projekt, pro Kunde&Projekt, pro Projekt detailliert.
- **FR-REP-2 Freie Gruppierung:** beliebig verschachtelbare Dimensionen Tag,
  Woche, Monat, Projekt, Kunde, Mitarbeiter; Option „detailliert" (Einzel-
  einträge anhängen). Pro Ebene Zwischensummen; Gesamtsumme.
- **FR-REP-3 Filter:** Zeitraum (von/bis), Projekt, Kunde, Mitarbeiter
  (Mitarbeiter-Filter nur für Admin; Nicht-Admin sieht nur eigene Daten).
- **FR-REP-4 Erweiterbarkeit:** Eine neue Dimension ist ein einzelner
  Register-Eintrag (funktional: Gruppierungsoptionen sind zentral definiert).

---

## 12. Importformate — Verwaltung & KI-Assistent

- **FR-IMP-1 Sichtbarkeit/Eigentum:** Formate gehören einem Nutzer; globale
  Formate (Flag `is_global`, nur Admin setzbar) sind für alle sichtbar.
  `/import-formats` listet sichtbare Formate; Bearbeiten/Löschen nur durch
  Eigentümer oder Admin. Admin kann ein Format global schalten
  (`POST /import-formats/{id}/promote`).
- **FR-IMP-2 Anlegen Schritt 1 (`/import-formats/new`):** Name + Beispiel-CSV
  hochladen. Ohne `ANTHROPIC_API_KEY` erscheint ein Hinweis, dass KI-Vorschläge
  deaktiviert sind.
- **FR-IMP-3 KI-Vorschlag:** Aus dem CSV-Auszug schlägt die KI vor:
  Quelle/Trennzeichen/Encoding/Datums-/Zeitformat, **column_map**
  (Quelle→Ziel), **Transformationen** und **Ziel-Regeln** sowie kurze Notiz.
  Ergebnis ist ein Vorschlag (nicht gespeichert) und wird im Review-Screen
  gezeigt.
- **FR-IMP-4 Review-Screen (Schritt 2):** zeigt links das editierbare Mapping +
  erweiterte Optionen + **Beispieldaten** (sichtbar/editierbar) + (bei aktiver
  KI) einen **Nachschärf-Chat**; rechts die **Live-Vorschau** „Quelle → Ziel".
  Speichern via `POST /import-formats`.
- **FR-IMP-5 Nachschärf-Chat (Anlegen):** Freitext-Anweisung → KI überarbeitet
  ihren vorherigen Vorschlag unter Berücksichtigung der bisherigen (auch manuell
  geänderten) Werte; `POST /import-formats/refine`. Leere Anweisung → Hinweis;
  KI-Fehler → Meldung **ohne** Verlust des Zwischenstands.
- **FR-IMP-6 Bearbeiten (`/import-formats/{id}/edit`):** vollwertiger Editor mit
  Mapping, erweiterten Optionen, **Beispieldaten** (vorbefüllt), Live-Vorschau
  und (bei aktiver KI) Nachschärf-Chat, der **auf der Edit-Seite bleibt**
  (`POST /import-formats/{id}/refine`). Speichern via `POST /import-formats/{id}/edit`.
- **FR-IMP-7 Alle Spalten verfügbar:** Beim Bearbeiten stehen **alle** Spalten
  der gespeicherten Beispieldaten als Mapping-Quelle und als
  Transformations-Quelle bereit — auch ignorierte; zusätzlich jedes gemappte,
  nicht in den Beispieldaten enthaltene Feld.
- **FR-IMP-8 KI-Vorgaben:** Globale (Admin) + persönliche (Profil) KI-Hinweise
  werden bei jedem Vorschlag/Nachschärfen als verbindliche Vorgaben mitgegeben.
- **FR-IMP-9 KI-Robustheit:** Der System-Prompt weist u. a. explizit darauf
  hin, dass `01:30:00` = 1 Std 30 Min = 90 Minuten ist und für Dauer das
  `duration`-Ziel zu bevorzugen ist.

---

## 13. Importformate — Mapping-Ziele

- **FR-MAP-1 Standard-Ziele:** entry_date, start_time, end_time, **duration**
  (automatische Einheit, bevorzugt), duration_minutes (Minuten erzwingen),
  duration_hours (Stunden erzwingen), project_code, description, tags,
  sync_target, external_ref.
- **FR-MAP-2 Sync-Feld-Ziele:** Alle Eintrag-Ebenen-Sync-Felder sind als Ziel
  verfügbar, Token `sync:<ziel>.<key>` (z. B. `sync:jira.issue_key`,
  `sync:bcs.subject`, `sync:bcs.task`). Werte landen im
  `sync_metadata_override` des erzeugten Eintrags.
- **FR-MAP-3 Lesbare Labels:** In der UI heißen die Dauer-Ziele „Dauer
  (automatisch)", „Dauer in Minuten", „Dauer in Stunden"; Sync-Ziele „<ziel>:
  <Feldlabel>".
- **FR-MAP-4 column_map-Form:** ziel-orientiert, `{Zielfeld: Quellspalte}`. Je
  Zielfeld genau eine Quelle; **eine Quelle darf mehrere Ziele speisen**.
  Ungültige Ziele werden beim Speichern verworfen. (Die KI denkt intern
  quell→ziel; an der KI-Grenze wird invertiert.)
- **FR-MAP-5 Ziel-orientierter Editor:** Das Mapping-UI zeigt links die
  TimeHub-Zielfelder (statisch, eine Zeile je Feld) und rechts je ein
  Quell-Dropdown (Spalten aus den Beispieldaten + „— keine —"). Dauer ist
  **eine** Zeile mit Einheit-Auswahl (auto/Minuten/Stunden →
  duration/duration_minutes/duration_hours). Sync-Felder stehen in einem
  eigenen Abschnitt. Bestehende Formate werden beim Upgrade automatisch von
  quell- auf ziel-orientiert umgestellt (Migration).

---

## 14. Importformate — Transformationen (import-only)

- **FR-TF-1 Zweck:** Pro Regel wird **ein** Zielwert aus einer Quellspalte
  abgeleitet; Transformationen überschreiben/erzeugen Werte und laufen nach dem
  einfachen column_map. Der Export bleibt unberührt (round-trippable).
- **FR-TF-2 Operationen:**
  - `copy` — Quellwert übernehmen.
  - `regex` — Teilstring über Muster + Capture-Gruppe (Default 1) extrahieren.
  - `date` — Quelldatum mit `date_from` parsen, in das Format-Datumsformat
    ausgeben.
  - `split` — an Trenner `sep` teilen, `index` (0-basiert) nehmen.
  - `constant` — fester Wert.
  - `duration` — `HH:MM:SS`/`HH:MM` → Minuten (bzw. Dezimalstunden, wenn Ziel
    `duration_hours`).
  - Jede Operation kennt ein `default` (Fallback bei leerem Ergebnis).
- **FR-TF-3 Sicherheit:** Regex läuft gegen auf 2000 Zeichen gekappten Input;
  ungültige Muster werden abgefangen (kein Absturz). (Hinweis: kein Schutz vor
  bewusst bösartigen ReDoS-Mustern.)
- **FR-TF-4 Editor:** Pro Regel Ziel, Quelle, Operation und je nach Operation
  die passenden Parameter; serialisiert in ein verstecktes JSON-Feld;
  Änderungen aktualisieren die Live-Vorschau.

---

## 15. Importformate — Dauer-Auto-Erkennung

- **FR-DUR-1 Auto-Ziel `duration`:** erkennt die Einheit selbst:
  enthält `:` → Uhrzeit (`HH:MM:SS`/`HH:MM`); enthält `.` oder `,` →
  Dezimalstunden; reine Ganzzahl → Minuten. Ergebnis immer Minuten.
- **FR-DUR-2 Explizite Ziele:** `duration_minutes`/`duration_hours` erzwingen
  die Einheit; Uhrzeit-Werte (`:`) werden dennoch als Uhrzeit interpretiert.
- **FR-DUR-3 Importer-Robustheit:** Auch direkt gemappte Dauer-Spalten parsen
  Uhrzeit-Formate (`01:30:00` → 90), unabhängig davon, ob eine Transformation
  genutzt wird. (Behebt „could not derive a positive duration" beim direkten
  Mapping.)
- **FR-DUR-4 Beispiele:** `01:30:00` → 90; `01:30` → 90; `1,5`/`1.5` (als
  Stunden) → 90; `90` (als Minuten) → 90.

---

## 16. Importformate — Bedingter Ziel-Override (Ziel-Regeln)

- **FR-RULE-1 Regelarten:** (a) „Wenn Zielfeld befüllt → Ziel setzen"
  (`{when: "<Zielfeld-Token>", set_target}`); (b) „Wenn Quellspalte matcht →
  Ziel setzen" (`{when_source, pattern, set_target}`). Im UI wird die Variante
  (a) angeboten (z. B. „Wenn `sync:jira.issue_key` befüllt → Jira").
- **FR-RULE-2 Anwendung beim Import:** Nur wenn beim Import die Checkbox „Ziel
  automatisch setzen" aktiv ist. Erste passende Regel gewinnt. Ein explizit
  gemapptes `sync_target` hat Vorrang vor Regeln. Unbekannte Ziele werden
  ignoriert.
- **FR-RULE-3 Zweck:** z. B. erkanntes Jira-Ticket (per Regex extrahiert) →
  Eintrag automatisch auf Jira routen, ohne manuelle Nacharbeit.

---

## 17. Import durchführen (`/import`)

- **FR-RUN-1 Web:** Format wählen, CSV hochladen, optional „Ziel automatisch
  setzen" (wendet die Ziel-Regeln an). `POST /import`.
- **FR-RUN-2 Ergebnis:** Anzeige importierter/fehlgeschlagener Zeilen; pro
  Fehlerzeile Zeilennummer, Fehlertext und Rohdaten; neu angelegte Projekte
  werden gelistet.
- **FR-RUN-3 Projekt-Auto-Anlage:** Unbekannte `project_code` werden (per
  Default) als Projekt angelegt; Matching ist gegen normalisierte Codes
  (Groß/Klein, Leerzeichen/`-`/`_` ignoriert).
- **FR-RUN-4 Dauer-Quellen:** Reihenfolge der Dauerermittlung: `duration`
  (auto) → `duration_minutes` → `duration_hours` → Start+Ende.
- **FR-RUN-5 Beispieldaten merken:** Die hochgeladene CSV wird (gekürzt) am
  Format als Beispieldaten gespeichert, **nur wenn dort noch keine vorhanden
  sind** — damit Vorschau & KI im Bearbeiten ohne erneuten Upload laufen.
  Spätere Importe überschreiben gespeicherte Beispieldaten nicht.
- **FR-RUN-6 API:** `POST /api/v1/import-formats/{id}/run` (multipart `file`,
  optional `?apply_target_rules=true`). `POST /api/v1/import-formats/suggest`
  (multipart `file`) liefert einen KI-Vorschlag (inkl. Transforms/Ziel-Regeln),
  ohne zu speichern; 503 wenn KI deaktiviert.

---

## 18. Importformate — gespeicherte Felder & API

- **FR-FMT-1 Felder:** name, source_hint, separator, encoding, date_format,
  time_format, column_map, transforms (Liste), target_rules (Liste),
  sample_data? (gekürzter CSV-Auszug), default_project_code?, notes,
  owner_id?, is_global, created_at/updated_at.
- **FR-FMT-2 API:** `GET/POST /api/v1/import-formats` (Scope `visible`/`mine`/
  `global`/`all`), `GET/PATCH/DELETE /api/v1/import-formats/{id}`. Schreiben nur
  Eigentümer/Admin; `is_global` nur Admin.
- **FR-FMT-3 Live-Vorschau-Endpoint:** `POST /import-formats/preview`
  (Formularfelder sample_text/separator/date_format/time_format/
  column_map_json/transforms_json) rendert die Quelle→Ziel-Vorschau als
  HTML-Fragment (inkl. Transformationen). Nur eingeloggt (sonst 401).

---

## 19. Export & Reporting-Service

- **FR-EXP-1 Timesheet-API:** `GET /api/v1/reports/timesheet?format=json|csv|
  markdown|md` mit Filtern date_from/date_to/project_id/user_id/sync_target/tag
  und optional `csv_template_id`. Nicht-Admin nur eigene Daten.
- **FR-EXP-2 CSV-Standardspalten:** Datum, Start, Ende, Dauer (h), Projekt,
  Projektname, Kunde, Consultant, Beschreibung, Tags, SyncZiel, ExtRef
  (Trenner `;`, Dezimal `,`), sofern kein Template gewählt ist.
- **FR-EXP-3 Markdown/JSON:** Markdown-Tabelle mit Summenzeile; JSON als
  Liste der Eintragsfelder inkl. berechneter `duration_hours`.
- **FR-EXP-4 Export über Importformat (Round-Trip):** `GET /entries/export`
  erzeugt CSV mit den Quell-Headern des Formats, gefüllt aus den Zielfeldern —
  so ist „Export A → Re-Import mit demselben Format" verlustfrei. Sync-Feld-
  Ziele (`sync:…`) und `duration` werden dabei korrekt ausgegeben
  (`duration` = Minuten).

---

## 20. CSV-Templates (Export-Profile)

- **FR-TPL-1 Zweck:** Wiederverwendbare CSV-**Export**-Profile (Spalten,
  Trenner, Datumsformat, Encoding, Dezimaltrenner).
- **FR-TPL-2 API:** `GET/POST /api/v1/csv-templates`, `GET/PATCH/DELETE
  /api/v1/csv-templates/{id}` (Schreiben Admin). Name eindeutig (409 bei
  Dublette). Nutzbar via `reports/timesheet?format=csv&csv_template_id=…`.

---

## 21. System & Betrieb (funktional relevant)

- **FR-SYS-1 Health:** `GET /healthz` (Liveness), `GET /readyz` (DB-Check),
  `GET /favicon.ico`.
- **FR-SYS-2 Statische Dateien** unter `/static` (App-Icon).
- **FR-SYS-3 CORS:** konfigurierbar (`CORS_ORIGINS`, Default `*`).
- **FR-SYS-4 Verhalten-relevante Konfiguration:** `ANTHROPIC_API_KEY` schaltet
  KI-Funktionen frei; `AI_MAPPING_MODEL` (Default `claude-sonnet-4-6`) und
  `AI_MAPPING_MAX_SAMPLE_LINES` (Default 15) steuern den KI-Aufruf;
  `ACCESS_TOKEN_EXPIRE_MINUTES` die Token-Gültigkeit; `INITIAL_ADMIN_*` den
  Bootstrap-Admin.
- **FR-SYS-5 Persistenz:** Alle dauerhaften Daten liegen in der Datenbank;
  Sitzungen in signiertem Cookie; Theme in eigenem Cookie.

---

## 22. Navigation (Web)

Eingeloggt sichtbar: **Dashboard** (`/`), **Kalender** (`/calendar`),
**Reports** (`/reports`), **Projekte** (`/projects`), **Import** (`/import`),
**Formate** (`/import-formats`), **Nutzer** (`/users`, nur Admin), **Profil**
(`/profile`), **API** (`/docs`), Theme-Auswahl, Logout. Nicht eingeloggt:
Login + API. Mobil als Hamburger-Menü.

---

## 23. Abgrenzung (bewusst nicht in diesem Stand)

- Echte Pushes nach Jira/Salesforce/BCS (nur Datenmodell + Bereitschaft
  vorbereitet; Salesforce-Recherche in `docs/salesforce-integration.md`).
- OAuth/SSO (Microsoft/Authentik).
- Eigene Tag-/Kunden-Verwaltung (Tags als freie Liste; Kunde als Projektfeld).
