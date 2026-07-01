---
name: bcs-webservices
description: >
  Projektron BCS SOAP web services (TimerecordingWebService and siblings) —
  endpoints, WS-Security/Impersonation auth, the CreateOrUpdateTimeRecord and
  GetTimesheet field shapes, idempotency, licensing/permission setup, and
  ready-to-paste SOAP request examples. Use when integrating with, debugging,
  or estimating any Projektron BCS web service (time/effort booking, work
  package lookup, attendance), especially the bcs-hosting.de cloud.
---

# Projektron BCS Web Services

Institutional knowledge for talking to Projektron BCS over SOAP. Verified against
BCS 25.x on `*.bcs-hosting.de`. Companion artifacts: the WSDL + all XSD parts live
in `docs/bcs-schema/`, the full design rationale in `docs/bcs-integration.md`.

## Licensing & access (clarify first)

- Web services are a **chargeable, separately activated** part of BCS. "Activated"
  ≠ "assigned to all users".
- **Each user a booking is made for needs the web service licence.** The system
  user `Synchronisation` (`5_JUser`) is the **only** licence-exempt user.
- The CSV path (importing efforts) needs the separate **Import-Export module** —
  also chargeable. Neither price is public; ask Projektron.
- To check activation: the admin licence overview, or just try a call (below).
  Guessing WSDL URLs proves nothing (see endpoint gotcha).

## Endpoint & WSDL gotchas

- Web services sit under **`/webservices/…`**, a **sibling of `/bcs`**, NOT under
  it. `https://<host>/bcs/webservices/…` returns "page does not exist or no
  permission" even when licensed.
  - WSDL: `https://<host>/webservices/TimerecordingWebService?wsdl` (1.1) /
    `?wsdl2` (2.0); XSD parts `?xsd=1 … ?xsd=N`.
  - SOAP endpoint: same URL without the query.
- **The WSDL's SOAP 1.2 port points at `http://localhost:8080/…`** (BCS default,
  not host-rewritten). Bind **SOAP 1.1** explicitly, or override the endpoint
  address in the client. With `zeep`:
  `client.create_service('{http://www.projektron.de/ws/timerecording}TimerecordingWebServiceSoap11Binding', endpoint_url)`.
- WSDL/XSD GETs are themselves permission-protected. Server-to-server, send the
  same credentials as **HTTP Basic** on the WSDL/XSD fetch (the SOAP body uses
  WS-Security separately).

## Authentication

- **WS-Security UsernameToken Profile 1.1**, password type **PasswordText**
  (cleartext) → **HTTPS mandatory**. No OAuth at the WS layer.
- **No spaces/newlines** around `<Username>`/`<Password>` values.
- BCS-via-Microsoft-OAuth users typically have **no usable local password**, so
  per-user self-auth is impractical — use the central user + impersonation.

> **Chosen model (07/2026, confirmed with Projektron): self-auth per user.**
> Each user stores their own BCS credentials and books with them directly — no
> `ImpersonateAs`, no `impersonationOids`. Reason: the OID allowlist doesn't
> scale (every user OID must be listed in the service config by hand; no
> wildcard / role-based option). Trade-off accepted: every booking user needs a
> WS-usable local BCS password (alongside SSO) + the WS licence. The
> impersonation section below is kept for reference / other deployments.

### Impersonation (book on behalf of a consultant)

- Authenticate as the system user `Synchronisation`, add an
  **`<ImpersonateAs>` header** (raw, no namespace) with the consultant's
  **BCS username** (often = e-mail). The operation then runs in *their* scope and
  records them as `insUserOid`.
- Admin must list the consultants' OIDs in **`impersonationOids`** on the service:
  `<Service name="TimerecordingWebService" activated="true" impersonationOids="…"/>`.
- The impersonated user needs the WS licence; `Synchronisation` does not.

## Key operations (TimerecordingWebService)

| Operation | Use |
| --- | --- |
| `CreateOrUpdateTimeRecord` | **Upsert** an effort booking. Faults: `AccessDenied`, `UnableToCreateOrUpdateTimeRecord`, `ViolatedConstraint`. |
| `GetTimesheet` | Bookable work packages (and other timesheet rows) for a user/date. |
| `GetTimeRecord` / `GetTimeRecords` | Read existing bookings. |
| `DeleteTimeRecord` | Delete a booking. |
| `GetTimeTrackingSettings` | Configured granularity; harmless smoke-test call. |
| `GetClosureDate` / `SetClosureDate` | Booking closures (a closed day rejects writes). |
| `GetTimeRecordAttributes` | Allowed attribute values of a record. |
| Attendance/Breaks ops | Attendance & breaks (not effort booking). |

