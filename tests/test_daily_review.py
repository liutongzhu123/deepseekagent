#!/usr/bin/env python3
"""daily_review 失败路径测试（对应 bin/daily_review.py 的 F1–F6）。"""

from __future__ import annotations

import importlib.util
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("daily_review", ROOT / "bin" / "daily_review.py")
dr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dr)
TODAY = datetime.now(dr.TZ).date()


def make_repo(tmp_path, files):
    path = tmp_path / "repo"
    path.mkdir()

    def run(*args):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    run("init", "-q"); run("config", "user.name", "t"); run("config", "user.email", "t@t")
    for name, body in files.items():
        (path / name).write_text(body, encoding="utf-8")
    run("add", "-A"); run("commit", "-q", "-m", "c")
    return path


def test_parse_diff_and_f3():
    files = dr.parse_diff("--- a/a.py\n+++ b/a.py\n@@ -1,0 +3,2 @@\n+x=1\n+y=2\n")
    assert files["a.py"] == {3, 4}
    assert dr.is_code("bin/x.py") and not dr.is_code("docs/a.png")
    assert not dr.is_code("data/x.py") and not dr.is_code(".git/x.py")


def test_f1_no_commits(tmp_path):
    assert dr.review(make_repo(tmp_path, {"a.py": "print(1)\n"}), date(2000, 1, 1)) is None
    assert "无需巡检" in dr.build_report(date(2000, 1, 1), None)


def test_f4_git_failure(tmp_path):
    with pytest.raises(RuntimeError):
        dr.review(tmp_path / "missing", TODAY)
    assert dr.main(["--repo", str(tmp_path / "missing")]) == 2


def test_f6_decorated_and_exported_not_dead(tmp_path):
    source = ("__all__ = ['exported']\ndef deco(f):\n    return f\n@deco\n"
              "def decorated():\n    return 1\ndef exported():\n    return 2\n")
    assert dr.review(make_repo(tmp_path, {"a.py": source}), TODAY)["dead_code"] == []


def test_end_to_end_duplicate_and_dead(tmp_path):
    block = "".join(f"line_{i} = compute(input_{i}, offset)\n" for i in range(6))
    result = dr.review(make_repo(tmp_path, {
        "old.py": block, "new.py": block + "def unused_new():\n    return 1\n"}), TODAY)
    assert [f["name"] for f in result["dead_code"]] == ["unused_new"]
    # 6 行块有 2 个滑动窗口，F5 要求每对文件合并成 1 条
    assert len(result["duplicates"]) == 1, "两个文件里的同一代码块应合并报一条重复"


def test_f2_shallow_clone(tmp_path):
    origin = make_repo(tmp_path, {"a.py": "x = 1\n"})
    subprocess.run(["git", "-C", str(origin), "commit", "-q", "--allow-empty", "-m", "2"],
                   check=True)
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow)],
                   check=True, capture_output=True)
    with pytest.raises(RuntimeError, match="浅克隆"):
        dr.review(shallow, TODAY)
