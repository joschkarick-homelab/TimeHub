"""Microsoft 365 (Graph) calendar integration.

Two layers live here on purpose, mirroring the Salesforce module:

* a global, admin-managed credential store in ``AppSetting`` — the Entra app
  registration (client id, tenant, client secret, redirect URI, display
  timezone). Secrets are Fernet-encrypted at rest, like the SF password.
* a thin Graph client doing the OAuth2 authorization-code + PKCE dance and the
  read-only ``/me/calendarView`` fetch, with on-demand access-token refresh.

The per-user OAuth tokens live in the ``m365_connections`` table (see
``app.models.m365_connection``), not here — this module only knows how to mint,
refresh and use them. The calendar is consumed read-only: TimeHub never writes
back to Outlook.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy.orm import Session

from app.security import decrypt_secret, encrypt_secret
from app.services import app_settings as app_settings_svc

log = logging.getLogger(__name__)

# ── AppSetting keys (global, admin-managed) ─────────────────────────────────
M365_CLIENT_ID_KEY = "m365.client_id"
M365_TENANT_KEY = "m365.tenant"
M365_CLIENT_SECRET_KEY = "m365.client_secret"
M365_REDIRECT_URI_KEY = "m365.redirect_uri"
M365_TIMEZONE_KEY = "m365.timezone"
# SSO uses its own redirect URI (the login callback), distinct from the calendar
# connect callback above — both must be registered on the Entra app.
M365_LOGIN_REDIRECT_URI_KEY = "m365.login_redirect_uri"

# "organizations" works for any work/school tenant without hard-coding the
# tenant id; a specific tenant id locks sign-in to that org.
_DEFAULT_TENANT = "organizations"
# Graph honours an IANA timezone in the Prefer header and returns event times
# in it — so the day grid (which thinks in local minutes-of-day) lines up.
_DEFAULT_TIMEZONE = "Europe/Berlin"

# Delegated scopes: offline_access → refresh token; Calendars.Read → the
# signed-in user's calendar; the rest identify the account for display.
SCOPES = "openid profile email offline_access User.Read Calendars.Read"
# SSO login only needs the standard sign-in scopes (no admin consent, no
# offline_access/Graph) — just enough to get a validated ID token.
OIDC_SCOPES = "openid profile email"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_AUTH_HOST = "https://login.microsoftonline.com"
_HTTP_TIMEOUT = 20
# Refresh a little before the token actually expires to avoid a racing 401.
_EXPIRY_SKEW = timedelta(seconds=60)


class M365Error(RuntimeError):
    """Any failure talking to Microsoft (config, network, OAuth, Graph). The
    calendar view swallows it into a banner so time tracking keeps working."""


# ── Global config store (AppSetting) ────────────────────────────────────────


def get_config(db: Session) -> dict:
    return {
        "client_id": app_settings_svc.get_setting(db, M365_CLIENT_ID_KEY, ""),
        "tenant": app_settings_svc.get_setting(db, M365_TENANT_KEY, "") or _DEFAULT_TENANT,
        "client_secret": decrypt_secret(
            app_settings_svc.get_setting(db, M365_CLIENT_SECRET_KEY, "")
        ),
        "redirect_uri": app_settings_svc.get_setting(db, M365_REDIRECT_URI_KEY, ""),
        "login_redirect_uri": app_settings_svc.get_setting(db, M365_LOGIN_REDIRECT_URI_KEY, ""),
        "timezone": app_settings_svc.get_setting(db, M365_TIMEZONE_KEY, "") or _DEFAULT_TIMEZONE,
    }


def configured(db: Session) -> bool:
    """True once the app registration is usable (client id + secret present)."""
    c = get_config(db)
    return bool(c["client_id"] and c["client_secret"])


def save_config(
    db: Session,
    *,
    client_id: str | None = None,
    tenant: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str | None = None,
    login_redirect_uri: str | None = None,
    timezone: str | None = None,
) -> None:
    """Persist global config. The client secret only overwrites when a
    non-empty value is given (so the admin form can render an empty input
    without wiping the stored secret) — same convention as the SF store."""
    if client_id is not None:
        app_settings_svc.set_setting(db, M365_CLIENT_ID_KEY, client_id.strip())
    if tenant is not None:
        app_settings_svc.set_setting(db, M365_TENANT_KEY, tenant.strip() or _DEFAULT_TENANT)
    if client_secret is not None and client_secret.strip():
        app_settings_svc.set_setting(
            db, M365_CLIENT_SECRET_KEY, encrypt_secret(client_secret.strip())
        )
    if redirect_uri is not None:
        app_settings_svc.set_setting(db, M365_REDIRECT_URI_KEY, redirect_uri.strip())
    if login_redirect_uri is not None:
        app_settings_svc.set_setting(
            db, M365_LOGIN_REDIRECT_URI_KEY, login_redirect_uri.strip()
        )
    if timezone is not None:
        app_settings_svc.set_setting(db, M365_TIMEZONE_KEY, timezone.strip() or _DEFAULT_TIMEZONE)


# ── OAuth: PKCE + authorization-code flow ───────────────────────────────────


def make_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for an S256 PKCE exchange. The verifier
    is stashed in the session and replayed at the token step; the challenge is
    what we hand Microsoft in the authorize redirect."""
    verifier = secrets.token_urlsafe(48)  # 64 chars, within the 43–128 range
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def authorize_url(
    db: Session,
    *,
    state: str,
    code_challenge: str,
    redirect_uri: str,
    scope: str = SCOPES,
    nonce: str | None = None,
) -> str:
    """Build the authorize redirect. Defaults to the calendar scopes; the SSO
    login flow passes ``scope=OIDC_SCOPES`` and a ``nonce`` (bound into the ID
    token and re-checked at the callback to block replay)."""
    c = get_config(db)
    if not c["client_id"]:
        raise M365Error("Microsoft 365 ist nicht konfiguriert")
    params = {
        "client_id": c["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if nonce:
        params["nonce"] = nonce
    return f"{_AUTH_HOST}/{c['tenant']}/oauth2/v2.0/authorize?{urlencode(params)}"


def _token_request(db: Session, data: dict) -> dict:
    c = get_config(db)
    if not (c["client_id"] and c["client_secret"]):
        raise M365Error("Microsoft 365 ist nicht konfiguriert")
    url = f"{_AUTH_HOST}/{c['tenant']}/oauth2/v2.0/token"
    body = {"client_id": c["client_id"], "client_secret": c["client_secret"], **data}
    try:
        resp = httpx.post(url, data=body, timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as e:
        raise M365Error(f"Microsoft nicht erreichbar: {e}") from e
    if resp.status_code >= 400:
        detail = ""
        try:
            j = resp.json()
            detail = j.get("error_description") or j.get("error") or ""
        except ValueError:
            detail = resp.text[:300]
        raise M365Error(f"Token-Anfrage fehlgeschlagen: {detail[:300]}")
    return resp.json()


def exchange_code(
    db: Session, *, code: str, code_verifier: str, redirect_uri: str, scope: str = SCOPES
) -> dict:
    return _token_request(
        db,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "scope": scope,
        },
    )


def refresh_tokens(db: Session, refresh_token: str) -> dict:
    return _token_request(
        db,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": SCOPES},
    )


# ── OIDC login: ID-token validation ──────────────────────────────────────────
# SSO trusts nothing the browser relays: the ID token is verified server-side
# against the tenant's published signing keys (JWKS), with audience, issuer,
# expiry and the per-login nonce all checked. With a concrete tenant configured,
# the issuer check pins sign-in to that single organisation.

_DISCOVERY_PATH = "/v2.0/.well-known/openid-configuration"


def _fetch_discovery(db: Session) -> dict:
    """Tenant OIDC metadata (``issuer`` + ``jwks_uri``). Split out so tests can
    stub it instead of reaching Microsoft."""
    c = get_config(db)
    url = f"{_AUTH_HOST}/{c['tenant']}{_DISCOVERY_PATH}"
    try:
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as e:
        raise M365Error(f"Microsoft nicht erreichbar: {e}") from e
    if resp.status_code >= 400:
        raise M365Error(f"OIDC-Discovery fehlgeschlagen: HTTP {resp.status_code}")
    return resp.json()


def _resolve_signing_key(jwks_uri: str, id_token: str):
    """Return the public signing key whose ``kid`` matches the token header.
    Split out so tests can stub the JWKS lookup with a local key."""
    if not jwks_uri:
        raise M365Error("OIDC-Discovery ohne jwks_uri")
    try:
        return jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token).key
    except jwt.PyJWTError as e:
        raise M365Error(f"Token-Signaturschlüssel nicht abrufbar: {e}") from e


def _verify_issuer_and_nonce(claims: dict, *, issuer_template: str, nonce: str) -> None:
    """Reject the token unless the replay nonce matches and the issuer is the
    Microsoft v2.0 issuer for the tenant that signed it. The discovery issuer is
    concrete for a single tenant and carries a ``{tenantid}`` placeholder for the
    multi-tenant (``organizations``/``common``) endpoints."""
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise M365Error("ID-Token ungültig: nonce stimmt nicht überein")
    tid = str(claims.get("tid", ""))
    iss = str(claims.get("iss", ""))
    if "{tenantid}" in issuer_template:
        if not tid or iss != issuer_template.replace("{tenantid}", tid):
            raise M365Error("ID-Token ungültig: Aussteller nicht erlaubt")
    elif iss != issuer_template:
        raise M365Error("ID-Token ungültig: Aussteller nicht erlaubt")


def validate_id_token(db: Session, id_token: str, *, nonce: str) -> dict:
    """Fully validate an Entra ID token and return its claims, or raise
    ``M365Error``. Checks signature (RS256 / tenant JWKS), audience (our client
    id), expiry/issued-at, issuer and the per-login nonce."""
    c = get_config(db)
    if not c["client_id"]:
        raise M365Error("Microsoft 365 ist nicht konfiguriert")
    disc = _fetch_discovery(db)
    key = _resolve_signing_key(disc.get("jwks_uri", ""), id_token)
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=c["client_id"],
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as e:
        raise M365Error(f"ID-Token ungültig: {e}") from e
    _verify_issuer_and_nonce(claims, issuer_template=disc.get("issuer", ""), nonce=nonce)
    return claims


def profile_from_claims(claims: dict) -> dict:
    """Extract the fields we match/provision on from validated ID-token claims:
    a normalized (lower-cased) ``email``, the stable ``oid`` and the display
    ``name``."""
    email = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or ""
    ).strip().lower()
    return {
        "email": email,
        "oid": (claims.get("oid") or "").strip(),
        "name": (claims.get("name") or "").strip(),
    }


