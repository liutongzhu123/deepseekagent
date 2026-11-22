"""R16 measurement unit spelling and capitalization checks."""
from __future__ import annotations

import re

from ._shared import ReportData, _finding

_UNIT_TOKEN = re.compile(r"(?<=\d)\s*([A-Za-zµμΩ°℃][A-Za-zµμΩ°℃0-9²³·/]*)")
_UNIT_UNITS = (
    "Pa", "kPa", "MPa", "hPa", "GPa", "℃", "°C", "N", "kN", "N·m", "kN·m",
    "%", "‰", "mm", "cm", "m", "km", "μm", "µm", "nm", "mm²", "m²", "m³", "cm³",
    "L", "mL", "μL", "µL", "mg", "g", "kg", "t", "s", "min", "h", "d", "Hz",
    "kHz", "MHz", "V", "kV", "mV", "A", "mA", "W", "kW", "MW", "Ω", "kΩ",
    "r/min", "rpm", "dB", "dB(A)", "ppm", "mol/L", "mg/L", "mg/m³", "g/m³",
    "μg/m³", "µg/m³", "J", "kJ",
)
_UNIT_CANONICAL: dict[str, str] = {}
for _unit in _UNIT_UNITS:
    _UNIT_CANONICAL.setdefault(_unit.lower(), _unit)


def _unit_issue(token: str) -> str | None:
    if len(token) == 1 and token not in {"℃", "%", "‰"}:
        return None
    canonical = _UNIT_CANONICAL.get(token.lower())
    return canonical if canonical and token != canonical else None


def r16_units(data: ReportData):
    title = "计量单位规范"
    problems: list[str] = []
    scanned = 0
    for index, line in enumerate(data.paragraphs, start=1):
        for match in _UNIT_TOKEN.finditer(line):
            token = match.group(1)
            scanned += 1
            canonical = _unit_issue(token)
            if canonical:
                problems.append(f"第{index}段「{token}」应为「{canonical}」")
    if problems:
        return _finding(
            "R16", title, "fail", "medium",
            f"单位写法错误 {len(problems)} 处：" + "；".join(problems[:6]),
            f"数值单位={scanned}; 错误={len(problems)}",
            "按白名单统一单位大小写：MPa、kPa、℃、N·m 等",
            section="计量单位", cell="、".join(sorted({p.split("「")[1].split("」")[0] for p in problems}))[:80],
        )
    if scanned == 0:
        return _finding("R16", title, "skip", "info", "未识别到数值后的计量单位")
    return _finding(
        "R16", title, "pass", "info",
        f"扫描 {scanned} 个数值单位，未发现白名单内的大小写错误",
        section="计量单位", cell="单位",
    )
