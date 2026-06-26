import logging
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

import jinja2
from fastapi import (
    HTTPException,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ImportFormat, Project, SavedView, TimeEntry, User
from app.services import app_settings as app_settings_svc
from app.services import salesforce as sf_svc
from app.web.templating import join_base

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@jinja2.pass_context
def _template_path(context, path: str) -> str:
    request = context["request"]
    return join_base(request.scope.get("root_path", ""), path)


templates.env.globals["path"] = _template_path


# ── German date formatting ────────────────────────────────────────────────────
# Templates render `date` objects in German notation (DD.MM.YYYY) via the
# `de_date` Jinja filter. We map weekday names ourselves instead of relying on
# locale.setlocale(), which needs the de_DE locale installed in the container.
_DE_WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def de_date(value: date | datetime | str | None, weekday: bool = False) -> str:
    """Format a date as ``17.06.2026`` (optionally prefixed ``Mi, 17.06.2026``).

    Tolerates ``None``/empty and ISO strings so it can be dropped onto any value
    a template might hand it without guarding first.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return value
    formatted = value.strftime("%d.%m.%Y")
    if weekday:
        return f"{_DE_WEEKDAYS[value.weekday()]}, {formatted}"
    return formatted


templates.env.filters["de_date"] = de_date


def de_day_label(value: date | datetime | str | None, today: date | None = None) -> str:
    """Relative German day headline used once per day-group.

    ``Heute · 17.06.2026`` / ``Gestern · 16.06.2026`` for today/yesterday,
    otherwise the weekday: ``Mi · 15.06.2026``. The full date is always kept
    so longer groups still carry their day context. ``today`` is injectable for
    deterministic tests; it defaults to the server's current date.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return value
    if isinstance(value, datetime):
        value = value.date()
    full = value.strftime("%d.%m.%Y")
    delta = ((today or date.today()) - value).days
    if delta == 0:
        return f"Heute · {full}"
    if delta == 1:
        return f"Gestern · {full}"
    return f"{_DE_WEEKDAYS[value.weekday()]} · {full}"


templates.env.filters["de_day_label"] = de_day_label


# ── CSRF protection ──────────────────────────────────────────────────────────
# The web UI authenticates via a session cookie, so every state-changing form
# POST needs a CSRF token. We use a per-session synchronizer token: it lives in
# the (signed, server-side) session, is embedded into pages as a <meta> tag /
# hidden field, and must come back on unsafe requests via the X-CSRF-Token
# header (fetch/XHR) or a `csrf_token` form field.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


_FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def _ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def csrf_protect(request: Request) -> None:
    """Router-level guard: mint the session CSRF token on every request (so
    templates can embed it) and verify it on unsafe methods."""
    expected = _ensure_csrf_token(request)
    if request.method in _SAFE_METHODS:
        return
    sent = request.headers.get("X-CSRF-Token")
    if not sent:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(_FORM_CONTENT_TYPES):
            form = await request.form()
            value = form.get("csrf_token")
            sent = value if isinstance(value, str) else None
    if not sent or not secrets.compare_digest(sent, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF-Token fehlt oder ist ungültig",
        )


# Defensive upper bounds so an unfiltered/huge dataset can't load the entire
# history into memory and render it. High enough not to affect normal use; when
# hit, the templates show a "narrow your filter" banner.
DASHBOARD_ENTRY_CAP = 1000


REPORT_ROW_CAP = 10000


def _maybe_user(request: Request, db: Session) -> User | None:
    """Identity comes from the Hub (X-MSQ-*) or dev-bypass, resolved lazily and
    cached on request.state. No session-token decoding."""
    from app.identity import resolve_request_user

    return resolve_request_user(request, db)


class LoginRequired(Exception):
    """Raised when an unauthenticated request hits a protected web page; an
    exception handler (registered in app.main) turns it into a 401 JSON response
    — there is no app login page behind the Hub. Lets route bodies say
    `user = _require_login(request, db)` in one line instead of repeating the
    maybe-user/401 dance everywhere."""


def _require_login(request: Request, db: Session) -> User:
    user = _maybe_user(request, db)
    if user is None:
        raise LoginRequired()
    return user


def _require_admin(request: Request, db: Session) -> User:
    user = _require_login(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return user


# Themes offered in the UI; each is a real, working palette (see base.html).
# "dark" is the default look.
_THEMES = {"dark", "light", "mindsquare"}
_DEFAULT_THEME = "dark"


def _ctx(request: Request, user: User | None, **extra) -> dict:
    theme = request.cookies.get("theme")
    if theme not in _THEMES:
        theme = _DEFAULT_THEME
    return {
        "request": request,
        "user": user,
        "theme": theme,
        "ai_enabled": bool(get_settings().anthropic_api_key),
        **extra,
    }


def _filter_query(
    df: date | None,
    dt: date | None,
    project_id: int | None,
    customer: str | None = None,
    target: str | None = None,
) -> str:
    """Rebuild the dashboard filter as a relative URL, so CRUD actions can
    bounce back to the exact same filtered view instead of resetting to '/'.

    The resolved (concrete) dates are used so the result set is reproduced
    one-to-one, even when the active view came from a relative range token."""
    from urllib.parse import urlencode

    params = {}
    if df is not None:
        params["date_from"] = df.isoformat()
    if dt is not None:
        params["date_to"] = dt.isoformat()
    if project_id is not None:
        params["project_id"] = project_id
    if customer:
        params["customer"] = customer
    if target:
        params["target"] = target
    return "/?" + urlencode(params) if params else "/"


# ── Relative date ranges ──────────────────────────────────────────────────────
# Saved views and the filter bars offer relative ranges so a standing view stays
# meaningful over time (e.g. "this_month" always tracks the current month).
# Only ``custom`` falls back to explicit date_from/date_to.
DATE_RANGES: dict[str, str] = {
    "all": "Gesamter Zeitraum",
    "this_week": "Diese Woche",
    "last_week": "Letzte Woche",
    "this_month": "Dieser Monat",
    "last_month": "Letzter Monat",
    "this_year": "Dieses Jahr",
    "custom": "Benutzerdefiniert",
}


def _month_end(d: date) -> date:
    """Last day of the month containing ``d``."""
    from calendar import monthrange

    return d.replace(day=monthrange(d.year, d.month)[1])


def resolve_date_range(
    token: str | None,
    date_from: date | None,
    date_to: date | None,
    *,
    today: date | None = None,
) -> tuple[date | None, date | None]:
    """Translate a relative range token into a concrete (from, to) window.

    ``custom`` (and any unknown token) returns the explicit dates unchanged;
    ``all`` returns (None, None). ``today`` is injectable for deterministic
    tests and defaults to the server's current date.
    """
    today = today or date.today()
    if token == "all":
        return None, None
    if token == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if token == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if token == "this_month":
        start = today.replace(day=1)
        return start, _month_end(start)
    if token == "last_month":
        first_this = today.replace(day=1)
        prev_end = first_this - timedelta(days=1)
        return prev_end.replace(day=1), prev_end
    if token == "this_year":
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    # custom / unknown → use the explicit dates as-is.
    return date_from, date_to


def resolve_range_param(
    date_range: str | None,
    date_from: str | None,
    date_to: str | None,
    *,
    default: str,
    today: date | None = None,
) -> tuple[str, date | None, date | None]:
    """Turn the raw filter querystring into ``(token, from, to)``.

    An explicit, known range token wins; absent that, explicit dates imply
    ``custom`` and everything else falls back to ``default``. Shared by the
    dashboard and reports filter bars so their range handling can't drift.
    """
    token = date_range if date_range in DATE_RANGES else None
    if token is None:
        token = "custom" if (date_from or date_to) else default
    df, dt = resolve_date_range(token, _parse_date(date_from), _parse_date(date_to), today=today)
    return token, df, dt


def load_saved_views(
    db: Session, user: User, kind: str, view: str | None
) -> tuple[list[SavedView], SavedView | None]:
    """Return ``(all of the user's views for this page, the selected one)``.

    ``view`` is the raw querystring id; an unknown/invalid id yields ``None``
    for the active view without raising.
    """
    rows = list(
        db.execute(
            select(SavedView)
            .where(SavedView.user_id == user.id, SavedView.kind == kind)
            .order_by(SavedView.name)
        ).scalars()
    )
    active: SavedView | None = None
    if view:
        try:
            vid = int(view)
        except ValueError:
            vid = None
        if vid is not None:
            active = next((v for v in rows if v.id == vid), None)
    return rows, active


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _group_by_day(entries: list[TimeEntry]) -> list[dict]:
    """Group entries by entry_date (descending) and attach per-day subtotals."""
    grouped: dict[date, list[TimeEntry]] = {}
    for e in entries:
        grouped.setdefault(e.entry_date, []).append(e)
    days = []
    for day in sorted(grouped.keys(), reverse=True):
        items = grouped[day]
        days.append({
            "date": day,
            "entries": items,
            "total_minutes": sum(e.duration_minutes for e in items),
        })
    return days


def _parse_time(value: str | None):
    """Parse an HTML <input type=time> value (HH:MM, sometimes HH:MM:SS)."""
    if not value or not value.strip():
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def _parse_duration_minutes(value: str | int | None) -> int | None:
    """Parse the optional "Dauer (Min)" form field.

    HTML <input type=number> fields submit an empty string when left blank, and
    FastAPI cannot coerce "" into ``int | None`` — it raises a 422 before the
    route runs (the user sees raw JSON instead of the form). Accept the raw
    string here and treat blank as "no duration given" so start+end can drive
    it instead."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError("Dauer muss eine ganze Zahl sein") from None


def _resolve_duration(start, end, duration_minutes: int | None) -> int:
    """start+end win when both present (derive duration); otherwise use the
    explicit duration field. Raises ValueError if neither yields a positive
    duration."""
    if start is not None and end is not None:
        delta = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
        if delta <= 0:
            raise ValueError("Ende muss nach dem Start liegen")
        return delta
    if duration_minutes and duration_minutes > 0:
        return duration_minutes
    raise ValueError("Dauer angeben oder Start + Ende ausfüllen")


def _owned_entry_or_404(db: Session, entry_id: int, user: User) -> TimeEntry:
    entry = db.get(TimeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    # Time data is per-user: only the owner may view or manage an entry.
    if entry.user_id != user.id:
        raise HTTPException(status_code=403, detail="not your entry")
    return entry


def _owned_project_or_404(db: Session, project_id: int, user: User) -> Project:
    """Projects are per-user; only the owner may reference or manage one."""
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _safe_next(target: str | None, fallback: str = "/") -> str:
    """Only allow same-site relative redirects (avoid open-redirects)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _json_user_or_401(request: Request, db: Session) -> User | JSONResponse:
    user = _maybe_user(request, db)
    if user is None:
        return JSONResponse({"error": "Nicht angemeldet"}, status_code=401)
    return user


def _visible_formats(db: Session, user: User) -> list[ImportFormat]:
    stmt = (
        select(ImportFormat)
        .where(or_(ImportFormat.is_global.is_(True), ImportFormat.owner_id == user.id))
        .order_by(ImportFormat.is_global.desc(), ImportFormat.name)
    )
    return list(db.execute(stmt).scalars())


def _sync_dynamic_options(db: Session, user: User | None) -> dict:
    """Runtime-Auswahllisten für SyncFields mit options_source. Aktuell:
    aktive Salesforce-Projektbesetzungen des aktuellen Users (E-Mail-Match).
    Fehler / fehlende Creds → leere Map (UI fällt auf freies Eingabefeld zurück)."""
    options: dict[str, list[dict]] = {}
    if user is None:
        return options
    client = sf_svc.client_for_user(db, user)
    if client is None or not user.email:
        return options
    try:
        items = sf_svc.list_assignments_for_user(client, user.email)
    except sf_svc.SalesforceError as e:
        log.info("SF assignment lookup skipped: %s", e)
        return options
    if items:
        options["sf_assignments"] = items
    return options


def _ai_hints(db: Session, user: User | None) -> str:
    """Combine global (admin) and personal standing instructions for the AI."""
    parts = []
    g = app_settings_svc.get_setting(db, app_settings_svc.AI_HINTS_KEY, "")
    if g and g.strip():
        parts.append(g.strip())
    if user and user.ai_hints and user.ai_hints.strip():
        parts.append(user.ai_hints.strip())
    return "\n".join(parts)


_KNOWN_SYNC_TARGETS = ["intern", "jira", "salesforce", "bcs", "none"]

