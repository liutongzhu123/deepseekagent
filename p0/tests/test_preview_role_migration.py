import csv
import json
import os
from io import StringIO

import pytest

from scripts import preview_role_migration as migration


def test_preview_suggests_only_documented_roles_and_flags_reviewer_for_confirmation():
    source = StringIO(
        "username,role,department,projects,active\n"
        "admin-a,admin,管理部,P-A,yes\n"
        'sales-a,sales,销售部,"P-A, P-B, P-A",true\n'
        "reviewer-a,reviewer,审核组,P-C,true\n"
        "viewer-a,viewer,访客组,,true\n"
        "old-a,unmapped,,P-D,true\n"
    )

    rows = migration.build_preview(source)

    assert [row["suggested_role"] for row in rows] == [
        "admin", "business_manager", "", "", ""
    ]
    assert rows[1]["source_projects_for_review"] == ["P-A", "P-B"]
    assert rows[2]["review_permission_status"] == "must_confirm_per_account; no_grant_included"
    assert rows[2]["decision"] == "manual_role_mapping_required"
    assert rows[3]["decision"] == "manual_role_mapping_required"
    assert all(row["scope_status"] == "must_confirm_per_account" for row in rows)
    assert all(row["preview_only"] is True for row in rows)


def test_inactive_accounts_are_never_proposed_for_activation():
    row = migration.build_preview(StringIO("username,role,active\nold-admin,admin,false\n"))[0]

    assert row["suggested_role"] == "admin"
    assert row["decision"] == "inactive_account_requires_separate_review"


@pytest.mark.parametrize(
    "csv_text, message",
    [
        ("username,role,email\nu,sales,u@example.test\n", "unsupported columns: email"),
        ("username,role,password\nu,sales,secret\n", "unsupported columns: password"),
        ("username,role,role\nu,sales,sales\n", "duplicate"),
        ("username,role,active\nu,sales,maybe\n", "active must be"),
        ("username,role\nu,sales,extra\n", "more fields than the header"),
        ("username,role\nUser,sales\nuser,admin\n", "duplicate username"),
        ("username,role\n", "no account rows"),
    ],
)
def test_invalid_or_sensitive_csv_inputs_fail_closed(csv_text, message):
    with pytest.raises(ValueError, match=message):
        migration.build_preview(StringIO(csv_text))


def test_cli_writes_new_mode_0600_json_only_outside_repository(tmp_path, capsys):
    source = tmp_path / "accounts.csv"
    source.write_text("username,role\nsales-a,sales\n", encoding="utf-8")
    source.chmod(0o600)
    output = tmp_path / "preview.json"

    assert migration.main([str(source), "--output", str(output)]) == 0
    assert os.stat(output).st_mode & 0o777 == 0o600
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["preview_only"] is True
    assert report["accounts"][0]["suggested_role"] == "business_manager"
    assert "只读预览" in capsys.readouterr().out


def test_cli_refuses_repository_output_and_overwrite(tmp_path):
    source = tmp_path / "accounts.csv"
    source.write_text("username,role\nsales-a,sales\n", encoding="utf-8")
    source.chmod(0o600)

    assert migration.main([str(source), "--output", str(migration.REPO_ROOT / "preview.json")]) == 1

    existing = tmp_path / "existing.json"
    existing.write_text("keep", encoding="utf-8")
    assert migration.main([str(source), "--output", str(existing)]) == 1
    assert existing.read_text(encoding="utf-8") == "keep"


def test_cli_rejects_group_or_world_readable_input(tmp_path):
    source = tmp_path / "accounts.csv"
    source.write_text("username,role\nsales-a,sales\n", encoding="utf-8")
    source.chmod(0o644)

    assert migration.main([str(source), "--output", str(tmp_path / "preview.json")]) == 1
    assert not (tmp_path / "preview.json").exists()
