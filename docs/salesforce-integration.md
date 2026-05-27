# Salesforce-Anbindung — Recherche & geplanter Workflow

Status: **noch nicht implementiert**. Dieses Dokument hält fest, was die
Salesforce-/Certinia-PSA-APIs für unseren Use Case anbieten und wie der Push
aussehen soll, damit der spätere Einbau in TimeHub einer klaren Vorlage folgt.

## 1. Welches Datenmodell?

| Option | Wann passt es? | Relevante Objekte |
| --- | --- | --- |
| **Certinia PSA** (ex-FinancialForce) | Standard für Consulting-PSA — Zeiten pro Projekt/Assignment/Kontierungsmonat. Unser Use Case. | `pse__Project__c`, `pse__Assignment__c`, `pse__Resource__c` (Lookup auf `Contact`, **nicht** `User`!), `pse__Time_Period__c`, `pse__Timecard_Header__c` (Wochen-Container), `pse__Timecard__c` (Einzeleintrag, ggf. von PSA gesplittet) |
| Salesforce Field Service | Nur sinnvoll, wenn Field Service eh im Einsatz ist. | `TimeSheet`, `TimeSheetEntry` |
| Vanilla Salesforce | Kein dediziertes Time-Tracking — Custom Objects. | — |

Wir gehen für TimeHub von Certinia PSA aus.

## 2. Welche API?

- **REST API** (`/services/data/v60.0/sobjects/...`) reicht für n Einträge
  pro Tag pro Berater. Standard.
- **Certinia PSA REST API** (`/services/apexrest/pse/...`) — kapselt
  Header + Splits + Validierung. **Bevorzugen** für Timecard-Saves, sobald
  wir Multi-Day-Wochen oder Validierungslogik brauchen.
- **Bulk API** nur für Massenmigrationen >2k Records sinnvoll.

## 3. Auth

**OAuth 2.0 JWT Bearer Flow** für Server-to-Server.

- Connected App in der Salesforce-Org anlegen, X.509-Zertifikat hinterlegen
- Pre-Authorization der gewünschten Profile/Permission Sets
- Permission Set: `PSATimecardAPI` + Object/Field-Level Security auf
  `pse__Timecard*__c`
- Server hält den Private Key (in `.env`), tauscht ihn gegen kurzlebige
  Access Tokens. Kein User-Login pro Request.

## 4. Konkreter Workflow für TimeHub

Vorbedingungen pro Berater und Projekt:

1. Im TimeHub-Projekt ist `sync_metadata.salesforce.project_id` gepflegt
   (Mapping zu `pse__Project__c.Id`).
2. Im TimeHub-Userprofil ist entweder `salesforce_user_id` oder
   `salesforce_contact_id` gefüllt (Contact-ID gewinnt, wenn beide da sind).
   Wenn nur User-ID da ist, holen wir den Contact über
   `SELECT ContactId FROM User WHERE Id = :uid`.

Beim Push eines Eintrags:

```
1. resolve_contact_id(user)
2. assignment = SOQL:
     SELECT Id FROM pse__Assignment__c
     WHERE pse__Project__c   = :project_id
       AND pse__Resource__c  = :contact_id
       AND pse__Closed_for_Time_Entry__c = false
   → wenn nichts: Fehler "keine offene Projektbesetzung"
3. period = SOQL:
     SELECT Id FROM pse__Time_Period__c
     WHERE pse__Type__c = 'Week'
       AND pse__Start_Date__c <= :entry_date
       AND pse__End_Date__c   >= :entry_date
   → wenn nichts: Fehler "Kontierungswoche fehlt — Admin in SF anlegen"
4. POST /services/data/v60.0/sobjects/pse__Timecard_Header__c
   {
     "pse__Resource__c":   "<contact_id>",
     "pse__Project__c":    "<project_id>",
     "pse__Assignment__c": "<assignment_id>",
     "pse__Time_Period__c":"<period_id>",
     "pse__Start_Date__c": "<period_start>",
     "pse__End_Date__c":   "<period_end>",
     "pse__Monday_Hours__c": h_mon, ..., "pse__Sunday_Hours__c": h_sun,
     "pse__Status__c": "Saved"
   }
   → speichert Salesforce-ID in `time_entries.sync_metadata_override.salesforce.timecard_header_id`
```

