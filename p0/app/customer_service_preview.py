"""Resolve internal quote preview lines against synthetic or read-only CRM facts.

This module never computes a quote total or applies business pricing rules.
Every resolved source reference identifies whether it came from synthetic data.
"""
from __future__ import annotations

import math
import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any


# Failure handling for this preview resolver:
# F1 invalid catalog/request envelope -> reject the preview instead of returning partial facts.
# F2 incomplete request row -> report missing fields and ask for clarification.
# F3 no exact price match -> mark missing; do not infer a nearby price.
# F4 duplicate matching records -> mark ambiguous; do not choose one arbitrarily.
# F5 expired source record -> do not expose its price as current.
# F6 project-level qualification -> never promote it to standard-level qualification.
# F7 simulated upstream outage -> report source_unavailable; do not fall back to fixture facts.
# F8 low parser confidence -> keep source facts visible but require human review.


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _valid_on(record: dict, today: date) -> bool:
    start = str(record.get("valid_from") or "").strip()
    end = str(record.get("valid_until") or "").strip()
    try:
        return (not start or date.fromisoformat(start) <= today) and (not end or today <= date.fromisoformat(end))
    except ValueError:
        return False


def _valid_quantity(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    return isinstance(value, float) and math.isfinite(value) and value > 0


def _source_ref(source_type: str, record: dict) -> dict:
    source = record.get("source") or {}
    return {
        "source_type": source_type,
        "record_id": str(source.get("record_id") or record.get("record_id") or ""),
        "revision": str(source.get("revision") or ""),
        "valid_until": record.get("valid_until") or record.get("end_date") or None,
        "synthetic": source.get("system") == "synthetic-fixture",
    }


def _project_matches(row: dict, project_name: str) -> bool:
    requested = project_name.strip()
    return requested in {
        str(row.get("project_name") or "").strip(),
        str(row.get("project_id") or "").strip(),
    }


def _mapping_for(catalog: dict, item_name: str) -> tuple[str, list[dict], bool]:
    matches = [
        item for item in catalog["name_mappings"]
        if str(item.get("alias") or "").strip() == item_name.strip()
    ]
    canonicals = {str(item.get("canonical") or "").strip() for item in matches}
    canonicals.discard("")
    if len(canonicals) > 1:
        return item_name, [], True
    if not canonicals:
        return item_name, [], False
    if any(item.get("review_status") not in {"approved", "demo-approved"} for item in matches):
        return item_name, [_source_ref("name_mapping", item) for item in matches], True
    canonical = next(iter(canonicals))
    return canonical, [_source_ref("name_mapping", item) for item in matches], False


def _resolve_price(
    catalog: dict, request: dict, item_name: str, today: date
) -> tuple[str, dict | None, list[dict], list[dict]]:
    quantity = request.get("quantity")
    unit = str(request.get("unit") or "").strip()
    is_crm = catalog["dataset"]["kind"] == "crm_snapshot"
    candidates = [
        row for row in catalog["prices"]
        if _project_matches(row, str(request.get("project_name") or ""))
        and str(row.get("item_name") or "").strip() == item_name
        and str(row.get("standard") or "").strip() == str(request.get("standard") or "").strip()
        and str(row.get("unit") or "").strip() == unit
        and (is_crm or float(row.get("minimum_quantity") or 0) <= quantity)
    ]
    sample_name = str(request.get("sample_name") or "").strip()
    sample_match_level = "not_provided"
    if is_crm and sample_name:
        sample_matches = [
            row for row in candidates
            if str(row.get("sample_name") or "").strip() == sample_name
        ]
        if sample_matches:
            candidates = sample_matches
            sample_match_level = "exact"
        elif candidates:
            # Keep the established CRM reconciliation fallback visible for
            # manual review; it is not an exact sample-level match.
            sample_match_level = "fallback"
    valid = [row for row in candidates if _valid_on(row, today)]
    if len(valid) > 1:
        price_candidates = []
        for row in valid:
            try:
                candidate_price = float(row.get("unit_price"))
            except (TypeError, ValueError):
                candidate_price = None
            if candidate_price is not None and (not math.isfinite(candidate_price) or candidate_price <= 0):
                candidate_price = None
            price_candidates.append({
                "sample_name": str(row.get("sample_name") or ""),
                "unit_price": candidate_price,
                "currency": row.get("currency"),
                "tax_inclusive": row.get("tax_inclusive"),
                "reference_quantity": row.get("source_quantity") if is_crm else None,
                "valid_until": row.get("valid_until") or None,
                "record_id": (row.get("source") or {}).get("record_id") or row.get("record_id") or "",
            })
        return "ambiguous", None, [_source_ref("crm_price", row) for row in valid], price_candidates
    if len(valid) == 1:
        row = valid[0]
        try:
            unit_price = float(row.get("unit_price") or 0)
        except (TypeError, ValueError):
            return "ambiguous", None, [_source_ref("crm_price", row)], []
        if not math.isfinite(unit_price) or unit_price <= 0:
            return "missing", None, [_source_ref("crm_price", row)], []
        preview = {
            "unit_price": unit_price,
            "currency": row.get("currency"),
            "tax_inclusive": row.get("tax_inclusive"),
            "valid_until": row.get("valid_until") or None,
        }
        if is_crm:
            preview["reference_quantity"] = row.get("source_quantity")
            preview["sample_match_level"] = sample_match_level
        return "matched", preview, [_source_ref("crm_price", row)], []
    if candidates:
        return "expired", None, [_source_ref("crm_price", row) for row in candidates], []
    return "missing", None, [], []


def _resolve_schedule(catalog: dict, request: dict, today: date) -> tuple[str, dict | None, list[dict]]:
    candidates = [
        row for row in catalog["schedules"]
        if _project_matches(row, str(request.get("project_name") or ""))
    ]
    current = []
    for row in candidates:
        end_date = str(row.get("end_date") or "").strip()
        try:
            if not end_date or date.fromisoformat(end_date) >= today:
                current.append(row)
        except ValueError:
            continue
    if len(current) > 1:
        return "ambiguous", None, [_source_ref("schedule", row) for row in current]
    if len(current) == 1:
        row = current[0]
        preview = {
            "start_date": row.get("start_date", ""),
            "end_date": row.get("end_date", ""),
            "status": row.get("status", ""),
        }
        # CRM status values have no customer-approved meaning yet. Preserve
        # their source facts for a human, but do not promote a future date (or
        # an unknown status label) into a confirmed schedule. The explicit
        # synthetic marker is safe to interpret only inside the demo fixture.
        is_confirmed_demo = (
            catalog.get("dataset", {}).get("kind") == "synthetic"
            and preview["status"] == "synthetic-planned"
            and bool(preview["start_date"])
            and bool(preview["end_date"])
        )
        return (
            ("matched" if is_confirmed_demo else "unknown"),
            preview,
            [_source_ref("schedule", row)],
        )
    if candidates:
        return "expired", None, [_source_ref("schedule", row) for row in candidates]
    return "unknown", None, []


def _resolve_qualification(catalog: dict, request: dict, today: date) -> tuple[str, dict | None, list[dict]]:
    if catalog["dataset"]["kind"] == "crm_snapshot":
        # The current CRM qualification rows have no reliable project scope.
        # Return the same opaque result for empty/nonempty sources so a
        # project-scoped caller cannot infer the global qualification count.
        return "unknown", {
            "scope_level": "unstructured",
            "note": "当前 CRM 适配器未提供可核验的标准/项目范围，需人工核验",
        }, []
    project = str(request.get("project_name") or "")
    standard = str(request.get("standard") or "").strip()
    candidates = [
        row for row in catalog["qualifications"]
        if _project_matches(row, project)
        and str(row.get("scope_level") or "") == "standard"
        and str(row.get("standard") or "").strip() == standard
    ]
    scope_status = "standard_scope"
    if not candidates:
        candidates = [
            row for row in catalog["qualifications"]
            if _project_matches(row, project)
            and str(row.get("scope_level") or "") == "project"
        ]
        scope_status = "project_scope_only"
    current = [row for row in candidates if _valid_on(row, today)]
    if len(current) > 1:
        return "ambiguous", None, [_source_ref("qualification", row) for row in current]
    if len(current) == 1:
        row = current[0]
        preview = {
            "certificate_no": row.get("certificate_no", ""),
            "scope_level": row.get("scope_level", ""),
            "standard": row.get("standard") or None,
        }
        return scope_status, preview, [_source_ref("qualification", row)]
    if candidates:
        return "expired", None, [_source_ref("qualification", row) for row in candidates]
    return "not_found", None, []


def resolve_preview(catalog: dict, requests: dict, today: date | None = None) -> dict:
    """Build source-aware facts only; pricing totals and thresholds stay unevaluated."""
    catalog_meta = catalog.get("dataset") if isinstance(catalog, dict) else None
    request_meta = requests.get("dataset") if isinstance(requests, dict) else None
    catalog_kind = catalog_meta.get("kind") if isinstance(catalog_meta, dict) else ""
    source_system = "synthetic-fixture" if catalog_kind == "synthetic" else "crm-readonly"
    catalog_is_valid = (
        catalog_kind == "synthetic"
        and catalog_meta.get("demo_only") is True
        and catalog_meta.get("production_usable") is False
        or catalog_kind == "crm_snapshot"
        and catalog_meta.get("mode") in {"excel", "csv", "base"}
        and catalog_meta.get("demo_only") is False
        and catalog_meta.get("production_usable") is False
    )
    if (
        not isinstance(catalog, dict)
        or not isinstance(catalog_meta, dict)
        or not catalog_is_valid
        or not isinstance(requests, dict)
        or not isinstance(request_meta, dict)
        or request_meta.get("kind") not in {"synthetic", "parsed_quote"}
        or request_meta.get("demo_only") is not True
        or not isinstance(requests.get("lines"), list)
    ):
        raise ValueError("合成目录或询价样例标识无效")
    for key in ("prices", "schedules", "qualifications", "name_mappings"):
        if not isinstance(catalog.get(key), list) or any(not isinstance(row, dict) for row in catalog[key]):
            raise ValueError(f"合成目录 {key} 必须是列表")
    catalog_sources = [
        record.get("source")
        for key in ("prices", "schedules", "qualifications", "name_mappings")
        for record in catalog[key]
        if isinstance(record, dict)
    ]
    if any(not isinstance(source, dict) or source.get("system") != source_system for source in catalog_sources):
        raise ValueError("目录来源标记无效")

    current_day = today or _today()
    lines = []
    for index, request in enumerate(requests["lines"]):
        if not isinstance(request, dict):
            raise ValueError(f"第 {index + 1} 条询价必须是对象")
        line_id = str(request.get("line_id") or f"SYN-LINE-{index + 1:03d}")
        project = str(request.get("project_name") or "").strip()
        item = str(request.get("item_name") or "").strip()
        sample_name = str(request.get("sample_name") or "").strip()
        standard = str(request.get("standard") or "").strip()
        quantity = request.get("quantity")
        unit = str(request.get("unit") or "").strip()
        missing = []
        if not project:
            missing.append("检测项目")
        if not item:
            missing.append("检验项目")
        if not standard:
            missing.append("标准号")
        if not _valid_quantity(quantity):
            missing.append("有效样品数量")
            quantity = None
        if not unit:
            missing.append("单位")

        confidence = request.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or confidence < 0
            or confidence > 1
        ):
            raise ValueError(f"第 {index + 1} 条询价的解析置信度无效")
        low_confidence = confidence is not None and confidence < 0.85

        mapped_item, mapping_refs, mapping_ambiguous = _mapping_for(catalog, item)
        price_candidates = []
        if mapping_ambiguous:
            price_status, price_preview, price_refs = "ambiguous", None, []
        elif missing:
            price_status, price_preview, price_refs = "missing", None, []
        elif request.get("simulate_source_unavailable") is True:
            price_status, price_preview, price_refs = "source_unavailable", None, []
        else:
            price_status, price_preview, price_refs, price_candidates = _resolve_price(
                catalog, request, mapped_item, current_day
            )

        if missing or request.get("simulate_source_unavailable") is True:
            schedule_status, schedule_preview, schedule_refs = (
                ("source_unavailable", None, []) if request.get("simulate_source_unavailable") is True
                else ("unknown", None, [])
            )
            qualification_status, qualification_preview, qualification_refs = (
                ("source_unavailable", None, []) if request.get("simulate_source_unavailable") is True
                else ("not_found", None, [])
            )
        else:
            schedule_status, schedule_preview, schedule_refs = _resolve_schedule(catalog, request, current_day)
            qualification_status, qualification_preview, qualification_refs = _resolve_qualification(
                catalog, request, current_day
            )

        source_refs = mapping_refs + price_refs + schedule_refs + qualification_refs
        missing_facts = list(missing)
        for status, label in (
            (price_status, "有效 CRM 价格"),
            (schedule_status, "当前排期"),
            (qualification_status, "资质范围"),
        ):
            if status in {"missing", "expired", "unknown", "not_found", "source_unavailable", "ambiguous"}:
                missing_facts.append(label)
        if missing:
            action = "ask_customer"
        elif low_confidence or any(status in {"expired", "unknown", "not_found", "source_unavailable", "ambiguous"} for status in (
            price_status, schedule_status, qualification_status
        )) or price_status == "missing":
            action = "manual_review"
        else:
            action = "preview_only"
        if low_confidence:
            missing_facts.append("解析置信度低于 0.85，需人工复核")
        if price_preview and not price_preview.get("valid_until"):
            missing_facts.append("价格有效期未提供")
        if price_preview and (price_preview.get("currency") is None or price_preview.get("tax_inclusive") is None):
            missing_facts.append("币种或含税口径待确认")
        if price_preview and catalog_kind == "crm_snapshot":
            missing_facts.append("CRM价格行数量未定义为最低起订量，需人工核验数量适用性")
            if price_preview.get("sample_match_level") == "fallback":
                missing_facts.append("样品名称未精确命中，采用兼容回退匹配，需人工确认")
            elif price_preview.get("sample_match_level") == "not_provided":
                missing_facts.append("询价未提供样品名称，无法做样品级精确匹配")
        if schedule_preview and (not schedule_preview.get("start_date") or not schedule_preview.get("end_date")):
            missing_facts.append("排期日期不完整")
        if any(fact in missing_facts for fact in (
            "价格有效期未提供", "币种或含税口径待确认", "排期日期不完整",
            "CRM价格行数量未定义为最低起订量，需人工核验数量适用性",
            "样品名称未精确命中，采用兼容回退匹配，需人工确认",
            "询价未提供样品名称，无法做样品级精确匹配",
        )):
            action = "manual_review"

        lines.append({
            "line_id": line_id,
            "matched_item_name": mapped_item if not mapping_ambiguous else None,
            "request": {
                "project_name": project,
                "item_name": item,
                "sample_name": sample_name,
                "standard": standard,
                "quantity": quantity,
                "unit": unit,
            },
            "price_status": price_status,
            "price_preview": price_preview,
            "price_candidates": price_candidates,
            "schedule_status": schedule_status,
            "schedule_preview": schedule_preview,
            "qualification_status": qualification_status,
            "qualification_preview": qualification_preview,
            "source_refs": source_refs,
            "missing_fields": missing_facts,
            "match_confidence": (
                1.0
                if price_status == "matched"
                and (catalog_kind == "synthetic" or price_preview.get("sample_match_level") == "exact")
                else None
            ),
            "input_evidence": {
                "source_row": request.get("source_row"),
                "parse_confidence": confidence,
            },
            "action": action,
        })

    input_origin = requests["dataset"]["kind"]
    is_synthetic = catalog_kind == "synthetic"
    warnings = (
        [
            "全量数据均为合成演示值，不得用于真实报价。",
            "本预览只匹配事实来源，不计算总价、不评估阈值，也不应用折扣规则。",
        ]
        if is_synthetic
        else [
            f"数据来自当前配置的 CRM 只读适配器（{catalog_meta['mode']}）；不代表 CRM API 已接通或客户验收。",
            "本预览不计算总价、不评估阈值、不应用折扣规则，也不写回 CRM。",
        ]
    )
    if input_origin == "parsed_quote":
        warnings.append("解析明细仅用于本次内部预览，不由预览接口保存。")
    return {
        "preview_id": f"{'SYN' if is_synthetic else 'CRM'}-PREVIEW-{uuid.uuid4().hex[:12]}",
        "environment": "synthetic" if is_synthetic else "internal",
        "input_origin": "parsed_quote" if input_origin == "parsed_quote" else "synthetic_fixture",
        "demo_only": is_synthetic,
        "source_mode": catalog_meta.get("mode") if not is_synthetic else None,
        "source_revision": catalog_meta.get("revision") if not is_synthetic else None,
        "status": "manual_review",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rule_snapshot": {
            "rule_set_id": "SYN-RULES-UNAPPROVED" if is_synthetic else "CRM-RULES-NOT-EVALUATED",
            "version": str(catalog["dataset"].get("revision") or catalog["dataset"].get("version", "unknown")),
            "approved": False,
            "threshold_policy": "unconfirmed",
        },
        "suggested_action": "internal_preview_only",
        "warnings": warnings,
        "lines": lines,
        "decision_summary": {
            "total_preview": None,
            "currency": "CNY" if is_synthetic else None,
            "calculation_status": "not_calculated",
            "threshold_result": "not_evaluated",
        },
    }


