# BCS-Anbindung — Recherche, Lizenzfrage & geplanter Workflow

Status: **noch nicht implementiert / Planung**. Dieses Dokument hält fest, wie
TimeHub Aufwände nach Projektron BCS bringen soll, welche Lizenzfrage daran
hängt und welcher Ausbaupfad gewählt wurde. Es ergänzt `vibe-coding-anforderungen.md`
(Abschnitte zur Feld-Registry, Zeilen 471–472) und `export-konzept.md`.

> Begriff: **BCS** = das Ziel, das im UI teils als „BSC" auftaucht. Im Code und
> hier durchgängig `bcs`.

---

## 1. Entscheidung: Zweistufig — erst CSV, später Webservices

Es gibt zwei grundsätzlich verschiedene Wege, Aufwände in BCS zu bringen:

| Weg | Lizenz/Modul nötig? | Charakter |
| --- | --- | --- |
| **CSV-Import via Import-Export-Modul** | **Ja, eigenes Modul** (vermutlich kostenpflichtig) | BCS liest Aufwandsbuchungen per Datei-Import ein. Nutzt vorhandene TimeHub-Export-Templates. Leichter manueller bzw. zeitgesteuerter Schritt. |
| **SOAP-Webservices** | **Ja (Add-on)** | Echtzeit-, bidirektionaler, vollautomatischer Push ohne Handarbeit. |

> **Wichtige Korrektur (verifiziert):** Beide Wege hängen sehr wahrscheinlich an
> einem **kostenpflichtigen Add-on**. Der CSV-Weg ist **nicht** automatisch
> „gratis" — er setzt das **Import-Export-Modul** voraus, das laut Projektron
> „regelmäßigen Import von Aufwandsbuchungen" leistet, aber separat lizenziert
> ist. Ob es in eurem Vertrag steckt, ist offen (siehe §2).

**Gewählter Pfad (E-BCS-1): zweistufig.**

- **Stufe 1 (v1):** Aufwände über **CSV-Export aus TimeHub → Import in BCS**
  (Import-Export-Modul). Voraussetzung: Modul ist im Lizenzumfang.
- **Stufe 2 (Ausbau):** echter **Webservices-Push**, sobald Volumen/Bedarf den
  manuellen Import-Schritt nicht mehr rechtfertigen und die Lizenz geklärt ist.

Das passt zum aktuellen Stand: BCS-Felder (`subject`, `task`) sind registriert,
ein Push-Client ist bewusst noch nicht gebaut; die UI leistet v1 nur
„als manuell erledigt markieren" bzw. CSV-Ausgabe.

---

## 2. Lizenzfrage Webservices (nur relevant für Stufe 2)

Die Projektron-BCS-Webservices sind ein **kostenpflichtiger, separat zu
aktivierender Bestandteil**. Die Nutzung setzt aktivierte BCS-User-Lizenzen für
Webservices voraus. Kommunikation läuft über SOAP 1.1/1.2 + WSDL 1.1/2.0.

**Standard-Webservices** (verifiziert, Projektron):
- **„Externe Zeiterfassung"** (External Time Recording) — der relevante für
  Aufwände/Zeiten.
- **„Externes Ticketsystem"** (External Ticket System).
- ein **frei konfigurierbarer** Webservice für individuelle Anbindungen.

→ Es gibt **keinen** Webservice namens `timerecording`.

### 2.1 Ist die Lizenz schon aktiviert? — Prüfwege

