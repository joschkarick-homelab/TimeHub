import base64
import binascii
import json
import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ImportFormat, User
from app.schemas.import_format import SUPPORTED_TARGETS
from app.services import reports as report_svc
from app.services import sync_fields as sf
from app.services.ai_mapping import AiMappingError, suggest_mapping
from app.services.csv_import import import_csv
from app.services.transforms import clean_target_rules, clean_transforms
from app.web.common import (
    _KNOWN_SYNC_TARGETS,
    _ai_hints,
    _ctx,
    _maybe_user,
    _require_admin,
    _require_login,
    _visible_formats,
    templates,
)

log = logging.getLogger(__name__)
router = APIRouter()


# Friendlier labels for a few plain targets (the duration trio especially).
_TARGET_LABELS = {
    "entry_date": "Datum",
    "start_time": "Startzeit",
    "end_time": "Endzeit",
    "duration": "Dauer (automatisch)",
    "duration_minutes": "Dauer in Minuten",
    "duration_hours": "Dauer in Stunden",
    "duration_human": "Dauer als Text (1w 2d 3h 4m)",
    "project_code": "Projekt (Code/Name)",
    "customer": "Kunde",
    "description": "Beschreibung",
    "tags": "Tags",
    "sync_target": "Sync-Ziel (pro Zeile)",
    "external_ref": "Externe Referenz",
}


# Order of the always-shown standard target rows (duration is injected after the
# time fields by the template).
_STANDARD_ROW_ORDER = [
    "entry_date", "start_time", "end_time",
    "project_code", "customer", "description", "tags", "sync_target", "external_ref",
]


def _target_label(token: str) -> str:
    """Human label for any mapping target (plain or sync), for previews/UI."""
    if token.startswith("sync:"):
        return sf.target_label(token)
    return _TARGET_LABELS.get(token, token)


def _mapping_rows() -> dict:
    """Structured target rows for the target-oriented mapping editor."""
    standard = [{"value": t, "label": _target_label(t)} for t in _STANDARD_ROW_ORDER]
    sync = [
        {"value": t, "label": sf.target_label(t)}
        for t in sorted(SUPPORTED_TARGETS) if t.startswith("sync:")
    ]
    return {"standard": standard, "sync": sync}


def _parse_column_map(raw: str) -> dict:
    """Parse the target-keyed column_map JSON ({target: source}), keeping only
    known targets with a non-empty source."""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k in SUPPORTED_TARGETS and v}


def _invert_map(d: dict) -> dict:
    """Swap keys/values (target<->source). Used at the AI boundary, which speaks
    source->target while TimeHub stores target->source."""
    return {v: k for k, v in d.items()}


def _target_options() -> list[dict]:
    """Flat list of all mapping targets with labels (used for the 'supported
    fields' hint on the upload screen)."""
    base = sorted(t for t in SUPPORTED_TARGETS if not t.startswith("sync:"))
    sync = sorted(t for t in SUPPORTED_TARGETS if t.startswith("sync:"))
    return [{"value": t, "label": _target_label(t)} for t in base + sync]


def _parse_transforms(raw: str) -> list[dict]:
    """Parse the transforms_json hidden field into a clean list of rules.
    Invalid entries are dropped rather than failing the whole save."""
    try:
        data = json.loads(raw) if raw.strip() else []
    except (json.JSONDecodeError, ValueError):
        return []
    return clean_transforms(data, SUPPORTED_TARGETS)


def _parse_target_rules(raw: str) -> list[dict]:
    try:
        data = json.loads(raw) if raw.strip() else []
    except (json.JSONDecodeError, ValueError):
        return []
    return clean_target_rules(data, set(_KNOWN_SYNC_TARGETS))


# Keep the stored sample small — same budget as what we send to the AI.
_SAMPLE_MAX_LINES = 30


def _trim_sample(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:_SAMPLE_MAX_LINES])[:8000]


