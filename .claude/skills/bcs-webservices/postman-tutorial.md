# BCS TimerecordingWebService per Postman testen

Kleines Tutorial, um den BCS-Webservice ohne Code zu testen. Es gibt eine
fertige Collection (`bcs-timerecording.postman_collection.json`) zum Importieren
— dieses Dokument erklärt Setup, Ablauf und typische Fehler.

## 0. Voraussetzungen

- Ein **BCS-User mit Webservice-Berechtigung + Lizenz** und einem
  **lokalen Passwort** (UsernameToken-tauglich — ein reiner MS-OAuth-Account hat
  keins). Für Self-Auth-Tests reicht dein eigener Account.
- Postman (Desktop oder Web).

## 1. Collection importieren

Postman → **Import** → die Datei `bcs-timerecording.postman_collection.json`
wählen. Danach unter der Collection → **Variables** eintragen:

| Variable | Wert |
| --- | --- |
| `host` | `mindsquare.bcs-hosting.de` |
| `bcsUser` | dein BCS-Login |
| `bcsPassword` | dein BCS-Passwort |
| `testDate` | ein Tag mit buchbaren Arbeitspaketen, z. B. `2026-07-01` |
| `workPackageOid` | erst nach Schritt 3 füllen |

Tipp: `bcsPassword` besser in einer Postman-**Environment**-Variable mit Typ
*secret* statt in der Collection speichern.

## 2. Request-Grundlagen (falls du selbst baust)

- **Methode:** `POST`
- **URL:** `https://{{host}}/webservices/TimerecordingWebService`
  (Pfad ist `/webservices/…`, **nicht** `/bcs/webservices/…`).
- **Header:**
  - `Content-Type: text/xml; charset=UTF-8`
  - `SOAPAction: http://www.projektron.de/ws/timerecording/<Operation>`
- **Body:** `raw`, XML. WS-Security-UsernameToken steht im SOAP-Header (siehe
  Collection). **Keine** Leerzeichen/Zeilenumbrüche um Username/Password.

## 3. Ablauf

1. **Request „1) GetTimesheet"** senden (read-only, ungefährlich).
   Erfolg → im Response `timesheetEntries.task`-Einträge mit `bcsOid` + `name`.
   Eine `bcsOid` in die Variable **`workPackageOid`** kopieren.
2. **Request „2) CreateOrUpdateTimeRecord"** senden. Response enthält `oid`
   (die Datensatz-OID der Buchung).
3. In der BCS-UI prüfen, dass die Buchung am `testDate` auf dem Arbeitspaket
   steht.

## 4. Die drei offenen Fragen damit klären

1. **`expense`-Einheit:** In Request 2 steht `expense=90`. Zeigt die Buchung in
   BCS **1,5 h** → Minuten (erwartet). Zeigt sie 90 h → dann sind es Stunden.
2. **`id`-Upsert:** Request 2 **zweimal** senden. Es darf **nur eine** Buchung
   entstehen (Update), keine zwei — dann greift der Idempotenz-Key `id`.
3. **`employee`/Impersonation:** bei Self-Auth nicht relevant (du buchst für
   dich). Nur für das Impersonation-Modell nötig.

## 5. Typische Fehler

| Symptom | Ursache / Fix |
| --- | --- |
| „The page does not exist or you do not have the necessary permissions" | Falscher Pfad (`/bcs/webservices/…`) **oder** fehlende WS-Berechtigung. Pfad prüfen, sonst Admin. |
| SOAP-Fault `AccessDenied` (`{code,message}`) | Kein WS-Nutzungsrecht/Lizenz auf dem User, oder keine Buch-Rechte auf dem Arbeitspaket. |
| Fault zu Auth / leere Antwort | Tippfehler in User/Passwort, oder Leerzeichen/Zeilenumbruch um die Werte. |
| TLS-Fehler | HTTPS ist Pflicht (Passwort im Klartext). Nicht auf HTTP ausweichen. |
| `ViolatedConstraint` bei Request 2 | Constraint verletzt (z. B. Tag abgeschlossen / Arbeitspaket nicht buchbar). |

## 6. Response lesen

Postman zeigt die SOAP-Antwort als XML. Interessant:
- GetTimesheet → `timesheetEntries.task[@bcsOid, @name]`
- CreateOrUpdateTimeRecord → `oid` (= external ref), `actualTimeEffort` etc.
- Fehler → `soapenv:Fault` mit `detail` → `{code, message}`.