1. **WSDL-URL-Raten funktioniert NICHT** (verifiziert durch Test).
   Ein Webservice ist in BCS ein **konfiguriertes Objekt**: Er wird in der
   Administration angelegt/aktiviert und bekommt **dort** seinen Endpunkt. Es
   gibt **keinen festen, ratbaren `*.wsdl`-Pfad** unter der Basis-URL. Die WSDL
   ist erst abrufbar, *nachdem* der Service konfiguriert **und** lizenziert ist.
   Getestet und erwartungsgemäß ohne Treffer:
   - `https://<account>.bcs-hosting.de/ws/timerecording.wsdl`
   - `https://<account>.bcs-hosting.de/bcs/ws/timerecording.wsdl`
   - `https://<account>.bcs-hosting.de/bcs/webservices/timerecording.wsdl`

   „WSDL nicht erreichbar" ist daher mit „nicht aktiviert" vereinbar, aber
   **kein Beweis** — ohne Konfiguration existiert schlicht kein Pfad.

2. **Lizenzübersicht im Admin (verbindlich im System).**
   Als BCS-Admin unter *Administration → Systeminformation/Lizenz* die
   freigeschalteten Module/Lizenzbestandteile prüfen. Die Lizenzdatei/das
   Zertifikat listet aktivierte Features explizit. (Menüpfad je BCS-Version
   leicht abweichend.) Dort auch sehen, ob der WS „Externe Zeiterfassung" und
   das Import-Export-Modul gelistet sind.

3. **Vertrags-/Projektron-Kontakt (sicher, empfohlen).**
   Eine Anfrage klärt für **beide** Wege Aktivierung + Preis in einem Rutsch:
   „Sind das **Import-Export-Modul** und/oder der Webservice **Externe
   Zeiterfassung** in unserem Vertrag aktiv — und was kostet das jeweils?"

### 2.2 Kosten

Projektron veröffentlicht **weder für die Webservices-Lizenz noch für das
Import-Export-Modul** Preise. Öffentlich verfügbar (Stand 2026, Richtwerte):

- Lizenzierung **pro Nutzer** (gestaffeltes Rollenmodell, gezählt werden
  Logins/24 h).
- BCS.start: ab ~20 €/User/Monat.
- Voll-BCS: durchschnittliche Lizenzgröße ~250–350 € (einmalig/User) bzw.
  Miete ~4 % des Lizenzpreises/Monat.
- **Webservices und Import-Export-Modul = Add-ons**, Preis jeweils nur über
  individuelles Projektron-Angebot.

→ Für belastbare Zahlen ist eine Anfrage bei Projektron nötig — und zwar für
**beide** Module, weil beide Wege daran hängen.

---

## 3. Stufe 1 — CSV-Import (v1, via Import-Export-Modul)

> Voraussetzung: Das **Import-Export-Modul** ist im Lizenzumfang (siehe §2).
> Falls nicht, ist auch Stufe 1 erst nach Klärung mit Projektron möglich — dann
> bleibt v1 reine CSV-Ausgabe aus TimeHub + manuelles Markieren.

### 3.1 Was BCS pro Aufwand braucht

Aus der Feld-Registry: ein BCS-Aufwand braucht `subject` und `task` (beide
Pflicht, Eintrag-Ebene) plus die Kernfelder eines Zeiteintrags (Datum, Dauer,
Beschreibung, Bearbeiter/User).

### 3.2 Workflow

1. In TimeHub ein **CSV-Export-Template** im von BCS erwarteten Importformat
   anlegen (Spalten-Mapping inkl. `sync:bcs.subject`, `sync:bcs.task`).
2. Einträge mit Ziel `bcs` filtern/exportieren (Status-Matrix → BCS-Spalte).
3. Datei im BCS-UI über den Aufwands-Import einlesen.
4. In TimeHub die Einträge als `manually_synced` markieren.

### 3.3 Offen für Stufe 1

- [ ] Exaktes Spaltenformat des BCS-Aufwands-Imports beschaffen (Beispiel-Import
      aus BCS exportieren oder Doku/Support).
- [ ] BCS-Export-Template in TimeHub anlegen und gegen einen Testimport prüfen.
- [ ] Klären, wie `subject`/`task` in BCS adressiert werden (Name vs. ID).

---

## 4. Stufe 2 — Webservices-Push (Ausbau)

