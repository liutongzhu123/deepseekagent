"""B7: classify a standard by number prefix and name (国标/行标/企标).

Chinese standards declare their issuing level in the number prefix, so the
prefix is authoritative: ``GB*`` is a national standard, an industry code such
as ``QC/T`` or ``JB/T`` is an industry standard, and ``Q/<organisation>`` is an
enterprise standard.  The name is only a fallback, used when the number is
missing or unrecognised: it may repeat the number or state the level outright
("企业标准").  Local, group and international standards keep their own labels
instead of being forced into one of the three requested classes.
"""
from __future__ import annotations

import re
import unicodedata

LABELS = {"national": "国标", "industry": "行标", "enterprise": "企标",
          "local": "地标", "group": "团标", "international": "国际",
          "unknown": "未知"}

# Official industry-standard prefixes (《行业标准管理办法》).  The number may
# carry /T (recommended), /Z (guidance) or an engineering suffix (JGJ, CJJ).
_INDUSTRY = (
    "AQ 安全生产 BB 包装 CB 船舶 CH 测绘 CJ 城镇建设 CY 新闻出版 DA 档案 "
    "DL 电力 DZ 地质矿产 EJ 核工业 FZ 纺织 GA 公安 GH 供销 GY 广播电视 "
    "HB 航空 HG 化工 HJ 环境保护 HS 海关 HY 海洋 JB 机械 JC 建材 JG 建筑 "
    "JR 金融 JT 交通 JY 教育 LB 旅游 LD 劳动 LS 粮食 LY 林业 MH 民航 "
    "MT 煤炭 MZ 民政 NB 能源 NY 农业 QB 轻工 QC 汽车 QJ 航天 QX 气象 "
    "SB 商业 SC 水产 SH 石化 SJ 电子 SL 水利 SN 商检 SY 石油 "
    "TB 铁道 TD 土地 WB 物资 WH 文化 WJ 兵工 WM 经贸 WS 卫生 XB 稀土 "
    "YB 冶金 YC 烟草 YD 通信 YS 有色 YY 医药 YZ 邮政 ZY 中医药"
)
_WORDS = _INDUSTRY.split()
INDUSTRY_NAMES = dict(zip(_WORDS[::2], _WORDS[1::2]))

_INTERNATIONAL = re.compile(
    r"^(?:ISO|IEC|EN|DIN|JIS|ASTM|SAE|ANSI|UL|CSA|FMVSS|ECE|UN)(?=[\s/]|\d|$)"
)
_ENTERPRISE_GROUP = re.compile(r"^(?:QMAT|QSQR|QJD|SQR|VW)(?=[\s\d-]|$)")
_TYPE_WORDS = (("企业标准", "enterprise"), ("行业标准", "industry"),
               ("国家标准", "national"), ("团体标准", "group"),
               ("地方标准", "local"), ("国际标准", "international"),
               ("军用标准", "national"))


