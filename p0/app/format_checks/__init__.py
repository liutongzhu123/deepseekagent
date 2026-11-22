"""Checks split from the legacy format-rule module."""

from .bookmarks import r13_bookmarks
from .empty_cells import r14_empty_cells
from .fonts import r17_fonts
from .numbering import r15_numbering
from .pages import r12_pages
from .registry import FORMAT_CHECKS
from .units import r16_units

__all__ = ["FORMAT_CHECKS", "r12_pages", "r13_bookmarks", "r14_empty_cells", "r15_numbering", "r16_units", "r17_fonts"]