**Lizenz vorhanden (Stand 06/2026).** Relevant ist der **`TimerecordingWebService`**:
laut BCS-Doku legt er neue Buchungen an, fragt/ändert/löscht bestehende, bucht
Anwesenheiten/Pausen und setzt Buchungsabschlüsse. Genau unser Aufwands-Push.

Die WSDL liegt versioniert unter [`docs/bcs/TimerecordingWebService.wsdl`](bcs/TimerecordingWebService.wsdl).

### 4.1 Endpunkt / WSDL (verifiziert aus WSDL)

Pfad ist **`/webservices/…`** (Geschwister-Kontext neben `/bcs`), bestätigt
durch den abgerufenen `soap:address`:

```
SOAP 1.1: https://mindsquare.bcs-hosting.de:443/webservices/TimerecordingWebService
WSDL:     …/webservices/TimerecordingWebService?wsdl   (1.1) | ?wsdl2 (2.0)
XSD-Teile:…/webservices/TimerecordingWebService?xsd=1 … ?xsd=12
```

- Style: **document/literal**, SOAP 1.1 **und** 1.2 Binding vorhanden.
- ⚠️ **WSDL-Fallstrick:** Der SOAP-**1.2**-Port zeigt im `soap12:address` auf
  `http://localhost:8080/…` (BCS-Default, nicht ersetzt). Daher **SOAP 1.1**
  nutzen *oder* die Endpoint-Adresse im Client hart überschreiben.

### 4.2 Auth (verifiziert aus WSDL-Policy + Doku)

Die WSDL trägt die Policy `UsernameTokenOverTransport`
(`SignedSupportingTokens` → `UsernameToken`, `AlwaysToRecipient`):

- **WS-Security UsernameToken Profile 1.1**, PasswordType **PasswordText**
  (Klartext), zwingend über **HTTPS** (Transport-Security).
- **Keine** Spaces/Zeilenumbrüche um Username/Password.
- Alle Operationen laufen im **Permission-Kontext des Login-Users**; dieser wird
  als `insUserOid`/`updUserOid` geführt → **dedizierter technischer User**
  (Projektron-Empfehlung), der für die Berater bucht.

### 4.3 Benötigte Berechtigungen (Admin) — *aktueller Blocker*

Für den technischen Service-User:

1. **Systemrecht „Webservices nutzen"** (über Rolle/Gruppe in der
   Rechteverwaltung; exakter Label-Wortlaut steht im internen PDF).
