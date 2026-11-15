"""Domain enumerations persisted as strings."""

from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class InvestigationStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AuthorizationBasis(StrEnum):
    """Lawful basis / authorization declared by the investigator."""

    NUMBER_HOLDER_CONSENT = "number_holder_consent"
    OWN_NUMBER = "own_number"
    FRAUD_PREVENTION = "fraud_prevention"
    SECURITY_INCIDENT_RESPONSE = "security_incident_response"
    LEGAL_OBLIGATION = "legal_obligation"
    LAW_ENFORCEMENT_REQUEST = "law_enforcement_request"
    JOURNALISTIC_RESEARCH = "journalistic_research"
    AUTHORIZED_PENETRATION_TEST = "authorized_penetration_test"


class JobType(StrEnum):
    PROFILE_DISCOVERY = "profile_discovery"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING)


class VerificationStatus(StrEnum):
    VERIFIED_PUBLIC_REFERENCE = "VERIFIED_PUBLIC_REFERENCE"
    PUBLIC_CANDIDATE = "PUBLIC_CANDIDATE"
    UNVERIFIED = "UNVERIFIED"
    INACCESSIBLE = "INACCESSIBLE"
    NOT_FOUND = "NOT_FOUND"
    RESTRICTED = "RESTRICTED"
    ERROR = "ERROR"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class DiscoverySource(StrEnum):
    SEARCH_INDEX = "search_index"
    SEARCH_SNIPPET_EXTRACTION = "search_snippet_extraction"
    USER_PROVIDED = "user_provided"
    OFFICIAL_API = "official_api"
    GENERATED_OFFICIAL_LINK = "generated_official_link"


class EvidenceSourceType(StrEnum):
    SEARCH_RESULT = "search_result"
    LIVE_PROFILE_PAGE = "live_profile_page"
    OFFICIAL_PLATFORM_API = "official_platform_api"
    USER_SUBMISSION = "user_submission"
    GENERATED_LINK = "generated_link"


class SourceReliability(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"


class ValidationStatus(StrEnum):
    VALID = "VALID"
    POSSIBLE_NOT_VALID = "POSSIBLE_NOT_VALID"
    INVALID = "INVALID"
