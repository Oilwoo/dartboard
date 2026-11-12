import json
from datetime import date

import pytest

from dartboard.dart import RateLimited, list_disclosures

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


def test_filing_type_is_asked_for_and_kept_on_each_filing():
    fetch, calls = pages({"status": "000", "total_page": 1, "list": [dict(ROW, corp_cls="Y")]})
    out = list_disclosures("k", date(2026, 11, 3), date(2026, 11, 3), filing_type="B001", fetch=fetch)
    assert "pblntf_detail_ty=B001" in calls[0]
    assert (out[0].filing_type, out[0].corp_cls) == ("B001", "Y")
