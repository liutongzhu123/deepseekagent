#!/usr/bin/env python3
"""Measure how much of a standard PDF the parser actually sees.

Reports page/character stats, pdfplumber table counts, how much table and
clause text reached the parsed ``items``, and a page-level loss breakdown.
Only aggregate counts are printed, never clause text or limit values, so the
output can be committed for secret customer standards.

Usage:
  python3 scripts/audit_parse_coverage.py --samples DIR|FILE [...] [--json OUT]
"""
import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import watermark  # noqa: E402
from app.pdf_text import extract_pages  # noqa: E402
from app.standard_parser import parse_standard  # noqa: E402

GRAM = 6
#: A table with at least this many characters carries real clause content
#: (limits, test conditions, inspection items), not just a symbol legend.
LARGE_TABLE_CHARS = 150


def _shingles(text: str, n: int = GRAM) -> set[str]:
    compact = re.sub(r"\s+", "", text or "")
    return {compact[i:i + n] for i in range(max(0, len(compact) - n + 1))}


def _table_stats(pdf_bytes: bytes, item_text: str, entries: list[dict]) -> dict:
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - optional dependency
        return {"tables": 0, "table_chars": 0, "table_coverage": 0.0,
                "large_tables": 0, "large_table_chars": 0,
                "large_table_coverage": 0.0, "large_tables_parsed": 0,
                "large_tables_structured": 0, "large_tables_with_header": 0,
                "large_tables_with_merges": 0}
    from io import BytesIO

    item_grams = _shingles(item_text)
    total = {"tables": 0, "table_chars": 0, "grams": 0, "covered": 0}
    large = {"tables": 0, "table_chars": 0, "grams": 0, "covered": 0,
             "parsed": 0, "structured": 0, "with_header": 0, "with_merges": 0}

    def structured(page_no: int, bbox: list[float]) -> dict | None:
        for entry in entries or []:
            raw = entry.get("raw_bbox") or []
            if entry.get("page") == page_no and len(raw) == 4 and all(
                abs(float(a) - float(b)) < 2 for a, b in zip(raw, bbox)
            ):
                return entry
        return None

    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                page_tables = page.find_tables()
            except Exception:  # noqa: BLE001 - a broken table must not stop the audit
                continue
            for table in page_tables:
                try:
                    cells = table.extract()
                except Exception:  # noqa: BLE001 - skip the broken table only
                    continue
                if not cells:
                    continue
                cell_text = "".join(c or "" for row in cells for c in row)
                table_grams = _shingles(cell_text)
                hit = len(table_grams & item_grams)
                is_large = len(cell_text) >= LARGE_TABLE_CHARS
                entry = structured(page_no, list(table.bbox))
                buckets = (total, large) if is_large else (total,)
                for bucket in buckets:
                    bucket["tables"] += 1
                    bucket["table_chars"] += len(cell_text)
                    bucket["grams"] += len(table_grams)
                    bucket["covered"] += hit
                if is_large:
                    large["parsed"] += int(bool(table_grams) and hit / len(table_grams) >= 0.5)
                    large["structured"] += int(entry is not None)
                    large["with_header"] += int(
                        bool(entry) and bool(entry.get("header_detected"))
                    )
                    large["with_merges"] += int(bool(entry) and bool(entry.get("has_merges")))
    return {
        "tables": total["tables"],
        "table_chars": total["table_chars"],
        "table_coverage": round(100 * total["covered"] / total["grams"], 1) if total["grams"] else 0.0,
        "large_tables": large["tables"],
        "large_table_chars": large["table_chars"],
        "large_table_coverage": round(100 * large["covered"] / large["grams"], 1) if large["grams"] else 0.0,
        "large_tables_parsed": large["parsed"],
        "large_tables_structured": large["structured"],
        "large_tables_with_header": large["with_header"],
        "large_tables_with_merges": large["with_merges"],
    }


def _appendix_start(pages: list[str]) -> int | None:
    """First appendix page; TOC entries with dot leaders are skipped."""
    pattern = re.compile(r"\s*附\s*录\s*[A-Za-z]?\s*")
    for index, text in enumerate(pages):
        if any(pattern.fullmatch(line) for line in (text or "").splitlines()):
            return index
    return None


