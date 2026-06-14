# TimeHub — Anforderungsdokument (Vibe-Coding-Workshop)

> Dieses Dokument beschreibt **TimeHub** so, dass die App von Grund auf neu
> „vibe-codet" werden kann — ohne Blick in den bestehenden Code. Es definiert
> *was* die App tut und *welchen Charakter* sie hat, nicht *wie* der Code im
> Detail aussieht. Wo eine konkrete technische Entscheidung sinnvoll ist, wird
> sie als Empfehlung genannt; abweichen ist erlaubt, solange das Verhalten und
> der Charakter erhalten bleiben.
>
> Sprache der gesamten UI: **Deutsch**.

---

## 1. Kernidee: Die Drehscheibe für Zeiterfassung

TimeHub ist eine **Drehscheibe** (engl. *hub*) zwischen den Tools, in denen
Consultants ihre Zeit *erfassen*, und den Plattformen, in denen die Zeit am
Ende *landen muss* (Jira, Salesforce, BCS, interne CSV-Templates …).

Das Problem, das TimeHub löst:

> Berater erfassen ihre Stunden gerne dort, wo es ihnen leichtfällt — in
> Clockify, Toggl, einer Excel-Liste oder direkt in einer Kalender-Ansicht.
> Am Ende der Woche müssen dieselben Stunden aber sauber, projektgenau und
> im richtigen Format in mehrere Zielsysteme übertragen werden. Dieser letzte
> Schritt ist heute lästige, fehleranfällige Handarbeit.

TimeHub macht aus diesem Schritt **einen einzigen, geführten Vorgang**: Stunden
rein (Import oder direkte Erfassung), einmal den Sync-Wizard durchklicken,
fertig. Die Zeit ist überall dort, wo sie hingehört.

