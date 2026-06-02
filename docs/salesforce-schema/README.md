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

## Feld-Mapping TimeHub → Zeiterfassung__c

| TimeHub | Zeiterfassung__c | Hinweis |
| --- | --- | --- |
| `entry_date` | `Tag__c` (date, **Pflicht**) | direkt |
| `description` | `Taetigkeitsbeschreibung__c` (textarea 255, **Pflicht**) | bei >255 Zeichen kappen |
| `duration_minutes` | `Arbeitszeit_Minuten__c` (double) | direkt |
| `duration_minutes / 60` | `Arbeitszeit__c` (double) | beide setzen |
| `start_time` (HH) | `Von_Stunde__c` (double, **Pflicht**) | falls leer: 0 |
| `start_time` (MM, 00/15/30/45) | `Von_Minute__c` (picklist) | falls nicht im Raster: runden |
| `end_time` (HH) | `Bis_Stunde__c` (double, **Pflicht**) | falls leer: `Von_Stunde__c + Arbeitszeit__c` |
| `end_time` (MM, 00/15/30/45) | `Bis_Minute__c` (picklist) | |
| `start_time` (datetime) | `Von__c` (datetime) | optional, mit Tag kombinieren |
| `end_time` (datetime) | `Bis__c` (datetime) | optional |
| (nicht in TimeHub) | `Pause__c` (double, **Pflicht**) | Default 0 |
| (nicht in TimeHub) | `Remote__c` (boolean, **Pflicht**) | Default false; ggf. später als Eintrag-Feld konfigurierbar |
| (gesetzt vom Lookup) | `Kontierungsmonat__c` (reference, **Pflicht**) | per SOQL ermittelt |

## Offene Punkte (für die nächste Iteration)

1. **Pflichtfelder Von/Bis-Stunde:** TimeHub-Einträge ohne `start_time`/`end_time`
   müssen einen Standard bekommen — z. B. `Von_Stunde__c=0`,
   `Bis_Stunde__c=Arbeitszeit__c`. Ist das in eurer Org so akzeptiert oder
   müssen die Zeiten echt sein?
2. **Remote-Flag:** TimeHub kennt das aktuell nicht. Sollen wir das pro Eintrag
   pflegbar machen (zusätzliches Sync-Feld) oder pro Projektbesetzung als
   Default am Projekt hinterlegen?
3. **Status des Kontierungsmonats:** Schreiben in einen geschlossenen Monat
   (`Abgeschlossen__c=true` oder `Status__c=abgeschlossen/kontrolliert`)
   sollte vermutlich blockiert werden. Welche Status-Werte gelten als
   „schreibgeschützt"?
4. **Bestehender Kontierungsmonat nicht da:** Soll TimeHub einen anlegen
   (würde die Status-Logik in SF triggern) oder fehlen lassen und im Preview
   melden „Kontierungsmonat anlegen lassen"?
5. **Tätigkeitsbeschreibung-Limit (255 Zeichen):** Bei längeren Beschreibungen
   in TimeHub — abschneiden + Ellipsis, oder den Sync verweigern?
6. **Konfigurierbarkeit:** Die Feld-/Objektnamen sollten in den Admin-Settings
   pflegbar sein, damit Schema-Änderungen keinen Code-Patch brauchen.
