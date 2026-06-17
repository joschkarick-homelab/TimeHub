"""Minimal Salesforce client for the sync-preview use case.

Authentication is SOAP-Login (Username + Password + Security Token); the
returned session id is then used as Bearer for the REST API. Stdlib only —
no extra dependency. The client and the credentials store live next to each
other on purpose: credentials are admin-managed via AppSetting, so this
module knows how to read them on demand.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from xml.sax.saxutils import escape

from sqlalchemy.orm import Session

from app.security import decrypt_secret, encrypt_secret
from app.services import app_settings as app_settings_svc

log = logging.getLogger(__name__)

SF_USERNAME_KEY = "sf.username"
SF_PASSWORD_KEY = "sf.password"
SF_TOKEN_KEY = "sf.security_token"
SF_LOGIN_URL_KEY = "sf.login_url"
SF_API_VERSION_KEY = "sf.api_version"

_DEFAULT_LOGIN_URL = "https://login.salesforce.com"
_DEFAULT_API_VERSION = "60.0"
# Salesforce IDs are 15 or 18 case-sensitive alnum chars.
_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15,18}$")
# sObject API names: letters/digits/underscore, ending with __c for custom.
_SF_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


class SalesforceError(RuntimeError):
    pass


# ---------- HTTP & XML helpers ----------


def _http(method: str, url: str, *, data: bytes | None = None,
          headers: dict | None = None, timeout: int = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, TimeoutError) as e:
        # DNS failures, refused connections and read timeouts must surface as a
        # SalesforceError so the sync loop can stop cleanly instead of leaking a
        # raw exception (which would abort mid-push and lose committed progress).
        raise SalesforceError(f"Salesforce nicht erreichbar: {e}") from e


def _login_envelope(username: str, password: str) -> bytes:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">'
          '<env:Body>'
            '<n1:login xmlns:n1="urn:partner.soap.sforce.com">'
              f'<n1:username>{escape(username)}</n1:username>'
              f'<n1:password>{escape(password)}</n1:password>'
            '</n1:login>'
          '</env:Body>'
        '</env:Envelope>'
    )
    return body.encode("utf-8")


def _parse_login_response(xml_bytes: bytes) -> tuple[str, str]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise SalesforceError(f"Salesforce hat kein XML geliefert: {e}") from e
    fault = root.find(".//{*}Fault")
    if fault is not None:
        msg = fault.findtext(".//{*}faultstring") or "Salesforce-Login fehlgeschlagen"
        raise SalesforceError(msg)
    session_id = root.findtext(".//{*}sessionId")
    server_url = root.findtext(".//{*}serverUrl")
    if not session_id or not server_url:
        raise SalesforceError("Login-Antwort ohne sessionId/serverUrl")
    parsed = urllib.parse.urlparse(server_url)
    instance_url = f"{parsed.scheme}://{parsed.netloc}"
    return session_id, instance_url


def _ensure_id(value: str) -> str:
    """Guard against SOQL injection of attacker-controlled IDs. We only embed
    values that pass the strict SF-ID shape."""
    if not _SF_ID_RE.fullmatch(value or ""):
        raise SalesforceError(f"Ungültige Salesforce-Id: {value!r}")
    return value


def _ensure_sobject_name(value: str) -> str:
    if not _SF_NAME_RE.fullmatch(value or ""):
        raise SalesforceError(f"Ungültiger sObject-Name: {value!r}")
    return value


# ---------- Client ----------


class SalesforceClient:
    def __init__(self, username: str, password: str, security_token: str = "",
                 login_url: str = _DEFAULT_LOGIN_URL,
                 api_version: str = _DEFAULT_API_VERSION):
        if not username or not password:
            raise SalesforceError("Salesforce-Zugangsdaten unvollständig")
        self.username = username
        self._password = password + (security_token or "")
        self.login_url = (login_url or _DEFAULT_LOGIN_URL).rstrip("/")
        self.api_version = api_version or _DEFAULT_API_VERSION
        self.session_id: str | None = None
        self.instance_url: str | None = None

    def login(self) -> None:
        url = f"{self.login_url}/services/Soap/u/{self.api_version}"
        headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "login"}
        status, body = _http("POST", url, data=_login_envelope(self.username, self._password),
                             headers=headers, timeout=20)
        # Status 500 for SOAP faults is normal; the parser pulls out the faultstring.
        if status >= 400 and b"<" not in body[:40]:
            raise SalesforceError(f"SOAP-Login HTTP {status}")
        self.session_id, self.instance_url = _parse_login_response(body)

    def _ensure_login(self) -> None:
        if not self.session_id:
            self.login()

    def query(self, soql: str) -> dict:
        self._ensure_login()
        url = (f"{self.instance_url}/services/data/v{self.api_version}/query"
               f"?{urllib.parse.urlencode({'q': soql})}")
        status, body = _http("GET", url, headers={
            "Authorization": f"Bearer {self.session_id}",
            "Accept": "application/json",
        })
        if status >= 400:
            raise SalesforceError(f"SOQL-Abfrage fehlgeschlagen (HTTP {status}): "
                                  f"{body.decode('utf-8', errors='replace')[:300]}")
        return json.loads(body)


# ---------- Credential store + factory ----------


def get_credentials(db: Session) -> dict:
    return {
        "username": app_settings_svc.get_setting(db, SF_USERNAME_KEY, ""),
        "password": decrypt_secret(app_settings_svc.get_setting(db, SF_PASSWORD_KEY, "")),
        "security_token": decrypt_secret(app_settings_svc.get_setting(db, SF_TOKEN_KEY, "")),
        "login_url": app_settings_svc.get_setting(db, SF_LOGIN_URL_KEY, _DEFAULT_LOGIN_URL),
        "api_version": app_settings_svc.get_setting(db, SF_API_VERSION_KEY, _DEFAULT_API_VERSION),
    }


def credentials_configured(db: Session) -> bool:
    c = get_credentials(db)
    return bool(c["username"] and c["password"])


def save_credentials(db: Session, *, username: str | None = None,
                     password: str | None = None, security_token: str | None = None,
                     login_url: str | None = None, api_version: str | None = None,
                     clear_security_token: bool = False) -> None:
    """Persist credentials. Password/token only overwrite when a non-empty value
    is provided — empty fields keep the existing secret (so the admin form can
    safely render empty password inputs). Pass clear_security_token=True to
    explicitly drop a previously stored token (e.g. when switching to an API
    user whose org doesn't require one)."""
    if username is not None:
        app_settings_svc.set_setting(db, SF_USERNAME_KEY, username.strip())
    if password is not None and password.strip():
        app_settings_svc.set_setting(db, SF_PASSWORD_KEY, encrypt_secret(password))
    if clear_security_token:
        app_settings_svc.set_setting(db, SF_TOKEN_KEY, "")
    elif security_token is not None and security_token.strip():
        app_settings_svc.set_setting(db, SF_TOKEN_KEY, encrypt_secret(security_token.strip()))
    if login_url is not None:
        app_settings_svc.set_setting(db, SF_LOGIN_URL_KEY,
                                     login_url.strip() or _DEFAULT_LOGIN_URL)
    if api_version is not None:
        app_settings_svc.set_setting(db, SF_API_VERSION_KEY,
                                     api_version.strip() or _DEFAULT_API_VERSION)


def client_from_settings(db: Session) -> SalesforceClient | None:
    c = get_credentials(db)
    if not (c["username"] and c["password"]):
        return None
    return SalesforceClient(c["username"], c["password"], c["security_token"],
                            c["login_url"], c["api_version"])


# ---------- High-level queries used by the sync flow ----------


def get_assignment(client: SalesforceClient, assignment_id: str) -> dict | None:
    """Look up a Projektbesetzung__c (mindsquare-Org Custom Object).

    Liefert Projekt- und Mitarbeiterinfos zurück. Resource ist entweder
    Mitarbeiter__c (interner User) oder Externe_Projektbesetzung__c (Contact)."""
    aid = _ensure_id(assignment_id)
    soql = (
        "SELECT Id, Name, Projekt__c, Projektbezeichnung__c, "
        "Mitarbeiter__c, MitarbeiterName__c, Mitarbeiternachname__c, "
        "Externe_Projektbesetzung__c, Externe_Projektbesetzung_Formel__c, "
        "Geschlossen__c, Aktiv__c "
        f"FROM Projektbesetzung__c WHERE Id = '{aid}' LIMIT 1"
    )
    res = client.query(soql)
    records = res.get("records") or []
    if not records:
        return None
    r = records[0]
    name_parts = [r.get("MitarbeiterName__c"), r.get("Mitarbeiternachname__c")]
    internal_name = " ".join(p for p in name_parts if p).strip()
    resource_name = internal_name or (r.get("Externe_Projektbesetzung_Formel__c") or "")
    return {
        "id": r["Id"],
        "name": r.get("Name"),  # PB-Nummer
        "project_id": r.get("Projekt__c"),
        "project_name": r.get("Projektbezeichnung__c") or "",
        "resource_id": r.get("Mitarbeiter__c") or r.get("Externe_Projektbesetzung__c"),
        "resource_name": resource_name,
        "is_external": bool(r.get("Externe_Projektbesetzung__c") and not r.get("Mitarbeiter__c")),
        "closed": bool(r.get("Geschlossen__c")),
        "active": r.get("Aktiv__c"),
    }


def describe_sobject(client: SalesforceClient, object_name: str) -> dict:
    """Fetch the SF describe metadata for an sObject. Available to any API user
    that can read the object — no admin/2FA required."""
    name = _ensure_sobject_name(object_name)
    client._ensure_login()
    url = (f"{client.instance_url}/services/data/v{client.api_version}/"
           f"sobjects/{name}/describe")
    status, body = _http("GET", url, headers={
        "Authorization": f"Bearer {client.session_id}",
        "Accept": "application/json",
    })
    if status >= 400:
        raise SalesforceError(
            f"Describe '{name}' fehlgeschlagen (HTTP {status}): "
            f"{body.decode('utf-8', errors='replace')[:300]}"
        )
    return json.loads(body)


def _assignment_label(project_name: str, customer: str, number: str,
                      pb_number: str) -> str:
    """Dropdown-Label für eine Projektbesetzung. Bevorzugt den Projektnamen,
    ergänzt um Kunde und Projektnummer in Klammern. Fällt nur dann auf die
    PB-Nummer zurück, wenn sonst gar nichts da ist (sonst bliebe der Eintrag
    leer)."""
    head = project_name or number or customer or pb_number or "?"
    suffix = [p for p in (customer, number) if p and p != head]
    return f"{head} ({' · '.join(suffix)})" if suffix else head


def list_assignments_for_user(client: SalesforceClient, email: str) -> list[dict]:
    """Aktuell auswählbare Projektbesetzungen des Users mit der gegebenen
    E-Mail — interner Mitarbeiter ODER externer Contact. Liefert
    Dropdown-fertige `{value, label, search}`-Einträge.

    Eingeschränkt auf Projektbesetzungen, die der User jetzt wirklich bebuchen
    kann:
      * aktiv auswählbar  → `Aktiv__c = 'Ja'`,
      * Status offen      → `Geschlossen__c = false`,
      * zurzeit laufend   → `Projektstart__c <= heute <= Projektende__c`
        (offene Grenzen, also leere Start-/Endfelder, gelten als laufend).

    `search` bündelt Kunde (AccountName), Projektbezeichnung und Projektnummer
    (P0000…) als Klartext für die Fuzzy-Suche im Frontend — interne SF-Ids und
    die PB-Nummer sind bewusst NICHT enthalten.

    Wenn die E-Mail-Adresse unplausibel ist (z. B. ein Hochkomma enthält),
    wird ohne Query eine leere Liste zurückgegeben — die Eingabe steckt direkt
    im SOQL, also wird sie defensiv geprüft."""
    e = (email or "").strip()
    if not e or len(e) > 254 or any(c in e for c in ("'", "\\", "\n", "\r")):
        return []
    soql = (
        "SELECT Id, Name, Projektbezeichnung__c, Projektnummer__c, AccountName__c "
        "FROM Projektbesetzung__c "
        f"WHERE (Mitarbeiter__r.Email = '{e}' OR Externe_Projektbesetzung__r.Email = '{e}') "
        "AND Aktiv__c = 'Ja' "
        "AND Geschlossen__c = false "
        "AND (Projektstart__c = null OR Projektstart__c <= TODAY) "
        "AND (Projektende__c = null OR Projektende__c >= TODAY) "
        "ORDER BY Projektbezeichnung__c NULLS LAST, Name"
    )
    res = client.query(soql)
    out: list[dict] = []
    for r in res.get("records") or []:
        name = (r.get("Name") or "").strip()
        bez = (r.get("Projektbezeichnung__c") or "").strip()
        customer = (r.get("AccountName__c") or "").strip()
        number = (r.get("Projektnummer__c") or "").strip()
        search = " ".join(p for p in (customer, bez, number) if p).lower()
        out.append({
            "value": r["Id"],
            "label": _assignment_label(bez, customer, number, name),
            "search": search,
        })
    return out


def get_monthly_period(client: SalesforceClient, assignment_id: str,
                       date_iso: str) -> dict | None:
    """Finde den Kontierungsmonat__c, der für DIESE Projektbesetzung das
    Tagesdatum enthält. In der mindsquare-Org ist der Kontierungsmonat pro
    Projektbesetzung referenziert — globale Monatszeiträume gibt es nicht."""
    aid = _ensure_id(assignment_id)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso):
        raise SalesforceError(f"Ungültiges Datum für Kontierungsmonat-Abfrage: {date_iso!r}")
    soql = (
        "SELECT Id, Name, Monatsbeginn__c, Monatsende__c, Status__c, Abgeschlossen__c "
        "FROM Kontierungsmonat__c "
        f"WHERE Projektbesetzung__c = '{aid}' "
        f"AND Monatsbeginn__c <= {date_iso} "
        f"AND Monatsende__c >= {date_iso} "
        "LIMIT 1"
    )
    res = client.query(soql)
    records = res.get("records") or []
    if not records:
        return None
    r = records[0]
    return {
        "id": r["Id"],
        "name": r.get("Name"),
        "start_date": r["Monatsbeginn__c"],
        "end_date": r["Monatsende__c"],
        "status": r.get("Status__c"),
        "closed": bool(r.get("Abgeschlossen__c")),
    }