# ── Token persistence on the connection row ─────────────────────────────────


def store_tokens(db: Session, conn, token_response: dict, *, account: str | None = None) -> None:
    """Encrypt and persist the tokens from a code/refresh exchange onto the
    connection. A refresh response may omit ``refresh_token`` (Microsoft only
    rotates it sometimes) — keep the existing one in that case."""
    access = token_response.get("access_token")
    if not access:
        raise M365Error("Token-Antwort ohne access_token")
    conn.access_token = encrypt_secret(access)
    new_refresh = token_response.get("refresh_token")
    if new_refresh:
        conn.refresh_token = encrypt_secret(new_refresh)
    expires_in = int(token_response.get("expires_in") or 3600)
    conn.token_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    if account is not None:
        conn.account = account
    conn.last_error = None


def _valid_access_token(db: Session, conn) -> str:
    """Return a usable access token, refreshing on demand. Commits the refreshed
    tokens so a later request in the same range doesn't refresh again."""
    expires = conn.token_expires_at
    if expires is not None and expires.tzinfo is None:
        # SQLite hands back naive datetimes; treat stored values as UTC.
        expires = expires.replace(tzinfo=UTC)
    if expires is None or expires <= datetime.now(UTC) + _EXPIRY_SKEW:
        rt = decrypt_secret(conn.refresh_token or "")
        if not rt:
            raise M365Error("Keine gültige Microsoft-Sitzung – bitte neu verbinden")
        tokens = refresh_tokens(db, rt)
        store_tokens(db, conn, tokens)
        db.add(conn)
        db.commit()
    return decrypt_secret(conn.access_token or "")