def _repeated_furniture_chars(pages: list[str]) -> int:
    """Characters carried by lines repeated on at least half of the pages."""
    counter: Counter[str] = Counter()
    for text in pages:
        for line in {ln.strip() for ln in (text or "").splitlines()}:
            if len(line) >= 4:
                counter[line] += 1
    return sum(len(line) * count for line, count in counter.items()
               if count >= max(2, len(pages) // 2))


def audit_pdf(path: Path) -> dict:
    data = path.read_bytes()
    raw_pages = extract_pages(data)
    pages, _ = watermark.clean_pages(raw_pages)
    parsed = parse_standard(pages, path.name, pdf_bytes=data)
    items = parsed["items"]
    item_text = "".join(
        (item.get("requirement") or "") + (item.get("method") or "") for item in items
    )
    item_grams = _shingles(item_text)
    table_entries = parsed.get("tables") or []
    table_text = "".join(
        cell
        for entry in table_entries
        for cell in list(entry.get("header") or [])
        + [c for row in entry.get("rows") or [] for c in row]
        if cell
    )
    page_grams = _shingles("".join(pages))
    table_entry_chars = len(table_text)
    page_coverage = [
        100 * len(grams & item_grams) / len(grams) if (grams := _shingles(text)) else 0.0
        for text in pages
    ]
    sizes = [len(text) for text in pages]
    sparse = [i for i, cov in enumerate(page_coverage) if cov < 10 and pages[i].strip()]
    raw_chars = sum(len(text) for text in raw_pages)
    appendix = _appendix_start(pages)
    table = _table_stats(data, item_text, table_entries)
    coverage_grams = (
        round(100 * len(page_grams & item_grams) / len(page_grams), 1)
        if page_grams else 0.0
    )
    coverage_with_tables_grams = (
        round(100 * len(page_grams & (item_grams | _shingles(table_text))) / len(page_grams), 1)
        if page_grams else 0.0
    )
    coverage_with_tables = (
        round(100 * (len(item_text) + table_entry_chars) / raw_chars, 1)
        if raw_chars else 0.0
    )
    return {
        "file": path.name,
        "pages": len(pages),
        "text_pages": sum(1 for text in pages if text.strip()),
        "raw_chars": raw_chars,
        "watermark_chars": raw_chars - sum(sizes),
        "page_chars": {
            "min": min(sizes, default=0),
            "median": statistics.median(sizes) if sizes else 0,
            "max": max(sizes, default=0),
        },
        "page_coverage": {
            "median": round(statistics.median(page_coverage), 1) if page_coverage else 0,
            "max": round(max(page_coverage, default=0.0), 1),
        },
        "items": len(items),
        "item_chars": len(item_text),
        "table_entry_chars": table_entry_chars,
        "table_entries": len(table_entries),
        "coverage": round(100 * len(item_text) / raw_chars, 1) if raw_chars else 0.0,
        "coverage_grams": coverage_grams,
        "coverage_with_tables": coverage_with_tables,
        "coverage_with_tables_grams": coverage_with_tables_grams,
        "mapping_source": parsed["mapping_source"],
        "table": table,
        "sparse_pages": len(sparse),
        "appendix_pages": 0 if appendix is None else len(pages) - appendix,
        "appendix_chars": 0 if appendix is None else sum(sizes[appendix:]),
        "appendix_items": 0 if appendix is None else sum(
            1 for item in items if (item.get("page") or 0) > appendix
            or (item.get("method_page") or 0) > appendix
        ),
        "furniture_chars": _repeated_furniture_chars(pages),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", nargs="+", required=True,
                        help="PDF files or directories of PDF files")
    parser.add_argument("--json", help="write the full result as JSON here")
    args = parser.parse_args()

    paths = [p for raw in args.samples for p in
             (sorted(Path(raw).glob("*.pdf")) if Path(raw).is_dir() else [Path(raw)])
             if not p.name.startswith("._")]
    results = [audit_pdf(path) for path in paths]
    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    for result in results:
        table = result["table"]
        print(
            f"{result['file'][:28]:28} pages={result['pages']:>3} "
            f"text={result['text_pages']:>3} chars={result['raw_chars']:>6} "
            f"items={result['items']:>3} item_chars={result['item_chars']:>6} "
            f"cov={result['coverage']:>5}% cov+tables={result['coverage_with_tables']:>5}% "
            f"tables={table['tables']:>3} large={table['large_tables']:>3} "
            f"structured={table['large_tables_structured']:>2} "
            f"header={table['large_tables_with_header']:>2} "
            f"table_cov={table['large_table_coverage']:>5}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
