import logging
from urllib.parse import urlencode

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
    status,
)
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SavedView
from app.web.common import (
    DATE_RANGES,
    _parse_date,
    _require_login,
    _safe_next,
    redirect_to,
)
from app.web.templating import join_base

log = logging.getLogger(__name__)
router = APIRouter()


_KINDS = {"dashboard", "reports"}


def _redirect_with(request: Request, back: str, **params: str) -> RedirectResponse:
    """`back` is app-relative (no slug); prefix it with the Hub mount path once."""
    sep = "&" if "?" in back else "?"
    url = f"{back}{sep}{urlencode(params)}" if params else back
    url = join_base(request.scope.get("root_path", ""), url)
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.post("/views")
def save_view(
    request: Request,
    name: str = Form(...),
    kind: str = Form("reports"),
    date_range: str = Form("custom"),
    date_from: str = Form(""),
    date_to: str = Form(""),
    project_id: str = Form(""),
    customer: str = Form(""),
    group_by: list[str] = Form(default_factory=list),
    detailed: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create or overwrite a named saved view for the current user.

    Saving with an existing name (same page/kind) updates that view, so the
    natural "save" gesture doubles as "update" without a separate edit screen.
    """
    user = _require_login(request, db)
    kind = kind if kind in _KINDS else "reports"
    name = name.strip()
    back = _safe_next(next, "/reports" if kind == "reports" else "/")
    if not name:
        return _redirect_with(request, back, error="Bitte einen Namen für die Ansicht angeben")

    rng = date_range if date_range in DATE_RANGES else "custom"
    pid: int | None = None
    if project_id:
        try:
            pid = int(project_id)
        except ValueError:
            pid = None

    existing = db.execute(
        select(SavedView).where(
            SavedView.user_id == user.id,
            SavedView.kind == kind,
            SavedView.name == name,
        )
    ).scalar_one_or_none()
    view = existing or SavedView(user_id=user.id, kind=kind, name=name)
    view.date_range = rng
    view.date_from = _parse_date(date_from)
    view.date_to = _parse_date(date_to)
    view.project_id = pid
    view.customer = customer.strip() or None
    view.group_by = [g for g in group_by if g] if kind == "reports" else []
    view.detailed = (detailed in ("1", "true", "on", "yes")) and kind == "reports"
    db.add(view)
    db.commit()

    return _redirect_with(request, back, view=str(view.id), flash=f"Ansicht '{name}' gespeichert")


@router.post("/views/{view_id}/delete")
def delete_view(
    request: Request,
    view_id: int,
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    view = db.get(SavedView, view_id)
    if view is not None and view.user_id == user.id:
        db.delete(view)
        db.commit()
    return redirect_to(request, next)