TimeHub erfasst Zeiten dabei wahlweise **granular** (Viertelstundentakt,
mehrere Projekte pro Tag, mit Uhrzeiten) oder **grob** („8 h auf Projekt X").

---

## 2. Charakter der App — die Leitplanken

Diese Prinzipien sind **wichtiger als jedes Einzelfeature**. Im Zweifel
entscheidet immer das Prinzip, das dem Nutzer am meisten Arbeit abnimmt.

1. **TimeHub nimmt Arbeit ab, es erzeugt keine.** Jeder Klick, den die App dem
   Nutzer abverlangt, muss sich rechtfertigen. Wenn TimeHub etwas erraten,
   ableiten oder vorbefüllen kann, tut es das — und lässt den Nutzer nur noch
   bestätigen oder korrigieren.

2. **Minimal möglicher User-Input.** Der Nutzer soll im Idealfall nur das
   liefern, was *nur er* wissen kann: in welchem Lieblingstool er erfasst, und
   wie *seine* Projekte auf die Zielsysteme abgebildet werden (Mappings). Alles
   andere — Spalten erkennen, Dauer parsen, Projekte anlegen, Ziele zuordnen,
   Payloads bauen — erledigt die App.

3. **Geführt statt frei.** Der zentrale Sync ist ein **Wizard**: ein klarer,
   linearer Ablauf mit Vorschau, Korrektur-Loop und Bestätigung. Der Nutzer
   wird an die Hand genommen, nicht vor ein leeres Formular gesetzt.

4. **Nichts geht ungefragt raus.** Schreibende Syncs in fremde Systeme passieren
   erst nach einer **Read-only-Vorschau** und einer expliziten Bestätigung.
   Vorschau und Ausführung sind sauber getrennt.

5. **Warnen, nicht blockieren.** Unvollständige oder formal fragwürdige Daten
   werden gespeichert und sichtbar markiert (`⚠`), aber sie verhindern das
   Weiterarbeiten nicht. Der Nutzer behält die Kontrolle; TimeHub gibt Hinweise.

6. **KI ist Beschleuniger, nie Voraussetzung.** Wo künstliche Intelligenz hilft
   (Spalten-Mapping erkennen), schlägt sie etwas vor, das der Nutzer prüft. Ohne
   KI-Schlüssel funktioniert die komplette App weiter — nur die Vorschläge
   fehlen.

7. **API-first & self-hosted.** Jede Kernfunktion ist über eine dokumentierte
   REST-API erreichbar; die Web-UI ist ein dünner Wrapper darum. Tools dürfen
   Zeiten direkt reindrücken. Die App läuft selbstgehostet im Docker-Container.

8. **Wiederverwenden statt wiederholen.** Einmal eingerichtete Importformate,
   Export-Templates und Projekt-Mappings bleiben gespeichert und stehen jede
   Woche wieder bereit. Routine wird zur Konfiguration, nicht zur Wiederholung.

---

## 3. Der Kern-Usecase: Die Woche eines Consultants

Dies ist der Ablauf, um den herum die ganze App gebaut wird. Alles andere ist
Unterstützung für genau diesen Flow.

### 3.1 Während der Woche — erfassen, wo es leichtfällt

Der Nutzer erfasst seine Stunden in **seinem Lieblingstool**:

- **Clockify / Toggl** o. Ä. — wie gewohnt, mit Projekten und Tags.
- **Eine Excel-/CSV-Liste** — z. B. ein Kunden-Template.
- **Direkt in TimeHub** — über die Schnellerfassung im Dashboard oder per
  Drag-and-Drop im Kalender.

TimeHub schreibt ihm **nichts** vor. Es ist egal, wo und wie erfasst wird.

### 3.2 Bei Bedarf — Import nach TimeHub

Wer extern erfasst hat, **importiert** seinen Export (CSV) nach TimeHub —
typischerweise einmal täglich oder einmal die Woche.

- Beim **ersten Mal** für ein bestimmtes Tool legt der Nutzer ein
  **Importformat** an: Er lädt eine Beispiel-CSV hoch, die KI schlägt das
  Mapping (Spalten → TimeHub-Felder, Trennzeichen, Datumsformat, Dauer-Parsing)
  vor, der Nutzer korrigiert per Dropdown und **speichert das Format**.
- Ab dann ist Import ein **Zwei-Klick-Vorgang**: Format wählen, Datei
  hochladen. Unbekannte Projekte werden automatisch angelegt, Dauern korrekt
  geparst, Tags übernommen.

Wer direkt in TimeHub erfasst, überspringt diesen Schritt komplett.

### 3.3 Am Ende der Woche — einmal den Sync-Wizard

Jetzt liegen alle Stunden der Woche in TimeHub. Der Nutzer öffnet das
**Sync-Center** und startet den **Wizard**:

1. **Überblick:** Eine Status-Matrix zeigt pro Eintrag, was wohin noch offen,
   erledigt oder blockiert ist (Ampel pro Zielsystem).
2. **Pro Ziel eine Karte:** „Salesforce: 7 Einträge, 12,5 h → bereit." Der
   Nutzer sieht gebündelt, was ansteht.
3. **Lücken füllen — ohne das System zu verlassen:** Blockierte Einträge
   (z. B. „Jira-Ticket fehlt", „Salesforce-Projektbesetzung nicht gemappt")
   werden direkt im Wizard erfragt. **Die Projekt-Mappings stellt hier der
   Nutzer bereit** — TimeHub kennt seine Projekte, aber nicht zwingend deren
   Gegenstück im Zielsystem. Ein Mapping für ein Projekt entsperrt alle
   betroffenen Einträge auf einmal.
4. **Vorschau:** TimeHub baut die exakten Ziel-Payloads und zeigt sie
   read-only an („so sähe das in Salesforce aus").
5. **Abnicken:** Eine Bestätigung pro Ziel löst den echten Push aus (bzw. den
   CSV-Download für Ziele ohne API-Push). Jeder Eintrag bekommt seinen Status
   (✓ synced / ✗ failed mit Fehlertext / – übersprungen mit Grund).

Was einmal gesynct ist, taucht nächste Woche nicht wieder auf. Der Wizard ist
**idempotent**: Doppel-Syncs werden verhindert.

### 3.4 Die Rolle der Mappings

Der einzige inhaltliche Beitrag, den TimeHub vom Nutzer zwingend braucht, sind
die **Mappings** — und zwar auf zwei Ebenen:

- **Spalten-Mapping (Import):** Wie heißen die Spalten seines Tools, und auf
  welche TimeHub-Felder gehen sie? → Einmal pro Tool als Importformat, KI-
  unterstützt.
- **Projekt-Mapping (Sync):** Welches TimeHub-Projekt entspricht welchem
  Ziel-Objekt (Jira-Ticket, Salesforce-Projektbesetzung, BCS-Subject/Task)? →
  Der Nutzer stellt es bereit, der Wizard fragt es genau dann ab, wenn es fehlt.

Alles dazwischen ist Automatik.

---

## 4. Rollen

- **Consultant** — der Normalfall. Erfasst, importiert und synct **seine
  eigenen** Zeiten und Projekte. Sieht nur die eigenen Daten.
- **Admin** — verwaltet Nutzer, globale Einstellungen, globale Importformate,
  CSV-Export-Templates und die Salesforce-Zugangsdaten. Hat in Reports den
  nutzerübergreifenden Blick.

Jeder eingeloggte Nutzer pflegt seine eigenen Projekte, Einträge und (privaten)
Importformate selbst.

---

## 5. Feature-Übersicht (Module)

| # | Modul | Zweck |
| - | ----- | ----- |
| A | Auth & Nutzer & Profil | Login, API-Keys, Rollen, persönliche KI-Hinweise |
| B | Projekte & Sync-Mappings | Projekte pro Nutzer, zielabhängige Mapping-Felder |
| C | Zeiterfassung: Dashboard | Schnellerfassung, gefilterte Eintragsliste, Status |
| D | Zeiterfassung: Kalender | Drag-and-Drop-Erfassung im Tagesraster |
| E | Import & Importformate | KI-gestütztes CSV-Mapping, wiederverwendbare Profile |
| F | Sync-Ziele & Felder | Ziel-Registry, zielabhängige Pflichtfelder, Validierung |
| G | Sync-Center & Wizard | Status-Matrix, geführter Multi-Ziel-Sync, Inline-Fixes |
| H | Salesforce-Integration | Vollständiger Live-Push als Referenz-Integration |
| I | Reporting & Export | Flexible Reports, CSV-Export über Templates/Formate |
| J | System & Konfiguration | Health, Theming, globale Einstellungen, Deployment |

---

## 6. Modul A — Auth, Nutzer & Profil

### Authentifizierung
- **Web-Login:** Login-Formular → bei Erfolg Session-Cookie + Redirect aufs
  Dashboard; bei falschen Daten freundliche Fehlerseite. Logout räumt die
  Session.
- **API-Login:** `POST /api/v1/auth/login` mit `{email, password}` →
  `{access_token, token_type: "bearer"}` (JWT).
- **Drei Auth-Wege** für geschützte Endpunkte, in dieser Reihenfolge geprüft:
  `Authorization: Bearer <jwt>` → `X-API-Key: <key>` → Session-Cookie.
  Inaktive Nutzer oder falsche Credentials → 401.
- **JWT-Gültigkeit** konfigurierbar (Default großzügig, z. B. 30 Tage).
- **Bootstrap-Admin:** Existiert beim Start kein Nutzer, wird aus
  Konfigurationswerten (`INITIAL_ADMIN_EMAIL/_PASSWORD/_NAME`) ein Admin
  angelegt.

### API-Keys (für Tool-Integrationen)
- Nutzer erzeugt benannte API-Keys (`POST /api/v1/auth/api-keys {name}`). Der
  **vollständige Key wird genau einmal** bei Erstellung zurückgegeben; in der DB
  liegt nur ein Hash + ein Klartext-Prefix zur Wiedererkennung.
- Keys auflisten, widerrufen (löschen). `last_used_at` wird bei Nutzung
  aktualisiert.

### Nutzerverwaltung (Admin)
- Liste + Anlegen (E-Mail, Name, Passwort, Admin-Flag). Doppelte E-Mail →
  Fehlermeldung.
- Aktiv-Status und Admin-Flag pro Nutzer umschaltbar. **Schutzregeln:** Der
  eigene Account kann nicht deaktiviert, die eigenen Adminrechte können nicht
  entzogen werden.
- API: `GET/POST /api/v1/users`, `GET/PATCH/DELETE /api/v1/users/{id}` (alles
  Admin). Doppelte E-Mail → 409.

### Profil (jeder Nutzer)
- Anzeigename und **persönliche KI-Hinweise** änderbar; E-Mail read-only;
  Rolle/Status nur Anzeige.
- **Salesforce-Identität (automatisch).** Die Zuordnung TimeHub-Nutzer → echter
  SF-User wird **nicht manuell gepflegt**, sondern automatisch aus der Login-
  Session über den `FederationIdentifier` (Entra `oid`/UPN) aufgelöst und
  gecacht (Details: Modul H, 13.1/13.3). Im Profil ist sie höchstens
  **read-only** sichtbar. Sie ist der Anker dafür, dass alle SF-Dropdowns auf
  *seine* Datensätze gefiltert werden und Einträge dem richtigen SF-User
  gehören.

---

## 7. Modul B — Projekte & Sync-Mappings

Projekte sind die Brücke zwischen erfassten Stunden und Zielsystemen. **Jeder
Nutzer pflegt und sieht nur seine eigenen Projekte** (Listen, Dropdowns, Import-
Auto-Anlage, API). Nur Admin-Reports blicken nutzerübergreifend.

### Felder
`name` (Pflicht), `code` (stabiler Schlüssel, **eindeutig pro Besitzer** —
verschiedene Nutzer dürfen denselben Code führen), `customer?`, `color` (Hex,
Default ein angenehmes Indigo), `status` (`active`/`inactive`),
`sync_targets` (Liste der Default-Zielsysteme), `sync_metadata` (zielabhängige
Mapping-Felder, siehe Modul F), Zeitstempel.

### Code-Automatik (Arbeit abnehmen)
- Beim Anlegen wird der Code **automatisch aus dem Namen abgeleitet**, wenn das
  Feld leer bleibt: nicht-alphanumerische Zeichen → `-`, Großbuchstaben, auf
  ~60 Zeichen gekürzt, Ränder getrimmt; leerer Rest → `PROJEKT`; bei Kollision
  `-2`, `-3`, …
- Beim Bearbeiten bleibt der bestehende Code erhalten, solange das Feld leer
  bleibt; ein explizit gesetzter Code wird übernommen.

### Anzeige
- `display_label` = `"CODE – Name (Kunde)"`. Sind Code und Name gleich oder ist
  der Name leer → nur Code. Kunde-Suffix nur, wenn `customer` gesetzt. Dieses
  Label erscheint in **allen** Projekt-Dropdowns. Ein Farbpunkt zeigt die
  `color`.
- Die Projektliste markiert `⚠ unvollständig`, wenn ein vom Ziel gefordertes
  Projekt-Pflichtfeld (z. B. Salesforce-Projektbesetzung) fehlt.

### Regeln
- Anlegen/Bearbeiten/Löschen steht jedem Nutzer für seine Projekte offen.
  **Löschen blockiert**, wenn Zeiteinträge am Projekt hängen.
- API: `GET /api/v1/projects` (optional `?status=`), `POST`,
  `GET/PATCH/DELETE /api/v1/projects/{id}` — alles auf die eigenen Projekte
  beschränkt (fremde → 404). Doppelter Code beim selben Nutzer → 409.

---

## 8. Modul C — Dashboard (`/`)

Das Dashboard ist die tägliche Arbeitsfläche: erfassen und kontrollieren.

- **Kennzahl-Kacheln:** gefilterte Stunden (+ Anzahl), aktive Projekte,
  angemeldeter Nutzer + Rolle.
- **Schnellerfassung:** Datum (Default heute), Projekt, Von/Bis (Uhrzeit),
  Dauer (Min), Beschreibung. Hat das gewählte Projekt ein Ziel mit
  Eintrag-Pflichtfeldern, erscheinen **nur diese** Felder dynamisch und werden
  mitgespeichert.
- **Filter:** Von/Bis/Projekt. Default-Fenster = **laufende Woche (Mo–So)**,
  wenn nichts gewählt ist.
- **Eintragsliste**, gruppiert nach Tag mit Σ-Zeile (Anzahl + Σ h). Spalten:
  Datum, Projekt (Farbpunkt + Label inkl. Kunde), Dauer, Beschreibung, Ziel,
  Status. Ist ein Eintrag nicht sync-bereit, zeigt die Statusspalte
  `⚠ <fehlende Felder>` statt eines Sync-Status.
- **Sync-Status-Matrix:** pro Eintrag drei Ampelpunkte (Jira / BCS /
  Salesforce), read-only, mit Tooltip für den Grund (fehlende Felder bzw.
  letzter Fehler). Klick auf einen Spaltenkopf startet den Wizard, gefiltert
  auf dieses Ziel.
- **Aktionen je Eintrag:** bearbeiten, löschen (mit Confirm).
- **CSV-Export:** existieren Einträge und sichtbare Importformate, kann der
  gefilterte Stand als CSV im Format eines gewählten Importformats exportiert
  werden.
- **Lade-Indikator:** Jeder Formular-Submit zeigt ein globales Overlay mit
  Spinner und kontextpassendem Text („KI analysiert die CSV …", „Salesforce
  wird abgefragt …", „Import läuft …"). Einzelne Formulare können opt-out.
- **Mobil:** Navigation als Hamburger; Tabellen horizontal scrollbar; Formulare
  einspaltig.

### Eintrag bearbeiten (`/entries/{id}/edit`)
Datum, Projekt, Von/Bis, Dauer, Beschreibung, **Sync-Ziel-Override** (leer =
„Projekt-Standard") und die zielabhängigen Eintrag-Felder (dynamisch nach dem
effektiven Ziel). Löschen-Button.

### Zeiteintrag — Datenregeln
- **`duration_minutes` ist autoritativ.** Start und Ende sind optional. Sind
  beide gesetzt, wird die Dauer daraus abgeleitet (Ende muss > Start sein);
  sonst gilt das Dauer-Feld; sonst Fehler. Dauer ist immer positiv.
- **15-Minuten-Raster in der UI**, minutengenaue Speicherung.
- **Eigentum:** Nutzer sieht/ändert nur eigene Einträge; Admin alle und kann via
  `user_id` für andere anlegen/filtern.
- **Quelle** je Eintrag: `manual` / `api` / `csv`.

---

## 9. Modul D — Kalender (`/calendar`)

Direkte, visuelle Erfassung im Tagesraster — für alle, die lieber „malen" als
Formulare ausfüllen.

- **Umschaltbar 1/3/5/7 Tage**; Navigation Zurück/Heute/Vor verschiebt um die
  Tagesanzahl. Die 7-Tage-Ansicht startet montags.
- **24-h-Zeitraster**, Auto-Scroll auf 7:00, sticky Tagesköpfe und Stunden-
  Gutter, feste Skala.
- **Bestehende Zeiten:** Einträge mit Start+Ende als positionierte Blöcke;
  Einträge ohne Uhrzeit als Chip im Tageskopf. Block-Hintergrund = **getönte
  Projektfarbe**, linker Rand 3 px in voller Farbe. Nicht sync-bereite Einträge
  tragen einen `⚠`-Marker.
- **Drag-Anlegen (Maus):** Aufziehen auf dem Raster öffnet ein Popover
  (Projekt-Dropdown inkl. Kunde + Farbe, Von/Bis vorbefüllt, Beschreibung +
  zielabhängige Felder); speichern.
- **Verschieben** (auch tagübergreifend) per Drag; untere Kante zieht die Dauer
  (15-Min-Raster).
- **Klick** auf einen Block (ohne nennenswerte Bewegung) öffnet die
  Eintrag-Bearbeitung.
- **Touch:** Aufziehen nur mit Maus; vorhandene Blöcke verschieben/anpassen auch
  per Touch. Scrollposition über Reloads erhalten.

---

## 10. Modul E — Import & Importformate

Hier wird das Prinzip „minimal möglicher User-Input" am sichtbarsten: TimeHub
lernt jedes Quellformat **einmal** und wendet es danach automatisch an.

### Importformate (wiederverwendbare Profile)
Ein **Importformat** beschreibt, wie die CSV eines bestimmten Tools (Toggl,
Clockify, ein Kunden-Excel-Template …) gelesen wird.

**Felder:** `name`, `source_hint`, `separator`, `encoding`, `date_format`,
`time_format`, `column_map` (Zielfeld → Quellspalte), `transforms`,
`target_rules`, `sample_data?`, `default_project_code?`, `notes`, `owner_id?`,
`is_global`, Zeitstempel.

**Sichtbarkeit:** Formate gehören einem Nutzer und sind standardmäßig privat.
Admins können ein Format **global** schalten (`is_global`); globale Formate sind
für alle sichtbar und stehen oben in der Liste. Bearbeiten/Löschen nur
Eigentümer oder Admin.

### KI-gestützter Mapping-Assistent
1. **Schritt 1:** Name vergeben + Beispiel-CSV hochladen.
2. TimeHub schickt die ersten ~15 Zeilen an Claude (zentraler
   `ANTHROPIC_API_KEY`, **kein nutzereigener Key nötig**).
3. Das Modell liefert einen Vorschlag: `source_hint`, Trennzeichen, Encoding,
   Datums-/Zeitformat, `column_map`, `transforms`, `target_rules`,
   `default_project_code`, kurze Notiz.
4. **Review-Screen:** links Mapping-Editor + erweiterte Optionen + Beispieldaten
   (sichtbar/editierbar) + ein **Nachschärf-Chat** (Freitext-Anweisung → KI
   revidiert den Vorschlag, behält manuelle Anpassungen bei); rechts eine
   **Live-Vorschau „Quelle → Ziel"** inkl. Transforms (debounced bei jeder
   Änderung).
5. Speichern. Ab dann steht das Format in der Liste.

**KI optional:** Ohne `ANTHROPIC_API_KEY` läuft alles weiter — der
„Vorschlag erzeugen"-Button meldet nur, dass die KI-Hilfe deaktiviert ist; das
Mapping kann manuell oder per API gepflegt werden.

**KI-Hinweise verbindlich:** Globale (Admin) und persönliche (Profil)
KI-Hinweise werden bei jedem Vorschlag/Nachschärfen als separater System-Block
mitgegeben.

### Mapping-Modell
- **Standard-Zielfelder:** `entry_date`, `start_time`, `end_time`,
  `duration` (Auto, bevorzugt), `duration_minutes`, `duration_hours`,
  `duration_human` (Jira-Text „1w 2d 3h 4m"; 1w = 5d, 1d = 8h), `project_code`,
  `customer`, `description`, `tags`, `sync_target`, `external_ref`.
- **Sync-Feld-Ziele:** alle Eintrag-Sync-Felder als Token
  `sync:<ziel>.<key>` (z. B. `sync:jira.issue_key`, `sync:bcs.subject`).
- **`column_map` ist ziel-orientiert** (`{Zielfeld: Quellspalte}`): je Zielfeld
  genau eine Quelle, aber **eine Quelle darf mehrere Ziele speisen**. Unbekannte
  Ziele werden beim Speichern verworfen.
- **Editor:** linke Spalte Zielfelder (statisch, eine Zeile je Feld), rechts ein
  Quell-Dropdown („— keine —" + Spalten der Beispieldaten). Reihenfolge: Datum,
  Startzeit, Endzeit, **Dauer** (eine Zeile mit Einheit-Auswahl
  auto/Min/Std/Text), Projekt, Kunde, Beschreibung, Tags, Sync-Ziel, Externe
  Referenz, danach Abschnitt „Sync-Felder (zielabhängig)".

### Transforms (nur beim Import, Export bleibt unberührt)
Ein Transform leitet **einen** Zielwert aus einer Quellspalte ab und läuft nach
dem einfachen `column_map`. Operationen:
- `copy` — Wert übernehmen
- `regex` — Teilstring via Muster + Capture-Gruppe (Default 1)
- `date` — `strptime(date_from)` → `strftime(Format-Datumsformat)`
- `split` — an `sep` teilen, Element `index` (0-basiert)
- `constant` — fester Wert
- `duration` — `HH:MM:SS`/`HH:MM` → Minuten (bzw. Dezimalstunden bei Ziel
  `duration_hours`)
- Jede Operation kennt ein `default` als Fallback bei leerem Ergebnis.

**Sicherheit:** Regex läuft gegen auf ~2000 Zeichen gekappten Input; ungültige
Muster werden abgefangen.

### Dauer-Parsing (Auto)
- Auto-Ziel `duration`: enthält `:` → Uhrzeit; enthält `.`/`,` →
  Dezimalstunden; reine Ganzzahl → Minuten. Ergebnis immer Minuten.
- Explizite Ziele `duration_minutes`/`duration_hours` erzwingen die Einheit,
  Uhrzeit-Werte (`:`) werden trotzdem als Uhrzeit interpretiert (`01:30:00` →
  90 Min).
- Reihenfolge der Dauer-Ermittlung: `duration` → `duration_minutes` →
  `duration_hours` → Start+Ende.

### Ziel-Regeln (`target_rules`)
- (a) „Wenn Zielfeld befüllt → Ziel X" oder (b) „Wenn Quellspalte regex-matcht →
  Ziel X". Anwendung nur, wenn beim Import „Ziel automatisch setzen" aktiv ist;
  erste passende Regel gewinnt; eine explizit gemappte `sync_target`-Spalte hat
  Vorrang.

### Import durchführen (`/import`)
- **Web:** Format wählen, CSV hochladen, optional „Ziel automatisch setzen".
- **Ergebnisseite:** importierte und fehlgeschlagene Zeilen mit Zeilennummer,
  Fehlertext und Rohdaten; neu angelegte Projekte werden gelistet.
- **Projekt-Auto-Anlage:** unbekannte `project_code` werden (per Default)
  angelegt; Matching gegen normalisierten Code (Groß/Klein, Leerzeichen/`-`/`_`
  ignoriert). Ist `customer` gemappt, wird er beim Anlegen direkt gepflegt; bei
  bestehenden Projekten ohne Kunde ergänzt (vorhandener Kunde bleibt).
- **Beispieldaten merken:** die hochgeladene CSV (gekürzt) wird am Format als
  `sample_data` gespeichert, falls dort noch keine vorhanden ist; spätere
  Importe überschreiben sie nicht.

### Import-API
- `POST /api/v1/intake/time-entries` — externes Tool drückt Einträge rein
  (`source=api`).
- `POST /api/v1/intake/csv` (multipart: `file` + `mapping`-JSON).
- `POST /api/v1/import-formats/suggest` (multipart `file`) — KI-Vorschlag ohne
  Speichern; 503 wenn KI deaktiviert.
- `POST /api/v1/import-formats/{id}/run` (multipart `file`, optional
  `?apply_target_rules=true`) — gespeichertes Format anwenden.
- `GET/POST /api/v1/import-formats` (Scope `visible`/`mine`/`global`/`all`),
  `GET/PATCH/DELETE /api/v1/import-formats/{id}`.

---

## 11. Modul F — Sync-Ziele & zielabhängige Felder

Das Herz der Drehscheibe: Jeder Eintrag kann in eine **Menge** von Zielsystemen
fließen, und jedes Ziel verlangt eigene Pflichtfelder.

### Ziele
`jira`, `salesforce`, `bcs` brauchen einen echten Push. Ein Eintrag ohne
Zielsystem (leere Menge) hat schlicht nichts zu tun.

### Mehrere Ziele pro Eintrag
- Ein Projekt hat eine Default-Ziel-**Liste** (`sync_targets`), z. B.
  `["jira", "bcs"]` → „läuft standardmäßig nach Jira und BCS".
- Ein Eintrag kann diese Liste überschreiben (`sync_targets_override`); leer =
  erbt vom Projekt / aus Regeln. Dieselbe Stunde kann so z. B. nach Jira *und*
  Salesforce.

### Feld-Registry (code-definiert, erweiterbar)

| Ziel | Feld | Ebene | Pflicht | Muster |
| --- | --- | --- | --- | --- |
| jira | `default_issue` (Standard-Ticket) | Projekt | nein | `[A-Z][A-Z0-9]+-\d+` |
| jira | `issue_key` (Jira-Ticket) | Eintrag | **ja** | wie oben; erbt `default_issue` |
| salesforce | `assignment_id` (Projektbesetzung) | Projekt | **ja** | 15-/18-stellige SF-Id; im UI Dropdown der aktiven Projektbesetzungen des Users (Live-SOQL) |
| salesforce | `remote` (Remote / Vor Ort) | Eintrag | nein | Picklist `true`/`false`, Default „Remote" |
| bcs | `subject` | Eintrag | **ja** | — |
| bcs | `task` | Eintrag | **ja** | — |

- **Speicherort:** `projects.sync_metadata[ziel][key]` und
  `time_entries.sync_metadata_override[ziel][key]`.
- **Bedingte Anzeige:** in den Masken erscheinen **nur** die Felder des aktuell
  relevanten Ziels.
- **Vererbung:** ein leeres Eintrag-Feld erbt den Projekt-Default (z. B. Jira
  `default_issue` → `issue_key`).
- **Validierung warnt, blockiert nicht:** malformierte Werte werden gespeichert,
  aber als Format-Fehler gewertet; ein leeres Pflichtfeld macht den Eintrag
  „nicht sync-bereit", verhindert das Speichern aber nicht.

### Sync-Bereitschaft & Status pro Ziel
Pro Eintrag × Ziel wird ein Status geführt (eigene schmale Tabelle `EntrySync`,
eine Zeile je Eintrag×Ziel):

```
EntrySync(id, entry_id, target, status, external_ref, attempts,
          last_error, synced_at, created_at, updated_at)
  status ∈ pending | synced | manually_synced | failed | skipped
  UNIQUE(entry_id, target)
```

Vorteile gegenüber einem einzelnen Status-Feld: triviale Aggregate für die
Matrix („7 offen für Jira"), natürlicher Platz für Retry (`attempts`,
`last_error`) und eine eigene Remote-Id pro Ziel.

### Optionale Regel-Engine
Eine kleine `SyncRule`-Engine kann die Ziel-Menge je Eintrag verfeinern
(`add_target` / `remove_target` / `set_targets`), bedingt auf Tags
(`has_tag`), Projektcode (`project_code`) oder „immer" (`always`). Regeln
greifen beim Anlegen/Import; **manuelle Übersteuerung am Eintrag gewinnt** und
wird von erneuter Regel-Auswertung nicht überschrieben. Bereits gesyncte Zeilen
werden nie gelöscht. (Pflege-UI für Regeln ist v1-optional; API/Seed reicht
zunächst.)

### Status-Ampel (pro Matrix-Zelle)

| Farbe | Bedeutung | Bedingung |
| --- | --- | --- |
| ⚪ grau | nicht relevant | Ziel ∉ effektive Menge des Eintrags |
| 🟢 grün | erledigt | `status ∈ {synced, manually_synced}` |
| 🟡 gelb | offen, kann | in Menge, sync-bereit, `status=pending` |
| 🔴 rot | blockiert | in Menge, aber nicht bereit (Tooltip: fehlende Felder) **oder** `status=failed` (Tooltip: letzter Fehler) |

---

## 12. Modul G — Sync-Center & Export-Wizard (`/sync`)

Der zentrale Ort, an dem die Woche „rausgeht". Generalisierung des Salesforce-
Flows (Vorschau → Ausführung) auf **alle** Ziele.

### Status-Matrix (Einstieg)
Read-only-Übersicht (auch im Dashboard): Zeile = Eintrag, Spalten = Jira / BCS /
Salesforce, drei unabhängige Ampelpunkte mit Tooltip. Klick auf einen
Spaltenkopf oder eine gelbe Zelle startet den Wizard, gefiltert auf diesen
Scope. Bestehende Datum-/Projekt-Filter gelten weiter.

### Wizard (Hybrid: Batch pro Ziel, Einzeleintrag herauslösbar)
1. **Sammeln:** alle sync-bereiten (gelben) Einträge je Ziel.
2. **Karte pro Ziel** mit Zusammenfassung („Salesforce: 7 Einträge, 12,5 h").
   Blockierte (rote) Einträge landen in einem separaten „kann nicht"-Stapel der
   Karte — **mit Grund**.
3. **Lücken inline füllen — ohne das System zu verlassen:** Blockierte Einträge
   werden nach fehlenden Daten gruppiert und direkt im Wizard erfragt:
   - Fehlende **Projekt-Daten** (z. B. Salesforce-Projektbesetzung) als **ein
     Formular pro Projekt** → entsperrt alle betroffenen Einträge auf einmal.
     **Hier stellt der Nutzer das Projekt-Mapping bereit.**
   - Fehlende **Eintrags-Daten** (z. B. Jira-Ticket, BCS Subject/Task) pro
     Eintrag.
   - Die Felder nutzen denselben Renderer wie der Projekt-/Eintrag-Edit,
     inklusive **dynamischer Dropdowns** (Live-Projektbesetzungen via SOQL; ohne
     SF-Credentials graceful zum Textfeld). Endpunkte schreiben nur die
     gesendeten Felder.
4. **Vorschau (read-only):** TimeHub baut die exakten Ziel-Payloads und zeigt
   sie an. **Es wird nichts geschrieben.**
5. **Hybrid-Korrektur:** Einzeleintrag aus dem Batch herauslösen → Inline-Edit
   (Deeplink `/entries/{id}/edit?next=/sync`, landet danach wieder im Wizard) →
   zurück an dieselbe Stelle. Der Rest-Batch bleibt stehen.
6. **Abnicken:** Bestätigung pro Ziel → echter Push (bzw. „als manuell erledigt
   markieren" / CSV-Download für Ziele ohne API-Client). `EntrySync.status`
   wird gesetzt, die Matrix färbt sich nach.

### Ziel-Verfügbarkeit in v1
- **Salesforce:** vollständiger Live-Push (siehe Modul H).
- **Jira / BCS:** Felder registriert, **Push-Clients noch nicht** gebaut. Die
  Karten leisten in v1 nur „als manuell-erledigt markieren" (`manually_synced`)
  bzw. CSV-Ausgabe, bis echte API-Clients existieren.
- **CSV-Export-Kachel:** Format-Dropdown → Export im Format eines Importformats
  (Round-Trip-fähig, siehe Modul I).

---

## 13. Modul H — Salesforce-Integration (Referenz-Push)

Salesforce ist die **vollständig ausgebaute** Beispiel-Integration und zeigt,
wie ein echtes Zielsystem angebunden wird. (Konkret auf das Salesforce-
Datenmodell von mindsquare zugeschnitten; in `docs/salesforce-integration.md`
und `docs/salesforce-schema/` liegen die Objekt-/Feld-Details.)

- **Anbindung über einen dedizierten Integration User** (Server-zu-Server,
  **kein** Per-User-OAuth) per **OAuth 2.0 JWT Bearer Flow** über eine Connected
  App — Details und Begründung in 13.2. Admin-pflegbar sind die Connected-App-
  Parameter (Consumer Key, Integration-User-Username, Login-URL Default
  `login.salesforce.com`, API-Version Default `60.0`); der **private Schlüssel**
  liegt im Secret-Store, nicht in der DB (siehe 13.5). Ein „Verbindung
  testen"-Button holt ein Token über den JWT-Flow und meldet Erfolg/Fehler.
- **Mapping-Anker:** Pflichtfeld am Projekt ist `salesforce.assignment_id` (Id
  der Projektbesetzung). Daraus werden Projekt und Mitarbeiter beim Sync
  abgeleitet. Im Projekt-Edit ist das ein **Dropdown** der aktiven
  Projektbesetzungen des Users (Live-SOQL, gefiltert auf die aufgelöste
  SF-Identität des Nutzers — siehe 13.1, geschlossene ausgeblendet); ohne
  SF-Verbindung/Treffer ein freies Textfeld.
- **Vorschau** (`POST /sync/salesforce/preview` mit `entry_ids`): pro Eintrag
  wird eine `Zeiterfassung__c`-Payload gerendert, gruppiert nach
  (Projektbesetzung × Kontierungsmonat). Pro Eintrag wird per SOQL die
  Projektbesetzung aufgelöst und der passende Kontierungsmonat gesucht. Die
  Dauer wird als Intervall ab Mitternacht kodiert (`Von=0`,
  `Bis=duration_minutes`, auf Viertelstunden gesnappt), plus Beschreibung
  (gekappt auf 255 Zeichen) und Remote-Flag. **Übersprungen** wird bei:
  fehlender Assignment, PB nicht in SF, PB geschlossen, fehlendem
  Kontierungsmonat, abgeschlossenem Kontierungsmonat oder Status ≠ „offen".
  Es wird nichts geschrieben.
- **Echter Push** (`POST /sync/salesforce/execute` mit `entry_ids`): legt pro
  Eintrag eine `Zeiterfassung__c` an. Vor jedem Insert wird re-validiert.
  **Idempotenz:** Einträge mit Status `synced`/`manually_synced` werden
  übersprungen. Pro-Eintrag-Fehler markieren nur diesen Eintrag (`failed`) und
  stoppen den Stapel nicht; ein Verbindungsfehler bricht ab. Bei Erfolg wird die
  neue SF-Id persistiert und der Status auf `synced` gesetzt. Die Ergebnisseite
  zeigt pro Eintrag ✓/✗/– mit Deep-Link bzw. Grund und eine Zusammenfassung.
- **Manuell-als-erfasst-Marker:** „✓ als manuell erfasst markieren"
  (`manually_synced`) für Einträge, die direkt in SF erfasst wurden (z. B. aus
  bereits geschlossenen Kontierungsmonaten) — verschwinden aus Auswahl/Vorschau/
  Push. Ein Undo („↶ wieder öffnen") setzt **nur** `manually_synced` zurück auf
  `pending`; echte SF-Syncs bleiben unangetastet, um Duplikate zu vermeiden.

### 13.1 Nutzer-Anker: eigene Salesforce-Identität

- **Jeder TimeHub-Nutzer ist genau einem echten SF-User zugeordnet.** Die
  Zuordnung läuft **automatisch** über den **`FederationIdentifier`** (Entra-
  `oid`/UPN aus der Login-Session) → SF-User; das Ergebnis wird **in der App
  gecacht**. Kein manuelles Eintragen einer SF-User-Id nötig (minimaler
  User-Input).
- Diese aufgelöste SF-Identität ist der verbindliche Anker für (a) das
  Owner-Setzen beim Schreiben (13.3), (b) die App-seitige Berechtigung (13.4)
  und (c) das Filtern aller SF-Dropdowns (13.6).
- Findet sich zur Entra-Identität **kein** SF-User, sind die SF-Funktionen für
  diesen Nutzer deaktiviert (freundlicher Hinweis statt stiller Fehler).

> **Abhängigkeit (zu klären):** Dieses Modell setzt **Entra-basierte Sessions**
> voraus (SSO via Microsoft Entra). Modul A beschreibt aktuell lokale Auth
> (JWT + API-Key), und §19 führt OAuth/SSO als „out of scope für v1". Mit dieser
> Entscheidung wandert **Entra-SSO in den Scope** — Modul A/§19 sind
> entsprechend nachzuziehen.

### 13.2 Auth & Lizenz: dedizierter Integration User (entschieden)

- **Anbindung über einen dedizierten Integration User — kein Per-User-OAuth.**
  Begründung **Lizenzkosten:** die ersten **5 Integration-User-Lizenzen sind
  kostenlos** (danach ~$10/User/Monat), während Per-User-OAuth „**API Enabled**"
  auf *jedem* User bräuchte und je nach Salesforce-Edition Add-on-Kosten
  verursacht.
- **Auth: OAuth 2.0 JWT Bearer Flow** (Server-zu-Server). TimeHub signiert ein
  JWT mit einem **Private Key** und tauscht es über eine **Connected App** gegen
  ein Access-Token; ein laufender User-Login ist dafür nicht nötig.
- **Stack-Empfehlung:** FastAPI + `simple_salesforce` als SF-Client.

### 13.3 Ownership-Modell

- Zeiterfassungen liegen im Custom Object **`Time_Entry__c`**.
- Beim Schreiben setzt TimeHub die **`OwnerId` auf den echten SF-User des
  Mitarbeiters** (aus der Entra→SF-Auflösung, 13.1) — nicht auf den Integration
  User. Vorteile: native SF-Reports „**by Owner**" funktionieren, und eine
  spätere **Migration auf Per-User-OAuth** bleibt offen (die Datensätze gehören
  bereits den richtigen Usern).
- Das Entra→SF-Mapping (`FederationIdentifier`) wird in der App gecacht.

> **Objektname abgleichen:** Dieser Abschnitt nennt das Zielobjekt generisch
> `Time_Entry__c`; der bestehende Vorschau-/Push-Flow oben (sowie
> `docs/salesforce-integration.md`) nutzt den mindsquare-spezifischen Namen
> `Zeiterfassung__c`. Vor der Umsetzung festlegen, ob es dasselbe Objekt unter
> zwei Namen ist oder zwei getrennte Ziele — und die Doku vereinheitlichen.

### 13.4 Sicherheit — App-Layer (primär)

Die „nur eigene Einträge"-Isolation **kann Salesforce nicht erzwingen** — für SF
ist jeder Schreibvorgang der Integration User. Die Durchsetzung liegt damit
**vollständig in TimeHub**:

- **Owner immer aus der validierten Entra-Session ableiten, nie aus
  Client-Input** (Schutz vor IDOR / Untergeschobenem Owner).
- **Jede Read-Query auf den Session-User scopen.** Vor **Update/Delete** die
  Ownership prüfen (`WHERE Id = X AND OwnerId = Y`).
- **SF-Ids gegen Format validieren (Regex)**, da `simple_salesforce` SOQL
  **nicht** parametrisiert → so wird **SOQL-Injection** verhindert. (Greift mit
  13.6 ineinander: Ids kommen ohnehin aus Dropdowns, werden aber zusätzlich
  serverseitig validiert.)

### 13.5 Sicherheit — SF-seitig (Blast Radius begrenzen)

- **Permission Set** für den Integration User: nur **CRUD auf `Time_Entry__c`**
  + die nötigen Felder (FLS), sonst nichts.
- **Connected App auf die Backend-IP beschränken** (Login IP Ranges), minimale
  **Scopes** (`api`, `refresh_token`).
- **Private Key im Secret-Store**, IP-gelockt, **rotierbar** — das ist das
  **Kronjuwel**: wer ihn hat, schreibt als *jeder* User. Entsprechend strikt
  behandeln (nie in DB/Repo/Logs, getrennte Rotation).

### 13.6 Alle SF-Ids ausschließlich per Dropdown

- **Keine SF-Id wird je frei getippt.** Jedes Feld, das einen SF-Datensatz
  referenziert, wird über ein **Live-SOQL-Dropdown** befüllt, das auf die
  Datensätze des Nutzers gefiltert ist — u. a.: Projektbesetzung
  (`assignment_id`), Kontierungsmonat, ggf. Projekt/Account/Owner-User,
  Remote-Picklist.
- **Ausnahme:** Authentifizierungs-Geheimnisse (privater JWT-Schlüssel,
  Connected-App-Consumer-Secret) — die bleiben geschützte Eingaben im
  Secret-Store und tauchen nie in Dropdowns auf.
- **Graceful Fallback:** Ist SF nicht erreichbar oder fehlen Credentials, wird
  das Dropdown zum freien Textfeld, damit das Mapping trotzdem pflegbar bleibt.
- Das verhindert Tippfehler in 15-/18-stelligen Ids und stützt direkt das
  Prinzip „minimal möglicher User-Input": der Nutzer *wählt*, statt zu *kennen*.

---

## 14. Modul I — Reporting & Export

### Reports (`/reports`)
- **Presets:** wöchentlich detailliert (Default), wöchentlich pro Tag&Projekt,
  monatlich pro Projekt, pro Kunde&Projekt, pro Projekt detailliert.
- **Freie Gruppierung:** Dimensionen Tag/Woche/Monat/Projekt/Kunde/Mitarbeiter
  beliebig verschachtelbar; Option „detailliert" hängt Einzeleinträge an;
  Zwischensummen je Ebene + Gesamtsumme.
- **Filter:** Zeitraum, Projekt, Kunde, Mitarbeiter. Der Mitarbeiter-Filter ist
  nur für Admins; Nicht-Admins sehen nur eigene Daten.

### Export
- **Timesheet-API:** `GET /api/v1/reports/timesheet?format=json|csv|markdown`
  mit Filtern (date_from/date_to/project_id/user_id/sync_target/tag) und
  optional `csv_template_id`. Nicht-Admins nur eigene Daten.
- **CSV-Standardspalten** (ohne Template): Datum, Start, Ende, Dauer (h),
  Projekt, Projektname, Kunde, Consultant, Beschreibung, Tags, SyncZiel, ExtRef
  (Trenner `;`, Dezimal `,`).
- **Export über Importformat:** `GET /entries/export` erzeugt eine CSV mit den
  **Quell-Headern des Formats**, gefüllt aus den Zielfeldern. **Round-Trip:**
  Export → Re-Import mit demselben Format → identische Einträge.
- **CSV-Templates** (Export-Profile): wiederverwendbare Profile (Spalten,
  Trenner, Datumsformat, Encoding, Dezimaltrenner) via
  `GET/POST /api/v1/csv-templates`, `GET/PATCH/DELETE /api/v1/csv-templates/{id}`
  (Schreiben Admin, Name eindeutig).

---

## 15. Modul J — System & Konfiguration

### Globale Einstellungen (Admin)
- **Globale KI-Vorgaben:** werden bei jedem KI-Aufruf zusätzlich zu den
  persönlichen Hinweisen als separater System-Block mitgegeben.
- **Theme-Auswahl** (Cookie, 1 Jahr): wählbare Themes (siehe Abschnitt 17),
  in Desktop- und Mobil-Navigation.
- **Salesforce-Zugangsdaten** (siehe Modul H).

### System-Endpunkte
- `GET /healthz` (Liveness), `GET /readyz` (DB-Check), `GET /favicon.ico`,
  statische Dateien unter `/static`.
- **CORS** konfigurierbar (`CORS_ORIGINS`, Default `*`).

### Verhaltensrelevante Konfiguration
- `ANTHROPIC_API_KEY` schaltet die KI frei; `AI_MAPPING_MODEL`
  (Default `claude-sonnet-4-6`), `AI_MAPPING_MAX_SAMPLE_LINES` (Default 15).
- `ACCESS_TOKEN_EXPIRE_MINUTES`, `INITIAL_ADMIN_*`.
- Salesforce: Connected-App-Parameter (Consumer Key, Integration-User-Username,
  Login-URL, API-Version) admin-gepflegt; der **private JWT-Schlüssel** liegt im
  **Secret-Store** (nicht in DB/Env-Klartext/Repo, IP-gelockt, rotierbar — siehe
  Modul H, 13.2/13.5).

### Navigation (Web)
Eingeloggt: **Dashboard** (`/`), **Kalender** (`/calendar`), **Reports**
(`/reports`), **Sync** (`/sync`), **Projekte** (`/projects`), **Import**
(`/import`), **Formate** (`/import-formats`), **Nutzer** (`/users`, nur Admin),
**Profil** (`/profile`), **API** (`/docs`), Theme-Auswahl, Logout. Nicht
eingeloggt: Login + API. Mobil als Hamburger.

---

## 16. Datenmodell (Kernentitäten)

```
users(id, email UNIQUE, full_name, hashed_password, is_admin, is_active,
      ai_hints?, salesforce_user_id?, created_at)
   -- salesforce_user_id: gecachte SF-Identität, aufgelöst aus dem Entra-
   --   FederationIdentifier (oid/UPN); Anker für Owner-Setzen, Filter & Auth

api_keys(id, user_id → users, name, prefix, key_hash UNIQUE,
         last_used_at, revoked_at, created_at)

projects(id, user_id → users, name, code, customer?, color, status,
         sync_targets (Liste), sync_metadata (JSON: {ziel: {key: val}}),
         created_at, updated_at)
   -- code eindeutig pro user_id

time_entries(id, user_id → users, project_id → projects,
             entry_date, start_time?, end_time?, duration_minutes,
             description, tags (JSON-Liste),
             sync_targets_override? (Liste), sync_metadata_override (JSON),
             source (manual|api|csv), external_ref?,
             created_at, updated_at)

entry_syncs(id, entry_id → time_entries (CASCADE), target,
            status (pending|synced|manually_synced|failed|skipped),
            external_ref?, attempts, last_error?, synced_at?,
            created_at, updated_at)
   -- UNIQUE(entry_id, target)

sync_rules(id, name, priority, scope (global|project_id),
           condition, action (add_target|remove_target|set_targets),
           target, enabled)

import_formats(id, name, source_hint, separator, encoding,
               date_format, time_format,
               column_map (JSON: ziel→quelle), transforms (JSON),
               target_rules (JSON), sample_data?, default_project_code?,
               notes, owner_id? → users, is_global,
               created_at, updated_at)

csv_templates(id, name UNIQUE, columns (JSON), separator, date_format,
              encoding, decimal_separator, created_at, updated_at)

app_settings(key, value)   -- globale KI-Hinweise, SF-Credentials, …
```

**Designentscheidungen:**
- `duration_minutes` ist autoritativ; Start/Ende optional.
- `tags` als JSON-Liste (flexibel, Reporting filtert drüber).
- Sync zweistufig: Default am Projekt, optional pro Eintrag überschreibbar.
- Status pro Ziel in eigener Tabelle (`entry_syncs`) statt JSON am Eintrag.
- API-Keys als Hash + Klartext-Prefix; voller Key nur einmalig.

---

## 17. Theming

**Inspiriert von [hister.org](https://hister.org) (Dark Mode).** Das Design ist
fertig implementiert und als CSS-Variablen in `base.html` festgeschrieben. Die
folgenden Tokens sind verbindlich — neue Seiten und Komponenten halten sich daran.

### Charakter

- **Dunkel, kantig, retro-bunt.** Dunkler Hintergrund als Default, kräftige
  Akzentfarben (kein Pastellbrei), Pixel-Style-Schatten (versetzt, kein Blur),
  0 px Border-Radius überall — konsequent eckig.
- **Outfit für Headings, Inter für Fließtext.** Outfit (700–900) in Nav-Links,
  Seitentiteln, Abschnitts-Überschriften und Kennzahlen; Inter (400–600) für
  alles andere.
- **Farbbalken vor Abschnitts-Headings.** Jeder `<h2>` in einer Karte bekommt
  einen 3 px hohen, farbigen Balken links (CSS `::before`-Trick mit
  `.th-section-title .th-section-{purple|teal|orange|olive}`).
- **Pixel-Schatten auf Karten und Buttons.** Kein `box-shadow` mit Blur —
  immer `3px 3px 0 <schattenfarbe>`. Buttons heben sich beim Hover um 1 px an
  und senken sich beim Active-Klick.

### Design-Tokens (CSS-Variablen)

```css
/* Hintergründe & Oberflächen */
--bg:        #1A1A1A   /* Seitenhintergrund */
--surface:   #242422   /* Karten (bg-white) */
--surface2:  #2C2C2A   /* Innere Flächen (bg-slate-50/100) */

/* Ränder */
--border:    #383836   /* Standard-Border */
--border2:   #464644   /* Stärkere Border, Inputs */

/* Text */
--text:      #E0E0DE   /* Haupttext */
--muted:     #888886   /* Sekundärtext */
--faint:     #555553   /* Dezimalhilfstext */

/* Akzentfarben */
--purple:    #9B8AFB   /* Primär-Aktion, aktiver Nav-Link */
--orange:    #E8A060   /* Sekundär-Aktion, Sync-Wizard */
--teal:      #5AC8A0   /* Tertiär-Aktion, Bestätigung */
--olive:     #A8B858   /* KI/Settings, Export */

/* Schatten der Akzentfarben */
--purple-shadow: #6B5FD8
--teal-shadow:   #3AA878
--orange-shadow: #C07830
```

### Button-Hierarchie

| Klasse / Farbe | Verwendung |
| --- | --- |
| `bg-indigo-600` → `--purple` | Primär-Aktion (Speichern, Anlegen) |
| `bg-slate-800` → `--teal` | Sekundär-Aktion (Filter anwenden) |
| `bg-emerald-600` → `--olive` | Bestätigungs-Aktion (Als erledigt markieren) |
| `bg-sky-600` → `--teal` | Integrations-Aktion (SF-Push) |
| `bg-rose-600` → `#c42929` | Destruktiv (Löschen) |

### Tailwind-Remapping

Da die App Tailwind CDN verwendet, werden Tailwind-Klassen in `base.html` per
`!important` umgebogen — z. B. `bg-white` → `var(--surface)`,
`border-slate-200` → `var(--border)`, `text-slate-900` → `var(--text)`.
Neue Komponenten können weiterhin Tailwind-Klassen benutzen; das Remap greift
automatisch.

### Theme-Umschaltung

Per Cookie `theme` (1 Jahr, gesetzt via `GET /set-theme?theme=<name>`).
Werte: `dark` (Default), `light`, `mindsquare`. Das `<html>`-Element trägt
`data-theme="{{ theme }}"`. Alternative Tokens für `light` und weitere Themes
können in `base.html` unter `html[data-theme="light"] { … }` ergänzt werden.

---

## 18. Nicht-funktionale Anforderungen

- **API-first:** Jede Kernfunktion unter `/api/v1`; OpenAPI-Doku automatisch
  unter `/docs`, `/redoc`, `/openapi.json`. Die Web-UI bleibt ein dünner Wrapper
  um die API.
- **Self-hosted & Docker-deploybar:** Single-Image (Multistage-Dockerfile),
  Postgres als zweiter Compose-Service mit persistentem Volume. Proxmox-LXC-
  freundlich. SQLite für lokale Entwicklung ohne Setup.
- **Migrationen:** versionierte DB-Migrationen (Alembic o. Ä.), beim
  Container-Start automatisch angewandt.
- **Tests:** Smoke-Tests für die Kernpfade; „Tests grün vor Commit".
- **Robustheit der Drehscheibe:** Syncs sind idempotent; Pro-Eintrag-Fehler
  isolieren, der Stapel läuft weiter; Verbindungsfehler brechen sauber ab.

**Empfohlener Stack (Orientierung, nicht Vorschrift):** Python 3.12, FastAPI,
SQLAlchemy 2.x + Alembic, PostgreSQL (prod) / SQLite (dev), Jinja2 + Tailwind +
HTMX-ready für die UI, JWT (User) + API-Key (Tools) für Auth, Claude für die
KI-Mapping-Vorschläge.

---

## 19. Abgrenzung & Roadmap

**Bewusst out of scope für v1:**
- Echter API-Push nach **Jira** (Worklogs) und **BCS** — Felder sind
  registriert, der Wizard hakt diese Ziele vorerst nur „manuell erledigt" ab.
- **OAuth/SSO** (Microsoft Entra ID, Authentik) zusätzlich zur lokalen Auth.
- Eigene Tag-/Kunden-Verwaltung (Tags = freie Liste; Kunde = Projektfeld,
  beim Import auto-pflegbar).
- Native Mobile-Apps.

**Roadmap-Ideen:**
- Beim Import unbekannte Projekte mit Vorschlägen aus den aktiven Salesforce-
  Projektbesetzungen des Users matchen, statt nur stumm anzulegen.
- Gleiches beim manuellen Projekt-Anlegen (Namenseingabe → SF-Vorschläge samt
  Übernahme der `assignment_id`).
- Mobile CRUD als Card-Layout statt horizontal-scrollbarer Tabelle.
- Pflege-UI für die Sync-Regel-Engine.
- **Entra-SSO in den Scope ziehen** — Voraussetzung für das entschiedene
  SF-Auth-/Ownership-Modell (Modul H, 13.1); Modul A/§19 entsprechend nachziehen.
- **Spätere Migration auf Per-User-OAuth** ist durch das Ownership-Modell
  vorbereitet (Datensätze gehören bereits den echten SF-Usern, Modul H, 13.3) —
  bewusst nicht v1, da der dedizierte Integration User die kostengünstigere und
  einfachere Anbindung ist (13.2).

---

## 20. Akzeptanz: Woran man merkt, dass es „TimeHub" ist

- Ein Consultant kann seine Woche in Toggl/Clockify erfassen, mit **zwei Klicks**
  importieren und mit **einem geführten Durchlauf** in Salesforce buchen.
- Das **einzige**, was er inhaltlich beisteuern muss, sind die **Mappings**
  (Spalten beim ersten Import, Projekt-Ziel-Zuordnung im Wizard) — alles andere
  füllt TimeHub vor oder leitet es ab.
- Nichts geht ungefragt in ein Fremdsystem; vor jedem Push steht eine Vorschau.
- Ohne KI-Schlüssel funktioniert die App vollständig — nur die Auto-Vorschläge
  fehlen.
- Ein erneuter Sync-Lauf bucht **nichts doppelt**.
```
