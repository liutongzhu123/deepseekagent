import os
import tempfile

import pytest

# Keep the test run hermetic: no login, no production data directory, no
# outbound WeKnora calls.  The production systemd unit never sets these.
os.environ.setdefault("P0_AUTH_DISABLED", "1")
os.environ.setdefault("P0_COOKIE_SECURE", "0")
os.environ.setdefault("P0_WEKNORA_REQUIRED", "0")
os.environ.setdefault("P0_DATA_DIR", tempfile.mkdtemp(prefix="p0-test-data-"))


@pytest.fixture(autouse=True)
def map_legacy_test_accounts_to_explicit_project_roles(monkeypatch):
    """Keep pre-migration fixtures meaningful under the four-role policy.

    Reviewer fixtures become technical accounts with explicit, audited review
    grants; sales becomes business_manager; viewer becomes technical. Runtime
    code never accepts these old role codes.
    """
    from app import auth

    create_user = auth.create_user

    def create_project_user(*args, **kwargs):
        original_role = kwargs.get("role")
        if original_role in {"reviewer", "sales", "viewer"}:
            kwargs["role"] = {
                "reviewer": "technical",
                "sales": "business_manager",
                "viewer": "technical",
            }[original_role]
        user = create_user(*args, **kwargs)
        if original_role == "reviewer":
            return auth.set_review_permissions(
                user["username"],
                ["report:read", "report:audit", "standard:review"],
                granted_by="test-fixture-admin",
                reason="test fixture models per-account confirmed legacy duties",
            )
        return user

    monkeypatch.setattr(auth, "create_user", create_project_user)
