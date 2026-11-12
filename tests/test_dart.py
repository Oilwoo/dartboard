import json
from datetime import date

import pytest

from dartboard.dart import RateLimited, is_routine, list_disclosures

ROW = {"rcept_no": "20261103000247", "rcept_dt": "20261103", "corp_code": "00126380", "corp_name": "테스트전자",
       "stock_code": "005930", "report_nm": "주요사항보고서(자기주식취득결정)"}


def pages(*responses):
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps(responses[len(calls) - 1]).encode()

    return fetch, calls


def test_pagination_collects_all_pages():
    fetch, calls = pages({"status": "000", "total_page": 2, "list": [ROW]},
                         {"status": "000", "total_page": 2, "list": [dict(ROW, rcept_no="2")]})
    out = list_disclosures("k", date(2026, 11, 1), date(2026, 11, 3), fetch=fetch)
    assert [d.rcept_no for d in out] == ["20261103000247", "2"]
    assert "page_no=2" in calls[1]


def test_no_data_returns_empty_list():
    fetch, _ = pages({"status": "013", "message": "조회된 데이타가 없습니다."})
    assert list_disclosures("k", date(2026, 11, 1), date(2026, 11, 3), fetch=fetch) == []


def test_rate_limit_raises():
    fetch, _ = pages({"status": "020", "message": "요청 제한을 초과하였습니다."})
    with pytest.raises(RateLimited):
        list_disclosures("k", date(2026, 11, 1), date(2026, 11, 3), fetch=fetch)


def test_long_range_without_corp_code_is_refused_before_calling():
    with pytest.raises(ValueError):
        list_disclosures("k", date(2026, 1, 1), date(2026, 6, 30), fetch=lambda url: b"")


def test_long_range_with_corp_code_is_allowed():
    fetch, calls = pages({"status": "013"})
    list_disclosures("k", date(2020, 1, 1), date(2026, 6, 30), corp_code="00126380", fetch=fetch)
    assert "corp_code=00126380" in calls[0]


# Names as OpenDART writes them (the dot is U+318D), plus spellings a person might type.
@pytest.mark.parametrize("name", [
    "임원ㆍ주요주주특정증권등소유상황보고서",
    "[기재정정]임원ㆍ주요주주특정증권등소유상황보고서",
    "임원·주요주주 특정증권등 소유상황보고서",
    "사업보고서 (2025.12)",
    "반기보고서 (2026.06)",
    "분기보고서 (2026.09)",
    "[기재정정][첨부추가]분기보고서 (2026.03)",
])
def test_routine_filings_are_recognised(name):
    assert is_routine(name)


@pytest.mark.parametrize("name", [
    "주요사항보고서(자기주식취득결정)",
    "[기재정정]주요사항보고서(유상증자결정)",
    "연결재무제표기준영업(잠정)실적(공정공시)",
    "임원ㆍ주요주주특정증권등거래계획보고서",
    "주식등의대량보유상황보고서(일반)",
    "증권발행실적보고서",
])
def test_decisions_and_other_reports_are_not_routine(name):
    assert not is_routine(name)
