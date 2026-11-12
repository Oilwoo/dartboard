"""Which filings get a judgment, and which of those can count in the official score.

Only three OpenDART detail types are asked for: company decisions and earnings that the filing text itself
explains. Every rule here reads only the filing list, so it is fixed before any price is seen.
"""
from __future__ import annotations

import re

from .dart import Disclosure

TARGET_TYPES = ("B001", "I001", "I002")  # 주요사항보고서, 수시공시, 공정공시
MARKETS = ("Y", "K")                    # 유가증권시장, 코스닥시장

# Exchange notices about meetings, IR events and record dates carry no new decision to judge.
ADMIN_NOTICES = ("기업설명회", "주주명부폐쇄", "기준일설정", "주주총회소집", "주주총회결과", "결산실적공시예고")

_PREFIXES = re.compile(r"^(\s*\[[^\]]*\])+")    # [기재정정], [첨부정정], [발행조건확정] and the like
_PERIOD = re.compile(r"\(\d{4}\.\d{2}\)")        # (2026.09) after periodic report names
_SPACES_AND_DOTS = re.compile(r"[\sㆍ·・]")      # OpenDART writes the middle dot as ㆍ (U+318D)


def base_title(report_nm: str) -> str:
    """The title without correction prefixes, periods, spaces or middle dots, for matching filings."""
    return _SPACES_AND_DOTS.sub("", _PERIOD.sub("", _PREFIXES.sub("", report_nm)))


def is_follow_up(report_nm: str) -> bool:
    """A correction or withdrawal of an earlier filing: judged and recorded, never officially scored."""
    return bool(_PREFIXES.match(report_nm)) or "철회" in report_nm


def is_admin_notice(report_nm: str) -> bool:
    return any(word in base_title(report_nm) for word in ADMIN_NOTICES)


def is_spac(corp_name: str) -> bool:
    # a SPAC trades near its fixed trust value until a merger, so filings say nothing about its price
    return "스팩" in corp_name or "기업인수목적" in corp_name


def classify(d: Disclosure) -> str:
    """'new' or 'follow_up' for filings to judge; 'out_of_scope' or 'admin' for filings to skip."""
    if d.corp_cls not in MARKETS or len(d.stock_code) != 6 or is_spac(d.corp_name):
        return "out_of_scope"
    if is_admin_notice(d.report_nm):  # before the follow-up check: a corrected notice is still a notice
        return "admin"
    if is_follow_up(d.report_nm):
        return "follow_up"
    return "new"