# ── Graph reads ─────────────────────────────────────────────────────────────


def _graph_get(access_token: str, path: str, *, params: dict | None = None,
               prefer: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    try:
        resp = httpx.get(f"{GRAPH_BASE}{path}", headers=headers, params=params,
                         timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as e:
        raise M365Error(f"Microsoft Graph nicht erreichbar: {e}") from e
    if resp.status_code >= 400:
        raise M365Error(f"Graph {path} HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def fetch_account(access_token: str) -> str:
    """Best-effort display name for the connected mailbox (UPN/mail)."""
    data = _graph_get(
        access_token, "/me", params={"$select": "userPrincipalName,mail,displayName"}
    )
    return (
        data.get("userPrincipalName")
        or data.get("mail")
        or data.get("displayName")
        or ""
    )


def _parse_graph_dt(node: dict | None) -> datetime | None:
    """Parse Graph's ``{dateTime, timeZone}`` into a naive datetime in the
    requested timezone. Graph emits up to 7 fractional digits which
    ``fromisoformat`` rejects on older Pythons, so trim to microseconds."""
    raw = (node or {}).get("dateTime")
    if not raw:
        return None
    cleaned = raw.rstrip("Z")
    if "." in cleaned:
        head, frac = cleaned.split(".", 1)
        cleaned = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _normalize_event(ev: dict) -> dict | None:
    start = _parse_graph_dt(ev.get("start"))
    if start is None:
        return None
    end = _parse_graph_dt(ev.get("end"))
    organizer = (((ev.get("organizer") or {}).get("emailAddress") or {}).get("name")) or ""
    return {
        "subject": (ev.get("subject") or "(ohne Titel)").strip() or "(ohne Titel)",
        "start_dt": start,
        "end_dt": end,
        "all_day": bool(ev.get("isAllDay")),
        "show_as": ev.get("showAs") or "busy",
        "organizer": organizer,
    }


def calendar_view(db: Session, conn, start: date, end: date) -> list[dict]:
    """Fetch the user's events overlapping the inclusive ``[start, end]`` day
    range, normalized into ``{subject, start_dt, end_dt, all_day, show_as,
    organizer}``. ``calendarView`` expands recurring series for us."""
    c = get_config(db)
    token = _valid_access_token(db, conn)
    params = {
        # calendarView's end is exclusive — extend by a day to include `end`.
        "startDateTime": f"{start.isoformat()}T00:00:00",
        "endDateTime": f"{(end + timedelta(days=1)).isoformat()}T00:00:00",
        "$select": "subject,start,end,isAllDay,showAs,organizer",
        "$orderby": "start/dateTime",
        "$top": "250",
    }
    data = _graph_get(
        token, "/me/calendarView", params=params,
        prefer=f'outlook.timezone="{c["timezone"]}"',
    )
    out: list[dict] = []
    for ev in data.get("value", []):
        norm = _normalize_event(ev)
        if norm is not None:
            out.append(norm)
    return out


def events_for_day(events: list[dict], day: date) -> dict:
    """Project normalized events onto a single day, clamped to that day's
    [00:00, 24:00) window. Returns ``{"timed": [...], "allday": [...]}`` ready
    for the template. Events spanning midnight are clamped per day and flagged
    with ``continued``/``continues`` so the block shows it runs over."""
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    timed: list[dict] = []
    allday: list[dict] = []
    for ev in events:
        s = ev.get("start_dt")
        e = ev.get("end_dt")
        if ev.get("all_day"):
            # All-day events use an exclusive end date (00:00 of the day after).
            end_date = e.date() if e else (day + timedelta(days=1))
            if s is not None and s.date() <= day < end_date:
                allday.append({"subject": ev["subject"], "show_as": ev.get("show_as", "busy")})
            continue
        if s is None:
            continue
        if e is None or e <= s:
            e = s + timedelta(minutes=30)
        if e <= day_start or s >= day_end:
            continue
        seg_start = max(s, day_start)
        seg_end = min(e, day_end)
        start_min = int((seg_start - day_start).total_seconds() // 60)
        end_min = int((seg_end - day_start).total_seconds() // 60)
        if end_min <= start_min:
            end_min = min(start_min + 15, 1440)
        timed.append({
            "subject": ev["subject"],
            "start": start_min,
            "end": end_min,
            "show_as": ev.get("show_as", "busy"),
            "organizer": ev.get("organizer", ""),
            "continued": s < day_start,
            "continues": e > day_end,
        })
    timed.sort(key=lambda x: x["start"])
    return {"timed": timed, "allday": allday}
