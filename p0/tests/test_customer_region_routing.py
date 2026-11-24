from app import customer_region_routing


def test_exact_region_match_is_preview_only():
    result = customer_region_routing.preview("上 海", {
        "enabled": True,
        "region_managers": [{"region": "上海", "manager_id": "manager-a"}],
        "backup_manager_id": "backup-manager",
    }, config_revision=7)

    assert result == {
        "status": "matched_candidate", "candidate_manager_id": "manager-a",
        "route_type": "exact_region", "config_revision": 7,
        "requires_human_review": True, "assigned": False, "notification_sent": False,
    }


def test_unmatched_region_uses_only_explicit_backup_candidate():
    result = customer_region_routing.preview("浙江省", {
        "enabled": True, "region_managers": [], "backup_manager_id": "backup-manager",
    })

    assert result["status"] == "backup_candidate"
    assert result["candidate_manager_id"] == "backup-manager"
    assert result["route_type"] == "fallback"
    assert result["assigned"] is False
    assert result["notification_sent"] is False


def test_disabled_or_unmatched_routing_never_invents_an_assignee():
    disabled = customer_region_routing.preview("上海", {
        "enabled": False, "region_managers": [], "backup_manager_id": "",
    })
    unmatched = customer_region_routing.preview("上海", {
        "enabled": True, "region_managers": [], "backup_manager_id": "",
    })

    assert disabled["status"] == "unconfigured"
    assert disabled["candidate_manager_id"] == ""
    assert unmatched["status"] == "unmatched"
    assert unmatched["candidate_manager_id"] == ""


def test_duplicate_mapping_fails_to_manual_review():
    result = customer_region_routing.preview("上海", {
        "enabled": True,
        "region_managers": [
            {"region": "上海", "manager_id": "manager-a"},
            {"region": "上 海", "manager_id": "manager-b"},
        ],
        "backup_manager_id": "backup-manager",
    })

    assert result["status"] == "ambiguous"
    assert result["candidate_manager_id"] == ""