def _coerce_bool(value) -> bool:
    """Liberal boolean conversion for the Remote flag from import transforms.
    Accepts true/1/yes/ja/x/wahr (case-insensitive); everything else is False."""
    if value is None or value is False or value == "":
        return False
    if value is True:
        return True
    s = str(value).strip().lower()
    return s in {"true", "1", "yes", "y", "ja", "j", "x", "wahr"}


_MAX_DAY_MINUTES = 23 * 60 + 45  # picklist tops out at 23:45


def snapped_total_minutes(minutes: int) -> int:
    """Snap a duration to the nearest 15-minute slot, capped at 23:45 — the
    shape Salesforce's Von/Bis picklist accepts."""
    total = max(0, minutes)
    return min(round(total / 15) * 15, _MAX_DAY_MINUTES)


def duration_snap_warning(minutes: int) -> str | None:
    """A note when snapping/capping changes the actually-tracked duration, so a
    push to Salesforce isn't a silent data loss for the user."""
    actual = max(0, minutes)
    snapped = snapped_total_minutes(minutes)
    if snapped == actual:
        return None
    if actual > _MAX_DAY_MINUTES:
        return f"Dauer {actual} min auf 23:45 gedeckelt"
    return f"Dauer von {actual} auf {snapped} min (15-Min-Raster) gerundet"


