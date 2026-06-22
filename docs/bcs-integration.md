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

| Weg | Lizenz nötig? | Charakter |
| --- | --- | --- |
| **CSV/Excel-Import in BCS** | **Nein** | BCS liest Aufwände per Datei-Import ein. Nutzt vorhandene TimeHub-Export-Templates. Leichter manueller Schritt. |
| **SOAP-Webservices** | **Ja (Add-on)** | Echtzeit-, bidirektionaler, vollautomatischer Push ohne Handarbeit. |

**Gewählter Pfad (E-BCS-1): zweistufig.**

- **Stufe 1 (v1):** Aufwände über **CSV-Export aus TimeHub → Import in BCS**.
  Kostet nichts, braucht keine Lizenzaktivierung, geht sofort produktiv.
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

### 2.1 Ist die Lizenz schon aktiviert? — Prüfwege

1. **Funktionstest über die WSDL (schnell, nicht-invasiv).**
   Die Webservices hängen unter der BCS-Basis-URL, typischerweise
   `https://<bcs-host>/bcs/webservices/...` (mit `?wsdl`).
   - WSDL-XML kommt zurück → Service ist deployed.
   - Lizenz-/Berechtigungsfehler beim *Aufruf* → Code da, aber nicht
     freigeschaltet.
   - Achtung: Der Webservice-Zugriff ist an eine **User-Lizenz / ein Recht**
     gebunden, nicht nur an ein globales Flag. „WSDL erreichbar" ≠ „nutzbar".

2. **Lizenzübersicht im Admin (verbindlich im System).**
   Als BCS-Admin unter *Administration → Systeminformation/Lizenz* die
   freigeschalteten Module/Lizenzbestandteile prüfen. Die Lizenzdatei/das
   Zertifikat listet aktivierte Features explizit. (Menüpfad je BCS-Version
   leicht abweichend.)

3. **Vertrags-/Projektron-Kontakt (sicher).**
   Wer den Projektron-Vertrag betreut, beantwortet „aktiviert ja/nein" + Preis
   in einem Rutsch. Empfohlen, weil es ohnehin ein Vertragsthema ist.

### 2.2 Kosten

Projektron veröffentlicht **keine Preise für die Webservices-Lizenz**.
Öffentlich verfügbar (Stand 2026, Richtwerte):

- Lizenzierung **pro Nutzer** (gestaffeltes Rollenmodell, gezählt werden
  Logins/24 h).
- BCS.start: ab ~20 €/User/Monat.
- Voll-BCS: durchschnittliche Lizenzgröße ~250–350 € (einmalig/User) bzw.
  Miete ~4 % des Lizenzpreises/Monat.
- **Webservices = Add-on**, Preis nur über individuelles Projektron-Angebot.

→ Für eine belastbare Zahl ist eine Anfrage bei Projektron nötig. Diese
Klärung ist Voraussetzung, bevor Stufe 2 eingeplant wird.

---

## 3. Stufe 1 — CSV-Import (v1, ohne Lizenz)

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

1. **Lizenz-/Preis-Anfrage an Projektron** stellen (klärt §2 komplett).
2. Parallel **Stufe 1 (CSV)** umsetzen — unabhängig von der Lizenzfrage.
3. Auf Basis der Projektron-Antwort entscheiden, wann Stufe 2 startet.
