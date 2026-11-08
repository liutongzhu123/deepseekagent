"""OEM report-template correctness (RA-4.18 / R11).

内容信号（依据/委托单位/生产企业/样品名称）与模板信号（封面/注意事项固定文字）
独立采集后比对。失败路径（``tests/test_template_rules.py`` 逐条覆盖）：

F1 内容 A + 模板 B → fail 错用模板；F2 内容指向多个主机厂 → fail
F3 只凭“气味”命中 BYD 但依据是吉利 → 不套 BYD（转 F4）
F4 已登记主机厂无模板配置（吉利）→ skip，绝不 pass
F5 GB/T 等通用依据、无主机厂信号 → warn；F6 多个模板标记 → fail
F7 配置缺失/不可读 → skip；F8/F9 页数变体/缺页数 → 模板族仍判
F10 空值、重复标记、非字符串配置项 → 不崩
F11 封面同一行既有标准号又有主机厂名称 → 只去掉标准号，不能把整行丢掉
    （否则 Q/BYDQ 标准号会把「比亚迪」模板标记一起吞掉，错用模板判成 skip）

固定注意事项（R28 / RA-4.11）失败路径（``tests/test_fixed_content.py`` 逐条覆盖）：

F1 无注意事项标题 → skip，不冒充 pass
F2 有标题但无编号条目 → fail，按应有条数逐条报缺
F3 缺中间条目且后续条目重新编号 → 按固定文本匹配报缺，不因编号连续而漏报
F4 条目位置互换 → 报顺序错误
F5 单条措辞被改 → 报措辞错误并给出应为/实际
F6 多出条目 → 报多余内容
F7 全角/半角、弯引号、空格差异 → 归一化后一致，不误报
F8 中文按 8 条、英文按 7 条；语言由标题判定
F9 英文模板固定文本尚未取得客户确认 → 用对应译文兜底，并在 README 标注局限
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:  # optional in minimal deployments; missing config skips the rule
    import yaml
except ImportError:  # pragma: no cover - requirements installs PyYAML
    yaml = None

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "templates"
_STANDARD_RE = re.compile(r"Q\s*[/／-]\s*[A-Z]{2,}(?:[.\-—–－][A-Za-z0-9]+)*")
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
    # 跳过 macOS 的 AppleDouble 元数据（._xxx）与 .DS_Store：
    # 2026-11-08 生产事故——部署目录里混入 ._byd_odor.yaml（二进制），
    # read_text 抛 UnicodeDecodeError，而这个异常没被捕获，导致 177 个测试全挂。
    if path.name.startswith("._") or path.name == ".DS_Store":
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
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
    lines: list[str] = []
    for line in list(paragraphs or [])[:_TEMPLATE_HEAD]:
        if not line:
            continue
        # F11: 只去掉标准号本身；整行丢弃会连带吞掉同一行的主机厂模板名称。
        lines.append(_STANDARD_RE.sub(" ", str(line)))
    text = "\n".join(lines)
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


# --- R28 固定注意事项（RA-4.11）：中文 8 条 / 英文 7 条 ----------------------
# 中文取自 12 份真实 BYD 草稿（逐字一致）。英文模板尚未取得客户样例，7 条对应
# 中文 1–7 条（第 8 条是资质认定标志的国内说明），措辞待客户英文报告确认后替换。

FIXED_NOTICES_ZH: tuple[str, ...] = (
    "报告无“检验检测专用章”或“试验专用章”无效。",
    "未经本检验中心批准，不得部分复制本检验报告。复制报告未重新加盖“检验检测专用章”或“试验专用章”无效。",
    "报告无主检、审核、批准人签字无效。",
    "报告涂改无效。",
    "对检验报告若有异议，请以书面形式通知本检验中心总师室受理。",
    "送样检验仅对样品负责。",
    "委托方（客户）对样品及提供的数据和信息的真实性承担责任。",
    "报告中未注资质认定标志时，仅供委托方（客户）内部使用，不具有对社会的证明作用。",
)

FIXED_NOTICES_EN: tuple[str, ...] = (
    "The report is invalid without the Special Seal for Inspection and Testing or the Special Seal for Test.",
    "The report shall not be partially reproduced without the approval of this inspection center. "
    "Copies without the Special Seal for Inspection and Testing or the Special Seal for Test re-affixed are invalid.",
    "The report is invalid without the signatures of the main tester, reviewer and approver.",
    "The report is invalid if altered.",
    "Any objection to the report shall be submitted in writing to the Chief Engineer Office of this inspection center.",
    "Sample inspection is only responsible for the samples submitted.",
    "The client is responsible for the authenticity of the samples and of the data and information provided.",
)

_NOTICE_HEADS_EN = frozenset({
    "notice", "notices", "note", "notes", "precaution", "precautions", "importantnote",
    "importantnotice", "importantnotes"})
_NOTICE_END_ZH = ("检验单位地址电话", "委托单位地址电话")
_NOTICE_END_EN = re.compile(r"^(address|tel|telephone|postcode|fax)")
_NOTICE_ITEM_RE = re.compile(
    r"^\s*(?:[（(]\s*(\d{1,2})\s*[）)]|(\d{1,2})\s*[.．、)）])\s*(\S.*)$")


@dataclass(frozen=True)
class FixedContentResult:
    status: str = "skip"  # pass | fail | skip
    message: str = ""
    evidence: str = ""
    suggestion: str = ""


def _normalize_notice(text: str) -> str:
    """Compare notice wording ignoring width, quotes, whitespace and end punctuation."""
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", "", text).rstrip(".。;；,，")


def _fixed_notice_items(paragraphs: Iterable[str]) -> tuple[str, list[str], list[int]] | None:
    """Return (language, item texts, item numbers) for the first notice heading."""
    lines = [str(line or "") for line in paragraphs or []]
    fallback: tuple[str, list[str], list[int]] | None = None
    for index, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line).rstrip(":：")
        if compact == "注意事项":
            language = "zh"
        elif compact.casefold() in _NOTICE_HEADS_EN:
            language = "en"
        else:
            continue
        texts: list[str] = []
        numbers: list[int] = []
        for follow in lines[index + 1:index + 31]:
            plain = re.sub(r"\s+", "", follow)
            if any(marker in plain for marker in _NOTICE_END_ZH):
                break
            if _NOTICE_END_EN.match(plain.casefold()):
                break
            match = _NOTICE_ITEM_RE.match(follow)
            if match:
                numbers.append(int(match.group(1) or match.group(2)))
                texts.append(match.group(3).strip())
        if texts:
            return language, texts, numbers
        if fallback is None:
            fallback = (language, texts, numbers)
    return fallback


def check_fixed_content(
    paragraphs: Iterable[str],
    expected_zh: Iterable[str] = FIXED_NOTICES_ZH,
    expected_en: Iterable[str] = FIXED_NOTICES_EN,
) -> FixedContentResult:
    """Compare the notice block with the fixed text, item by item (R28)."""
    found = _fixed_notice_items(paragraphs)
    if found is None:
        return FixedContentResult(message="未识别到注意事项段落（中文「注意事项」/英文 NOTICE），不判定")
    language, actual, numbers = found
    expected = [str(item) for item in (expected_zh if language == "zh" else expected_en)]
    label = "中文" if language == "zh" else "英文"
    normalized_expected = [_normalize_notice(item) for item in expected]
    normalized_actual = [_normalize_notice(item) for item in actual]
    matched: dict[int, int] = {}
    for actual_index, text in enumerate(normalized_actual):
        if text in normalized_expected:
            matched.setdefault(normalized_expected.index(text), actual_index)
    matched_actual = set(matched.values())

    missing, order, wording, extra = [], [], [], []
    if numbers != list(range(1, len(numbers) + 1)):
        order.append("编号顺序错误：实际 " + "、".join(map(str, numbers))
                     + f"，应为 1..{len(numbers)}")
    previous = -1
    for expected_index, actual_index in sorted(matched.items(), key=lambda pair: pair[1]):
        if expected_index < previous:
            order.append(f"第 {numbers[actual_index]} 条顺序错误：该条应排在第 {expected_index + 1} 位")
        previous = max(previous, expected_index)
    explained: set[int] = set()
    for actual_index, text in enumerate(normalized_actual):
        if actual_index in matched_actual:
            continue
        if actual_index < len(expected) and actual_index not in matched:
            wording.append(f"第 {numbers[actual_index]} 条措辞不符：应为「{expected[actual_index]}」，"
                           f"实际「{actual[actual_index]}」")
            explained.add(actual_index)
        else:
            extra.append(f"第 {numbers[actual_index]} 条为多余内容：「{actual[actual_index]}」")
    for expected_index, text in enumerate(expected):
        if expected_index not in matched and expected_index not in explained:
            missing.append(f"缺第 {expected_index + 1} 条：「{text}」")

    issues = missing + order + wording + extra
    evidence = (f"语言={language}; 应有={len(expected)}; 实际={len(actual)}; 缺={len(missing)}; "
                f"顺序={len(order)}; 措辞={len(wording)}; 多余={len(extra)}")
    if issues:
        return FixedContentResult(
            status="fail", message="；".join(issues), evidence=evidence,
            suggestion=f"按模板固定注意事项逐条补齐/调整顺序与措辞（{label} {len(expected)} 条），不要自行改写",
        )
    return FixedContentResult(
        status="pass", message=f"{label}固定注意事项 {len(expected)} 条逐条一致", evidence=evidence)