def _snap_quarter(hour: int, minute: int) -> tuple[int, str]:
    """Snap (h, m) to the nearest 15-minute slot; return (hour:int, minute:str)
    where minute is the picklist value '00'/'15'/'30'/'45'."""
    snapped = snapped_total_minutes(hour * 60 + minute)
    return snapped // 60, f"{snapped % 60:02d}"


def build_zeiterfassung_payload(entry, period_id: str,
                                remote_value=None) -> dict:
    """Construct the Zeiterfassung__c JSON for one TimeHub entry.

    Strategie: Von_Stunde__c=0, Bis_Stunde__c = Dauer in Stunden — die
    maßgebliche `duration_minutes` wird IMMER über das Von/Bis-Intervall
    transportiert, unabhängig davon, ob der Eintrag echte Start-/Endzeiten
    hat. Pause ist immer 0 (TimeHub trackt keine Pausen). Beschreibung wird
    auf 255 Zeichen begrenzt.

    Hintergrund: `Arbeitszeit__c` und `Arbeitszeit_Minuten__c` sind in der
    mindsquare-Org berechnete Felder (read-only) und werden bewusst NICHT
    geschrieben — sonst kommt INVALID_FIELD_FOR_INSERT_UPDATE. Salesforce
    leitet die Arbeitszeit selbst aus dem Von/Bis-Intervall ab. Wir dürfen
    deshalb NICHT die echten Uhrzeiten (z.B. 09:00–11:00) schreiben: die
    abgeleitete Arbeitszeit wäre dann die Enduhrzeit (11) statt der Dauer
    (2 Std.). Stattdessen kodieren wir die Dauer als Intervall ab Mitternacht
    (00:00 → Dauer).

    Der Workaround ist nötig, weil die interne Zeiterfassung in Salesforce
    nur die Bis-Zeit nutzt und anzeigt (das Von wird nicht abgezogen). Indem
    wir Von auf 00:00 setzen, ist die angezeigte Bis-Zeit genau die Dauer."""
    von_h, von_m = 0, "00"
    bis_h, bis_m = _snap_quarter(0, entry.duration_minutes)

    return {
        "Kontierungsmonat__c": period_id,
        "Tag__c": entry.entry_date.isoformat(),
        "Von_Stunde__c": von_h,
        "Von_Minute__c": von_m,
        "Bis_Stunde__c": bis_h,
        "Bis_Minute__c": bis_m,
        "Pause__c": 0,
        "Taetigkeitsbeschreibung__c": (entry.description or "")[:255],
        "Remote__c": _coerce_bool(remote_value),
    }