def crm_snapshot_catalog(snapshot: Any) -> dict:
    """Convert the existing read-only CRM snapshot to a provenance-safe view."""
    mode = str(getattr(snapshot, "mode", ""))
    if mode not in {"excel", "csv", "base"}:
        raise ValueError("仅支持配置好的 CRM Excel/CSV/基础库只读快照")

    groups = {
        "prices": [],
        "schedules": [],
        "qualifications": [],
        "name_mappings": [],
    }
    for price in getattr(snapshot, "prices", []):
        groups["prices"].append({
            "project_id": price.project_id,
            "project_name": price.project_name,
            "sample_name": price.sample_name,
            "item_name": price.item_name,
            "standard": price.standard,
            "unit": price.unit,
            "source_quantity": price.quantity,
            "unit_price": price.unit_price,
            "currency": None,
            "tax_inclusive": None,
            "valid_until": price.valid_until,
        })
    for schedule in getattr(snapshot, "schedules", []):
        groups["schedules"].append({
            "project_id": schedule.project_id,
            "project_name": schedule.project_name,
            "start_date": schedule.start_date,
            "end_date": schedule.end_date,
            "status": schedule.status,
        })
    for qualification in getattr(snapshot, "qualifications", []):
        groups["qualifications"].append({
            "name": qualification.name,
            "certificate_no": qualification.certificate_no,
            "scope": qualification.scope,
            "valid_until": qualification.valid_until,
            "status": qualification.status,
            "scope_level": "unstructured",
            "project_id": "",
            "standard": "",
        })
    for mapping in getattr(snapshot, "name_mappings", []):
        groups["name_mappings"].append({
            "alias": mapping.alias,
            "canonical": mapping.canonical,
            "note": mapping.note,
            "review_status": "unreviewed",
        })

    revision_material = json.dumps(
        {"mode": mode, **groups}, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    revision = f"{mode}-{hashlib.sha256(revision_material).hexdigest()[:16]}"
    record_prefixes = {
        "prices": "CRM-PRICE",
        "schedules": "CRM-SCHEDULE",
        "qualifications": "CRM-QUAL",
        "name_mappings": "CRM-MAP",
    }
    for kind, rows in groups.items():
        for index, row in enumerate(rows, start=1):
            row_digest = hashlib.sha256(
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:16]
            row["record_id"] = f"{record_prefixes[kind]}-{row_digest}-{index:04d}"
            row["source"] = {
                "system": "crm-readonly",
                "record_id": row["record_id"],
                "revision": revision,
            }
    return {
        "dataset": {
            "kind": "crm_snapshot",
            "mode": mode,
            "revision": revision,
            "demo_only": False,
            "production_usable": False,
        },
        **groups,
    }


class CrmProjectScopeError(ValueError):
    """The requested project cannot be mapped to one CRM project safely."""


def scope_crm_catalog_to_project(catalog: dict, project_id: str, project_name: str) -> dict:
    """Return a project-only catalog with identifiers and revision scoped to visible rows.

    CRM qualifications and name mappings currently lack trustworthy project
    scope, so they are excluded. Rows with conflicting project identifiers are
    excluded as well. The new revision and row IDs are derived only from the
    scoped catalog, preventing hidden projects from influencing returned
    version or candidate identifiers.
    """
    if not isinstance(catalog, dict) or catalog.get("dataset", {}).get("kind") != "crm_snapshot":
        raise ValueError("项目隔离只支持 CRM 快照目录")
    project_id = str(project_id or "").strip()
    project_name = str(project_name or "").strip()
    scoped_groups: dict[str, list[dict]] = {key: [] for key in (
        "prices", "schedules", "qualifications", "name_mappings"
    )}
    for kind in ("prices", "schedules"):
        for row in catalog.get(kind, []):
            row_id = str(row.get("project_id") or "").strip()
            row_name = str(row.get("project_name") or "").strip()
            if row_id and row_id != project_id:
                continue
            if row_name and row_name != project_name:
                continue
            if not ((project_id and row_id == project_id) or (project_name and row_name == project_name)):
                continue
            scoped_groups[kind].append({key: value for key, value in row.items() if key not in {"record_id", "source"}})

    mode = str(catalog["dataset"].get("mode") or "")
    revision_material = json.dumps(
        {"mode": mode, **scoped_groups}, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    revision = f"{mode}-{hashlib.sha256(revision_material).hexdigest()[:16]}"
    prefixes = {"prices": "CRM-PRICE", "schedules": "CRM-SCHEDULE"}
    for kind, prefix in prefixes.items():
        for index, row in enumerate(scoped_groups[kind], start=1):
            row_digest = hashlib.sha256(
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:16]
            record_id = f"{prefix}-{row_digest}-{index:04d}"
            row["record_id"] = record_id
            row["source"] = {
                "system": "crm-readonly",
                "record_id": record_id,
                "revision": revision,
            }

    return {
        "dataset": {**catalog["dataset"], "revision": revision},
        **scoped_groups,
    }
