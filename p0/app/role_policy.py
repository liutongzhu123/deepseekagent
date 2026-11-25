"""Project-level role policy for the four confirmed CNZ roles.

Legacy roles are deliberately absent. They must be explicitly migrated; an
old role string must never inherit a new role's permissions by accident.
"""

from __future__ import annotations

from collections.abc import Mapping

ROLE_CODES = ("customer", "business_manager", "admin", "technical")
ROLE_LABELS = {
    "customer": "新客户/小微客户",
    "business_manager": "业务经理",
    "admin": "系统管理员",
    "technical": "技术部门",
}

# Default access reflects the duties provided by the customer. Report review
# and standard review are account-specific exceptions because the customer did
# not assign those historical duties to any one of the four roles.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "customer": frozenset(),
    "business_manager": frozenset(
        {
            "rules:read",
            "standard:read",
            "standard:ask",
            "standard:export",
            "quote:parse",
            "quote:generate",
            "quote:download",
            "crm:read",
            "crm:prices:manage",
            "system:status",
            "customer-service:order:review",
            "customer-service:conversation:manage",
            "customer-service:quote:manage",
        }
    ),
    "admin": frozenset(
        {
            "rules:read",
            "standard:read",
            "standard:parse",
            "standard:export",
            "standard:publish",
            "standard:project",
            "standard:ask",
            "feedback:read",
            "kb:rebuild",
            "kb:stats",
            "user:manage",
            "audit:read",
            "audit:export",
            "config:read",
            "config:manage",
            "config:manage:knowledge",
            "config:manage:qualification",
            "config:manage:mapping",
            "config:manage:discount",
            "config:manage:risk",
            "config:manage:report",
            "config:manage:standard",
            "config:manage:quote",
            "config:manage:business",
            "system:status",
            "customer-service:registration:review",
            "crm:read",
            "crm:base:manage",
        }
    ),
    "technical": frozenset(
        {
            "rules:read",
            "system:status",
            "schedule:read",
            "schedule:manage",
            "rush-rule:manage",
            "standard:parse",
            "standard:read",
            "standard:ask",
        }
    ),
}

# Only these capabilities may be assigned as an account-specific exception.
# The UI/API must collect an approver and reason for every assignment.
ACCOUNT_REVIEW_EXCEPTIONS = frozenset(
    {
        "report:read",
        "report:audit",
        "standard:review",
    }
)


def valid_account_review_grants(user: Mapping[str, object] | None) -> frozenset[str]:
    """Return only complete, approved, allow-listed review grants."""
    if not isinstance(user, Mapping):
        return frozenset()
    # Review exceptions belong to named internal staff accounts only.  Even
    # if malformed or legacy data contains a grant on a customer identity,
    # never expose it as an effective permission.
    if user.get("role") == "customer":
        return frozenset()
    raw = user.get("permission_grants")
    permissions: set[str] = set()
    session_grants = user.get("_approved_review_permissions")
    if isinstance(session_grants, list):
        permissions.update(
            permission for permission in session_grants
            if isinstance(permission, str) and permission in ACCOUNT_REVIEW_EXCEPTIONS
        )
    if not isinstance(raw, list):
        return frozenset(permissions)
    for grant in raw:
        if not isinstance(grant, Mapping):
            continue
        permission = grant.get("permission")
        if (
            permission in ACCOUNT_REVIEW_EXCEPTIONS
            and str(grant.get("granted_by") or "").strip()
            and str(grant.get("granted_at") or "").strip()
            and str(grant.get("reason") or "").strip()
        ):
            permissions.add(str(permission))
    return frozenset(permissions)


def has_permission(user: Mapping[str, object] | None, action: str) -> bool:
    """Fail closed for unknown roles/actions and require metadata for exceptions."""
    if not isinstance(user, Mapping):
        return False
    if not user.get("active", True):
        return False
    role = user.get("role")
    if role not in ROLE_PERMISSIONS:
        return False
    if action in ROLE_PERMISSIONS[role]:
        return True
    return action in valid_account_review_grants(user)


def normalize_review_permissions(value: object) -> list[str]:
    """Validate an explicit per-account review permission selection."""
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("审核权限必须是权限列表")
    permissions: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in ACCOUNT_REVIEW_EXCEPTIONS:
            raise ValueError("只能逐账号授予报告审核/标准复核权限")
        if item not in permissions:
            permissions.append(item)
    if len(permissions) > len(ACCOUNT_REVIEW_EXCEPTIONS):
        raise ValueError("审核权限数量无效")
    return permissions
