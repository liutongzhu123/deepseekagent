"""Resolve registration-region manager candidates without assigning or notifying."""
from __future__ import annotations

import re
from typing import Any


def normalize_region(value: Any) -> str:
    """Normalize whitespace only; do not infer province/city hierarchy."""
    return re.sub(r"\s+", "", str(value or "")).casefold()


def preview(region: str, config: dict[str, Any], config_revision: int | None = None) -> dict[str, Any]:
    """Return a review candidate; never persist an assignment or send a notice."""
    result = {
        "status": "unconfigured",
        "candidate_manager_id": "",
        "route_type": "none",
        "config_revision": config_revision,
        "requires_human_review": True,
        "assigned": False,
        "notification_sent": False,
    }
    if not isinstance(config, dict) or not config.get("enabled"):
        return result

    target = normalize_region(region)
    if not target:
        result["status"] = "unmatched"
        result["route_type"] = "missing_region"
        return result
    entries = config.get("region_managers")
    if not isinstance(entries, list):
        result["status"] = "unavailable"
        return result
    matches = [
        entry for entry in entries
        if isinstance(entry, dict) and normalize_region(entry.get("region")) == target
    ]
    if len(matches) > 1:
        result["status"] = "ambiguous"
        result["route_type"] = "duplicate_mapping"
        return result
    if len(matches) == 1:
        manager_id = str(matches[0].get("manager_id") or "").strip()
        if manager_id:
            result.update(status="matched_candidate", candidate_manager_id=manager_id, route_type="exact_region")
            return result
    backup = str(config.get("backup_manager_id") or "").strip()
    if backup:
        result.update(status="backup_candidate", candidate_manager_id=backup, route_type="fallback")
    else:
        result["status"] = "unmatched"
        result["route_type"] = "no_fallback_configured"
    return result
