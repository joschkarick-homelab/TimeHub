"""BCS Timerecording web service client (Phase BCS).

Talks to the Projektron BCS ``TimerecordingWebService`` over SOAP. Auth is
WS-Security UsernameToken (Profile 1.1, PasswordText) with the central system
user ``Synchronisation``; each call carries an ``<ImpersonateAs>`` header with
the consultant's e-mail so the booking lands in *their* scope (decision
E-BCS-4, see docs/bcs-integration.md).

``zeep`` is imported lazily so the rest of the app (and the test suite for the
pure grouping/payload logic) keeps working even when the dependency or a live
BCS endpoint is unavailable — mirroring how the AI mapping degrades without
``anthropic``.

The SOAP-shaped helpers (`build_time_record_args`, `work_packages_from_timesheet`,
`oid_from_response`) are plain functions so they can be unit-tested without a
server.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.security import decrypt_secret, encrypt_secret
from app.services import app_settings as app_settings_svc

log = logging.getLogger(__name__)

BCS_USERNAME_KEY = "bcs.username"
BCS_PASSWORD_KEY = "bcs.password"
BCS_WSDL_URL_KEY = "bcs.wsdl_url"

# Tag the records we create so a re-push matches the same BCS object (Upsert).
EXTERNAL_SYSTEM_NAME = "TimeHub"

# SOAP 1.1 binding from the WSDL — the SOAP 1.2 port points at localhost:8080
# (BCS default, not rewritten for the host), so we bind 1.1 explicitly.
_WS_NAMESPACE = "http://www.projektron.de/ws/timerecording"
_SOAP11_BINDING = f"{{{_WS_NAMESPACE}}}TimerecordingWebServiceSoap11Binding"


class BcsError(RuntimeError):
    pass


# ---------- credential store ----------


def get_credentials(db: Session) -> dict:
    return {
        "username": app_settings_svc.get_setting(db, BCS_USERNAME_KEY, ""),
        "password": decrypt_secret(app_settings_svc.get_setting(db, BCS_PASSWORD_KEY, "")),
        "wsdl_url": app_settings_svc.get_setting(db, BCS_WSDL_URL_KEY, ""),
    }


def credentials_configured(db: Session) -> bool:
    c = get_credentials(db)
    return bool(c["username"] and c["password"] and c["wsdl_url"])


def save_credentials(db: Session, *, username: str | None = None,
                     password: str | None = None, wsdl_url: str | None = None) -> None:
    """Persist credentials. The password only overwrites when a non-empty value
    is provided, so the admin form can render an empty password input without
    wiping the stored secret (same contract as the Salesforce store)."""
    if username is not None:
        app_settings_svc.set_setting(db, BCS_USERNAME_KEY, username.strip())
    if password is not None and password.strip():
        app_settings_svc.set_setting(db, BCS_PASSWORD_KEY, encrypt_secret(password))
    if wsdl_url is not None:
        app_settings_svc.set_setting(db, BCS_WSDL_URL_KEY, wsdl_url.strip())


# ---------- pure SOAP-shaping helpers (no zeep needed) ----------


def endpoint_from_wsdl(wsdl_url: str) -> str:
    """Derive the SOAP request endpoint from the WSDL URL by dropping the query
    (``…/TimerecordingWebService?wsdl`` → ``…/TimerecordingWebService``)."""
    return (wsdl_url or "").split("?", 1)[0]


def build_time_record_args(*, external_id: str, work_package_oid: str, date_iso: str,
                           expense_minutes: int, comment: str,
                           employee_login: str | None = None) -> dict:
    """Build the keyword arguments for ``CreateOrUpdateTimeRecord``.

    * ``id`` (externalID) is our idempotency key — a re-push with the same value
      updates the same BCS record instead of duplicating it.
    * ``target.task.bcsOid`` is the work package (Vorgang) to book on.
    * ``expense`` is the aggregated duration. Unit assumed to be minutes
      (BCS convention; verify with the first live booking).
    """
    args: dict = {
        "id": {"_value_1": external_id, "systemName": EXTERNAL_SYSTEM_NAME},
        "target": {"task": {"bcsOid": work_package_oid}},
        "date": date_iso,
        "expense": int(expense_minutes),
        "comment": comment or "",
    }
    if employee_login:
        args["employee"] = {"login": employee_login}
    return args


def _attr(obj, name: str):
    """Read a field from a zeep object or a plain mapping/namespace, tolerantly."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def work_packages_from_timesheet(response) -> list[dict]:
    """Turn a ``GetTimesheetResponse`` into dropdown-ready options.

    Reads ``timesheetEntries.task[]``; each carries the attributes ``bcsOid``
    (→ value) and ``name`` (→ label), plus an optional project name for the
    fuzzy ``search`` text. Returns ``[{value, label, search}]``.
    """
    entries = _attr(response, "timesheetEntries")
    tasks = _attr(entries, "task") or []
    out: list[dict] = []
    seen: set[str] = set()
    for t in tasks:
        oid = _attr(t, "bcsOid")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        name = (_attr(t, "name") or "").strip()
        props = _attr(t, "timesheetEntryProperties")
        project = _attr(_attr(props, "project"), "name") or ""
        label = f"{name} ({project})" if project and project != name else (name or oid)
        search = " ".join(p for p in (name, project) if p).lower()
        out.append({"value": oid, "label": label, "search": search})
    out.sort(key=lambda o: o["label"].lower())
    return out


