#!/usr/bin/env python3
"""Rebuild the P0 knowledge index and run five cited Q&A samples.

Usage:
  python3 scripts/run_kb_demo.py \
    --standards-dir "/path/to/标准解析参考" \
    --store-dir "/path/to/p0/data/standards" \
    --out-dir "/path/to/deliverables/qa"

The output contains the index metadata and answers with citations; the real
standard text stays in the local workspace.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--standards-dir", required=True)
    parser.add_argument("--store-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--epb-file", required=True, help="EPB sample PDF filename inside --standards-dir")
    parser.add_argument("--pp-file", required=True, help="PP sample PDF filename inside --standards-dir")
    parser.add_argument("--questions", required=True, help="JSON list of question/evaluation specs")
    parser.add_argument("--reset", action="store_true", help="clear the store before rebuilding")
    args = parser.parse_args()

    store_dir = Path(args.store_dir)
    if args.reset and store_dir.exists():
        shutil.rmtree(store_dir)
    os.environ["P0_STANDARD_DIR"] = str(store_dir)

    sys.path.insert(0, str(ROOT))
    from app.pdf_text import extract_pages  # noqa: E402
    from app.standard_parser import parse_standard  # noqa: E402
    from app import standard_store  # noqa: E402
    from app.standard_qa import answer  # noqa: E402

    standards_dir = Path(args.standards_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = [args.epb_file, args.pp_file]
    created = []
    for file_name in files:
        data = (standards_dir / file_name).read_bytes()
        pages = extract_pages(data)
        parsed = parse_standard(pages, file_name, pdf_bytes=data)
        record = standard_store.create(parsed, data, file_name)
        published = standard_store.publish(record["id"])
        created.append(
            {
                "standard_id": record["id"],
                "standard_no": record["meta"].get("standard_no", ""),
                "standard_name": record["meta"].get("name", ""),
                "items": len(record["items"]),
                "chunks": published["chunks"],
            }
        )
        print(f"published {record['meta']['standard_no']} items={len(record['items'])} chunks={published['chunks']}")

    index = standard_store.rebuild_index()
    (out_dir / "kb_index.json").write_text(
        json.dumps(
            {
                "built_at": index.get("built_at", ""),
                "standard_count": index.get("standard_count", 0),
                "chunk_count": index.get("chunk_count", 0),
                "standards": index.get("standards", []),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    kbs = standard_store.published_kbs()
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    if isinstance(questions, dict):
        questions = questions.get("questions", [])

    results = []
    for item in questions:
        result = answer(item["question"], kbs)
        text = result.get("answer", "")
        checks = {
            "found_as_expected": bool(result.get("found")) == item["expect_found"],
            "keywords": all(keyword in text for keyword in item["must_contain"]),
            "citation_complete": all(
                citation.get("standard_no") and citation.get("clause") and citation.get("page") is not None
                for citation in result.get("citations", [])
            ),
        }
        if item["expect_found"]:
            checks["standard_matches"] = all(
                citation.get("standard_no") == item["standard_no"] for citation in result.get("citations", [])
            )
        else:
            checks["standard_matches"] = True
        passed = all(checks.values())
        results.append(
            {
                "id": item["id"],
                "question": item["question"],
                "expected_found": item["expect_found"],
                "found": result.get("found"),
                "answer": text,
                "citations": result.get("citations", []),
                "checks": checks,
                "passed": passed,
            }
        )
        print(f"{item['id']} passed={passed} found={result.get('found')} citations={len(result.get('citations', []))}")

    passed = sum(1 for result in results if result["passed"])
    report = {
        "built_at": index.get("built_at", ""),
        "standard_count": index.get("standard_count", 0),
        "chunk_count": index.get("chunk_count", 0),
        "standards": created,
        "questions": results,
        "passed": passed,
        "total": len(results),
    }
    (out_dir / "qa_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 知识问答 · 重建索引与 5 个问答样例（P0）",
        "",
        f"- 索引重建时间：{report['built_at']}",
        f"- 标准：{report['standard_count']} 份；知识片段：{report['chunk_count']} 个",
        f"- 样例通过：{passed}/{len(results)}",
        "- 检索方式：本地条款/项目名/关键词混合打分；不调用外部模型，不生成无出处内容",
        "",
        "## 1. 索引",
        "",
        "| 标准号 | 名称 | 项目数 | 知识片段 |",
        "|---|---|---:|---:|",
    ]
    for standard in created:
        lines.append(
            f"| {standard['standard_no']} | {standard['standard_name']} | {standard['items']} | {standard['chunks']} |"
        )
    lines += [
        "",
        "## 2. 问答样例",
        "",
    ]
    for result in results:
        lines += [
            f"### {result['id']}（{'通过' if result['passed'] else '未通过'}）",
            "",
            f"**问：** {result['question']}",
            "",
            f"**答：**",
            "",
            result["answer"].replace("\n", "\n\n"),
            "",
        ]
        if result["citations"]:
            lines += ["**引用：**", ""]
            for citation in result["citations"]:
                lines.append(
                    f"- 《{citation.get('standard_name')}》{citation.get('standard_no')} "
                    f"第 {citation.get('clause')} 条，第 {citation.get('page')} 页，"
                    f"{citation.get('kind_label')}"
                )
            lines.append("")
        else:
            lines += ["**引用：** 无（标准不在库，明确拒答）", ""]

    lines += ["## 3. 评估口径", ""]
    for item in questions:
        expected = "命中" if item["expect_found"] else "明确未找到"
        lines.append(
            f"- {item['id']}：预期{expected}；关键词：{'、'.join(item['must_contain'])}。"
        )
    lines += [
        "- 引用完整性：每条引用都必须带标准号、条款、页码。",
        "",
        "## 4. 交付物",
        "",
        f"- 索引元数据：`{out_dir / 'kb_index.json'}`",
        f"- 问答结果：`{out_dir / 'qa_results.json'}`",
        "",
        "## 5. 限制",
        "",
        "- 这是 P0 的本地检索基线，不是 WeKnora/向量检索；语义泛化能力有限。",
        "- 仅索引传入的样例标准；扫描件未 OCR，不入库。",
        "",
    ]
    (out_dir / "qa_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"passed {passed}/{len(results)}; out -> {out_dir}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
