"""Excel/CSV intake for report-review cross-document checks.

Phase 1 data adapter.  The customer CRM/task-order APIs are not available yet,
so the first step is a local inbox with four exports: task orders, original
records, samples and equipment ledgers.  This module only reads and normalizes
those files; it deliberately does not change audit rules or call a network.

The contract is intentionally small and explicit:

* file stem = dataset name (``task_orders`` / ``original_records`` /
  ``samples`` / ``equipment``; Chinese stems are accepted too);
* first row = headers, matched by aliases, normalized by removing whitespace;
* rows without the dataset key are skipped;
* missing required columns are reported, never silently invented.

No file is uploaded anywhere.  The caller owns the local directory.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

SUPPORTED_SUFFIXES = (".csv", ".xlsx", ".xlsm")


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    label: str
    file_stems: tuple[str, ...]
    aliases: dict[str, tuple[str, ...]]
    required: tuple[str, ...]
    key: str


DATASETS: dict[str, DatasetSpec] = {
    "task_orders": DatasetSpec(
        name="task_orders",
        label="任务单",
        file_stems=("task_orders", "任务单", "任务单清单"),
        aliases={
            "order_no": ("任务单号", "任务号", "任务单编号", "order_no", "task_no"),
            "report_no": ("报告编号", "report_no"),
            "sample_no": ("样品编号", "sample_no"),
            "customer": ("客户", "委托单位", "customer"),
            "sample_name": ("样品名称", "sample_name"),
            "item": ("检验项目", "项目", "item"),
            "standard": ("检验依据", "标准", "standard"),
            "commissioned_at": ("委托日期", "日期", "commissioned_at"),
        },
        required=("order_no",),
        key="order_no",
    ),
    "original_records": DatasetSpec(
        name="original_records",
        label="原始记录",
        file_stems=("original_records", "原始记录", "记录"),
        aliases={
            "record_no": ("原始记录编号", "记录编号", "记录号", "record_no"),
            "report_no": ("报告编号", "report_no"),
            "sample_no": ("样品编号", "sample_no"),
            "test_date": ("检验日期", "试验日期", "test_date"),
            "device_id": ("设备编号", "设备号", "device_id"),
            "result": ("检验结果", "结果", "result"),
            "operator": ("检验员", "操作员", "operator"),
        },
        required=("record_no",),
        key="record_no",
    ),
    "samples": DatasetSpec(
        name="samples",
        label="样品",
        file_stems=("samples", "样品", "样品清单"),
        aliases={
            "sample_no": ("样品编号", "sample_no"),
            "sample_name": ("样品名称", "sample_name"),
            "quantity": ("样品数量", "数量", "quantity"),
            "report_no": ("报告编号", "report_no"),
            "customer": ("委托单位", "客户", "customer"),
            "model": ("型号规格", "规格型号", "model"),
            "received_at": ("接收日期", "送样日期", "received_at"),
        },
        required=("sample_no",),
        key="sample_no",
    ),
    "equipment": DatasetSpec(
        name="equipment",
        label="设备台账",
        file_stems=("equipment", "设备", "设备台账"),
        aliases={
            "device_id": ("设备编号", "设备号", "device_id"),
            "device_name": ("设备名称", "device_name"),
            "valid_until": ("溯源有效期", "有效期至", "有效期", "valid_until"),
            "status": ("状态", "status"),
            "location": ("地点", "location"),
        },
        required=("device_id", "valid_until"),
        key="device_id",
    ),
}


class InputError(ValueError):
    """The inbox cannot be read or a required column is missing."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _norm_header(value: Any) -> str:
    """Normalize a header for alias matching (spaces, full-width, case)."""
    text = _text(value).lower()
    text = text.replace("\ufeff", "")
    return re.sub(r"[\s\u3000*＊]+", "", text)


def _alias_index(spec: DatasetSpec, headers: Iterable[Any]) -> tuple[dict[str, int], list[str], list[str]]:
    """Return (canonical -> column index, unknown headers, ambiguous headers)."""
    normalized = [_norm_header(header) for header in headers]
    mapping: dict[str, int] = {}
    unknown: list[str] = []
    ambiguous: list[str] = []
    for canonical, names in spec.aliases.items():
        wanted = {_norm_header(name) for name in names}
        hits = [index for index, header in enumerate(normalized) if header and header in wanted]
        if not hits:
            continue
        if len(hits) > 1:
            ambiguous.append(canonical)
        mapping[canonical] = hits[0]
    known = {_norm_header(name) for names in spec.aliases.values() for name in names}
    unknown = [_text(header) for header, norm in zip(headers, normalized) if norm and norm not in known]
    return mapping, unknown, ambiguous


