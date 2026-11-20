"""Spreadsheet formula-injection protection for every CSV export (OWASP "CSV injection").

A cell whose text a spreadsheet could evaluate as a formula is prefixed with ``'``. Covered:
leading ``= + - @``, TAB / CR / LF, the full-width variants of ``= + - @`` (U+FF1D, U+FF0B,
U+FF0D, U+FF20) that some spreadsheet apps normalise, and any of these hidden behind leading
whitespace.
"""

from __future__ import annotations

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20")


def neutralize_formula(text: str) -> str:
    """``text`` with a leading ``'`` if a spreadsheet might treat it as a formula."""
    if text.startswith(FORMULA_PREFIXES) or text.lstrip().startswith(FORMULA_PREFIXES):
        return "'" + text
    return text