def oid_from_response(response) -> str | None:
    """Pull the created/updated record OID out of a
    ``CreateOrUpdateTimeRecordResponse`` (→ EntrySync.external_ref)."""
    return _attr(response, "oid")


# ---------- client ----------


class BcsClient:
    """Thin wrapper around a zeep client for the Timerecording web service."""

    def __init__(self, username: str, password: str, wsdl_url: str):
        if not (username and password and wsdl_url):
            raise BcsError("BCS-Zugangsdaten unvollständig")
        self.username = username
        self._password = password
        self.wsdl_url = wsdl_url
        self.endpoint_url = endpoint_from_wsdl(wsdl_url)
        self._service = None  # bound lazily

    def _get_service(self):
        if self._service is not None:
            return self._service
        try:
            from requests import Session as RequestsSession
            from requests.auth import HTTPBasicAuth
            from zeep import Client
            from zeep.transports import Transport
            from zeep.wsse.username import UsernameToken
        except ImportError as e:  # pragma: no cover - dependency missing
            raise BcsError(f"SOAP-Bibliothek nicht verfügbar: {e}") from e

        # The WSDL/XSD endpoints are permission-protected; send the same creds as
        # HTTP Basic for those GETs so zeep can load the schema server-to-server.
        session = RequestsSession()
        session.auth = HTTPBasicAuth(self.username, self._password)
        transport = Transport(session=session, timeout=30)
        try:
            client = Client(
                self.wsdl_url,
                wsse=UsernameToken(self.username, self._password),
                transport=transport,
            )
            self._service = client.create_service(_SOAP11_BINDING, self.endpoint_url)
        except Exception as e:  # zeep raises a zoo of exceptions
            raise BcsError(f"BCS-WSDL konnte nicht geladen werden: {e}") from e
        return self._service

    @staticmethod
    def _impersonate_header(email: str):
        """Build the raw ``<ImpersonateAs>`` SOAP header element."""
        from lxml import etree
        el = etree.Element("ImpersonateAs")
        el.text = email
        return el

    def _call(self, op_name: str, *, impersonate: str | None = None, **kwargs):
        service = self._get_service()
        headers = [self._impersonate_header(impersonate)] if impersonate else None
        op = getattr(service, op_name)
        try:
            if headers:
                return op(**kwargs, _soapheaders=headers)
            return op(**kwargs)
        except BcsError:
            raise
        except Exception as e:
            raise BcsError(_format_fault(e)) from e

    def test_connection(self, impersonate: str | None = None) -> None:
        """Smoke test: a harmless ``GetTimeTrackingSettings`` call."""
        self._call("GetTimeTrackingSettings", impersonate=impersonate)

    def list_work_packages(self, email: str, date_iso: str) -> list[dict]:
        """Bookable work packages (tasks) for the user on a given date."""
        resp = self._call(
            "GetTimesheet",
            filter={"employee": {"login": email}, "date": date_iso,
                    "typesOfTimesheetEntries": {"tasks": {}}},
        )
        return work_packages_from_timesheet(resp)

    def create_or_update_time_record(self, args: dict, *, impersonate: str) -> str:
        """Upsert one time record; returns the BCS object OID."""
        resp = self._call("CreateOrUpdateTimeRecord", impersonate=impersonate, **args)
        oid = oid_from_response(resp)
        if not oid:
            raise BcsError("BCS-Antwort ohne Datensatz-OID")
        return oid


def _format_fault(exc: Exception) -> str:
    """Best-effort readable message from a zeep Fault. BCS wraps errors as
    ``{code, message}`` in the fault detail; fall back to the exception text."""
    message = getattr(exc, "message", None) or str(exc)
    detail = getattr(exc, "detail", None)
    if detail is not None:
        code = None
        text = None
        for el in detail.iter():
            tag = el.tag.split("}")[-1]
            if tag == "code":
                code = el.text
            elif tag == "message":
                text = el.text
        if text:
            return f"[{code or 'BCS'}] {text}"
    return message


def client_from_settings(db: Session) -> BcsClient | None:
    c = get_credentials(db)
    if not (c["username"] and c["password"] and c["wsdl_url"]):
        return None
    return BcsClient(c["username"], c["password"], c["wsdl_url"])


def work_package_oid_for(entry, project) -> str | None:
    """Resolve the work package OID for an entry: entry override beats the
    project default, both under ``sync_metadata[bcs]``."""
    for source, key in ((entry.sync_metadata_override or {}, "work_package"),
                        (project.sync_metadata or {}, "default_work_package")):
        oid = (source.get("bcs") or {}).get(key)
        if oid:
            return oid
    return None


def collect_emails(items: Iterable[tuple]) -> list[str]:
    """Unique consultant e-mails across (entry, user) pairs, first-seen order."""
    seen: list[str] = []
    for _entry, user in items:
        if user and user.email and user.email not in seen:
            seen.append(user.email)
    return seen
