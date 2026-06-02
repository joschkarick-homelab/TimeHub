"""Target-dependent sync fields (Phase 1).

A single code-defined registry declares which extra fields each sync target
needs, at which level (project or entry), whether they are required, and how to
validate them. Everything else — conditional rendering in the UI, validation,
and the "ready to sync" status — derives from this registry, so adding a field
later means one entry here.

Storage uses the existing JSON columns (no schema change):
  * project-level values  -> Project.sync_metadata[target][key]
  * entry-level values    -> TimeEntry.sync_metadata_override[target][key]
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Targets that never need a sync push, so their entries are always "ready".
NON_SYNC_TARGETS = {"intern", "none"}


@dataclass(frozen=True)
class SyncField:
    key: str  # storage key inside sync_metadata[target]
    label: str  # German UI label
    level: str  # "project" | "entry"
    required: bool = False
    pattern: str | None = None  # full-match regex for validation
    placeholder: str = ""
    help: str = ""
    # entry fields may fall back to a project-level value when left empty
    inherit_from_project: str | None = None  # project field key to inherit from


_JIRA_ISSUE = r"[A-Z][A-Z0-9]+-\d+"

# The registry. Keep targets in sync with models._enums.SyncTarget.
TARGET_FIELDS: dict[str, list[SyncField]] = {
    "jira": [
        SyncField(
            key="default_issue",
            label="Standard-Ticket (optional)",
            level="project",
            pattern=_JIRA_ISSUE,
            placeholder="ABC-1",
            help="Wird für neue Einträge dieses Projekts vorbelegt.",
        ),
        SyncField(
            key="issue_key",
            label="Jira-Ticket",
            level="entry",
            required=True,
            pattern=_JIRA_ISSUE,
            placeholder="ABC-123",
            help="Ticket, auf das die Zeit gebucht wird.",
            inherit_from_project="default_issue",
        ),
    ],
    "salesforce": [
        SyncField(
            key="assignment_id",
            label="Salesforce Projektbesetzung",
            level="project",
            required=True,
            pattern=r"[a-zA-Z0-9]{15,18}",
            placeholder="a01...",
            help="Id der Salesforce-Projektbesetzung; daraus werden Projekt und Mitarbeiter abgeleitet.",
        ),
        SyncField(
            key="remote",
            label="Remote (Salesforce)",
            level="entry",
            required=False,
            placeholder="true / false",
            help="Wird beim Sync in Zeiterfassung__c.Remote__c übernommen. Übliche Werte: true/false, 1/0, ja/nein. Üblicherweise per Import-Transformation aus der Quell-CSV befüllt.",
        ),
    ],
    "bcs": [
        SyncField(
            key="subject",
            label="BCS Subject",
            level="entry",
            required=True,
            placeholder="z. B. Support",
            help="Vorgang, auf den im BCS gebucht wird.",
        ),
        SyncField(
            key="task",
            label="BCS Task",
            level="entry",
            required=True,
            placeholder="z. B. Analyse",
            help="Task innerhalb des Subjects.",
        ),
    ],
    "intern": [],
    "none": [],
}


def fields_for(target: str, level: str | None = None) -> list[SyncField]:
    fields = TARGET_FIELDS.get(target, [])
    if level is None:
        return list(fields)
    return [f for f in fields if f.level == level]


def project_fields(target: str) -> list[SyncField]:
    return fields_for(target, "project")


def entry_fields(target: str) -> list[SyncField]:
    return fields_for(target, "entry")


def effective_target(entry, project) -> str:
    """Entry override wins over the project default."""
    return entry.sync_target_override or project.default_sync_target


def _get(md: dict | None, target: str, key: str) -> str | None:
    val = ((md or {}).get(target) or {}).get(key)
    return val or None


def project_value(project, target: str, key: str) -> str | None:
    return _get(project.sync_metadata, target, key)


def entry_value(entry, project, field: SyncField, target: str) -> str | None:
    """Resolve an entry-level field value, inheriting a project default when set."""
    val = _get(entry.sync_metadata_override, target, field.key)
    if not val and field.inherit_from_project:
        val = _get(project.sync_metadata, target, field.inherit_from_project)
    return val


def validate_value(field: SyncField, raw: str) -> str | None:
    """Return an error string if a non-empty value is malformed, else None."""
    if not raw:
        return None
    if field.pattern and not re.fullmatch(field.pattern, raw):
        return f"{field.label}: Format ungültig (erwartet z. B. {field.placeholder or field.pattern})"
    return None


def apply_fields(
    existing_md: dict | None, target: str, fields: list[SyncField], values: dict
) -> tuple[dict, list[str]]:
    """Merge submitted values into the target's metadata sub-dict.

    Returns the new top-level metadata dict (a fresh object, so SQLAlchemy's
    JSON change tracking fires) and a list of format warnings. Empty values
    clear the key; other targets' metadata is left untouched.
    """
    sub = dict((existing_md or {}).get(target, {}))
    warnings: list[str] = []
    for f in fields:
        raw = (values.get(f.key) or "").strip()
        if raw:
            sub[f.key] = raw
            err = validate_value(f, raw)
            if err:
                warnings.append(err)
        else:
            sub.pop(f.key, None)
    md = {**(existing_md or {})}
    if sub:
        md[target] = sub
    else:
        md.pop(target, None)
    return md, warnings


def entry_sync_status(entry, project) -> dict:
    """Whether an entry has everything its effective target needs to sync.

    Considers both project-level and entry-level required fields, plus format
    validity of any value that is present. intern/none never need a push.
    """
    target = effective_target(entry, project)
    if target in NON_SYNC_TARGETS:
        return {"target": target, "needs_sync": False, "ready": True, "missing": []}

    missing: list[str] = []
    for f in fields_for(target):
        if f.level == "entry":
            val = entry_value(entry, project, f, target)
        else:
            val = project_value(project, target, f.key)
        if f.required and not val:
            missing.append(f.label)
        elif val and validate_value(f, val):
            missing.append(f"{f.label} (Format)")
    return {"target": target, "needs_sync": True, "ready": not missing, "missing": missing}


def project_sync_status(project) -> dict:
    """Missing required project-level fields for the project's default target."""
    target = project.default_sync_target
    if target in NON_SYNC_TARGETS:
        return {"target": target, "needs_sync": False, "ready": True, "missing": []}
    missing: list[str] = []
    for f in project_fields(target):
        val = project_value(project, target, f.key)
        if f.required and not val:
            missing.append(f.label)
        elif val and validate_value(f, val):
            missing.append(f"{f.label} (Format)")
    return {"target": target, "needs_sync": True, "ready": not missing, "missing": missing}


