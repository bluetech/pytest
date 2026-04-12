from __future__ import annotations

from collections.abc import Sequence
import importlib.metadata
import re

from packaging.version import Version


class ColorMapping:
    COLORS = {
        "red": "\x1b[31m",
        "green": "\x1b[32m",
        "yellow": "\x1b[33m",
        "light-gray": "\x1b[90m",
        "light-red": "\x1b[91m",
        "light-green": "\x1b[92m",
        "bold": "\x1b[1m",
        "reset": "\x1b[0m",
        "kw": "\x1b[94m",
        # https://github.com/pygments/pygments/commit/d24e272894a56a98b1b718d9ac5fabc20124882a
        "kwspace": "\x1b[90m \x1b[39;49;00m"
        if Version(importlib.metadata.version("pygments")) > Version("2.19")
        else " ",
        "hl-reset": "\x1b[39;49;00m",
        "function": "\x1b[92m",
        "number": "\x1b[94m",
        "str": "\x1b[33m",
        "print": "\x1b[96m",
        "endline": "\x1b[90m\x1b[39;49;00m",
    }
    RE_COLORS = {k: re.escape(v) for k, v in COLORS.items()}
    NO_COLORS = {k: "" for k in COLORS.keys()}

    def format(self, lines: Sequence[str]) -> list[str]:
        """Straightforward replacement of color names to their ASCII codes."""
        return [line.format(**self.COLORS) for line in lines]

    def format_for_fnmatch(self, lines: Sequence[str]) -> list[str]:
        """Replace color names for use with LineMatcher.fnmatch_lines"""
        return [line.format(**self.COLORS).replace("[", "[[]") for line in lines]

    def format_for_rematch(self, lines: Sequence[str]) -> list[str]:
        """Replace color names for use with LineMatcher.re_match_lines"""
        return [line.format(**self.RE_COLORS) for line in lines]

    def strip_colors(self, lines: Sequence[str]) -> list[str]:
        """Entirely remove every color code"""
        return [line.format(**self.NO_COLORS) for line in lines]
