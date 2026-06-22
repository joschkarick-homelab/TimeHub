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

Erst sinnvoll, wenn die Lizenz geklärt/aktiviert ist. Grobplan:

- **Protokoll:** SOAP (z. B. via `zeep` in Python), WSDL vom BCS-Host ziehen.
- **Auth:** BCS-Service-User mit Webservice-Recht; Credentials verschlüsselt in
  der DB ablegen (wie bei Salesforce, Fernet aus `SECRET_KEY`).
- **Client:** analog zum Salesforce-Push als Sync-Target-Client, der
  `EntrySync(target="bcs")` von `pending` nach `synced`/`failed` bringt
  (`preview → execute`-Muster wiederverwenden).
- **Mapping:** `subject`/`task` auf die BCS-Webservice-Felder abbilden;
  `external_ref` = von BCS zurückgegebene Aufwands-ID für Idempotenz/Updates.

### 4.1 Offen für Stufe 2

- [ ] Lizenz aktiviert? (siehe §2.1) + Angebot/Preis vorliegend.
- [ ] WSDL der relevanten Aufwands-/Zeiterfassungs-Webservices beschaffen.
- [ ] Welche konkreten Webservice-Operationen für Anlegen/Ändern/Löschen von
      Aufwänden? (Standard- vs. frei konfigurierbare Webservices.)
- [ ] Service-User + Rechte in BCS einrichten.

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
