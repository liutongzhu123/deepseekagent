"""Evaluation helpers shared by the regression and acceptance scripts."""
from __future__ import annotations

from pathlib import Path


def report_files(directory: str | Path, pattern: str = "*.docx") -> list[Path]:
    """Return real report files, ignoring macOS AppleDouble sidecars.

    Copying a sample directory from a Mac leaves ``._name.docx`` metadata files
    next to the real reports.  They are not OOXML and must never count as
    samples; the same rule keeps ``~$`` Office lock files out.
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.glob(pattern)
        if path.is_file() and not path.name.startswith(("._", "~$"))
    )
