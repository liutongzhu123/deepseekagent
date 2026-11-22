"""Checks split from the legacy format-rule module."""

from .bookmarks import r13_bookmarks
from .empty_cells import r14_empty_cells
from .pages import r12_pages

__all__ = ["r12_pages", "r13_bookmarks", "r14_empty_cells"]