def _find_file(directory: Path, spec: DatasetSpec) -> tuple[Path | None, list[str]]:
    warnings: list[str] = []
    matches: list[Path] = []
    for stem in spec.file_stems:
        for suffix in SUPPORTED_SUFFIXES:
            candidate = directory / f"{stem}{suffix}"
            if candidate.is_file():
                matches.append(candidate)
    if not matches:
        return None, warnings
    matches = sorted(set(matches))
    if len(matches) > 1:
        warnings.append(f"{spec.label}：找到多个文件，使用 {matches[0].name}，忽略 " + "、".join(item.name for item in matches[1:]))
    return matches[0], warnings


def _read_csv(path: Path) -> tuple[list[str], list[list[Any]]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                rows = list(csv.reader(handle))
            break
        except UnicodeDecodeError as exc:  # pragma: no cover - depends on the file
            last_error = exc
    else:  # pragma: no cover
        raise InputError(f"{path.name} 无法解码：{last_error}")
    if not rows:
        return [], []
    return [str(cell) for cell in rows[0]], rows[1:]


def _read_xlsx(path: Path) -> tuple[list[str], list[list[Any]]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    if not rows:
        return [], []
    return [str(cell or "") for cell in rows[0]], rows[1:]


def _load_dataset(directory: Path, spec: DatasetSpec) -> tuple[list[dict[str, str]], dict[str, Any], list[str], list[str]]:
    path, warnings = _find_file(directory, spec)
    if path is None:
        message = f"{spec.label}：未找到文件（支持 " + "、".join(spec.file_stems) + " 的 .csv/.xlsx）"
        return [], {"file": "", "rows": 0}, warnings + [message], []
    try:
        headers, raw_rows = _read_csv(path) if path.suffix.lower() == ".csv" else _read_xlsx(path)
    except Exception as exc:  # openpyxl raises several types for bad files
        return [], {"file": path.name, "rows": 0}, warnings, [f"{spec.label}：读取 {path.name} 失败：{exc}"]

    mapping, unknown, ambiguous = _alias_index(spec, headers)
    errors = [f"{spec.label}：{path.name} 缺少必填列 {name}" for name in spec.required if name not in mapping]
    errors += [f"{spec.label}：{path.name} 列名重复映射到 {name}" for name in ambiguous]
    if unknown:
        warnings.append(f"{spec.label}：忽略未识别列 " + "、".join(unknown))

    rows: list[dict[str, str]] = []
    skipped = 0
    for raw in raw_rows:
        if not any(_text(cell) for cell in raw):
            continue
        row = {
            canonical: _text(raw[index]) if index < len(raw) else ""
            for canonical, index in mapping.items()
        }
        if not row.get(spec.key):
            skipped += 1
            continue
        rows.append(row)

    key_counts: dict[str, int] = {}
    for row in rows:
        key = row.get(spec.key, "")
        key_counts[key] = key_counts.get(key, 0) + 1
    duplicates = {key: count for key, count in key_counts.items() if count > 1}
    if duplicates:
        warnings.append(f"{spec.label}：{len(duplicates)} 个重复主键（{spec.key}），请在导入前确认")

    status = {
        "file": path.name,
        "rows": len(rows),
        "columns": sorted(mapping),
        "missing_required": [name for name in spec.required if name not in mapping],
        "duplicates": duplicates,
        "skipped_empty_key": skipped,
    }
    return rows, status, warnings, errors


@dataclass
class InputsSnapshot:
    source_dir: str
    datasets: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    statuses: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _indexes: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict, repr=False)

    def lookup(self, dataset: str, key: str) -> dict[str, str] | None:
        if dataset not in self._indexes:
            spec = DATASETS.get(dataset)
            if spec is None:
                raise KeyError(dataset)
            index: dict[str, dict[str, str]] = {}
            for row in self.datasets.get(dataset, []):
                value = row.get(spec.key, "")
                if value and value not in index:
                    index[value] = row
            self._indexes[dataset] = index
        return self._indexes[dataset].get(_text(key))

    def find(self, dataset: str, field: str, value: str) -> list[dict[str, str]]:
        """Return rows whose ``field`` equals ``value`` (empty value matches nothing)."""
        wanted = _text(value)
        if not wanted:
            return []
        return [row for row in self.datasets.get(dataset, []) if _text(row.get(field, "")) == wanted]

    def counts(self) -> dict[str, int]:
        return {name: len(rows) for name, rows in self.datasets.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dir": self.source_dir,
            "counts": self.counts(),
            "datasets": self.statuses,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def load_inputs(directory: str | Path) -> InputsSnapshot:
    """Load the four report-review exports from a local inbox directory."""
    root = Path(directory).expanduser()
    snapshot = InputsSnapshot(source_dir=str(root))
    if not root.is_dir():
        snapshot.errors.append(f"目录不存在：{root}")
        return snapshot
    for spec in DATASETS.values():
        rows, status, warnings, errors = _load_dataset(root, spec)
        snapshot.datasets[spec.name] = rows
        snapshot.statuses[spec.name] = {"label": spec.label, **status}
        snapshot.warnings.extend(warnings)
        snapshot.errors.extend(errors)
    return snapshot


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "datasets": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("datasets"), dict):
        raise ValueError("导入状态文件格式不正确")
    return data


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def import_inputs(directory: str | Path, state_path: str | Path | None = None) -> dict[str, Any]:
    """Import the four inbox datasets with business-key idempotency.

    Returns explicit ``success`` / ``skipped`` / ``failed`` lists; a bad row is
    reported with its row number and does not abort the rest of the batch.  The
    optional JSON state stores only business keys (no row values) so a second
    import of the same export is a no-op.
    """
    root = Path(directory).expanduser()
    state_file = Path(state_path).expanduser() if state_path else None
    state = {"version": 1, "datasets": {}}
    warnings: list[str] = []
    if state_file is not None:
        try:
            state = _read_state(state_file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f"导入状态读取失败，按空状态继续：{exc}")
    datasets = state.setdefault("datasets", {})

    success: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    if not root.is_dir():
        failed.append({"dataset": "*", "label": "目录", "row": 0, "key": "", "reason": f"目录不存在：{root}"})
        return {"success": success, "skipped": skipped, "failed": failed, "counts": {"success": 0, "skipped": 0, "failed": 1}, "warnings": warnings, "state_path": str(state_file or "")}

    for spec in DATASETS.values():
        path, file_warnings = _find_file(root, spec)
        warnings.extend(file_warnings)
        if path is None:
            failed.append({"dataset": spec.name, "label": spec.label, "row": 0, "key": "", "reason": "未找到文件"})
            continue
        try:
            headers, raw_rows = _read_csv(path) if path.suffix.lower() == ".csv" else _read_xlsx(path)
        except Exception as exc:  # noqa: BLE001 - optional file boundary
            failed.append({"dataset": spec.name, "label": spec.label, "row": 0, "key": "", "reason": f"读取失败：{exc}"})
            continue
        mapping, unknown, ambiguous = _alias_index(spec, headers)
        if unknown:
            warnings.append(f"{spec.label}：忽略未识别列 " + "、".join(unknown))
        missing = [name for name in spec.required if name not in mapping]
        if missing or ambiguous:
            reason = "缺少必填列 " + "、".join(missing) if missing else "列名重复映射到 " + "、".join(ambiguous)
            failed.append({"dataset": spec.name, "label": spec.label, "row": 0, "key": "", "reason": reason})
            continue

        seen_batch: set[str] = set()
        imported = datasets.setdefault(spec.name, {})
        for offset, raw in enumerate(raw_rows, start=2):
            if not any(_text(cell) for cell in raw):
                continue
            row = {
                canonical: _text(raw[index]) if index < len(raw) else ""
                for canonical, index in mapping.items()
            }
            key = row.get(spec.key, "")
            if not key:
                failed.append({"dataset": spec.name, "label": spec.label, "row": offset, "key": "", "reason": f"缺少主键 {spec.key}"})
                continue
            if key in seen_batch:
                skipped.append({"dataset": spec.name, "label": spec.label, "row": offset, "key": key, "reason": "批次内重复"})
                continue
            seen_batch.add(key)
            if key in imported:
                skipped.append({"dataset": spec.name, "label": spec.label, "row": offset, "key": key, "reason": "已导入"})
                continue
            imported[key] = {"row": offset, "file": path.name}
            success.append({"dataset": spec.name, "label": spec.label, "row": offset, "key": key})

    if state_file is not None:
        _write_state(state_file, state)
    counts = {"success": len(success), "skipped": len(skipped), "failed": len(failed)}
    return {
        "success": success,
        "skipped": skipped,
        "failed": failed,
        "counts": counts,
        "warnings": warnings,
        "state_path": str(state_file or ""),
    }
