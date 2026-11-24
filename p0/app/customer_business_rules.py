"""Read-only candidate evaluation for configured customer-service rules.

The configured rules have not been approved for production decisions. Results
from this module are traceable candidates for an employee to review, never an
automated quote, identity decision, or customer-facing response.
"""
from __future__ import annotations

from typing import Any


def evaluate_candidates(
    business: dict[str, Any], *, company_name: str, subtotal: float, rush_requested: bool
) -> dict[str, Any]:
    """Return deterministic rule candidates while always requiring human review.

    F1: exact blacklist and whitelist overlap is surfaced as a conflict.
    F2: company-list matching is exact after trimming; VIP keywords are
    substring candidates only and never establish verified customer identity.
    F3: malformed/missing configured sections fail to the caller for rejection.
    F4: candidate values never authorize or calculate a customer quote.
    """
    if not isinstance(business, dict):
        raise ValueError("business rules must be an object")
    company = str(company_name or "").strip()
    if not company:
        raise ValueError("company_name is required")
    amount = float(subtotal)
    if amount < 0:
        raise ValueError("subtotal must be non-negative")

    tiers = business.get("discount_tiers")
    if not isinstance(tiers, list) or not tiers:
        raise ValueError("discount_tiers must be a non-empty list")
    matching_tiers = [
        tier for tier in tiers
        if isinstance(tier, dict) and float(tier["min_amount"]) <= amount
    ]
    if not matching_tiers:
        raise ValueError("no discount tier matches subtotal")
    tier = max(matching_tiers, key=lambda item: float(item["min_amount"]))

    blacklist = business.get("blacklist", {}).get("companies", [])
    whitelist_rule = business.get("whitelist", {})
    if not isinstance(whitelist_rule, dict):
        raise ValueError("whitelist rule must be an object")
    whitelist = whitelist_rule.get("companies", [])
    whitelist_keywords = whitelist_rule.get("keywords", [])
    if not isinstance(blacklist, list) or not isinstance(whitelist, list):
        raise ValueError("company lists must be arrays")
    if not isinstance(whitelist_keywords, list):
        raise ValueError("whitelist keywords must be an array")
    blacklisted = company in {str(item).strip() for item in blacklist}
    whitelisted = company in {str(item).strip() for item in whitelist}
    matched_whitelist_keywords = sorted({
        str(keyword).strip()
        for keyword in whitelist_keywords
        if str(keyword).strip() and str(keyword).strip() in company
    })

    intermediary = business.get("intermediary", {})
    if not isinstance(intermediary, dict):
        raise ValueError("intermediary rule must be an object")
    keywords = intermediary.get("keywords", [])
    if not isinstance(keywords, list):
        raise ValueError("intermediary keywords must be an array")
    matched_keywords = sorted({
        str(keyword).strip()
        for keyword in keywords
        if str(keyword).strip() and str(keyword).strip() in company
    }) if intermediary.get("enabled", False) else []

    rush = business.get("rush", {})
    if not isinstance(rush, dict):
        raise ValueError("rush rule must be an object")
    rush_candidate = None
    if rush_requested and rush.get("enabled", False):
        rush_candidate = {
            "fee_rate": float(rush["fee_rate"]),
            "lead_days": int(rush["lead_days"]),
        }

    return {
        "decision": "manual_review",
        "auto_apply": False,
        "requires_human_review": True,
        "candidates": {
            "discount_tier": {
                "min_amount": float(tier["min_amount"]),
                "discount": float(tier["discount"]),
            },
            "rush": rush_candidate,
            "blacklist_match": blacklisted,
            "whitelist_match": whitelisted,
            "whitelist_keyword_match": bool(matched_whitelist_keywords),
            "whitelist_keyword_matches": matched_whitelist_keywords,
            "company_list_conflict": blacklisted and whitelisted,
            "intermediary_keywords": matched_keywords,
            "intermediary_block_configured": bool(
                intermediary.get("enabled", False) and intermediary.get("block", False)
            ),
        },
    }
