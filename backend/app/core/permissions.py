"""Role-based access control: permission catalogue and built-in roles."""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    PHONE_VALIDATE = "phone:validate"
    INVESTIGATION_CREATE = "investigation:create"
    INVESTIGATION_READ = "investigation:read"
    INVESTIGATION_DELETE = "investigation:delete"
    INVESTIGATION_READ_ANY = "investigation:read_any"
    DISCOVERY_RUN = "discovery:run"
    PROFILE_VERIFY = "profile:verify"
    REPORT_READ = "report:read"
    APIKEY_MANAGE = "apikey:manage"
    SELF_DATA_MANAGE = "self:data"
    ADMIN_USERS = "admin:users"
    ADMIN_AUDIT = "admin:audit"
    ADMIN_HEALTH = "admin:health"
    TELEGRAM_ACT_AS = "telegram:act_as"


ANALYST_PERMISSIONS: frozenset[Permission] = frozenset({
    Permission.PHONE_VALIDATE,
    Permission.INVESTIGATION_CREATE,
    Permission.INVESTIGATION_READ,
    Permission.INVESTIGATION_DELETE,
    Permission.DISCOVERY_RUN,
    Permission.PROFILE_VERIFY,
    Permission.REPORT_READ,
    Permission.APIKEY_MANAGE,
    Permission.SELF_DATA_MANAGE,
})

BUILTIN_ROLES: dict[str, frozenset[Permission]] = {
    "admin": frozenset(Permission) - {Permission.TELEGRAM_ACT_AS},
    "analyst": ANALYST_PERMISSIONS,
    "auditor": frozenset({Permission.ADMIN_AUDIT, Permission.ADMIN_HEALTH, Permission.SELF_DATA_MANAGE}),
    "service_telegram": frozenset({Permission.TELEGRAM_ACT_AS}),
}

DEFAULT_ROLE = "analyst"


def parse_permissions(values: list[str] | None) -> frozenset[Permission]:
    result: set[Permission] = set()
    for value in values or []:
        try:
            result.add(Permission(value))
        except ValueError:
            continue  # unknown permission strings grant nothing
    return frozenset(result)