def _peek_headers(sample: str, separator: str) -> list[str]:
    import csv as _csv
    import io as _io
    if not sample.strip():
        return []
    reader = _csv.reader(_io.StringIO(sample), delimiter=separator)
    try:
        return [h.lstrip("﻿").strip() for h in next(reader)]
    except StopIteration:
        return []


def _headers_union(sample: str, separator: str, column_map: dict) -> list[str]:
    """Every source column from the stored sample, plus any mapped source not in
    it — so ignored columns stay available as mapping/transform sources.
    column_map is target-keyed, so the sources are its values."""
    headers = _peek_headers(sample, separator)
    seen = set(headers)
    for src in column_map.values():
        if src and src not in seen:
            headers.append(src)
            seen.add(src)
    return headers


@router.get("/import-formats", response_class=HTMLResponse)
def formats_list(
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
    error: str | None = None,
):
    user = _require_login(request, db)
    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "import_formats.html",
        _ctx(request, user, formats=formats, flash=flash, error=error),
    )


@router.get("/import-formats/new", response_class=HTMLResponse)
def formats_new_form(request: Request, db: Session = Depends(get_db)):
    user = _require_login(request, db)
    return templates.TemplateResponse(
        "import_format_new.html",
        _ctx(request, user, error=None, target_options=_target_options()),
    )


@router.post("/import-formats/new", response_class=HTMLResponse)
async def formats_new_submit(
    request: Request,
    name: str = Form(...),
    sample: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)

    raw = await sample.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        suggestion = suggest_mapping(text, hints=_ai_hints(db, user))
    except AiMappingError as e:
        return templates.TemplateResponse(
            "import_format_new.html",
            _ctx(
                request,
                user,
                error=str(e),
                target_options=_target_options(),
                prefill_name=name,
            ),
            status_code=400,
        )

    sep = suggestion.separator if suggestion.separator != "\\t" else "\t"
    source_rows, target_rows = report_svc.preview_via_import_format(
        text,
        suggestion.column_map,
        separator=sep,
        date_format=suggestion.date_format,
        time_format=suggestion.time_format,
        transforms=suggestion.transforms,
    )
    return templates.TemplateResponse(
        "import_format_review.html",
        _ctx(
            request,
            user,
            name=name,
            suggestion=suggestion,
            source_rows=source_rows,
            target_rows=target_rows,
            target_fields=sorted(SUPPORTED_TARGETS),
            target_options=_target_options(),
            mapping_rows=_mapping_rows(),
            mapping=suggestion.column_map,
            headers=suggestion.detected_headers,
            transforms=suggestion.transforms,
            target_rules=suggestion.target_rules,
            sample_text=_trim_sample(text),
            tlabel=_target_label,
            error=None,
        ),
    )


