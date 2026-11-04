"""Checks that every number in a summary also appears in the source filing.

Unit conversions (875억 vs 87,500,000,000원) are flagged for a human, never guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

NUM = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(조|억|만|원|%|퍼센트|주|배|bp)?")


def _norm(integer: str, frac: str | None) -> str:
    value = integer.replace(",", "")
    return f"{value}.{frac.rstrip('0')}".rstrip(".") if frac else value


def _numbers(text: str) -> list[tuple[str, str, str]]:
    """(value, unit, original text) for each number with a unit. Bare numbers such as years are too ambiguous."""
    out = []
    for m in NUM.finditer(text):
        if m.group(3):
            unit = "%" if m.group(3) == "퍼센트" else m.group(3)
            out.append((_norm(m.group(1), m.group(2)), unit, m.group(0).strip()))
    return out


@dataclass
class FaithfulnessReport:
    checked: int
    supported: int
    unsupported: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return 1.0 if self.checked == 0 else self.supported / self.checked


def check(summary: str, source: str) -> FaithfulnessReport:
    with_unit = {(value, unit) for value, unit, _ in _numbers(source)}
    bare = {_norm(m.group(1), m.group(2)) for m in NUM.finditer(source)}

    def supported(value: str, unit: str) -> bool:
        # tables often put 원 or 주 in the header row instead of next to each number
        return (value, unit) in with_unit or (unit in ("원", "주") and value in bare)

    found = _numbers(summary)
    unsupported = [original for value, unit, original in found if not supported(value, unit)]
    return FaithfulnessReport(len(found), len(found) - len(unsupported), unsupported)
