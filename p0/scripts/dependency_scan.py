#!/usr/bin/env python3
"""Dependency health report for P1-6.

Uses ``pip-audit`` when installed (preferred).  If it is unavailable, falls
back to ``pip check`` + ``pip list --outdated`` so the check still produces
useful, non-gating evidence on an offline server.

    python scripts/dependency_scan.py --requirements requirements.txt --json
    python scripts/dependency_scan.py --strict-vuln

``--strict-vuln`` exits 1 when pip-audit reports a vulnerability; the fallback
never fails on outdated packages by default.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run(argv: list[str], timeout: int = 180, full_stdout: bool = False) -> dict:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )
        return {
            "command": " ".join(argv),
            "returncode": completed.returncode,
            "stdout": completed.stdout if full_stdout else completed.stdout[-20000:],
            "stderr": completed.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": " ".join(argv),
            "returncode": 124,
            "stdout": (exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "",
            "stderr": "timed out after %ss" % timeout,
        }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", default="requirements.txt")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict-vuln", action="store_true")
    args = parser.parse_args(argv)
    requirements = Path(args.requirements)
    pip_audit = shutil.which("pip-audit")
    if not pip_audit:
        candidate = Path(sys.executable).with_name("pip-audit")
        pip_audit = str(candidate) if candidate.exists() else None
    report: dict = {"requirements": str(requirements), "pip_audit": bool(pip_audit), "checks": []}
    vulnerabilities = 0
    if pip_audit and requirements.exists():
        result = run(
            [pip_audit, "-r", str(requirements), "--format", "json"],
            timeout=120,
            full_stdout=True,
        )
        if result["returncode"] in {0, 1} and result["stdout"].strip():
            payload = {}
            try:
                payload = json.loads(result["stdout"] or "{}")
                dependencies = (
                    payload.get("dependencies", [])
                    if isinstance(payload, dict)
                    else payload
                )
                if isinstance(dependencies, list):
                    vulnerabilities = sum(
                        len(item.get("vulns") or [])
                        for item in dependencies
                        if isinstance(item, dict)
                    )
                    report["vulnerable_packages"] = [
                        {
                            "name": item.get("name"),
                            "version": item.get("version"),
                            "vulns": len(item.get("vulns") or []),
                        }
                        for item in dependencies
                        if isinstance(item, dict) and item.get("vulns")
                    ]
            except json.JSONDecodeError:
                pass
            result["stdout"] = result["stdout"][-20000:]
            report["checks"].append({**result, "name": "pip-audit"})
        else:
            report["checks"].append({**result, "name": "pip-audit"})
            report["note"] = "pip-audit failed/timed out; falling back to pip check/outdated"
            report["checks"].append({**run([sys.executable, "-m", "pip", "check"]), "name": "pip-check"})
            report["checks"].append(
                {**run([sys.executable, "-m", "pip", "list", "--outdated", "--format", "json"]), "name": "pip-outdated"}
            )
    else:
        report["checks"].append({**run([sys.executable, "-m", "pip", "check"]), "name": "pip-check"})
        report["checks"].append(
            {**run([sys.executable, "-m", "pip", "list", "--outdated", "--format", "json"]), "name": "pip-outdated"}
        )
        report["note"] = "pip-audit not installed; fallback does not fail on outdated packages"
    report["vulnerabilities"] = vulnerabilities
    report["ok"] = vulnerabilities == 0
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else _human(report))
    return 1 if args.strict_vuln and vulnerabilities else 0


def _human(report: dict) -> str:
    lines = [f"requirements: {report['requirements']}", f"pip-audit: {report['pip_audit']}"]
    for check in report["checks"]:
        lines.append(f"- {check['name']}: rc={check['returncode']}")
        if check.get("stderr"):
            lines.append(f"  stderr: {check['stderr'].strip()[:500]}")
    lines.append(f"vulnerabilities: {report.get('vulnerabilities', 0)}")
    if report.get("note"):
        lines.append(f"note: {report['note']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
