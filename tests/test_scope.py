"""Which filings get a judgment. Titles are real ones from DART, 2026-11-02 to 11-06."""
import pytest

from dartboard.dart import Disclosure
from dartboard.scope import base_title, classify, is_follow_up


def filing(title, corp_name="테스트전자", corp_cls="Y", stock_code="005930"):
    return Disclosure("20261103000247", "20261103", "00126380", corp_name, stock_code, title, corp_cls, "I001")


@pytest.mark.parametrize("title", [
    "주요사항보고서(유상증자결정)",
    "주요사항보고서(전환사채권발행결정)",
    "단일판매ㆍ공급계약체결",
    "주식소각결정",
    "연결재무제표기준영업(잠정)실적(공정공시)",
    "최대주주변경을수반하는주식담보제공계약체결",
    "풍문또는보도에대한해명(미확정)",
])
def test_company_decisions_and_earnings_are_new_judgments(title):
    assert classify(filing(title)) == "new"


@pytest.mark.parametrize("title", [
    "[기재정정]주요사항보고서(유상증자결정)",
    "[첨부정정]주요사항보고서(전환사채권발행결정)",
    "[발행조건확정]증권신고서(지분증권)",
    "기타주요경영사항(제3자배정유상증자결정철회)",
    "주식병합결정((철회))",
])
def test_corrections_and_withdrawals_are_follow_ups(title):
    assert classify(filing(title)) == "follow_up"


@pytest.mark.parametrize("title", [
    "기업설명회(IR)개최",
    "주주명부폐쇄기간또는기준일설정",
    "현금ㆍ현물배당을위한주주명부폐쇄(기준일)결정",
    "주주총회소집결의(임시주주총회)",
    "임시주주총회결과",
    "결산실적공시예고(안내공시)",
    "[기재정정]주주총회소집결의",  # a corrected notice is still a notice
])
def test_administrative_notices_are_skipped(title):
    assert classify(filing(title)) == "admin"


def test_only_kospi_and_kosdaq_listed_companies_without_spacs():
    assert classify(filing("주식소각결정", corp_cls="N")) == "out_of_scope"     # KONEX
    assert classify(filing("주식소각결정", corp_cls="E", stock_code="")) == "out_of_scope"  # not listed
    assert classify(filing("주요사항보고서(회사합병결정)", corp_name="엔에이치스팩30호")) == "out_of_scope"
    assert classify(filing("주요사항보고서(회사합병결정)", corp_name="한국제5호기업인수목적")) == "out_of_scope"


def test_base_title_matches_a_correction_to_its_original():
    assert base_title("[기재정정]주요사항보고서 (자기주식취득결정)") == base_title("주요사항보고서(자기주식취득결정)")
    assert base_title("분기보고서 (2026.09)") == "분기보고서"
    assert not is_follow_up("주요사항보고서(유상증자결정)")
