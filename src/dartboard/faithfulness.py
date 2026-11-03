"""Faithfulness check ("요약 충실도"): does every number in a summary appear in the source filing?

A deterministic first pass. It catches the most damaging summary error in finance (a wrong figure)
without any model call. Unit conversions (억원 vs 원) are flagged for review, not guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# a number (with thousands separators or decimals) and an optional Korean/financial unit
NUM = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(조|억|만|원|%|퍼센트|주|배|bp)?")
IGNORE_UNITS = {None}  # bare numbers such as years or list numbering are too ambiguous to judge


def _norm(integer: str, frac: str | None) -> str:
    value = integer.replace(",", "")
    return f"{value}.{frac.rstrip('0')}".rstrip(".") if frac else value


def _numbers(text: str) -> list[tuple[str, str | None, str]]:
    """(normalized value, unit, original text) for each number with a unit."""
    out = []
    for m in NUM.finditer(text):
        unit = "%" if m.group(3) == "퍼센트" else m.group(3)
        if unit in IGNORE_UNITS:
            continue
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
    source_values = {(v, u) for v, u, _ in _numbers(source)}
    source_bare = {v for v, _, _ in _numbers(source)} | {_norm(m.group(1), m.group(2)) for m in NUM.finditer(source)}
    found = _numbers(summary)
    unsupported = []
    for value, unit, original in found:
        if (value, unit) in source_values:
            continue
        if unit in ("원", "주") and value in source_bare:  # same figure written with a different unit spacing
            continue
        unsupported.append(original)
    return FaithfulnessReport(checked=len(found), supported=len(found) - len(unsupported), unsupported=unsupported)