def _normalise(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").upper()
    for dash in "—–−":
        text = text.replace(dash, "-")
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip("()[]{}<>《》【】")


def _result(kind: str, code: str = "", subtype: str = "", *,
            confidence: float = 0.99, evidence: str = "",
            source: str = "number") -> dict:
    return {"type": kind, "label": LABELS[kind], "code": code,
            "subtype": subtype, "confidence": confidence,
            "evidence": evidence, "source": source}


def _number_result(number: str) -> dict | None:
    """Classify one normalised number; ``None`` means the prefix is unknown."""
    if number.startswith("Q/"):
        org = number[2:].split(" ")[0]
        return _result("enterprise", "Q/", f"企业标准（{org}）",
                       evidence="标准号前缀 Q/")
    if re.match(r"^Q[\s-][A-Z]", number) and re.search(r"\d", number):
        # File-name form of an enterprise number: "Q-JLY ..." / "Q JLY ...".
        return _result("enterprise", "Q/", "企业标准", confidence=0.85,
                       evidence="企业标准号（Q 开头）")
    match = _ENTERPRISE_GROUP.match(number)
    if match:
        return _result("enterprise", match.group(0), "企业标准",
                       evidence=f"企业标准前缀 {match.group(0)}")
    if _INTERNATIONAL.match(number):
        return _result("international", number.split(" ")[0].split("/")[0],
                       "国际/国外标准", evidence="国际标准前缀")
    if re.match(r"^DB(?:\d{2}|J|/)", number):
        return _result("local", "DB", "地方标准", evidence="标准号前缀 DB")
    if number.startswith("CECS"):
        return _result("group", "CECS", "团体标准", evidence="团体标准前缀 CECS")
    if re.match(r"^T\s*/", number):
        return _result("group", "T/", "团体标准", evidence="团体标准前缀 T/")
    match = re.match(r"^GB\s*/\s*([TZ])(?=[\s\d]|$)", number)
    if not match:
        # File-name spellings of the recommended prefix: GBT / GB-T / GB T.
        match = re.match(r"^GB\s*-?\s*T(?=[\s\d]|$)", number)
    if match:
        kind = match.group(1) if match.lastindex else "T"
        subtype = {"T": "推荐性国家标准", "Z": "国家标准化指导性技术文件"}[kind]
        return _result("national", f"GB/{kind}", subtype,
                       evidence=f"标准号前缀 GB/{kind}")
    for code, subtype in (("GBJ", "国家工程建设标准"),
                          ("GBZ", "国家职业卫生标准"),
                          ("GJB", "国家军用标准")):
        if re.match(rf"^{code}(?=[\s\d]|$)", number):
            return _result("national", code, subtype,
                           evidence=f"标准号前缀 {code}")
    if re.match(r"^GB(?=[\s\d]|$)", number):
        return _result("national", "GB", "强制性国家标准",
                       evidence="标准号前缀 GB")
    match = re.match(r"^(?P<code>[A-Z]{2})\s*/?\s*(?P<kind>[TZJGS])?"
                     r"(?=[\s\d]|$)", number)
    if match and match.group("code") in INDUSTRY_NAMES:
        code, kind = match.group("code"), match.group("kind") or ""
        suffix = f"/{kind}" if kind and kind in "TZ" else kind
        industry = INDUSTRY_NAMES[code]
        return _result("industry", code + suffix, f"{industry}行业标准",
                       evidence=f"标准号前缀 {code}{suffix}")
    return None


def _name_result(name: str) -> dict | None:
    """Classify from a title/file name: embedded number first, type word next."""
    text = _normalise(name)
    if not text:
        return None
    tokens = [token for token in
              re.split(r"[\s,;，；、:：()（）\[\]【】《》]+", text) if token]
    for size in (3, 2, 1):
        candidate = " ".join(tokens[:size])
        result = _number_result(candidate)
        if result:
            result.update(source="name",
                          confidence=round(result["confidence"] * 0.9, 2),
                          evidence=f"名称中的标准号 {candidate}")
            return result
    for word, kind in _TYPE_WORDS:
        if word in text:
            return _result(kind, "", word, confidence=0.7,
                           evidence=f"名称含「{word}」", source="name")
    return None


def classify_standard(standard_no: str = "", name: str = "") -> dict:
    """Return the standard's type, label, prefix, confidence and evidence."""
    number = _normalise(standard_no)
    result = _number_result(number) if number else None
    if result is None:
        result = _name_result(name)
    if result is None:
        result = _result("unknown", confidence=0.0,
                         evidence="未识别到标准类型", source="none")
    return result


def apply_classification(meta: dict) -> dict:
    """Attach the classification to a parsed standard's ``meta`` dict."""
    result = classify_standard(meta.get("standard_no", ""), meta.get("name", ""))
    meta["standard_type"] = result["type"]
    meta["standard_type_label"] = result["label"]
    meta["standard_type_subtype"] = result["subtype"]
    meta["standard_type_confidence"] = result["confidence"]
    meta["standard_type_evidence"] = result["evidence"]
    return result
