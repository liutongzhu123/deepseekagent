"""Seal rules: configurable YF/QA/ZY/ZW, position and occlusion judgement.

Failure branches F1-F10: broken config, disabled, unknown prefix, no data,
malformed box, bad seal type, English CMA, missing position, shift, occlusion.
"""
from __future__ import annotations

import copy

import pytest

from app import seal_rules

LAB = {"type": "实验室章", "page": 1, "bbox": [10, 10, 30, 30]}
CMA = {"type": "CMA章", "page": 1, "bbox": [10, 10, 30, 30]}

def _config(**changes):
    config = copy.deepcopy(seal_rules.DEFAULTS)
    config["enabled"] = True
    config.update(changes)
    return config

def _run(seals=None, report_no="YF25HNE8", **kwargs):
    return seal_rules.evaluate_seal_rules(report_no, seals or [], **kwargs)

def _find(result, code):
    return next(item for item in result["findings"] if item["code"] == code)

def test_config_defaults_and_yaml_overrides(tmp_path):
    default = seal_rules.load_config()
    assert default["enabled"] is False and set(default["types"]) == {"YF", "QA", "ZY", "ZW"}
    (broken := tmp_path / "broken.yaml").write_text("enabled: [", encoding="utf-8")
    assert seal_rules.load_config(broken)["enabled"] is False
    path = tmp_path / "seal.yaml"
    path.write_text("enabled: true\ntolerance_mm: 3\ntypes:\n  QA:\n    required: [检验专用章]\n"
                    "    expected: [{type: 检验专用章, page: 2, bbox: [1, 2, 30, 40]}]\n", encoding="utf-8")
    config = seal_rules.load_config(path)
    assert config["enabled"] and config["tolerance_mm"] == 3
    assert config["types"]["QA"]["required"] == ["检验专用章"]
    assert config["types"]["QA"]["expected"][0]["page"] == 2

def test_gates_and_malformed_observation():
    assert _run([LAB], config=copy.deepcopy(seal_rules.DEFAULTS))["reason"] == "module_disabled"
    assert _run([LAB], report_no="XX-1", config=_config())["reason"] == "unknown_report_type"
    assert _run(config=_config())["reason"] == "no_seal_observations"
    result = _run([{"type": "实验室章", "page": "nan", "bbox": [0, 0, 1, 1]}, {"type": "实验室章", "page": 1, "bbox": ["x", 0, 1, 1]}, LAB], config=_config())
    assert (result["invalid"], result["located"]) == (2, 1)
    assert _find(result, "SE01")["status"] == "pass"

def test_yf_types_and_english_cma():
    result = _run([CMA], config=_config())
    assert "缺少必需用章：实验室章" in _find(result, "SE01")["message"]
    assert "出现禁止用章：CMA章" in _find(result, "SE01")["message"]
    extra = _run([LAB, {"type": "合同专用章", "page": 1, "bbox": [40, 40, 60, 60]}], config=_config())
    assert "超出允许用章：合同专用章" in _find(extra, "SE01")["message"]
    assert _find(_run([CMA], report_no="QA17LX1LDT861", english=True, config=_config()), "SE01")["status"] == "fail"
    assert _find(_run([CMA], report_no="QA17LX1LDT861", config=_config()), "SE01")["status"] == "skip"

def test_position_shift_page_and_skip():
    config = _config()
    config["types"]["YF"]["expected"] = [{"type": "实验室章", "page": 1, "bbox": [10, 10, 30, 30]}]
    near = _run([{"type": "实验室章", "page": 1, "bbox": [12, 12, 32, 32]}], config=config)
    assert _find(near, "SE02")["status"] == "pass"
    far = _find(_run([{"type": "实验室章", "page": 1, "bbox": [40, 40, 60, 60]}], config=config), "SE02")
    assert far["status"] == "fail" and "偏移" in far["message"]
    moved = _find(_run([{"type": "实验室章", "page": 3, "bbox": [10, 10, 30, 30]}], config=config), "SE02")
    assert "期望第 1 页" in moved["message"]
    assert _find(_run([LAB], config=_config()), "SE02")["status"] == "skip"

def test_occlusion():
    config = _config()
    config["types"]["YF"]["protected"] = [{"name": "批准人签字", "page": 1, "bbox": [10, 10, 30, 30]}]
    covering = _find(_run([{"type": "实验室章", "page": 1, "bbox": [12, 12, 28, 28]}], config=config), "SE03")
    assert covering["status"] == "fail" and "批准人签字" in covering["message"]
    clear = _find(_run([{"type": "实验室章", "page": 1, "bbox": [40, 40, 60, 60]}], config=config), "SE03")
    assert clear["status"] == "pass"

@pytest.mark.parametrize("kind", ["QA", "ZY", "ZW"])
def test_four_types_are_configurable(kind):
    config = _config()
    config["types"][kind] = {"required": ["检验专用章"], "allowed": ["检验专用章"]}
    seal = {"type": "检验专用章", "page": 1, "bbox": [10, 10, 30, 30]}
    assert _find(_run([seal], report_no=f"{kind}001", config=config), "SE01")["status"] == "pass"
    assert _find(_run([LAB], report_no=f"{kind}001", config=config), "SE01")["status"] == "fail"
