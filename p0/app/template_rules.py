"""OEM report-template correctness (RA-4.18 / R11).

内容信号（依据/委托单位/生产企业/样品名称）与模板信号（封面/注意事项固定文字）
独立采集后比对。失败路径（``tests/test_template_rules.py`` 逐条覆盖）：

F1 内容 A + 模板 B → fail 错用模板；F2 内容指向多个主机厂 → fail
F3 只凭“气味”命中 BYD 但依据是吉利 → 不套 BYD（转 F4）
F4 已登记主机厂无模板配置（吉利）→ skip，绝不 pass
F5 GB/T 等通用依据、无主机厂信号 → warn；F6 多个模板标记 → fail
F7 配置缺失/不可读 → skip；F8/F9 页数变体/缺页数 → 模板族仍判
F10 空值、重复标记、非字符串配置项 → 不崩
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:  # optional in minimal deployments; missing config skips the rule
    import yaml
except ImportError:  # pragma: no cover - requirements installs PyYAML
    yaml = None

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "templates"
_STANDARD_RE = re.compile(r"Q\s*[/／-]\s*[A-Z]{2,}")
_TEMPLATE_HEAD = 40  # cover/notice only: body citations are content, not template proof


@dataclass(frozen=True)
class TemplateMatch:
    family: str = ""
    variant: str = ""
    oem: str = ""
    template_oem: str = ""
    status: str = "warn"  # pass | fail | warn | skip
    severity: str = "low"
    message: str = ""
    evidence: str = ""
    suggestion: str = ""


def _hits(text: str, needles: Iterable[object]) -> list[str]:
    return [str(item) for item in needles or [] if str(item) and str(item) in (text or "")]


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def load_configs() -> tuple[dict[str, dict], list[dict]]:
    """Return (oem registry, template family configs)."""
    oems = {str(k): v for k, v in _load_yaml(CONFIG_DIR / "oems.yaml").items() if isinstance(v, dict)}
    families: list[dict] = []
    if CONFIG_DIR.exists():
        for path in sorted(CONFIG_DIR.glob("*.yaml")):
            spec = _load_yaml(path) if path.name != "oems.yaml" else {}
            if spec.get("family") and spec.get("match"):
                families.append(spec)
    return oems, families


def _label(oems: dict[str, dict], key: str) -> str:
    return str((oems.get(key) or {}).get("label") or key)


def _variant(family: dict, page_totals: Iterable[object]) -> str:
    totals = {int(value) for value in page_totals or [] if str(value).strip()}
    for spec in (family.get("variants") or {}).values():
        expected = {int(value) for value in (spec.get("page_totals") or []) if str(value).strip()}
        if expected and totals and totals <= expected:
            return str(spec.get("label") or "")
    return ""


def _votes(oems: dict[str, dict], basis: str, client: str, supplier: str, sample_name: str) -> dict[str, list[str]]:
    votes: dict[str, list[str]] = {}
    for key, spec in oems.items():
        evidence = [f"依据={n}" for n in _hits(basis, spec.get("basis_any") or [])]
        for name, text in (("委托单位", client), ("生产企业", supplier), ("样品名称", sample_name)):
            evidence += [f"{name}={n}" for n in _hits(text, spec.get("name_any") or [])]
        if evidence:
            votes[key] = evidence
    return votes


def _marks(oems: dict[str, dict], paragraphs: Iterable[str]) -> dict[str, list[str]]:
    text = "\n".join(str(line) for line in list(paragraphs or [])[:_TEMPLATE_HEAD] if line and not _STANDARD_RE.search(str(line)))
    return {key: hits for key, spec in oems.items() if (hits := _hits(text, spec.get("template_any") or []))}


def _fail(message: str, evidence: str) -> TemplateMatch:
    return TemplateMatch(status="fail", severity="high", message=message, evidence=evidence,
                         suggestion="按报告所属主机厂模板重新出具，并复核检验依据、封面与页眉")


def match_template(
    *,
    basis: str = "",
    item: str = "",
    page_totals: Iterable[object] = (),
    client: str = "",
    supplier: str = "",
    sample_name: str = "",
    paragraphs: Iterable[str] = (),
    variant_hint: str = "",
) -> TemplateMatch:
    """Identify the OEM/template family and report a cross-OEM mismatch."""
    oems, families = load_configs()
    if not families:
        return TemplateMatch(status="skip", message="模板配置不可用，无法识别主机厂模板", evidence=f"未加载 {CONFIG_DIR}")

    votes = _votes(oems, basis, client, supplier, sample_name)
    marks = _marks(oems, paragraphs)
    oem, template_oem = next(iter(votes), ""), next(iter(marks), "")
    oem_label, template_label = _label(oems, oem), _label(oems, template_oem)
    evidence = f"basis={basis[:80]}; 内容={oem_label or '-'}; 版式={template_label or '-'}"

    if len(votes) > 1:
        detail = "；".join(f"{_label(oems, k)}（{'、'.join(v)}）" for k, v in votes.items())
        return _fail(f"报告内容同时指向多个主机厂：{detail}", f"{evidence}; {detail}")
    if len(marks) > 1:
        detail = "；".join(f"{_label(oems, k)}（{'、'.join(v)}）" for k, v in marks.items())
        return _fail(f"文档版式同时出现多个主机厂模板标记：{detail}", f"{evidence}; {detail}")
    if template_oem and oem and template_oem != oem:
        return _fail(f"错用模板：报告内容属于{oem_label}，但文档版式命中{template_label}模板", evidence)

    candidates = []
    for family in families:
        match = family.get("match") or {}
        hits = _hits(basis, match.get("basis_any") or [])
        if not hits and oem == str(family.get("oem") or ""):
            hits = _hits(item, match.get("item_any") or [])
        if hits:
            candidates.append((family, hits))
    if len(candidates) > 1:
        names = "、".join(str(family.get("family") or "") for family, _ in candidates)
        return _fail(f"报告同时命中多个主机厂模板：{names}", f"{evidence}; families={names}")
    if candidates:
        family, family_hits = candidates[0]
        family_oem = str(family.get("oem") or "")
        family_label = _label(oems, family_oem)
        if oem and family_oem and family_oem != oem:
            return _fail(f"错用模板：报告内容属于{oem_label}，但匹配到{family_label}模板", evidence)
        if template_oem and family_oem and template_oem != family_oem:
            return _fail(f"错用模板：模板族属于{family_label}，但版式标记属于{template_label}", evidence)
        variant = _variant(family, page_totals) or (str(variant_hint or "") if oem == family_oem else "")
        return TemplateMatch(
            family=str(family.get("family") or ""), variant=variant, oem=oem or family_oem,
            template_oem=template_oem or family_oem, status="pass", severity="info",
            message=f"识别为{family_label}报告模板（{variant or '变体未识别'}）",
            evidence=f"{evidence}; family={family.get('family')}; variant={variant or '-'}; "
                     f"{'、'.join(f'依据={n}' if n in basis else f'项目={n}' for n in family_hits)}",
        )
    if oem:
        return TemplateMatch(
            oem=oem, template_oem=template_oem, status="skip", severity="info",
            message=f"报告内容属于{oem_label}，但{oem_label}模板尚未接入（缺 D2 样例），无法判定模板正确性",
            evidence=f"{evidence}; {'、'.join(votes[oem])}", suggestion="提供该主机厂样例后补充模板配置（D2）",
        )
    if template_oem:
        return TemplateMatch(
            template_oem=template_oem, status="skip", severity="info",
            message=f"文档版式命中{template_label}模板，但检验依据/委托单位未标明主机厂，无法确认模板适用性",
            evidence=evidence, suggestion="补充检验依据或委托单位后复核",
        )
    return TemplateMatch(status="warn", severity="low", message="未识别为已知主机厂模板",
                         evidence=evidence, suggestion="确认主机厂模板")
