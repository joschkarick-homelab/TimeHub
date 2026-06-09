# TimeHub — Konzept: Gebündelter Multi-Ziel-Export

Status: **Entwurf / Brainstorming**. Dieses Dokument hält das gemeinsam
erarbeitete Konzept für den komfortablen, gebündelten Export von Zeiteinträgen
nach mehreren Zielsystemen (Jira, BCS, Salesforce) fest. Es ergänzt die
bestehenden Anforderungen (`anforderungen.md`, Abschnitte 19–21) und ersetzt
sie noch nicht.

> Begriff: **BCS** = das Ziel, das im UI teils als „BSC" auftaucht. Im Code und
> hier durchgängig `bcs`.

---

## 1. Ausgangslage & Ziel

**Workflow**, den wir komfortabel machen wollen:

1. Stunden werden erfasst (TimeHub, Toggl, Clockify …).
2. Ggf. Import nach TimeHub (täglich/wöchentlich).
3. **Export gruppiert nach Zielen** (täglich/wöchentlich) — dieser Schritt ist
   heute unkomfortabel.

Zwei Oberflächen sollen das lösen:

- **Status-Matrix** im Dashboard: pro Eintrag auf einen Blick sehen, was wohin
  noch offen / erledigt / blockiert / irrelevant ist.
- **Wizard**: die offenen Syncs gebündelt abnicken, mit kurzem Korrektur-Loop.

**Leitprinzip:** Wir bauen kein neues Fundament. Das Datenmodell ist für diesen
Workflow erstaunlich gut vorbereitet (`sync_status`, `entry_sync_status()`,
SF-`preview → execute`). Wir generalisieren Vorhandenes auf *mehrere* Ziele und
setzen zwei UI-Flächen darauf.

---

## 2. Getroffene Entscheidungen

| # | Frage | Entscheidung |
|---|-------|--------------|
| E1 | Ziele pro Eintrag | **Mehrere** (dieselbe Stunde kann z. B. nach Jira *und* BCS) |
| E2 | Matrix-Zeile | **Pro Eintrag** (Zeile = Eintrag, Spalten = Ziele) |
| E3 | Wizard-Modus | **Hybrid** (Batch pro Ziel, Einzeleintrag herauslösbar) |
| E4 | Status-Storage | **Eigene Tabelle `EntrySync`** (eine Zeile je Eintrag×Ziel) |
| E5 | Ziel-Herkunft | **Projekt-Default-Liste + Regeln** |

---

## 3. Datenmodell

### 3.1 Von „ein Ziel" zu „Ziel-Menge"

Heute ist alles auf genau ein Ziel verdrahtet (`effective_target()` in
`app/services/sync_fields.py:126`). Drei Stellen ändern sich:

- `Project.default_sync_target` (String) → **`Project.sync_targets`** (Liste),
  z. B. `["jira", "bcs"]`. „Dieses Projekt läuft standardmäßig nach Jira und BCS."
- `TimeEntry.sync_target_override` (String) → **`TimeEntry.sync_targets_override`**
  (optionale Liste). Leer = erbt vom Projekt/aus Regeln.
- **Effektive Menge** = `sync_targets_override` falls gesetzt, sonst Ergebnis der
  Regel-Auflösung (siehe §4). `intern`/`none` entfallen — „leere Menge =
  nichts zu tun".

`sync_metadata` (Projekt) und `sync_metadata_override` (Eintrag) sind bereits
zielweise verschachtelt (`{jira: {...}, salesforce: {...}}`) und bleiben
unverändert nutzbar.

### 3.2 `EntrySync` — Status pro Ziel

Der heutige eine `sync_status` + das eine `external_ref` reichen für mehrere
Ziele nicht (je Ziel braucht es eigenen Status *und* eigene Remote-Id). Neue
schmale Tabelle:

