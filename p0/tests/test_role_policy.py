from app import role_policy


def test_only_four_customer_roles_exist():
    assert set(role_policy.ROLE_CODES) == {
        "customer", "business_manager", "admin", "technical"
    }
    assert set(role_policy.ROLE_PERMISSIONS) == set(role_policy.ROLE_CODES)


def test_legacy_roles_fail_closed():
    for role in ("reviewer", "sales", "viewer", "owner", "contributor", "unknown"):
        assert not role_policy.has_permission({"role": role}, "standard:ask")
        assert not role_policy.has_permission({"role": role}, "user:manage")


def test_customer_cannot_use_employee_workbench_permissions():
    user = {"role": "customer", "active": True}
    assert not role_policy.has_permission(user, "quote:generate")
    assert not role_policy.has_permission(user, "user:manage")
    assert not role_policy.has_permission(user, "standard:ask")


def test_customer_never_receives_account_specific_review_exceptions():
    user = {
        "role": "customer",
        "permission_grants": [{
            "permission": "standard:review",
            "granted_by": "admin",
            "granted_at": "2026-11-25T12:00:00Z",
            "reason": "misconfiguration must fail closed",
        }],
    }
    assert not role_policy.has_permission(user, "standard:review")
    assert role_policy.valid_account_review_grants(user) == frozenset()


def test_business_manager_can_quote_and_manage_quote_data_but_not_global_rules():
    user = {"role": "business_manager", "active": True}
    assert role_policy.has_permission(user, "quote:generate")
    assert role_policy.has_permission(user, "crm:prices:manage")
    assert not role_policy.has_permission(user, "crm:base:manage")
    assert not role_policy.has_permission(user, "config:manage:discount")
    assert not role_policy.has_permission(user, "report:audit")
    assert role_policy.has_permission(user, "customer-service:registration:review")
    assert role_policy.has_permission(user, "customer-service:order:review")
    assert role_policy.has_permission(user, "customer-service:conversation:manage")


def test_technical_role_is_limited_to_schedule_and_rush_rules():
    user = {"role": "technical", "active": True}
    assert role_policy.has_permission(user, "schedule:manage")
    assert role_policy.has_permission(user, "rush-rule:manage")
    assert not role_policy.has_permission(user, "config:manage:discount")
    assert not role_policy.has_permission(user, "crm:quote:write")
    assert not role_policy.has_permission(user, "standard:review")
    assert not role_policy.has_permission(user, "crm:read")
    assert not role_policy.has_permission(user, "customer-service:order:review")


def test_admin_does_not_implicitly_inherit_report_or_standard_review():
    user = {"role": "admin", "active": True}
    assert role_policy.has_permission(user, "user:manage")
    assert role_policy.has_permission(user, "config:manage:discount")
    assert not role_policy.has_permission(user, "report:audit")
    assert not role_policy.has_permission(user, "standard:review")


def test_review_permission_requires_approved_account_metadata():
    incomplete = {
        "role": "technical",
        "permission_grants": [{"permission": "report:audit", "reason": "approved"}],
    }
    assert not role_policy.has_permission(incomplete, "report:audit")
    approved = {
        "role": "technical",
        "permission_grants": [{
            "permission": "report:audit",
            "granted_by": "system-admin-2",
            "granted_at": "2026-11-24T12:00:00Z",
            "reason": "逐账号确认的历史岗位职责",
        }],
    }
    assert role_policy.has_permission(approved, "report:audit")
    assert not role_policy.has_permission(approved, "standard:review")


def test_inactive_account_cannot_use_exception_grants():
    user = {
        "role": "business_manager",
        "active": False,
        "permission_grants": [{
            "permission": "report:read",
            "granted_by": "admin",
            "granted_at": "2026-11-24T12:00:00Z",
            "reason": "approved",
        }],
    }
    assert not role_policy.has_permission(user, "report:read")


def test_review_permission_input_is_allow_listed_and_deduplicated():
    assert role_policy.normalize_review_permissions(
        ["report:audit", "report:audit", "standard:review"]
    ) == ["report:audit", "standard:review"]
    try:
        role_policy.normalize_review_permissions(["user:manage"])
    except ValueError:
        pass
    else:
        raise AssertionError("non-review permission must be rejected")