def registry_json(level: str) -> dict[str, list[dict]]:
    """Serialize the registry for embedding in templates / client JS."""
    out: dict[str, list[dict]] = {}
    for target, fields in TARGET_FIELDS.items():
        out[target] = [
            {
                "key": f.key,
                "label": f.label,
                "required": f.required,
                "pattern": f.pattern or "",
                "placeholder": f.placeholder,
                "help": f.help,
                "inherit_from_project": f.inherit_from_project or "",
            }
            for f in fields
            if f.level == level
        ]
    return out


def entry_field_targets() -> set[str]:
    """Namespaced import/export targets for every entry-level sync field,
    e.g. {"sync:jira.issue_key", "sync:bcs.subject", ...}."""
    return {
        f"sync:{target}.{f.key}"
        for target, fields in TARGET_FIELDS.items()
        for f in fields
        if f.level == "entry"
    }


def parse_target_token(token: str) -> tuple[str, str] | None:
    """Split "sync:<target>.<key>" → (target, key); None for plain targets."""
    if not token.startswith("sync:"):
        return None
    rest = token[len("sync:"):]
    if "." not in rest:
        return None
    target, key = rest.split(".", 1)
    return target, key


def target_label(token: str) -> str:
    """Human label for a mapping target token (used in the format UI)."""
    parsed = parse_target_token(token)
    if parsed is None:
        return token
    target, key = parsed
    for f in TARGET_FIELDS.get(target, []):
        if f.key == key:
            return f"{target}: {f.label}"
    return token