```
EntrySync                      # eine Zeile pro (Eintrag × Ziel)
  id            PK
  entry_id      FK time_entries (CASCADE)
  target        "jira" | "salesforce" | "bcs"
  status        pending | synced | manually_synced | failed | skipped
  external_ref  Remote-Id im Zielsystem (z. B. Zeiterfassung__c-Id / Jira-Worklog-Id)
  attempts      int, default 0
  last_error    text, nullable
  synced_at     timestamptz, nullable
  created_at / updated_at
  UNIQUE(entry_id, target)
```

Warum Tabelle statt JSON-Feld am Eintrag:

- **Aggregate** für Matrix & Zähler sind triviale Queries („7 offen für Jira").
- **Retry** hat einen natürlichen Platz (`attempts`, `last_error`) → rote Ampel.
- **Audit** „was ging wann wohin" fällt nebenbei ab (`synced_at`, `external_ref`).
- Jedes Ziel hat seine **eigene Remote-Id** (das einzelne `external_ref`-Feld am
  Eintrag kann das nicht).

Die Werte des `SyncStatus`-Enums (`app/models/_enums.py`) werden 1:1 übernommen.

### 3.3 Migration & Backfill

- Schema: `EntrySync` anlegen, `Project.sync_targets` (Liste) und
  `TimeEntry.sync_targets_override` (Liste) ergänzen.
- Backfill: pro bestehendem Eintrag aus heutigem `effective_target` +
  `sync_status` **eine** `EntrySync`-Zeile erzeugen (Nicht-Sync-Ziele
  `intern`/`none` → keine Zeile). `Project.sync_targets` aus
  `default_sync_target` als ein-elementige Liste füllen.
- Optionaler Rollup am Eintrag (`overall: done|partial|blocked`) zum schnellen
  Filtern/Sortieren der Liste — ableitbar, nicht zwingend.

---

## 4. Regel-Mechanismus (E5: Projekt-Default + Regeln)

Basis ist die Projekt-Liste `sync_targets`. Regeln verfeinern die Ziel-Menge je
Eintrag (z. B. „Tag `billable` → zusätzlich Jira", „Beschreibung enthält
`intern` → BCS entfernen").

### 4.1 Modell

```
SyncRule
  id, name, priority (int, kleiner = früher)
  scope        "global" | project_id   (Projekt-Regeln überschreiben globale)
  condition    deklarativ, klein gehalten (siehe 4.2)
  action       add_target | remove_target | set_targets
  target       Ziel, auf das die Action wirkt (bei set_targets: Liste)
  enabled      bool
```

### 4.2 Bedingungs-Vokabular (klein starten, erweiterbar)

Bewusst minimal, analog zur bestehenden Felder-Registry (`sync_fields.py`):

- `tag_is` / `tag_in` — Tags sind die natürlichste Quelle (Toggl/Clockify liefern sie).
- `project_code_in` — bestimmte Projekte.
- später ggf. `description_matches` (Regex), `duration_gte` …

### 4.3 Auflösung & Materialisierung

`app/services/sync_rules.py: resolve_targets(entry, project) -> set[str]`:

1. Start: `set(project.sync_targets)`.
2. Regeln nach `priority` anwenden (global zuerst, dann Projekt-Scope).
3. Ergebnis = effektive Ziel-Menge des Eintrags.

**Wann läuft das?** Beim **Anlegen/Import** eines Eintrags. Das Ergebnis wird
*materialisiert*: für jedes Ziel der Menge eine `EntrySync`-Zeile mit
`status=pending`. Status braucht eine Zeile — also müssen die Ziele zum
Erfassungszeitpunkt feststehen.

**Manuelle Übersteuerung gewinnt:** Setzt der Nutzer `sync_targets_override`
oder fügt/entfernt er ein Ziel am Eintrag, bleibt das stehen und wird von einer
erneuten Regel-Auswertung nicht überschrieben.

**Re-Evaluierung (nach Regeländerung):** Aktion „Regeln neu anwenden" gleicht
bestehende Einträge ab — fehlende Ziele als `pending` ergänzen, nicht mehr
zutreffende `pending`/`skipped`-Zeilen entfernen. **Bereits gesyncte Zeilen
(`synced`/`manually_synced`) werden nie gelöscht** (Audit & Remote-Id bleiben).

### 4.4 `sync_fields.py` generalisieren

`entry_sync_status(entry, project)` rechnet heute für genau ein
`effective_target`. Es wird zu „rechne für *jedes* Ziel der effektiven Menge"
und liefert pro Ziel `{ready, missing[]}`. Die gesamte Validierungslogik
(Pflichtfelder, Regex) bleibt unverändert — sie läuft nur in einer Schleife.

---

## 5. Status-Ableitung (Ampel pro Zelle)

Pro Eintrag × Ziel:

| Farbe | Bedeutung | Bedingung |
|-------|-----------|-----------|
| ⚪ grau | nicht relevant | Ziel ∉ effektive Menge des Eintrags |
| 🟢 grün | erledigt | `EntrySync.status ∈ {synced, manually_synced}` |
| 🟡 gelb | offen, kann | in Menge, `ready`, Status `pending` |
| 🔴 rot | offen, blockiert | in Menge, aber `!ready` (Tooltip aus `missing[]`) **oder** `status=failed` (Tooltip aus `last_error`) |

Rot trägt seinen Grund also schon im Datensatz — entweder fehlende Pflichtfelder
oder der API-Fehler des letzten Versuchs.

---

## 6. Idee 1 — Dashboard-Status-Matrix

- Zeile = Eintrag, Spalten = Jira / BCS / Salesforce, drei unabhängige
  Ampelpunkte.
- **Read-only Überblick & Einstieg.** Rendert nur `entry_sync_status` je Ziel →
  schnell zu bauen, keine Schreiblogik.
- Tooltip an roten/gelben Zellen zeigt fehlende Felder bzw. Fehlertext.
- Klick auf **Spaltenkopf** (z. B. „Jira") oder eine **gelbe Zelle** startet den
  Wizard, gefiltert auf genau diesen Scope.
- Bestehende Filter (Datum/Projekt) gelten weiter; passt zum täglich/wöchentlich-
  Rhythmus.

---

## 7. Idee 2 — Export-Wizard (Hybrid)

Generalisierung des bestehenden Salesforce-Flows (`preview → execute` in
`app/web/router.py`) in einen ziel-agnostischen Ablauf:

1. **Sammeln:** alle gelben (sync-bereiten) Einträge je Ziel.
2. **Karte pro Ziel** mit Zusammenfassung („Salesforce: 7 Einträge, 12,5 h →
   Kontierungsmonat Mai"). Rot-blockierte Einträge landen in einem separaten
   „kann nicht"-Stapel der Karte (mit Grund).
3. **Hybrid-Korrektur:** Einzeleintrag aus dem Batch *herauslösen* → Inline-Edit
   → zurück an dieselbe Stelle. Der restliche Batch bleibt stehen.
4. **Abnicken** → `execute` → `EntrySync.status` wird gesetzt (synced bzw.
   failed mit `last_error`), Matrix färbt sich nach.

**Wichtige Realität:** Der **Salesforce-Push existiert bereits**, **Jira- und
BCS-Push-Clients noch nicht** (nur Felder in `sync_fields.py` registriert).
Bis die API-Clients stehen, leisten die Jira/BCS-Karten in v1 nur
„als manuell-erledigt markieren" (`manually_synced`) bzw. CSV-Ausgabe.

---

## 8. Bau-Reihenfolge (Phasen)

- **Phase 0 — Fundament (unsichtbar, entsperrt alles): ✅ umgesetzt.**
  `EntrySync`- und `SyncRule`-Modell, `Project.sync_targets` /
  `TimeEntry.sync_targets_override`, Migration `0008` mit Backfill,
  `sync_fields` um `effective_targets`/`status_for_target`/
  `entry_sync_statuses` erweitert (alt-Funktionen unverändert),
  `sync_rules`-Service (`resolve_targets` + Vokabular `always`/`has_tag`/
  `project_code`) und `entry_sync.reconcile_entry_syncs`. Materialisierung
  ist in die API-Erstell-/Update-Pfade und den CSV-Import verdrahtet.
  *Noch offen für eine Folge-Phase:* Verdrahtung der Web-Formular-Pfade
  (Dashboard/Kalender-Erstellung) — folgt mit der Matrix in Phase 1.
- **Phase 1 — Matrix: ✅ umgesetzt.** Read-only Statusübersicht im Dashboard:
  drei Ziel-Spalten (Jira/BCS/Salesforce) mit Ampelpunkt pro Eintrag (Desktop-
  Tabelle und Mobile-Karten), Tooltip trägt den Grund (fehlende Felder bzw.
  Fehlertext). `entry_sync.matrix_cell/matrix_row` leiten die Farbe aus
  `EntrySync.status` + `status_for_target` ab. Die in Phase 0 vertagten
  Web-Formular-Pfade (Dashboard-/Kalender-Erstellung, Edit) sind jetzt
  materialisiert, und die realen Schreibpfade (Salesforce-Push, manuell-
  erledigt-Markierung + Undo) sind auf `EntrySync` gebrückt, damit die Matrix
  der Realität entspricht.
- **Phase 2 — Wizard: ✅ umgesetzt.** Das Sync-Center ist zum Export-Wizard
  ausgebaut: eine Karte pro Ziel, gespeist aus den `EntrySync`-Buckets
  (`entry_sync.wizard_buckets`) mit bereit/blockiert/erledigt + Stunden.
  Salesforce delegiert an den bestehenden Live-`preview → execute`-Flow;
  Jira/BCS werden über `POST /sync/{target}/mark-done` als manuell erledigt
  abgehakt (bis die Push-Clients stehen). Blockierte Einträge listen den
  Grund und einen Korrektur-Deeplink (`/entries/{id}/edit?next=/sync`), der
  nach dem Speichern wieder im Wizard landet (Hybrid-Korrektur). Einstieg
  aus der Matrix: die Spaltenköpfe im Dashboard verlinken auf den Wizard.
- **Phase 2c — Salesforce-spezifische Fehlerfälle im Vorschau-Dialog (Idee, noch offen).**
  Im SF-`preview`-Flow gibt es drei Klassen von übersprungenen Einträgen, die
  der Nutzer heute ohne Systemwechsel nicht lösen kann. In Prioritätsreihenfolge:

  1. **Keine Projektbesetzung hinterlegt** — bereits gelöst (Inline-Dropdown mit
     Live-SOQL-Vorschlag im Wizard-Blockiert-Bereich).

  2. **Projektbesetzung vorhanden, aber geschlossen** (`Geschlossen__c = true`).
     Muss vom PM verlängert werden. Idee: kopierbare Nachricht im Vorschau-Dialog
     anbieten, z. B. *„Hi. Kannst du bitte Projekt \<Projektname\> verlängern?"*
     mit dem SF-Projektnamen vorausgefüllt (kein API-Schreibzugriff nötig).

  3. **Kein Kontierungsmonat zur Projektbesetzung** (`get_monthly_period` → None).
     Idee: direkt aus dem Vorschau-Dialog einen Kontierungsmonat anlegen
     (`POST Kontierungsmonat__c` mit `Projektbesetzung__c`, `Monatsbeginn__c`
     = erster des Monats, `Monatsende__c` = letzter des Monats,
     `Status__c = "offen"`), danach Vorschau neu laden. Einträge desselben
     (PB × Monat) werden gruppiert — ein Button entsperrt alle auf einmal.
- **Phase 3 — Jira/BCS-Push-Clients:** echte API-Integrationen, unabhängig
  parallelisierbar.

---

## 9. Offene Detailfragen

- Bedingungs-Vokabular der Regeln: reicht `tag_*` + `project_code_*` für v1?
- Regeln-Pflege-UI: eigene Admin-Seite, oder erst nur per API/Seed?
- Granularität der Wizard-Karten bei sehr vielen Einträgen (Paginierung?).
- Jira-Push: Worklog-API (REST v3) — Auth (PAT vs. OAuth), Felder-Mapping.
- BCS-Push: API-Verfügbarkeit / Auth noch offen.
- Soll der optionale Rollup-Status am Eintrag mitgeführt werden (Perf vs.
  Redundanz)?
