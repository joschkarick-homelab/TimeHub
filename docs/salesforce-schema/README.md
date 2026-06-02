# Salesforce-Schema (mindsquare-Org)

Dump der relevanten Custom Objects der Ziel-Org, mit denen TimeHub
synchronisieren soll. **Keine Standard-Certinia-PSA** — eigene Objekte mit
deutschen Namen.

## Dateien

| Datei | Inhalt |
| --- | --- |
| `Projektbesetzung__c.fields.json` | Projektzuordnung (Mitarbeiter × Projekt) — Anker für den Sync |
| `Kontierungsmonat__c.fields.json` | Abrechnungsmonat pro Projektbesetzung |
| `Zeiterfassung__c.fields.json` | Einzelner Tages-Zeiteintrag (eine Zeile pro TimeHub-Eintrag) |

Jede Datei ist die rohe `fields`-Liste, wie sie das Salesforce-REST-Describe
zurückgibt (`/services/data/v60.0/sobjects/<Name>/describe`).

## So aktualisierst du sie

Wenn ihr in Salesforce Felder hinzufügt/umbenennt:

1. TimeHub-UI → **Nutzer** → Sektion „Salesforce-Integration"
2. Block „Schema-Inspektor" ausklappen
3. Objektnamen eintragen (z. B. `Projektbesetzung__c`) und „Schema abrufen"
4. Auf der Ergebnis-Seite den JSON-Rohblock kopieren und in die entsprechende
   Datei hier einfügen (über den vorhandenen Inhalt drüber)

## Datenmodell (relevant für den Sync)

```
TimeHub.Project.sync_metadata.salesforce.assignment_id  ─►  Projektbesetzung__c.Id
                                                              │
                                                              │ (Projektbesetzung__c)
                                                              ▼
                          Kontierungsmonat__c (gefiltert über Monatsbeginn/Monatsende)
                                                              │
                                                              │ (Kontierungsmonat__c)
                                                              ▼
                                                   Zeiterfassung__c (ein Record pro Tag)
```

Wichtiger Unterschied zu Certinia-PSA: **Der Kontierungsmonat ist hier
pro Projektbesetzung** (Referenz `Projektbesetzung__c`). Wir können also
nicht einen Monatszeitraum global suchen — die Abfrage muss IMMER
`WHERE Projektbesetzung__c = '…' AND Monatsbeginn__c <= :tag AND
Monatsende__c >= :tag` enthalten.

## Feld-Mapping TimeHub → Zeiterfassung__c (umgesetzt)

| TimeHub | Zeiterfassung__c | Hinweis |
| --- | --- | --- |
| `entry_date` | `Tag__c` (date, **Pflicht**) | direkt |
| `description` (gekappt auf 255 Zeichen) | `Taetigkeitsbeschreibung__c` (**Pflicht**) | hartes Kappen, kein Ellipsis |
| `duration_minutes` | `Arbeitszeit_Minuten__c` (double) | direkt |
| `duration_minutes / 60` | `Arbeitszeit__c` (double) | beide gesetzt |
| `start_time.hour` | `Von_Stunde__c` (**Pflicht**) | wenn `start_time` leer: 0 |
| `start_time.minute` (gesnappt) | `Von_Minute__c` (picklist 00/15/30/45) | nächste Viertelstunde |
| `end_time.hour` | `Bis_Stunde__c` (**Pflicht**) | wenn `end_time` leer: Dauer-in-Stunden ab Mitternacht |
| `end_time.minute` (gesnappt) | `Bis_Minute__c` (picklist) | nächste Viertelstunde |
| (immer 0) | `Pause__c` (**Pflicht**) | TimeHub trackt keine Pausen |
| `sync_metadata_override.salesforce.remote` | `Remote__c` (boolean) | lenient parsing: true/1/yes/ja/x/wahr → true |
| (gesetzt vom Lookup) | `Kontierungsmonat__c` (reference, **Pflicht**) | per SOQL auf der zugehörigen Projektbesetzung gesucht |

Das `Remote__c`-Flag wird als Eintrag-Sync-Feld `sync:salesforce.remote`
exponiert — sowohl für die manuelle Erfassung am Eintrag als auch für
Import-Transformationen aus der Quell-CSV.

## Skip-Regeln in der Vorschau

Ein TimeHub-Eintrag erscheint in „Übersprungen", wenn:

- keine `salesforce.assignment_id` am Projekt gepflegt ist,
- die Projektbesetzung in SF nicht gefunden wird,
- die Projektbesetzung `Geschlossen__c=true` ist,
- kein Kontierungsmonat für (Projektbesetzung × Tagesdatum) existiert,
- der Kontierungsmonat `Abgeschlossen__c=true` ist,
- der Kontierungsmonat einen Status ≠ `offen` hat (also bereits eingereicht
  / in Bearbeitung / kontrolliert / Öffnung beantragt).

## UI

- Im Projekt-Edit (Ziel = salesforce) ist die Projektbesetzung ein
  **Dropdown**, das beim Render live alle aktiven Projektbesetzungen des
  eingeloggten Users über die SF-API holt (Match per `Mitarbeiter__r.Email`
  ODER `Externe_Projektbesetzung__r.Email = user.email`). Fehlen SF-Creds
  oder gibt es keine Treffer, fällt das UI auf ein freies Text-Input zurück.
- Im Eintrag-Edit (effektives Ziel = salesforce) ist „Remote / Vor Ort" ein
  Dropdown mit Default **Remote**. Beim Sync gilt: explizit gesetzter Wert
  am Eintrag → ggf. Override am Projekt → Default des Felds.

## Offene Punkte (für später)

1. **Vorschlag bei unbekannten Projekten:** Beim CSV-Import (auto-Anlegen)
   und beim manuellen Projekt-Anlegen sollen passende Salesforce-
   Projektbesetzungen aus den aktiven PBs des Users als Vorschlag angeboten
   werden (Match z. B. über Namensähnlichkeit / Projektbezeichnung).
2. **Bestehender Kontierungsmonat nicht da:** Aktuell „skipped" mit Hinweis.
   Soll TimeHub später einen anlegen können?
3. **Konfigurierbarkeit:** Feld-/Objektnamen sind in der mindsquare-Org
   hartcodiert. Wenn ihr in einer zweiten Org gegen ein anderes Schema
   syncen wollt, wird das ein Admin-Setting.
