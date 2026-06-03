from enum import StrEnum


class SyncTarget(StrEnum):
    JIRA = "jira"
    SALESFORCE = "salesforce"
    BCS = "bcs"
    INTERN = "intern"
    NONE = "none"


class SyncStatus(StrEnum):
    PENDING = "pending"
    EXPORTED = "exported"
    SYNCED = "synced"
    MANUALLY_SYNCED = "manually_synced"
    FAILED = "failed"
    SKIPPED = "skipped"


class EntrySource(StrEnum):
    MANUAL = "manual"
    API = "api"
    CSV = "csv"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