def _format_create_error(content: bytes) -> str:
    """Decode the Salesforce error array `[{message, errorCode, fields}]` into
    a single readable string."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return content.decode("utf-8", errors="replace")[:300]
    if isinstance(data, list) and data:
        parts: list[str] = []
        for e in data:
            code = e.get("errorCode") or "ERROR"
            msg = e.get("message") or ""
            fields = e.get("fields") or []
            suffix = f" (Felder: {', '.join(fields)})" if fields else ""
            parts.append(f"[{code}] {msg}{suffix}")
        return "; ".join(parts)[:600]
    if isinstance(data, dict):
        return (data.get("message") or json.dumps(data))[:300]
    return str(data)[:300]


def create_zeiterfassung(client: SalesforceClient, payload: dict) -> str:
    """POST eine Zeiterfassung__c in Salesforce und gib die neue Datensatz-Id
    zurück. Bei einem 4xx-Antwortcode wird der SF-Fehler in einen
    SalesforceError verpackt — die Fehlermeldung enthält Code + Beschreibung."""
    client._ensure_login()
    url = (f"{client.instance_url}/services/data/v{client.api_version}/"
           "sobjects/Zeiterfassung__c")
    body = json.dumps(payload).encode("utf-8")
    status_code, content = _http("POST", url, data=body, headers={
        "Authorization": f"Bearer {client.session_id}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    if status_code >= 400:
        raise SalesforceError(_format_create_error(content))
    try:
        res = json.loads(content)
    except (json.JSONDecodeError, ValueError) as e:
        raise SalesforceError(f"Unverständliche Salesforce-Antwort: {e}") from e
    if not res.get("success") or not res.get("id"):
        raise SalesforceError(_format_create_error(content))
    return res["id"]


def assignment_id_for(entry, project) -> str | None:
    """Resolve the assignment ID for an entry: entry override beats project
    default, both under sync_metadata[`salesforce`]`assignment_id`."""
    for source in ((entry.sync_metadata_override or {}),
                   (project.sync_metadata or {})):
        aid = (source.get("salesforce") or {}).get("assignment_id")
        if aid:
            return aid
    return None


def collect_assignment_ids(items: Iterable[tuple]) -> list[str]:
    """Unique assignment IDs across (entry, project) pairs, in first-seen order."""
    seen: list[str] = []
    for entry, project in items:
        aid = assignment_id_for(entry, project)
        if aid and aid not in seen:
            seen.append(aid)
    return seen