2. **Funktionale Buch-Rechte:** Aufwände erfassen/ändern — und zwar **stellver-
   tretend für andere User**, da der Service-User für die Berater bucht (siehe
   §4.5, Anforderung „User-Match").
3. Lese-Recht auf die buchbaren **Arbeitspakete/Vorgänge** der Zielprojekte.

### 4.4 Relevante Operationen (aus WSDL)

| Operation | Zweck im Sync |
| --- | --- |
| **`CreateOrUpdateTimeRecord`** | **Kern-Op**: Buchung anlegen/aktualisieren (Upsert). Faults: `AccessDenied`, `UnableToCreateOrUpdate`, **`ViolatedConstraint`**. |
| `GetTimeRecord` / `GetTimeRecords` | Bestehende Buchungen lesen (Idempotenz, Re-Sync). |
| `DeleteTimeRecord` | Buchung löschen (für künftiges „Eintrag in TimeHub gelöscht → BCS nachziehen"). |
| `GetTimeTrackingSettings` | Konfigurierte Zeiterfassungs-Granularität — **klärt Anforderung „eine Dauer pro Tag/AP"** (§4.6). |
| `GetTimeRecordAttributes` | Attribut-/Wertelisten einer Buchung — **Kandidat für die Arbeitspaket-Liste** (§4.5, Dropdown). |
| `GetClosureDate` / `SetClosureDate` | Buchungsabschluss; Push gegen abgeschlossene Tage scheitert sonst hart. |
| Attendances/Breaks-Ops | Anwesenheit/Pausen — **nicht** für Aufwands-Push nötig. |

### 4.5 Mapping der Anforderungen auf das Design

| # | Anforderung | Umsetzung |
| --- | --- | --- |
| 1 | Arbeitspaket pro Projekt **oder** Eintrag | Registry-Feld umstellen (s.u.): `bcs.default_work_package` (Projekt, optional) → `bcs.work_package` (Eintrag, Pflicht, `inherit_from_project`). Exakt das Jira-Muster `default_issue → issue_key`. |
| 2 | Dropdown aktiver Arbeitspakete | `options_source="bcs_work_packages"`, `searchable=True`; befüllt in `_sync_dynamic_options()` analog `sf_assignments` — nur **buchbare** APs des Users. *Offene Frage: welche WS-Op liefert die Liste? (`GetTimeRecordAttributes` o. `GetTimeTrackingSettings`; ggf. zusätzlicher Struktur-WS.)* |
| 3 | User-Match per E-Mail (BCS via MS-OAuth) | TimeHub-User.email → BCS-User-OID auflösen, OID im Request mitgeben (Service-User bucht stellvertretend). *Offene Frage: Auflösung E-Mail→OID — nimmt eine Op einen User-Parameter? sonst Zusatz-WS nötig.* |
| 4 | Login per WS-Security UsernameToken 1.1 | `zeep` + `zeep.wsse.username.UsernameToken` (PasswordText), HTTPS. Credentials wie SF im `AppSetting`-Store, Fernet-verschlüsselt. |
| 5 | Schreiben → bei Erfolg BCS-Status grün | `CreateOrUpdateTimeRecord` → bei Erfolg `set_target_status(db, entry, "bcs", "synced", external_ref=<timeRecordOid>)`; bei Fault `failed` mit Fehlertext. Identisch zum SF-Execute-Flow (`sync.py`). |
| 6 | „Nur eine Dauer pro Tag pro AP?" | **Sehr wahrscheinlich ja** — Validierung + defensives Design in §4.6. |

### 4.6 Anforderung 6 validieren — „eine Dauer pro Tag pro Arbeitspaket"

**Indizien aus der WSDL sprechen dafür:**
- Die Op heißt `CreateOr**Update**` (Upsert-Semantik, nicht reines Create).
- Eigener Fault **`ViolatedConstraintException`** — passt zu einer Eindeutig-
  keits-Constraint auf (User, Datum, Vorgang).
- Entspricht dem BCS-Standard „Tagesaufwand pro Vorgang".

**Aber nicht bewiesen** ohne die Input-XSD von `CreateOrUpdateTimeRecord` und
`GetTimeTrackingSettings`. Zwei To-dos:
- `GetTimeTrackingSettings` aufrufen → zeigt die konfigurierte Granularität.
- Input-XSD prüfen: braucht ein Update die bestehende `timeRecordOid`, oder
  ist es ein Upsert über den Natürlichen Schlüssel (User+Datum+Vorgang)?

**Defensives Design (unabhängig vom Ergebnis):** TimeHub erlaubt mehrere Ein-
träge/Tag/Projekt; BCS evtl. nur eine Buchung/Tag/AP. Der BCS-Push
**aggregiert deshalb vor dem Senden pro (User, Datum, Arbeitspaket)**:
Dauern summieren, Beschreibungen zusammenführen. Damit ist es egal, ob BCS eine
oder mehrere Buchungen erlaubt; die `timeRecordOid` wird auf **allen** Einträgen
der Gruppe als `external_ref` gespeichert (gemeinsame Idempotenz).

> **Architektur-Hinweis:** Das ist der wesentliche Unterschied zum SF-Push
> (dort 1 Eintrag → 1 `Zeiterfassung__c`). Der BCS-Push ist ein
> **Gruppierungs-Push** (n Einträge → 1 Buchung). `EntrySync` bleibt pro
> Eintrag, teilt sich aber `external_ref` und Status innerhalb der Gruppe.

### 4.7 Implementierungsplan (Code-Anfasspunkte)

1. **Dependency:** `zeep` in `requirements.txt` (SOAP + WS-Security). Hand-
   gerolltes XML wie bei SF wäre bei document/literal + WSSE zu fehleranfällig.
2. **`app/services/bcs.py`** (neu, Pendant zu `salesforce.py`): zeep-Client aus
   Settings, `CreateOrUpdateTimeRecord`, `get_time_records`, Arbeitspaket-Liste,
   E-Mail→User-OID, Credential-Store (`bcs.username`/`bcs.password`/`bcs.wsdl_url`).
3. **`app/services/bcs_push.py`** (neu, Pendant zu `sf_push.py`): read-only
   Resolver, der Einträge pro (User, Datum, AP) **gruppiert** und je Gruppe ein
   Payload + Blockier-Gründe liefert (Abschluss-Datum, fehlendes AP, …).
4. **`sync_fields.py`:** `bcs`-Block von `subject`/`task` auf
   `default_work_package` (Projekt) + `work_package` (Eintrag) umstellen.
5. **`common.py::_sync_dynamic_options`:** `bcs_work_packages` ergänzen.
6. **`web/routes/sync.py`:** BCS-`preview`/`execute` analog SF; bei Erfolg
   `set_target_status(... "bcs", "synced", external_ref=...)`.
7. **`web/routes/admin.py`:** BCS-Credential-Maske + Verbindungstest
   (`GetTimeTrackingSettings` als Smoke-Test) analog SF-Settings.
8. **Alembic:** keine Schema-Migration nötig — alles in den vorhandenen
   JSON-Spalten (`sync_metadata` / `sync_metadata_override`) und `EntrySync`.

### 4.8 Offen / benötigt (vor Implementierung)

- [x] Lizenz aktiviert (06/2026); WSDL vorhanden & Endpunkt bestätigt.
- [ ] **Berechtigungen** für den Service-User (inkl. stellvertretendes Buchen) —
      *aktueller Blocker*.
- [ ] **XSD-Teile** (`?xsd=1…12`) — für die exakte Input-Struktur von
      `CreateOrUpdateTimeRecord` (Pflichtfelder, Dauer-Kodierung, User-OID,
      Arbeitspaket-OID, Datum, Beschreibung) und `GetTimeRecordAttributes`.
- [ ] **Arbeitspaket-Liste:** welche Op liefert die buchbaren APs des Users?
- [ ] **E-Mail→BCS-User-OID:** über welche Op/welchen WS?
- [ ] `GetTimeTrackingSettings`-Antwort (Granularität) → bestätigt Anforderung 6.

---

## 5. Nächste Schritte

1. **Lizenz-/Preis-Anfrage an Projektron** stellen — für **Import-Export-Modul
   UND Webservice „Externe Zeiterfassung"** (klärt §2 komplett). Parallel im
   Admin-Lizenzbereich nachsehen, was bereits gelistet ist.
2. **TimeHub-seitige CSV-Ausgabe** für BCS vorbereiten — das geht unabhängig von
   der Lizenzfrage, weil es nur ein Export-Template in TimeHub ist. Der
   *Import* in BCS hängt dann am Import-Export-Modul.
3. Auf Basis der Projektron-Antwort entscheiden: CSV-Import nutzbar? Oder direkt
   Stufe 2 (Webservices) ansteuern?

> **Update 06/2026:** Webservice-Lizenz ist da, WSDL liegt vor, Endpunkt/Auth
> sind verifiziert → wir gehen **direkt Stufe 2** (Webservice-Push, §4). CSV
> bleibt nur Fallback. Verbleibende Blocker vor dem Bau: **Berechtigungen** des
> Service-Users (§4.3) und die **XSD-Teile** für die exakte Request-Struktur
> (§4.8).
