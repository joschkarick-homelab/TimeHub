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
from typing import Iterable
from xml.sax.saxutils import escape

from sqlalchemy.orm import Session

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
        "password": app_settings_svc.get_setting(db, SF_PASSWORD_KEY, ""),
        "security_token": app_settings_svc.get_setting(db, SF_TOKEN_KEY, ""),
        "login_url": app_settings_svc.get_setting(db, SF_LOGIN_URL_KEY, _DEFAULT_LOGIN_URL),
        "api_version": app_settings_svc.get_setting(db, SF_API_VERSION_KEY, _DEFAULT_API_VERSION),
    }


def credentials_configured(db: Session) -> bool:
    c = get_credentials(db)
    return bool(c["username"] and c["password"])


def save_credentials(db: Session, *, username: str | None = None,
                     password: str | None = None, security_token: str | None = None,
                     login_url: str | None = None, api_version: str | None = None) -> None:
    """Persist credentials. Password/token only overwrite when a non-empty value
    is provided — empty fields keep the existing secret (so the admin form can
    safely render empty password inputs)."""
    if username is not None:
        app_settings_svc.set_setting(db, SF_USERNAME_KEY, username.strip())
    if password is not None and password.strip():
        app_settings_svc.set_setting(db, SF_PASSWORD_KEY, password)
    if security_token is not None and security_token.strip():
        app_settings_svc.set_setting(db, SF_TOKEN_KEY, security_token.strip())
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
    aid = _ensure_id(assignment_id)
    soql = (
        "SELECT Id, Name, pse__Project__c, pse__Project__r.Name, "
        "pse__Resource__c, pse__Resource__r.Name, pse__Closed_for_Time_Entry__c "
        f"FROM pse__Assignment__c WHERE Id = '{aid}' LIMIT 1"
    )
    res = client.query(soql)
    records = res.get("records") or []
    if not records:
        return None
    r = records[0]
    return {
        "id": r["Id"],
        "name": r.get("Name"),
        "project_id": r.get("pse__Project__c"),
        "project_name": (r.get("pse__Project__r") or {}).get("Name"),
        "resource_id": r.get("pse__Resource__c"),
        "resource_name": (r.get("pse__Resource__r") or {}).get("Name"),
        "closed": bool(r.get("pse__Closed_for_Time_Entry__c")),
    }


def get_monthly_time_period(client: SalesforceClient, date_iso: str) -> dict | None:
    # SOQL date literals: YYYY-MM-DD without quotes. We trust ISO format from
    # date.isoformat() (validated upstream).
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso):
        raise SalesforceError(f"Ungültiges Datum für Time-Period-Abfrage: {date_iso!r}")
    soql = (
        "SELECT Id, Name, pse__Start_Date__c, pse__End_Date__c "
        "FROM pse__Time_Period__c "
        f"WHERE pse__Type__c = 'Month' AND pse__Start_Date__c <= {date_iso} "
        f"AND pse__End_Date__c >= {date_iso} LIMIT 1"
    )
    res = client.query(soql)
    records = res.get("records") or []
    if not records:
        return None
    r = records[0]
    return {
        "id": r["Id"],
        "name": r.get("Name"),
        "start_date": r["pse__Start_Date__c"],
        "end_date": r["pse__End_Date__c"],
    }


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