## Field mapping (from XSD)

**`CreateOrUpdateTimeRecord` request:**
- `id` (externalID, string + `systemName` attr): **idempotency key** — re-sending
  the same value updates the same record (true Upsert). No duplicates.
- `target` → choice of `task` / `ticket` / `event` / `scrum` / `workflow`. For a
  work package use `target.task.bcsOid` (the work package OID).
- `date` (xs:date).
- `expense` (xs:int): the effort. **Convention: minutes** (BCS uses
  `numberOfMinutesInADay` 0–1440 elsewhere) — verify once with a live booking.
- `comment` (string, nillable): description.
- `employee` (personIdentifier: `login` | `bcsOid`): whose record. With
  `ImpersonateAs` it may be redundant — verify whether both are needed.
- Optional: `startTime`, `billability`, `taskType`, `remainingExpense`,
  `otherValues`/`otherAttributes`.

**`CreateOrUpdateTimeRecordResponse`:** `oid` = the BCS record OID (store as your
external ref); plus `plannedTimeEffort` / `actualTimeEffort` / `remainingTimeEffort`.

**`GetTimesheet`:** request `filter.employee.login`, `filter.date`,
`filter.typesOfTimesheetEntries.tasks`. Response `timesheetEntries.task[]`, each a
`timesheetEntry` with **attributes `bcsOid` + `name`** (→ value + label) and
`timesheetEntryProperties` (project, pspPath, planned/booked/remaining duration).

## Booking model

BCS aggregates **one booking per (user, date, work package)**. When a source
system allows several entries per day on the same package, **sum the durations and
merge descriptions before sending**, and key the `id` on `user+date+workpackage`.
Caveat: because the Upsert keys on `id`, sending only *part* of a day's entries and
later the rest **overwrites** rather than adds — book a day's package together, or
always send the full day total.

## Ready-to-paste SOAP examples (Postman)

SOAP 1.1: `Content-Type: text/xml; charset=UTF-8`, header
`SOAPAction: http://www.projektron.de/ws/timerecording/<Operation>`.
Send to `https://<host>/webservices/TimerecordingWebService`.

### List bookable work packages

```xml
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:tns="http://www.projektron.de/ws/timerecording">
  <soapenv:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>Synchronisation</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">SECRET</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
    <ImpersonateAs>berater@firma.de</ImpersonateAs>
  </soapenv:Header>
  <soapenv:Body>
    <tns:GetTimesheet>
      <tns:filter>
        <tns:employee><tns:login>berater@firma.de</tns:login></tns:employee>
        <tns:date>2026-05-27</tns:date>
        <tns:typesOfTimesheetEntries><tns:tasks/></tns:typesOfTimesheetEntries>
      </tns:filter>
    </tns:GetTimesheet>
  </soapenv:Body>
</soapenv:Envelope>
```

### Create/update a booking

```xml
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:tns="http://www.projektron.de/ws/timerecording">
  <soapenv:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>Synchronisation</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">SECRET</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
    <ImpersonateAs>berater@firma.de</ImpersonateAs>
  </soapenv:Header>
  <soapenv:Body>
    <tns:CreateOrUpdateTimeRecord>
      <tns:id systemName="TimeHub">7:2026-05-27:WORKPACKAGE_OID</tns:id>
      <tns:target>
        <tns:task><tns:bcsOid>WORKPACKAGE_OID</tns:bcsOid></tns:task>
      </tns:target>
      <tns:date>2026-05-27</tns:date>
      <tns:expense>90</tns:expense>
      <tns:comment>Analyse; Doku</tns:comment>
    </tns:CreateOrUpdateTimeRecord>
  </soapenv:Body>
</soapenv:Envelope>
```

## Three things to verify on the first live call

1. **`expense` unit** — book `90`; if the record shows 1.5 h it's minutes.
2. **`employee` vs `ImpersonateAs`** — send only `ImpersonateAs`; check the record
   is attributed to the consultant. If not, add `<tns:employee><tns:login>…`.
3. **`id` Upsert** — send the same request twice; confirm one record (updated),
   not two.

Prerequisite for any of these: a BCS user with the WS licence + usage permission
**and a UsernameToken-usable password** (i.e. the `Synchronisation` user, or a
test account with a local password — an OAuth-only account won't work).