In der UI heißt das im Endausbau ungefähr:

> Berater wählt Projekt aus → System sucht **Projektbesetzung** zum Berater
> → Berater sieht den **aktuellen Kontierungsmonat** vorausgewählt → Trägt
> Stunden ein → Speichern pusht nach Salesforce.

## 5. Stolperfallen / Dinge, die wir früh wissen sollten

- **Resource = Contact, nicht User.** Pflicht. Daher die zwei
  Salesforce-Felder im TimeHub-Userprofil.
- **Submitted/Approved Timecards sind read-only** für Standard-User.
  Update/Delete schlägt fehl, sobald der Approval-Workflow läuft. Wir
  brauchen einen Status-Check (`pse__Status__c`) vor jedem Sync und im
  Fehlerfall einen Resubmit-Flow.
- **Time-Period muss existieren.** PSA legt sie nicht automatisch an. Wenn
  der aktuelle Monat/die aktuelle Woche fehlt, ist das ein Admin-Job in SF
  — wir können nur freundlich melden.
- **Trigger/Flows auf Header** sind kundenspezifisch (Billable/Non-Billable,
  Cost Rate, Region). Fehler kommen als generische
  `FIELD_CUSTOM_VALIDATION_EXCEPTION` zurück — Volltext in der UI zeigen,
  nicht wegschlucken.
- **Rate Limits**: 15k API Requests/Tag pro Org + Lizenzen. Bei 200
  Beratern × 5 Calls/Tag unkritisch; jeder Push sollte trotzdem 1 Call
  bleiben, kein Pre-Flight-Storm.
- **API-Versionen pinnen.** PSA Schema-URLs sind versionierte Pfade
  (`2024.1` etc.). Gegen eine Version testen und in `.env` festklopfen.
- **Sandbox-Test zwingend.** Custom Validations sind kundenspezifisch; was
  in unserer Sandbox läuft, kann in einer fremden Org am ersten Trigger
  scheitern.

## 6. Was TimeHub heute schon vorbereitet hat

- User-Modell: `salesforce_user_id` + `salesforce_contact_id` (nullable),
  editierbar im `/profile`-Reiter.
- Project-Modell: `sync_metadata` (JSON) — wir können dort
  `{"salesforce": {"project_id": "a0X..."}}` ablegen, ohne Schema-Änderung.
- Project hat bereits `default_sync_target` (Enum mit `salesforce`).
- TimeEntry hat `sync_target_override`, `sync_metadata_override`,
  `sync_status`, `external_ref` — der Push-Worker kann da seinen Output
  ablegen, ohne dass es ein zusätzliches Schema braucht.

## 7. Quellen

- [Certinia PSA REST API — Timecards (2024.1)](https://help.certinia.com/TechnicalReference/2024.1/ProfessionalServicesAutomation/Rest/Timecards.htm)
- [pse__Timecard_Header__c Schema (2024.2)](https://help.financialforce.com/TechnicalReference/2024.2/ProfessionalServicesAutomation/Schema/Timecard_Header__c.htm)
- [pse__Timecard__c Schema (2024.1)](https://help.certinia.com/TechnicalReference/2024.1/ProfessionalServicesAutomation/Schema/Timecard__c.htm)
- [PSATimecardAPI Permission Set](https://help.certinia.com/TechnicalReference/2024.3/ProfessionalServicesAutomation/Permissions/PSATimecardAPIps.htm)
- [Apex PSATimecardService (alternative zum direkten REST)](https://help.certinia.com/TechnicalReference/2023.2/ProfessionalServicesAutomation/Apex/PSATimecardService.htm)
- [Salesforce TimeSheet (Field Service, falls relevant)](https://developer.salesforce.com/docs/atlas.en-us.field_service_dev.meta/field_service_dev/sforce_api_objects_timesheet.htm)
- [OAuth 2.0 JWT Bearer Flow](https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_jwt_flow.htm)