@router.post("/import-formats/refine", response_class=HTMLResponse)
def formats_refine(
    request: Request,
    name: str = Form(...),
    sample_text: str = Form(""),
    instruction: str = Form(""),
    source_hint: str = Form("custom"),
    separator: str = Form(","),
    encoding: str = Form("utf-8"),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    default_project_code: str = Form(""),
    notes: str = Form(""),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    target_rules_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    """Refinement turn for the format wizard: re-run the AI with the current
    state as 'previous' plus the user's instruction, and re-render the review."""
    from app.schemas.import_format import ImportFormatSuggestion

    user = _require_login(request, db)

    canonical_map = _parse_column_map(column_map_json)  # target-keyed

    previous = {
        "source_hint": source_hint or "custom",
        "separator": separator or ",",
        "encoding": encoding or "utf-8",
        "date_format": date_format or "%Y-%m-%d",
        "time_format": time_format or "%H:%M",
        # the model speaks source->target
        "column_map": _invert_map(canonical_map),
        "transforms": _parse_transforms(transforms_json),
        "target_rules": _parse_target_rules(target_rules_json),
        "default_project_code": default_project_code.strip() or None,
        "notes": notes,
    }

    def _from_previous() -> "ImportFormatSuggestion":
        return ImportFormatSuggestion(
            source_hint=previous["source_hint"],
            separator=previous["separator"],
            encoding=previous["encoding"],
            date_format=previous["date_format"],
            time_format=previous["time_format"],
            column_map=canonical_map,
            transforms=previous["transforms"],
            target_rules=previous["target_rules"],
            default_project_code=previous["default_project_code"],
            notes=previous["notes"],
            detected_headers=list(canonical_map.values()),
        )

    error = None
    if not instruction.strip():
        suggestion = _from_previous()
        error = "Bitte eine Anweisung eingeben, was die KI anpassen soll."
    else:
        try:
            suggestion = suggest_mapping(
                sample_text, instruction=instruction, previous=previous, hints=_ai_hints(db, user)
            )
        except AiMappingError as e:
            suggestion = _from_previous()
            error = str(e)

    sep = suggestion.separator if suggestion.separator != "\\t" else "\t"
    source_rows, target_rows = report_svc.preview_via_import_format(
        sample_text,
        suggestion.column_map,
        separator=sep,
        date_format=suggestion.date_format,
        time_format=suggestion.time_format,
        transforms=suggestion.transforms,
    )
    # Show every sample column (plus any source the suggestion references) in the UI.
    if source_rows:
        headers = list(source_rows[0].keys())
        for src in suggestion.column_map.values():
            if src and src not in headers:
                headers.append(src)
        suggestion.detected_headers = headers

    return templates.TemplateResponse(
        "import_format_review.html",
        _ctx(
            request,
            user,
            name=name,
            suggestion=suggestion,
            source_rows=source_rows,
            target_rows=target_rows,
            target_fields=sorted(SUPPORTED_TARGETS),
            mapping_rows=_mapping_rows(),
            mapping=suggestion.column_map,
            target_options=_target_options(),
            headers=suggestion.detected_headers,
            transforms=suggestion.transforms,
            target_rules=suggestion.target_rules,
            sample_text=sample_text,
            tlabel=_target_label,
            error=error,
        ),
    )


@router.post("/import-formats/preview", response_class=HTMLResponse)
def formats_preview(
    request: Request,
    sample_text: str = Form(""),
    separator: str = Form(","),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    """Live preview fragment: render the current mapping + transforms against
    the sample. Returned as an HTML partial the editor swaps in on change."""
    user = _maybe_user(request, db)
    if user is None:
        return HTMLResponse("", status_code=401)
    column_map = _parse_column_map(column_map_json)
    transforms = _parse_transforms(transforms_json)
    sep = separator if separator and separator != "\\t" else (separator or ",")
    source_rows, target_rows = report_svc.preview_via_import_format(
        sample_text,
        column_map,
        separator=sep,
        date_format=date_format or "%Y-%m-%d",
        time_format=time_format or "%H:%M",
        transforms=transforms,
    )
    return templates.TemplateResponse(
        "_preview_panel.html",
        {
            "request": request,
            "source_rows": source_rows,
            "target_rows": target_rows,
            "target_fields": sorted(SUPPORTED_TARGETS),
            "tlabel": sf.target_label,
        },
    )


@router.post("/import-formats", response_class=HTMLResponse)
async def formats_save(
    request: Request,
    name: str = Form(...),
    source_hint: str = Form("custom"),
    separator: str = Form(","),
    encoding: str = Form("utf-8"),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    default_project_code: str = Form(""),
    notes: str = Form(""),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    target_rules_json: str = Form("[]"),
    sample_text: str = Form(""),
    is_global: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)

    column_map = _parse_column_map(column_map_json)

    fmt = ImportFormat(
        name=name,
        source_hint=source_hint or "custom",
        separator=separator or ",",
        encoding=encoding or "utf-8",
        date_format=date_format or "%Y-%m-%d",
        time_format=time_format or "%H:%M",
        column_map=column_map,
        transforms=_parse_transforms(transforms_json),
        target_rules=_parse_target_rules(target_rules_json),
        sample_data=(_trim_sample(sample_text) or None),
        default_project_code=(default_project_code.strip() or None),
        notes=notes,
        owner_id=user.id,
        is_global=(is_global and user.is_admin),
    )
    db.add(fmt)
    db.commit()
    base = request.scope.get("root_path", "")
    return RedirectResponse(
        url=f"{base}/import-formats?flash=Format+'{name}'+gespeichert",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/import-formats/{fmt_id}/edit", response_class=HTMLResponse)
def formats_edit_form(request: Request, fmt_id: int, db: Session = Depends(get_db)):
    user = _require_login(request, db)
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None or (not fmt.is_global and fmt.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="format not found")
    # writable check — same rule as the delete handler so we don't render an
    # edit form the user can't actually submit.
    if fmt.owner_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed to edit this format")
    sample = fmt.sample_data or ""
    sep = fmt.separator if fmt.separator != "\\t" else "\t"
    source_rows, target_rows = report_svc.preview_via_import_format(
        sample, fmt.column_map, separator=sep,
        date_format=fmt.date_format, time_format=fmt.time_format,
        transforms=fmt.transforms or [],
    )
    return templates.TemplateResponse(
        "import_format_edit.html",
        _ctx(
            request,
            user,
            fmt=fmt,
            target_options=_target_options(),
            mapping_rows=_mapping_rows(),
            mapping=fmt.column_map,
            column_map=fmt.column_map,
            transforms=fmt.transforms or [],
            target_rules=fmt.target_rules or [],
            # All columns from the stored sample stay available — even ignored
            # ones — plus any mapped header not in the sample.
            headers=_headers_union(sample, fmt.separator, fmt.column_map),
            sample_text=sample,
            source_rows=source_rows,
            target_rows=target_rows,
            target_fields=sorted(SUPPORTED_TARGETS),
            tlabel=_target_label,
            error=None,
        ),
    )


@router.post("/import-formats/{fmt_id}/edit", response_class=HTMLResponse)
async def formats_edit_submit(
    request: Request,
    fmt_id: int,
    name: str = Form(...),
    source_hint: str = Form("custom"),
    separator: str = Form(","),
    encoding: str = Form("utf-8"),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    default_project_code: str = Form(""),
    notes: str = Form(""),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    target_rules_json: str = Form("[]"),
    sample_text: str = Form(""),
    is_global: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="format not found")
    if fmt.owner_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")

    column_map = _parse_column_map(column_map_json)

    fmt.name = name
    fmt.source_hint = source_hint or "custom"
    fmt.separator = separator or ","
    fmt.encoding = encoding or "utf-8"
    fmt.date_format = date_format or "%Y-%m-%d"
    fmt.time_format = time_format or "%H:%M"
    fmt.column_map = column_map
    fmt.transforms = _parse_transforms(transforms_json)
    fmt.target_rules = _parse_target_rules(target_rules_json)
    fmt.sample_data = _trim_sample(sample_text) or None
    fmt.default_project_code = default_project_code.strip() or None
    fmt.notes = notes
    # only admins may flip the global flag
    if user.is_admin:
        fmt.is_global = bool(is_global)
    db.add(fmt)
    db.commit()
    base = request.scope.get("root_path", "")
    return RedirectResponse(
        url=f"{base}/import-formats?flash=Format+'{name}'+aktualisiert",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/import-formats/{fmt_id}/refine", response_class=HTMLResponse)
def formats_edit_refine(
    request: Request,
    fmt_id: int,
    name: str = Form(...),
    sample_text: str = Form(""),
    instruction: str = Form(""),
    source_hint: str = Form("custom"),
    separator: str = Form(","),
    encoding: str = Form("utf-8"),
    date_format: str = Form("%Y-%m-%d"),
    time_format: str = Form("%H:%M"),
    default_project_code: str = Form(""),
    notes: str = Form(""),
    column_map_json: str = Form("{}"),
    transforms_json: str = Form("[]"),
    target_rules_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    """AI refinement that stays on the edit screen: re-runs the model with the
    current state + instruction, then re-renders the edit form (unsaved) so the
    user can review and Save."""
    user = _require_login(request, db)
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="format not found")
    if fmt.owner_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")

    column_map = _parse_column_map(column_map_json)  # target-keyed
    transforms = _parse_transforms(transforms_json)
    target_rules = _parse_target_rules(target_rules_json)

    error = None
    if not instruction.strip():
        error = "Bitte eine Anweisung eingeben, was die KI anpassen soll."
    elif not sample_text.strip():
        error = "Für die KI werden Beispieldaten benötigt — bitte unten einfügen."
    else:
        previous = {
            "source_hint": source_hint, "separator": separator, "encoding": encoding,
            "date_format": date_format, "time_format": time_format,
            "column_map": _invert_map(column_map),  # the model speaks source->target
            "transforms": transforms, "target_rules": target_rules,
            "default_project_code": default_project_code.strip() or None, "notes": notes,
        }
        try:
            suggestion = suggest_mapping(
                sample_text, instruction=instruction, previous=previous, hints=_ai_hints(db, user)
            )
            source_hint = suggestion.source_hint
            separator = suggestion.separator
            encoding = suggestion.encoding
            date_format = suggestion.date_format
            time_format = suggestion.time_format
            column_map = suggestion.column_map
            transforms = suggestion.transforms
            target_rules = suggestion.target_rules
            notes = suggestion.notes or notes
        except AiMappingError as e:
            error = str(e)

    # Reflect the (unsaved) current/refined values in the re-render. The session
    # never commits this — we expunge so an accidental flush can't persist it.
    fmt.name = name
    fmt.source_hint = source_hint
    fmt.separator = separator
    fmt.encoding = encoding
    fmt.date_format = date_format
    fmt.time_format = time_format
    fmt.default_project_code = default_project_code.strip() or None
    fmt.notes = notes
    db.expunge(fmt)
    sep = separator if separator != "\\t" else "\t"
    source_rows, target_rows = report_svc.preview_via_import_format(
        sample_text, column_map, separator=sep,
        date_format=date_format, time_format=time_format, transforms=transforms,
    )
    return templates.TemplateResponse(
        "import_format_edit.html",
        _ctx(
            request,
            user,
            fmt=fmt,
            target_options=_target_options(),
            mapping_rows=_mapping_rows(),
            mapping=column_map,
            column_map=column_map,
            transforms=transforms,
            target_rules=target_rules,
            headers=_headers_union(sample_text, separator, column_map),
            sample_text=sample_text,
            source_rows=source_rows,
            target_rows=target_rows,
            target_fields=sorted(SUPPORTED_TARGETS),
            tlabel=_target_label,
            error=error,
        ),
    )


@router.post("/import-formats/{fmt_id}/delete", response_class=HTMLResponse)
def formats_delete(request: Request, fmt_id: int, db: Session = Depends(get_db)):
    user = _require_login(request, db)
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="Not found")
    if fmt.owner_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="not allowed")
    db.delete(fmt)
    db.commit()
    base = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{base}/import-formats", status_code=status.HTTP_302_FOUND)


@router.post("/import-formats/{fmt_id}/promote", response_class=HTMLResponse)
def formats_promote(request: Request, fmt_id: int, db: Session = Depends(get_db)):
    _require_admin(request, db)
    fmt = db.get(ImportFormat, fmt_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="Not found")
    fmt.is_global = not fmt.is_global
    db.add(fmt)
    db.commit()
    base = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{base}/import-formats", status_code=status.HTTP_302_FOUND)


@router.get("/import", response_class=HTMLResponse)
def import_form(
    request: Request,
    db: Session = Depends(get_db),
    result: str | None = None,
    error: str | None = None,
):
    user = _require_login(request, db)
    formats = _visible_formats(db, user)
    return templates.TemplateResponse(
        "import_run.html",
        _ctx(request, user, formats=formats, result=result, error=error),
    )


def _run_import(
    db: Session, user: User, fmt: ImportFormat, raw: bytes, apply_target_rules: bool,
    *, dry_run: bool,
) -> dict:
    return import_csv(
        db,
        user_id=user.id,
        raw_bytes=raw,
        column_map=fmt.column_map,
        default_project_code=fmt.default_project_code,
        separator=fmt.separator,
        encoding=fmt.encoding,
        date_format=fmt.date_format,
        time_format=fmt.time_format,
        transforms=fmt.transforms or [],
        target_rules=fmt.target_rules or [],
        apply_target_rules=apply_target_rules,
        dry_run=dry_run,
    )


@router.post("/import/preview", response_class=HTMLResponse)
async def import_preview(
    request: Request,
    format_id: int = Form(...),
    file: UploadFile = File(...),
    apply_target_rules: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Dry-run the import and show what would be created — no rows are written.
    The uploaded CSV is carried into the confirm form (base64) so the actual
    import doesn't require re-selecting the file."""
    user = _require_login(request, db)
    fmt = db.get(ImportFormat, format_id)
    if fmt is None or (not fmt.is_global and fmt.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="format not found")
    raw = await file.read()
    formats = _visible_formats(db, user)
    try:
        result = _run_import(db, user, fmt, raw, apply_target_rules, dry_run=True)
    except ValueError as e:
        return templates.TemplateResponse(
            "import_run.html",
            _ctx(request, user, formats=formats, result=None, error=str(e), fmt=fmt),
            status_code=400,
        )
    return templates.TemplateResponse(
        "import_run.html",
        _ctx(
            request, user, formats=formats, result=None, error=None, fmt=fmt,
            preview=result,
            preview_b64=base64.b64encode(raw).decode("ascii"),
            preview_format_id=format_id,
            preview_apply_target_rules=apply_target_rules,
        ),
    )


@router.post("/import", response_class=HTMLResponse)
async def import_run(
    request: Request,
    format_id: int = Form(...),
    file: UploadFile | None = File(None),
    raw_b64: str = Form(""),
    apply_target_rules: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _require_login(request, db)
    fmt = db.get(ImportFormat, format_id)
    if fmt is None or (not fmt.is_global and fmt.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="format not found")

    # The CSV comes either freshly uploaded, or carried over from the preview
    # step as base64 in a hidden field (so no re-upload is needed to confirm).
    formats = _visible_formats(db, user)
    if raw_b64:
        try:
            raw = base64.b64decode(raw_b64)
        except (binascii.Error, ValueError):
            return templates.TemplateResponse(
                "import_run.html",
                _ctx(request, user, formats=formats, result=None,
                     error="Vorschau-Daten konnten nicht gelesen werden — bitte erneut hochladen.",
                     fmt=fmt),
                status_code=400,
            )
    elif file is not None:
        raw = await file.read()
    else:
        return templates.TemplateResponse(
            "import_run.html",
            _ctx(request, user, formats=formats, result=None,
                 error="Keine Datei ausgewählt.", fmt=fmt),
            status_code=400,
        )

    try:
        result = _run_import(db, user, fmt, raw, apply_target_rules, dry_run=False)
    except ValueError as e:
        return templates.TemplateResponse(
            "import_run.html",
            _ctx(request, user, formats=formats, result=None, error=str(e), fmt=fmt),
            status_code=400,
        )

    # Remember the uploaded CSV as the format's sample (only if none stored yet),
    # so preview & AI keep working on the edit screen without a re-upload.
    if not fmt.sample_data:
        fmt.sample_data = _trim_sample(raw.decode("utf-8", errors="replace"))
        db.add(fmt)
        db.commit()

    return templates.TemplateResponse(
        "import_run.html",
        _ctx(request, user, formats=formats, result=result, error=None, fmt=fmt),
    )

