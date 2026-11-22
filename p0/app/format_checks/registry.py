"""Registry for checks extracted from the legacy format-rule module."""
from __future__ import annotations

from .bookmarks import r13_bookmarks
from .empty_cells import r14_empty_cells
from .fonts import r17_fonts
from .judgment_consistency import r30_judgment_consistency
from .numbering import r15_numbering
from .pages import r12_pages
from .units import r16_units

FORMAT_CHECKS = {
    "R12": r12_pages,
    "R13": r13_bookmarks,
    "R14": r14_empty_cells,
    "R15": r15_numbering,
    "R16": r16_units,
    "R17": r17_fonts,
    "R30": r30_judgment_consistency,
}
